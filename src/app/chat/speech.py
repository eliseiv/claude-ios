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

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

import openai

from app.chat.repository import ChatRepository
from app.chat.tools import TOOL_QUIZ_GENERATE
from app.chat.voices import PROVIDER_OPENAI, Voice, resolve_voice
from app.chats.provider_blocks import to_domain_blocks
from app.config import Settings
from app.errors import (
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
