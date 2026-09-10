"""Озвучка готового ответа ассистента (ADR-100): чистка текста, клиент синтеза, весь ход ручки.

Три вещи, живущие рядом намеренно:

1. **Приведение к произносимому виду** — чистая функция над текстом шага, применяемая ПРИ ЧТЕНИИ.
   `chat_steps.payload` не изменяется: он канон — его читает пользователь и его реплеит провайдер
   (ADR-021). Приём тот же, что у ADR-042 и ADR-065 §2; **отличие названо: те два срезают то, что
   пользователь ЧИТАЕТ, этот — только то, что он СЛЫШИТ.**
2. **Клиент синтеза** — тонкая обёртка над OpenAI `audio.speech`, по образцу `transcription.py`
   (ADR-095). Провайдер не зависит от `LLM_PROVIDER`, как модерация и распознавание.
3. **Ход ручки** `POST /v1/chat/speech` — порядок операций тут ИНВАРИАНТ, а не деталь:
   проверка баланса → чистка и потолок → синтез → **при успехе** списание в той же транзакции
   запроса. Отсюда следует, что исхода «кредит списан, звук не доставлен» НЕ СУЩЕСТВУЕТ по
   построению, а значит ветки возврата тоже не существует. **Контраст с ADR-060 §4 помечен с
   обеих сторон:** там списание идёт ДО сабмита (работа уходит в очередь fal и с момента приёма
   принадлежит ей) и потому обязателен возврат `media-refund:{jobId}`; здесь синтез синхронен и
   его исход известен внутри запроса. Правило одной поверхности на другую не переносить.

Звук на сервере НЕ хранится: ни таблицы, ни диска, ни кэша в процессе. Кэш — на клиенте, по паре
`(stepId, voiceId)`; оба значения он получает в ответе.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

import openai

from app.chat.repository import ChatRepository
from app.chat.tools import TOOL_QUIZ_GENERATE
from app.chat.voices import PROVIDER_OPENAI, Voice, resolve_voice
from app.chats.provider_blocks import to_domain_blocks
from app.config import Settings
from app.errors import (
    AppError,
    GatewayTimeoutError,
    InsufficientCreditsError,
    NothingToSpeakError,
    SessionNotFoundError,
    StepNotFoundError,
    UpstreamError,
    VoiceOutputDisabledError,
    VoiceOutputNotConfiguredError,
)
from app.observability.logging import get_logger, log_event
from app.observability.metrics import voice_mode_speech_segments_total
from app.preferences.service import PreferencesService
from app.wallet.service import WalletService

logger = get_logger(__name__)


# ---------------------------------------------------------------------------------------------
# 1. Приведение к произносимому виду (ADR-100 §6)
# ---------------------------------------------------------------------------------------------
# Порядок шагов ФИКСИРОВАН: перестановка меняет результат. Отдельно зафиксировано, что шаги 1–7
# идут строго ДО потолка (шаг 8, `apply_speech_cap`): в обратном порядке потолок отсчитал бы
# символы, которые всё равно будут удалены, и клип оказался бы вдвое короче задуманного.

# Шаг 1: огороженный блок кода. Разбор построчный, а не одной регуляркой, потому что закрывающая
# ограда обязана совпадать с открывающей по символу и быть не короче её, а незакрытая ограда
# должна съедать текст до конца — регулярка с обратной ссылкой это выражает, но не читается.
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

# Шаг 1 (вторая половина): блок кода отступом. Строки продолжения списка исключены явно — иначе
# вложенный пункт «    - foo» пропал бы вместе с кодом, а это обычная проза.
_INDENTED_CODE_RE = re.compile(r"^(?: {4}|\t)(?![-*+>]\s|\d+[.)]\s)\S")

# Шаг 3: строка таблицы Markdown — либо начинается с `|`, либо это строка-разделитель
# (`---|:---:`). Прочитанная вслух таблица — шум, поэтому удаляется целиком, а не «разглаживается».
_TABLE_ROW_RE = re.compile(r"^\|")
_TABLE_DELIMITER_RE = re.compile(r"^:?-{2,}[\s:|-]*$")

# Шаг 2: inline-код — обратные кавычки снимаются, слово остаётся.
_INLINE_CODE_RE = re.compile(r"`+([^`\n]*)`+")

# Шаг 4: картинка удаляется целиком (её «текст» — alt для незрячих, а не часть речи); ссылка
# заменяется своим текстом; автоссылка, голый URL и адрес почты удаляются — прочитанный вслух URL
# нечитаем по определению.
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)\s]*(?:\s+[^)]*)?\)")
_INLINE_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)\s]*(?:\s+[^)]*)?\)")
_REFERENCE_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_AUTOLINK_RE = re.compile(r"<(?:https?|mailto):[^>\s]*>")
_BARE_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# Шаг 5: маркеры разметки снимаются, слова остаются.
_BLOCKQUOTE_RE = re.compile(r"^\s*(?:>\s?)+")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+")
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}([-*_=])(?:\s*\1){2,}\s*$")
# Открывающая звёздочка обязана прилегать к тексту, закрывающая — тоже (правило CommonMark).
# Без этого «5*7» и «2 * 3 = 6» разбирались бы как выделение: числа склеивались бы в «57», то
# есть чистка МЕНЯЛА БЫ смысл произносимого, а не только его оформление.
_BOLD_RE = re.compile(r"\*{1,3}(?=\S)([^*\n]*[^*\s])\*{1,3}")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_UNDERSCORE_EMPHASIS_RE = re.compile(r"(?<![\w\\])_{1,3}([^_\n]+)_{1,3}(?!\w)")

# Шаг 6: эмодзи, вариационные селекторы, ZWJ и прочая декоративная графика. Диапазоны, а не
# список: набор эмодзи открыт и растёт с каждой версией Unicode, а перечень зафиксировал бы
# сегодняшний.
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\U0001fc00-\U0001fffd" "←-⇿⌀-⏿①-⓿■-➿" "⤀-⥿⬀-⯿〰〽㊗㊙" "‍⃣™ℹ︎️]"
)

# Шаг 8: граница предложения для среза по потолку. Точка/восклицательный/вопросительный знак или
# многоточие, за которыми идёт пробел или конец строки.
_SENTENCE_END_RE = re.compile(r"[.!?…](?=\s|$)")


def _strip_fenced_code_blocks(text: str) -> str:
    """Шаг 1а: огороженные блоки кода (``` / ~~~) удаляются ЦЕЛИКОМ, вместе с оградами.

    Закрывающая ограда обязана быть того же символа и не короче открывающей (правило CommonMark),
    иначе ``` внутри ~~~-блока закрыл бы его раньше времени. Незакрытая ограда съедает текст до
    конца: оборванный ответ с открытым блоком кода — обычное дело, и произносить его хвост нельзя.
    """
    out: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        stripped = line.lstrip()
        match = _FENCE_RE.match(stripped)
        if fence is None:
            if match is not None:
                fence = match.group(1)
                continue
            out.append(line)
            continue
        if match is None:
            continue
        closing = match.group(1)
        if closing[0] == fence[0] and len(closing) >= len(fence):
            fence = None
    return "\n".join(out)


def _strip_block_lines(text: str) -> str:
    """Шаги 1б и 3: блоки кода отступом и строки таблиц удаляются целиком."""
    return "\n".join(
        line
        for line in text.split("\n")
        if not _INDENTED_CODE_RE.match(line)
        and not _TABLE_ROW_RE.match(line.strip())
        and not (_TABLE_DELIMITER_RE.match(line.strip()) and "|" in line)
    )


def _strip_links_and_addresses(text: str) -> str:
    """Шаг 4: картинки/URL/почта удаляются, ссылка заменяется своим текстом."""
    text = _IMAGE_RE.sub(" ", text)
    text = _INLINE_LINK_RE.sub(r"\1", text)
    text = _REFERENCE_LINK_RE.sub(r"\1", text)
    text = _AUTOLINK_RE.sub(" ", text)
    text = _BARE_URL_RE.sub(" ", text)
    return _EMAIL_RE.sub(" ", text)


def _strip_markup_markers(text: str) -> str:
    """Шаг 5: заголовки, цитаты, списки, разделители и выделение — маркеры прочь, слова на месте."""
    lines: list[str] = []
    for raw in text.split("\n"):
        if _HORIZONTAL_RULE_RE.match(raw):
            continue
        line = _BLOCKQUOTE_RE.sub("", raw)
        line = _HEADING_RE.sub("", line)
        line = _BULLET_RE.sub("", line)
        line = _ORDERED_RE.sub("", line)
        lines.append(line)
    text = "\n".join(lines)
    text = _BOLD_RE.sub(r"\1", text)
    text = _STRIKE_RE.sub(r"\1", text)
    return _UNDERSCORE_EMPHASIS_RE.sub(r"\1", text)


def _drop_symbol_only_tokens(text: str) -> str:
    """Шаг 6 (вторая половина): токены без единого буквенно-цифрового знака удаляются.

    Это то, что осталось от разметки и декора: «•», «→», «|», «—», голая скобка от вырезанной
    ссылки. Синтезатор либо проговорит их названиями, либо споткнётся; ни то, ни другое не речь.
    Токен, где есть хоть одна буква или цифра, сохраняется целиком вместе со своей пунктуацией —
    точки в конце предложения нужны и потолку (§8), и интонации.
    """
    return " ".join(token for token in text.split() if any(char.isalnum() for char in token))


def to_spoken_text(text: str) -> str:
    """Шаги 1–7 ADR-100 §6: текст шага → произносимая проза. Чистая функция, порядок фиксирован.

    Возвращает пустую строку, если произносить нечего (ответ целиком из кода, шаг без текста) —
    вызывающий обязан отдать `422 nothing_to_speak`, а НЕ тишину: тишина неотличима от зависшего
    плеера, то же основание, по которому ADR-095 §7 отказался отвечать пустотой на пустую запись.

    Потолок здесь НЕ применяется: он шаг 8 и живёт в `apply_speech_cap`, потому что вызывающему
    нужен признак `truncated`, а этой функции — оставаться чистым преобразованием текста.
    """
    text = _strip_fenced_code_blocks(text)  # 1а
    text = _strip_block_lines(text)  # 1б + 3
    text = _INLINE_CODE_RE.sub(r"\1", text)  # 2
    text = text.replace("`", " ")
    text = _strip_links_and_addresses(text)  # 4
    text = _strip_markup_markers(text)  # 5
    text = _EMOJI_RE.sub(" ", text)  # 6
    text = _drop_symbol_only_tokens(text)  # 6 + 7 (схлопывание пробелов)
    return text.strip()


def apply_speech_cap(text: str, max_chars: int) -> tuple[str, bool]:
    """Шаг 8: жёсткий потолок длины на УЖЕ очищенном тексте. Возвращает `(текст, truncated)`.

    Срез по последней границе предложения внутри лимита; при её отсутствии — по границе слова;
    если и её нет (одно слово длиннее лимита) — жёстко по символам, потому что инвариант «длина
    запроса к поставщику ограничена сверху числом, известным ДО вызова» не имеет исключений.

    Потолок не ставится на генерацию (`max_tokens`) намеренно: это превратило бы длинный ответ в
    `status=blocked` (ADR-025), то есть в неудавшийся ход без списания. Лимит длины РЕЧИ не имеет
    права ронять ход, в котором пользователь ждёт ТЕКСТ. Поэтому текст остаётся полным, звучит его
    начало, и клиент знает об этом по `truncated`.
    """
    if len(text) <= max_chars:
        return text, False
    head = text[:max_chars]
    boundaries = list(_SENTENCE_END_RE.finditer(head))
    if boundaries:
        return head[: boundaries[-1].end()].strip(), True
    cut = head.rsplit(" ", 1)[0].strip()
    if cut:
        return cut, True
    return head.strip(), True


# ---------------------------------------------------------------------------------------------
# 1b. Сегментация потока для голосового режима (ADR-104 §6)
# ---------------------------------------------------------------------------------------------
# Живёт ЗДЕСЬ, рядом с чисткой и клиентом синтеза, намеренно: «что такое произносимый текст»
# имеет ровно одну реализацию (`to_spoken_text`), и сегментация обязана применять именно её.
# Второй набор правил разошёлся бы с первым НЕСЛЫШНО ДЛЯ ТЕСТОВ И СЛЫШНО ДЛЯ ПОЛЬЗОВАТЕЛЯ.
#
# Сегмент — ПРЕДЛОЖЕНИЕ на растущем буфере, а не дельта: дельты провайдера приходят кусками
# произвольной длины, и граница предложения в них ни при чём.


def _open_fence(text: str) -> bool:
    """Осталась ли в тексте НЕЗАКРЫТАЯ ограда блока кода (``` / ~~~).

    Правило совпадения ограды — то же, что у `_strip_fenced_code_blocks` (CommonMark:
    закрывающая того же символа и не короче открывающей). Второго разбора ограды не заводится:
    он разошёлся бы с чисткой, и сегмент выпускался бы посреди кода.
    """
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE_RE.match(line.lstrip())
        if match is None:
            continue
        if fence is None:
            fence = match.group(1)
            continue
        closing = match.group(1)
        if closing[0] == fence[0] and len(closing) >= len(fence):
            fence = None
    return fence is not None


def _tail_is_table_line(text: str) -> bool:
    """Заканчивается ли префикс строкой таблицы Markdown.

    Таблица растёт строка за строкой, и срез внутри неё оставил бы половину таблицы в одном
    сегменте, половину в другом. Чистка удаляет строки таблицы ЦЕЛИКОМ (шаг 3), поэтому
    «половина таблицы» — это не испорченная речь, а МОЛЧА разное поведение для одного и того же
    текста в зависимости от того, где его застал срез. Ждём, пока таблица кончится.
    """
    last = text.rsplit("\n", 1)[-1].strip()
    if not last:
        return False
    return bool(_TABLE_ROW_RE.match(last)) or bool(_TABLE_DELIMITER_RE.match(last) and "|" in last)


def _unclosed_link(text: str) -> bool:
    """Обрывается ли префикс внутри незакрытой ссылки/картинки Markdown.

    Две формы: незакрытый текст ссылки (`[` без парного `]`) и незакрытый адрес (`](` без `)`).
    Срез между ними отдал бы синтезатору голую скобку в одном сегменте и голый URL в другом —
    а чистка (шаг 4) удаляет ссылку только целиком.
    """
    if text.count("[") > text.count("]"):
        return True
    closing = text.rfind("]")
    if closing == -1:
        return False
    rest = text[closing + 1 :]
    return rest.startswith("(") and ")" not in rest


def _inside_unclosed_construct(prefix: str) -> bool:
    """Правило (в) ADR-104 §6: срез не внутри незакрытой конструкции.

    Перечень закрываемых конструкций — ограда блока кода, строка таблицы, незакрытая ссылка —
    ЗАКРЫТ и назван в контракте. Остаточный риск назван прямо и здесь: сегмент, очищенный в
    отрыве, может отличаться от того же текста, очищенного целиком; правило закрывает ИЗВЕСТНЫЕ
    конструкции, а не все мыслимые.
    """
    return _open_fence(prefix) or _tail_is_table_line(prefix) or _unclosed_link(prefix)


def next_speech_segment(buffer: str, min_chars: int) -> tuple[str, str] | None:
    """Отрезать от растущего буфера очередной сегмент озвучки. `None` — ещё рано (ADR-104 §6).

    Кандидат — НАИБОЛЬШИЙ префикс буфера, который: (а) заканчивается на границе предложения (та
    же `_SENTENCE_END_RE`, что уже используется потолком, — второй границы предложения в системе
    не заводится); (б) не короче ``min_chars``; (в) не находится внутри незакрытой конструкции.

    Возвращает пару «сегмент, остаток» СЫРОГО текста: чистка применяется вызывающим, потому что
    ему нужен ещё и признак «после чистки пусто» (такой сегмент не отправляется и не
    оплачивается). Функция чистая — её исход зависит только от буфера и порога.
    """
    if len(buffer) < min_chars:
        return None
    for match in reversed(list(_SENTENCE_END_RE.finditer(buffer))):
        cut = match.end()
        if cut < min_chars:
            # Границы идут по возрастанию, дальше только короче — ждать больше нечего.
            return None
        prefix = buffer[:cut]
        if _inside_unclosed_construct(prefix):
            continue
        return prefix, buffer[cut:]
    return None


def assistant_text_of_step(payload: dict[str, Any]) -> str:
    """Текст assistant-шага для озвучки — через тот же read-boundary адаптер, что и история.

    `to_domain_blocks` (ADR-058) обязателен, а не удобен: на OpenAI-инстансе шаг хранит НЕ
    доменные блоки, а нормализованное сообщение провайдера, и наивный поиск `type == "text"` нашёл
    бы там пусто — озвучка молча отвечала бы `nothing_to_speak` на каждом ответе целого инстанса.
    Второго разбора формы payload не заводится: он разошёлся бы с историей при первой же правке.
    """
    parts = [
        str(block.get("text", ""))
        for block in to_domain_blocks(payload.get("content"))
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n\n".join(part for part in parts if part)


# ---------------------------------------------------------------------------------------------
# 2. Клиент синтеза (ADR-100 §8)
# ---------------------------------------------------------------------------------------------


class SpeechClient:
    """Тонкая обёртка над OpenAI `audio.speech` — по образцу `TranscriptionClient` (ADR-095).

    Ключ — существующий `OPENAI_API_KEY`; отдельной переменной под синтез не заводится (третье
    имя для одного факта «ключ OpenAI на инстансе»). Пустой ключ — это `configured is False`, а не
    исключение при создании: каталог голосов обязан отвечать по флагу инстанса, а не по ключу.
    """

    def __init__(self, settings: Settings) -> None:
        self._model = settings.tts_model
        self._response_format = cast(Literal["mp3", "aac"], settings.resolved_tts_audio_format())
        self._api_key = settings.openai_api_key
        self._client = openai.AsyncOpenAI(
            api_key=self._api_key or "placeholder",
            timeout=settings.tts_timeout_seconds,
            max_retries=0,
        )

    @property
    def configured(self) -> bool:
        """Есть ли чем синтезировать на этом инстансе (ADR-100 §8)."""
        return bool(self._api_key)

    async def synthesize(self, *, text: str, voice: Voice) -> bytes:
        """Синтезировать речь и вернуть байты файла.

        `instructions` записи реестра уходят поставщику вместе с текстом — это и есть носитель
        манеры речи персонажа. Ошибки наружу: таймаут → `504`, любой другой отказ → `502`; текст
        исключения НЕ пробрасывается, он цитирует запрос целиком (то же правило, что у
        распознавания, ADR-095 §6, и прокси ассетов, ADR-085).

        Поле `provider` записи ПРОВЕРЯЕТСЯ здесь, а не лежит справочно. Ради него реестр и хранит
        тройку: перевод отдельного голоса на другого поставщика — правка одной строки. Правка,
        которую никто не читает, — мёртвое объявление: голос с `provider="elevenlabs"` ушёл бы в
        OpenAI с чужим идентификатором и вернулся бы невнятной ошибкой поставщика вместо честного
        «этот инстанс так не умеет». Отсюда 503 (мис-конфигурация, деньги не тронуты), а не 502.
        """
        if voice.provider != PROVIDER_OPENAI:
            log_event(
                logger,
                logging.ERROR,
                "speech_voice_provider_unsupported",
                voiceId=voice.id,
                provider=voice.provider,
            )
            raise VoiceOutputNotConfiguredError("voice provider is not supported by this build")
        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=voice.provider_voice_id,
                input=text,
                instructions=voice.instructions,
                response_format=self._response_format,
            )
        except openai.APITimeoutError as exc:
            log_event(
                logger,
                logging.WARNING,
                "speech_synthesis_timeout",
                model=self._model,
                voiceId=voice.id,
                errorType=type(exc).__name__,
            )
            raise GatewayTimeoutError("speech synthesis timed out") from exc
        except openai.APIError as exc:
            log_event(
                logger,
                logging.WARNING,
                "speech_synthesis_failed",
                model=self._model,
                voiceId=voice.id,
                errorType=type(exc).__name__,
            )
            raise UpstreamError("speech provider error") from exc
        return response.content


# ---------------------------------------------------------------------------------------------
# 2b. Потоковый синтез одного хода голосового режима (ADR-104 §6)
# ---------------------------------------------------------------------------------------------


@dataclass
class VoiceTurnBudget:
    """Величины, нормативно принадлежащие ХОДУ, а не его ноге (ADR-104 §6).

    Заведён потому, что ход с клиентскими инструментами исполняется НЕСКОЛЬКИМИ ногами с одним
    `turnId`, а синтез создаётся на ногу: всё, что лежало бы в объекте ноги, обнулялось бы на
    второй и переставало быть потурновым. Форма дефекта одна на три величины, поэтому и носитель
    один:

    * ``spoken_total`` — совокупный расход `TTS_MAX_CHARS`. На ноге он давал бы фактический
      потолок «число ног × `TTS_MAX_CHARS`», то есть снимал бы ограничение с нашего счёта у
      поставщика — ровно то, ради чего потолок единственно и существует.
    * ``next_segment`` — монотонный номер сегмента ВНУТРИ хода. На ноге пара `(turnId, segment)`
      переставала бы быть уникальной, а на её уникальности держится упорядоченное проигрывание,
      которое ADR-104 §11 отдаёт устройству.
    * ``heard_segments`` — сколько сегментов ХОДА пользователь фактически дослушал; это и есть
      `spokenSegments` кадра `interrupted` и пометки `payload.interrupted`.
    * ``capped`` — «потолок хода исчерпан, дальше по этому ходу не синтезируем»: признак
      потурновый по той же причине, что и бюджет, который его порождает.

    **Признака исчерпанного бакета здесь НЕТ, и это не пропуск.** Единица бакета `rl:speech` —
    ОЗВУЧЕННЫЙ ШАГ (ADR-104 §13.2), и отказ гасит синтез именно шага: «следующий озвученный шаг
    просит свой токен заново». Признак живёт на объекте ноги; положив его сюда, мы погасили бы
    вторую озвучку хода за отказ, случившийся на первой. Единица ГАШЕНИЯ — шаг, единица
    БЮДЖЕТА — ход, и одна из другой не выводится.

    **Контраст помечен с обеих сторон:** число доставленных сегментов ШАГА (`delivered_segments`
    ниже) остаётся на ноге и потурновым НЕ становится — это предикат списания синтеза, а единица
    списания — озвученный assistant-шаг, а не ход (ADR-104 §6). Правило одной величины на другую
    не переносить.
    """

    spoken_total: int = 0
    next_segment: int = 0
    heard_segments: int = 0
    capped: bool = False


class VoiceSpeechSink(Protocol):
    """Транспорт кадров звука. Реализуется обработчиком сокета; синтезу о WebSocket знать нечего.

    Разделение не косметическое: сегментация, потолок и тарификация — свойства ХОДА и обязаны
    быть проверяемы без сокета, а порядок и вид кадров — свойство транспорта.
    """

    async def audio_begin(self, *, segment: int, media_type: str, voice_id: str) -> None: ...

    async def audio_chunk(self, data: bytes) -> None: ...

    async def audio_end(self, *, segment: int, truncated: bool) -> None: ...

    async def speech_failed(self) -> None: ...

    async def speech_rate_limited(self) -> None: ...


class VoiceTurnSpeech:
    """Озвучка ОДНОГО хода голосового режима по мере генерации (ADR-104 §6).

    Инварианты, каждый из которых проверяем снаружи:

    * **сегмент = предложение на растущем буфере**, не дельта (`next_speech_segment`);
    * **чистка — та же `to_spoken_text`**, второй реализации «произносимого» не заводится;
    * **потолок `TTS_MAX_CHARS` — СОВОКУПНЫЙ на ход**, не на сегмент: посегментный перестал бы
      ограничивать наш счёт у поставщика, ради чего он единственно и существует. Исчерпан →
      синтез прекращается на последнем ЗАВЕРШЁННОМ сегменте, его `audio.end` несёт
      ``truncated: true``, а ТЕКСТ при этом остаётся полным и продолжает идти в `delta`/`done`;
    * **порядок сегментов**: единственный воркер разбирает очередь, поэтому `audio.begin`
      сегмента N всегда предшествует `audio.begin` сегмента N+1;
    * **отказ синтезатора хода НЕ роняет**: он гасит только звук (`scope:"speech"`), а
      `delta`/`done` идут дальше — сломанный синтезатор обязан стоить молчания, а не ответа.

    Класс НЕ трогает кошелёк: момент списания — закрытие озвученного assistant-шага, а `stepId`
    к этому времени существует только у вызывающего (шаг создаётся в финализации хода). Здесь
    считается лишь то, от чего списание зависит: доставлен ли хотя бы один сегмент.
    """

    def __init__(
        self,
        *,
        client: SpeechClient,
        settings: Settings,
        voice: Voice,
        sink: VoiceSpeechSink,
        budget: VoiceTurnBudget,
        limiter: Callable[[], Awaitable[bool]],
    ) -> None:
        self._client = client
        self._settings = settings
        self._voice = voice
        self._sink = sink
        # Величины ХОДА живут снаружи и переживают смену ноги (см. `VoiceTurnBudget`).
        self._budget = budget
        # Бакет `rl:speech` (`TTS_RATE_LIMIT_PER_MIN`) — тот же, что у `POST /v1/chat/speech`,
        # потому что защищает он одно и то же: наш счёт у поставщика СИНТЕЗА. Передан колбэком,
        # а не импортирован: синтезу нечего знать ни о Redis, ни о том, чей это пользователь.
        self._limiter = limiter
        self._buffer = ""
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._delivered = 0
        self._stopped = False
        self._failed = False
        # Токен бакета `rl:speech` берётся ОДИН раз на озвученный ШАГ (ADR-104 §13.2), поэтому
        # признак «уже взят» принадлежит ноге, а не ходу: следующий озвученный шаг просит свой.
        self._token_taken = False
        # Бакет исчерпан → синтез ЭТОГО шага погашен целиком. Признак принадлежит ноге, а не
        # ходу: ADR-104 §13.2 требует, чтобы следующий озвученный шаг просил токен заново.
        self._rate_limited = False
        # Был ли у этого шага хоть один НЕПУСТОЙ после чистки кандидат. Отличает «произносить
        # было нечего» (контракт: `speech.skipped {nothing_to_speak}` — «очищенный текст ответа
        # пуст») от «текст был, но замолчали» (бакет, потолок, прерывание, отказ поставщика):
        # там та же причина была бы ложью из закрытого перечня.
        self._had_speakable_text = False

    # ---- наблюдаемое состояние хода ----

    @property
    def delivered_segments(self) -> int:
        """Сколько сегментов ЭТОЙ НОГИ доставлено (отправлен `audio.end`).

        Единица здесь — ШАГ, а не ход, и это не небрежность: величина служит ровно одному
        предикату — «хотя бы один сегмент этого ШАГА доставлен → синтез шага списывается»
        (ADR-104 §6), а списание идёт по ключу `tts:{stepId}:{voiceId}`, то есть на шаг.
        Потурновое число дослушанных сегментов — `VoiceTurnBudget.heard_segments`; оно уходит в
        `spokenSegments`. Правило одной величины на другую не переносить ни в одну сторону.
        """
        return self._delivered

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def had_speakable_text(self) -> bool:
        """Был ли у шага хоть один кандидат, непустой ПОСЛЕ чистки (ADR-104 §13.2, контракт).

        Предикат кадра `speech.skipped {reason:"nothing_to_speak"}`, и только его: контракт
        определяет эту причину дословно как «очищенный текст ответа пуст». Молчание по любой
        другой причине — исчерпанный бакет, исчерпанный бюджет хода, прерывание, отказ
        синтезатора — этой причины НЕ получает: у каждой из них своя строка таблицы отказов.
        """
        return self._had_speakable_text

    @property
    def stopped(self) -> bool:
        return self._stopped

    # ---- управление ----

    def start(self) -> None:
        """Поднять воркер синтеза. Отдельная задача, а не работа внутри `on_text_delta`.

        Синтез сегмента — сетевой вызов на секунду с лишним. Выполненный прямо в обратном вызове
        дельты, он остановил бы приём дельт от модели: текст переставал бы идти ровно там, где
        пользователь его ждёт, и потоковость терялась бы ради звука.
        """
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    def _muted(self) -> bool:
        """Синтез по этому ходу дальше не идёт: прерван, сломан, упёрся в потолок или в бакет."""
        return self._stopped or self._failed or self._rate_limited or self._budget.capped

    def feed_delta(self, text: str) -> None:
        """Принять приращение текста ответа и выпустить готовые сегменты. Не блокирует."""
        if self._muted():
            return
        self._buffer += text
        while True:
            split = next_speech_segment(self._buffer, self._settings.voice_mode_segment_min_chars)
            if split is None:
                return
            segment, self._buffer = split
            self._queue.put_nowait(segment)

    def interrupt(self) -> None:
        """Прервать синтез немедленно и навсегда для ЭТОГО хода (ADR-104 §5).

        Синтез прекращается ВСЕГДА и немедленно — это ровно то, о чём просил пользователь.
        Уже синтезированный, но не отправленный сегмент считается `interrupted`: `audio.end` по
        нему не уходит, и в число дослушанных он не попадает.
        """
        self._stopped = True

    async def finish(self) -> None:
        """Дозвучить остаток буфера последним сегментом и дождаться конца очереди.

        Вызывается при закрытии хода. Ожидание обязательно: списание синтеза идёт ПОСЛЕ всего
        звука шага, а `done` — после списания, поэтому исхода «списано, но не доставлено» нет.
        """
        remainder = self._buffer.strip()
        self._buffer = ""
        if remainder and not self._muted():
            self._queue.put_nowait(remainder)
        self._queue.put_nowait(None)
        if self._worker is not None:
            await self._worker
            self._worker = None

    # ---- воркер ----

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if self._stopped:
                # Очередь дочитывается до сентинела, но работа больше не делается: сегменты,
                # оставшиеся в ней после прерывания, синтезатору не отдаются и не оплачиваются.
                voice_mode_speech_segments_total.labels(outcome="interrupted").inc()
                continue
            if self._muted():
                # У этих сегментов ИСХОДА НЕТ, и метка им не ставится намеренно. Отказ
                # синтезатора считается ОДИН раз — на сегменте, где он произошёл; исчерпанный
                # потолок и исчерпанный бакет — по одному разу на ход. Пометить хвост
                # `upstream_error` значило бы умножить одну аварию на длину ответа и обесценить
                # алерт, `capped` — сосчитать один потолок много раз, а `interrupted` /
                # `skipped_empty` — назвать неверную причину.
                continue
            await self._speak(item)

    async def _speak(self, raw: str) -> None:
        cleaned = to_spoken_text(raw)
        if cleaned:
            self._had_speakable_text = True
        if not cleaned:
            # Пустой после чистки сегмент не отправляется и не оплачивается: синтезатор НЕ
            # вызывался, произносить было нечего. С отказом поставщика этот исход не сливается.
            voice_mode_speech_segments_total.labels(outcome="skipped_empty").inc()
            return
        # Потолок СОВОКУПНЫЙ на ход: остаток бюджета, а не длина сегмента. Исчерпание
        # проверяется ДО `apply_speech_cap` намеренно: при остатке ровно `0` она вернула бы
        # `('', True)`, пустой текст ушёл бы в ветку «произносить нечего», и метрика получила бы
        # `skipped_empty` — то есть НЕВЕРНУЮ причину («синтезатор не звали, потому что нечего
        # произносить» вместо «сработал потолок»), а признак потолка не выставился бы вовсе.
        remaining = self._settings.tts_max_chars - self._budget.spoken_total
        if remaining <= 0:
            # Бюджет ХОДА занят ровно предыдущим сегментом: тот ушёл с `truncated: false`, и
            # носителя признака на сегменте больше нет. Исход всё равно `capped`, а не
            # `skipped_empty`: потолок исчерпан именно на ЭТОМ кандидате, и он непуст. Без
            # инкремента второй способ исчерпания молча выпадал бы из серии, по доле которой
            # калибруется сам потолок. Инкремент один на ход — дальше гасит признак `capped`.
            # Клиент узнаёт о потолке из `done.speechTruncated` (ADR-104 §13.8).
            self._budget.capped = True
            voice_mode_speech_segments_total.labels(outcome="capped").inc()
            return
        spoken, truncated = apply_speech_cap(cleaned, remaining)
        if not spoken:  # pragma: no cover — при remaining > 0 срез не бывает пустым
            voice_mode_speech_segments_total.labels(outcome="skipped_empty").inc()
            return
        # Бакет `rl:speech` — ОДИН токен на ОЗВУЧЕННЫЙ ШАГ (ADR-104 §13.2), та же единица, что
        # у ключа списания `tts:{stepId}:{voiceId}`. Инвариант, общий обеим поверхностям: один
        # токен покупает не больше `TTS_MAX_CHARS` символов, ушедших поставщику. Бакет защищает
        # наш СЧЁТ, а счёт измеряется символами, — поэтому токен на каждое обращение считал бы
        # не то, что защищает: ход из девяти сегментов брал бы девять токенов за те же ≤700
        # символов, за которые кнопка «прослушать» берёт один.
        # Остальные сегменты этого шага токена не просят: они уже оплачены им и ограничены
        # сверху совокупным потолком хода. Следующий озвученный шаг просит свой токен заново.
        # Отказ гасит синтез ЭТОГО ШАГА целиком, а не выборочно (речь с дырами неотличима от
        # поломки) и ход НЕ роняет: `delta`/`done` идут дальше.
        if not self._token_taken:
            if not await self._limiter():
                self._rate_limited = True
                voice_mode_speech_segments_total.labels(outcome="rate_limited").inc()
                await self._sink.speech_rate_limited()
                return
            self._token_taken = True
        try:
            audio = await self._client.synthesize(text=spoken, voice=self._voice)
        except AppError:
            # Отказ синтезатора ход НЕ затрагивает: звук прекращается, `delta`/`done` идут.
            # Наружу — кадр `error {scope:"speech"}`, а не закрытие сокета.
            self._failed = True
            voice_mode_speech_segments_total.labels(outcome="upstream_error").inc()
            await self._sink.speech_failed()
            return
        if self._stopped:
            # Прерывание пришло, пока сегмент синтезировался: `audio.end` не отправляется.
            voice_mode_speech_segments_total.labels(outcome="interrupted").inc()
            return
        segment = self._budget.next_segment
        self._budget.next_segment += 1
        await self._sink.audio_begin(
            segment=segment,
            media_type=self._settings.tts_media_type(),
            voice_id=self._voice.id,
        )
        await self._sink.audio_chunk(audio)
        await self._sink.audio_end(segment=segment, truncated=truncated)
        self._delivered += 1
        self._budget.heard_segments += 1
        self._budget.spoken_total += len(spoken)
        if truncated:
            # Синтез прекращается на последнем ЗАВЕРШЁННОМ сегменте; текст при этом остаётся
            # полным и продолжает идти в `delta` и в `done` — обрезается речь, не ответ.
            self._budget.capped = True
        voice_mode_speech_segments_total.labels(outcome="capped" if truncated else "ok").inc()


# ---------------------------------------------------------------------------------------------
# 3. Ход ручки POST /v1/chat/speech (ADR-100 §9)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeechResult:
    """Успешный исход синтеза. `repeat` — предикат метрики `ok` против `repeat` (ADR-100 §10)."""

    step_id: uuid.UUID
    voice_id: str
    media_type: str
    audio: bytes
    truncated: bool
    credits_charged: int
    repeat: bool
    source_chars: int
    spoken_chars: int


class SpeechSynthesisService:
    """Единственное место, где сходятся резолв голоса, чистка, синтез и списание."""

    def __init__(
        self,
        *,
        repo: ChatRepository,
        preferences: PreferencesService,
        wallet: WalletService,
        client: SpeechClient,
        settings: Settings,
    ) -> None:
        self._repo = repo
        self._preferences = preferences
        self._wallet = wallet
        self._client = client
        self._settings = settings

    async def synthesize(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, step_id: uuid.UUID
    ) -> SpeechResult:
        """Озвучить сохранённый assistant-шаг. Порядок операций — инвариант (см. docstring модуля).

        Policy Engine на этом пути НЕ вызывается и подписка НЕ требуется: он решает, можно ли
        СГЕНЕРИРОВАТЬ ответ, а ответ уже сгенерирован и оплачен. Гейт только балансовый — как у
        генерации медиа (ADR-060 §Границы).
        """
        if not self._settings.voice_output_enabled:
            raise VoiceOutputDisabledError("speech output is not enabled on this instance")
        if not self._client.configured:
            raise VoiceOutputNotConfiguredError("speech output is not configured")

        # Изоляция по владельцу: сессия резолвится по (id, user_id), шаг — ВНУТРИ неё. Чужой или
        # несуществующий неотличимы (404), существование чужого отдельным кодом не раскрывается.
        session = await self._repo.get_session(session_id, user_id)
        if session is None:
            raise SessionNotFoundError("session not found")
        step = await self._repo.get_assistant_step(session.id, step_id)
        if step is None:
            raise StepNotFoundError("assistant step not found in this session")

        voice = resolve_voice(
            character_id=session.character_id,
            user_default_voice_id=await self._preferences.get_default_voice_id(user_id),
        )

        # Идемпотентность по паре «шаг + голос». Ключ живёт в леджере ВЕЧНО и служит признаком «за
        # этот ответ этим голосом уже заплачено»: холодный старт, переустановка приложения и второе
        # устройство синтезируют заново, но денег не берут. Смена голоса — другой ключ и новое
        # списание. Читается ДО балансового гейта намеренно: пользователь с нулевым балансом обязан
        # получить уже оплаченный звук, а не 409.
        idempotency_key = f"tts:{step_id}:{voice.id}"
        already_paid = await self._wallet.has_idempotency_key(user_id, idempotency_key)
        cost = self._settings.tts_credit_cost
        if not already_paid and await self._wallet.current_balance(user_id) < cost:
            # BYOK и trial платят за озвучку ВНУТРЕННИМИ кредитами — в отличие от хода: синтез в
            # любом случае идёт нашим ключом OpenAI, ключ пользователя может быть вообще
            # anthropic-овским (ADR-044). Названное следствие: BYOK-пользователь с нулевым
            # балансом получает 409 на озвучку, продолжая нормально переписываться.
            raise InsufficientCreditsError("insufficient_credits")

        # Ход с КВИЗОМ не озвучивается вовсе (ADR-100 §6). Это не оптимизация, а перенос уже
        # принятого правила на новую поверхность: ADR-065 §2 срезает текст assistant-шага квиз-хода
        # из ИСТОРИИ, потому что в нём остаются вопросы и правильные ответы, а карточки показывает
        # приложение. `chat_steps.payload` при этом канон и текст в нём сохранён — значит любая
        # НОВАЯ ручка, отдающая пользователю сохранённый текст ассистента, обязана применить то же
        # правило, иначе дырка открывается заново, просто через звук. Предикат ТОТ ЖЕ, что у
        # истории (непустой `result` инструмента квиза в этом ходе), и берётся тем же запросом —
        # второго определения «это квиз-ход» не заводится: разойдясь, они дали бы поверхность, где
        # спойлер снова слышен.
        quiz_result = await self._repo.last_tool_result_for_message_step(
            session.id, step.message_step_id, TOOL_QUIZ_GENERATE
        )
        source_text = "" if quiz_result else assistant_text_of_step(step.payload)
        spoken, truncated = apply_speech_cap(
            to_spoken_text(source_text), self._settings.tts_max_chars
        )
        if not spoken:
            raise NothingToSpeakError("nothing to speak in this step")

        audio = await self._client.synthesize(text=spoken, voice=voice)

        # Списание — ПОСЛЕ успешного синтеза, в той же транзакции запроса. Провал поставщика ⇒
        # списания не было ⇒ возвращать нечего. Если ответ не дойдёт до клиента по сети, ключ уже
        # в леджере и повторный запрос отдаст звук бесплатно.
        if already_paid:
            credits_charged, repeat = 0, True
        else:
            consumed = await self._wallet.consume(
                user_id=user_id,
                amount=cost,
                idempotency_key=idempotency_key,
                meta={"source": "speech_synthesis", "voiceId": voice.id, "stepId": str(step_id)},
                session_id=session_id,
            )
            repeat = consumed.idempotent_replay
            credits_charged = 0 if repeat else cost

        return SpeechResult(
            step_id=step_id,
            voice_id=voice.id,
            media_type=self._settings.tts_media_type(),
            audio=audio,
            truncated=truncated,
            credits_charged=credits_charged,
            repeat=repeat,
            source_chars=len(source_text),
            spoken_chars=len(spoken),
        )
