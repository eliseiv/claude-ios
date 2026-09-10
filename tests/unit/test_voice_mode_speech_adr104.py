"""Unit: сегментация потока и совокупный потолок озвучки голосового режима (ADR-104 §6).

Норма — `docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`, разделы
«Unit — сегментация потока» и «Unit — совокупный потолок».

Сегменты здесь рождаются из ДЕЛЬТ (`VoiceTurnSpeech.feed_delta` — ровно та точка, куда обработчик
сокета отдаёт приращение текста в `_on_delta`), а не подаются в сегментатор готовым списком:
тест, который сам конструирует то, что в реальности производит другой слой, покрытием цепи не
считается. Цепь целиком («сокет → оркестратор → фейковый LLM-клиент → сегмент → кадр») проверяется
в `tests/integration/test_voice_mode_*_adr104.py`.
"""

from __future__ import annotations

from typing import Any

from app.chat.speech import VoiceTurnBudget, VoiceTurnSpeech, to_spoken_text
from app.chat.voices import Voice, get_voice
from app.config import Settings, get_settings
from app.errors import UpstreamError
from app.observability.metrics import voice_mode_speech_segments_total

# ---------------------------------------------------------------------------------------------
# Дублёры границы: синтезатор и транспорт кадров. Ни один не подменяет наш код — оба стоят ровно
# на ВНЕШНИХ швах `VoiceTurnSpeech` (провайдер синтеза и сокет).
# ---------------------------------------------------------------------------------------------


class _FakeSpeechClient:
    """Синтезатор. Записывает ТЕКСТ каждого вызова — по нему считается совокупный расход."""

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.texts: list[str] = []
        self._fail_on = fail_on

    async def synthesize(self, *, text: str, voice: Voice) -> bytes:
        self.texts.append(text)
        if self._fail_on is not None and len(self.texts) == self._fail_on:
            raise UpstreamError("speech provider error")
        return f"audio:{voice.id}:{len(self.texts)}".encode()


class _RecordingSink:
    """Транспорт кадров звука. Записывает ровно то, что увидел бы клиент на сокете."""

    def __init__(self) -> None:
        self.begins: list[dict[str, Any]] = []
        self.ends: list[dict[str, Any]] = []
        self.chunks: list[bytes] = []
        self.failed = 0
        self.rate_limited = 0

    async def audio_begin(self, *, segment: int, media_type: str, voice_id: str) -> None:
        self.begins.append({"segment": segment, "mediaType": media_type, "voiceId": voice_id})

    async def audio_chunk(self, data: bytes) -> None:
        self.chunks.append(data)

    async def audio_end(self, *, segment: int, truncated: bool) -> None:
        self.ends.append({"segment": segment, "truncated": truncated})

    async def speech_failed(self) -> None:
        self.failed += 1

    async def speech_rate_limited(self) -> None:
        self.rate_limited += 1


def _voice() -> Voice:
    entry = get_voice("default_female")
    assert entry is not None
    return entry


def _settings(**overrides: Any) -> Settings:
    """Настройки хода. `model_copy` вместо env: тест не трогает процессный кэш настроек."""
    base: dict[str, Any] = {
        "voice_mode_segment_min_chars": 5,
        "tts_max_chars": 700,
        "tts_rate_limit_per_min": 1000,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


async def _allow() -> bool:
    return True


def _build(
    *,
    settings: Settings | None,
    client: _FakeSpeechClient,
    sink: _RecordingSink,
    budget: VoiceTurnBudget,
) -> VoiceTurnSpeech:
    return VoiceTurnSpeech(
        client=client,  # type: ignore[arg-type]
        settings=settings if settings is not None else _settings(),
        voice=_voice(),
        sink=sink,
        budget=budget,
        limiter=_allow,
    )


async def _speak(
    deltas: list[str],
    *,
    settings: Settings | None = None,
    budget: VoiceTurnBudget | None = None,
) -> tuple[_FakeSpeechClient, _RecordingSink, VoiceTurnBudget]:
    """Полный ход озвучки: дельты → сегменты → дозвучивание остатка при закрытии хода."""
    client, sink = _FakeSpeechClient(), _RecordingSink()
    turn_budget = budget if budget is not None else VoiceTurnBudget()
    speech = _build(settings=settings, client=client, sink=sink, budget=turn_budget)
    speech.start()
    for delta in deltas:
        speech.feed_delta(delta)
    await speech.finish()
    return client, sink, turn_budget


async def _released(
    deltas: list[str], *, settings: Settings | None = None
) -> tuple[_FakeSpeechClient, _RecordingSink]:
    """Только ВЫПУЩЕННЫЕ сегменты: остаток буфера намеренно не дозвучивается.

    Нужен там, где кейс проверяет «сегмент ещё НЕ выпускается»: `finish()` дозвучивает остаток
    последним сегментом и стёр бы разницу между «удержано» и «выпущено».
    """
    client, sink = _FakeSpeechClient(), _RecordingSink()
    speech = _build(settings=settings, client=client, sink=sink, budget=VoiceTurnBudget())
    speech.start()
    for delta in deltas:
        speech.feed_delta(delta)
    speech._buffer = ""  # noqa: SLF001 — остаток выбрасывается: наблюдаем только выпущенное
    await speech.finish()
    return client, sink


def _segment_metric(outcome: str) -> float:
    return voice_mode_speech_segments_total.labels(outcome=outcome)._value.get()  # noqa: SLF001


# ---------------------------------------------------------------------------------------------
# Unit — сегментация потока
# ---------------------------------------------------------------------------------------------


async def test_segment_is_a_sentence_not_a_delta() -> None:
    """Единица — предложение на растущем буфере, а не дельта (09-testing §Unit — сегментация).

    Падает на реализации «сегмент = дельта»: там синтезатор получил бы три куска ровно в том
    виде, в каком их прислал провайдер, и первый («Привет») предложением не был бы.
    """
    client, sink, _ = await _speak(["Привет", ", как ", "дела? И ещё"])

    assert client.texts == ["Привет, как дела?", "И ещё"]
    assert [end["segment"] for end in sink.ends] == [0, 1]


async def test_unclosed_code_fence_holds_the_segment_and_plain_text_does_not() -> None:
    """Незакрытая ограда держит сегмент; после закрытия — выпускается без кода (обе стороны).

    Обратная сторона обязательна: реализация, которая не выпускает сегмент НИКОГДА, прошла бы
    половину теста.
    """
    # (а) ограда открыта — сегмент не выпускается, хотя точка внутри есть.
    held, _ = await _released(["Смотри решение:\n```python\nprint(1). ", "ещё строка. "])
    assert held.texts == []

    # (б) ограда закрыта — сегмент выпущен, и код из него удалён.
    closed, _ = await _released(["Смотри решение:\n```python\nprint(1)\n", "```\nГотово совсем. "])
    assert closed.texts, "закрытая ограда обязана выпустить сегмент"
    assert "print" not in closed.texts[0]
    assert "Готово совсем" in closed.texts[0]

    # (в) обычный текст с точкой выпускается сразу и ничего не ждёт.
    plain, _ = await _released(["Обычное предложение готово. "])
    assert plain.texts == ["Обычное предложение готово."]


async def test_fence_left_open_until_the_end_of_the_turn_is_eaten_whole() -> None:
    """Незакрытая до конца хода ограда съедается целиком и хода НЕ роняет."""
    client, sink, _ = await _speak(["Смотри:\n```\nimport os\nos.remove(path)\n"])

    assert all("import" not in text and "os.remove" not in text for text in client.texts)
    assert sink.failed == 0


async def test_table_row_holds_the_segment() -> None:
    """Строка таблицы — самостоятельный предикат удержания среза.

    Наблюдаемая величина здесь — НЕ множество произнесённого: чистка удаляет строки таблицы
    ЦЕЛИКОМ, поэтому срез внутри таблицы даёт тот же звук и виден только по ЛИШНЕМУ кандидату,
    оказавшемуся после чистки пустым. Именно он и означает «половина таблицы попала в один
    сегмент, половина в другой»: поведение молча зависит от того, где текст застал срез.

    Падает на реализации без предиката: там кандидат, обрывающийся на строке таблицы, будет
    выпущен и даст `skipped_empty`.
    """
    before_skipped = _segment_metric("skipped_empty")
    client, _ = await _released(
        [
            "Итоги ниже.\n| столбец | другой столбец |\n",
            "| значение | ещё значение. |\n",
            "После таблицы идёт вывод. ",
        ]
    )

    # «Итоги ниже.» выпущено ДО таблицы, а таблица дождалась своего конца и ушла вместе с выводом.
    assert client.texts == ["Итоги ниже.", "После таблицы идёт вывод."]
    assert (
        _segment_metric("skipped_empty") == before_skipped
    ), "внутри таблицы срез не делается: пустых после чистки кандидатов быть не должно"


async def test_unclosed_link_holds_the_segment_in_both_forms() -> None:
    """Незакрытая ссылка — тоже самостоятельный предикат, и форм у неё две.

    Незакрытый ТЕКСТ ссылки (`[` без `]`) и незакрытый АДРЕС (`](` без `)`) — разные ветви;
    снятие любой из них не уронило бы соседнюю.
    """
    unclosed_text, _ = await _released(["Смотри тут: [читать про версию 3. и дальше"])
    assert unclosed_text.texts == []

    unclosed_url, _ = await _released(["Смотри [док](https://example.dev/path. и дальше"])
    assert unclosed_url.texts == []


async def test_segment_min_chars_holds_a_short_sentence() -> None:
    """`VOICE_MODE_SEGMENT_MIN_CHARS` (diff): короткая реплика отдельным сегментом не идёт.

    Падает, если минимальная длина не применяется: каждое короткое предложение стало бы отдельным
    ПЛАТНЫМ вызовом к поставщику.
    """
    strict = _settings(voice_mode_segment_min_chars=80)

    held, _ = await _released(["Да. "], settings=strict)
    assert held.texts == []

    # …копится до следующей границы: перевалив порог, сегмент выпускается целиком.
    grown_text = (
        "Да. Длинное продолжение той же самой мысли, которое уверенно переваливает "
        "за порог в восемьдесят символов. "
    )
    grown, _ = await _released([grown_text], settings=strict)
    assert grown.texts == [grown_text.strip()]

    # Обратная сторона: при низком пороге то же короткое предложение проходит.
    loose, _ = await _released(["Да, конечно. "], settings=_settings())
    assert loose.texts == ["Да, конечно."]


async def test_cleaning_is_the_same_function_as_the_speech_endpoint() -> None:
    """Чистка — та же `to_spoken_text` (diff): вторая копия правил обязана уронить кейс."""
    raw = (
        "Итог такой, смотри 🎉 подробности в [документации](https://example.dev) и в таблице.\n"
        "| ключ | значение |\n| --- | --- |\n| a | b |\nНа этом всё готово. "
    )
    client, _ = await _released([raw])

    assert client.texts, "сегмент обязан выпуститься"
    assert client.texts[0] == to_spoken_text(raw.rstrip())


# ---------------------------------------------------------------------------------------------
# Unit — совокупный потолок
# ---------------------------------------------------------------------------------------------

_FIVE_SENTENCES = [
    "Первое предложение длиной примерно сорок символов. ",
    "Второе предложение длиной примерно сорок симв. ",
    "Третье предложение длиной примерно сорок симв. ",
    "Четвёртое предложение длиной примерно сорок с. ",
    "Пятое предложение длиной примерно сорок симво. ",
]


async def test_cap_is_cumulative_per_turn_not_per_segment() -> None:
    """Потолок на ХОД, а не на сегмент (несущий кейс, diff).

    Падает на посегментном применении: там каждый из пяти сегментов (~45 символов) прошёл бы
    проверку `≤ 100` по отдельности, синтезатору ушло бы ~230 символов, `truncated` не выставился
    бы ни разу — ровно та реализация, при которой потолок перестаёт ограничивать наш счёт у
    поставщика.
    """
    before_capped = _segment_metric("capped")
    client, sink, budget = await _speak(_FIVE_SENTENCES, settings=_settings(tts_max_chars=100))

    assert sum(len(text) for text in client.texts) <= 100
    assert sink.ends[-1]["truncated"] is True
    assert budget.capped is True
    # Дальнейшие сегменты не синтезируются: доставлено меньше, чем предложений в ответе.
    assert len(sink.ends) < len(_FIVE_SENTENCES)
    assert _segment_metric("capped") == before_capped + 1


async def test_cap_leaves_the_answer_text_intact() -> None:
    """Обратная сторона того же кейса: потолок режет РЕЧЬ, а не ответ.

    Синтезатору ушло только начало, а исходный текст хода не тронут ни на символ — потолок к нему
    не применялся. Падает, если потолок перенесли на генерацию. Полная пара «`delta` = склейка =
    `done.response.assistantMessage`» проверяется на сокете (integration §Совокупный потолок).
    """
    client, _, _ = await _speak(_FIVE_SENTENCES, settings=_settings(tts_max_chars=100))

    answer = "".join(_FIVE_SENTENCES)
    for sentence in _FIVE_SENTENCES:
        assert sentence.strip() in answer
    spoken = " ".join(client.texts)
    assert _FIVE_SENTENCES[-1].strip() not in spoken


async def test_budget_and_numbering_survive_a_new_leg_of_the_same_turn() -> None:
    """Бюджет и нумерация — величины ХОДА и переживают смену ноги (M1/M2 на уровне носителя).

    Падает на реализации, где расход и номер сегмента живут в объекте ноги: там вторая нога
    начала бы счёт заново — фактический потолок стал бы «число ног × `TTS_MAX_CHARS`», а пара
    `(turnId, segment)` перестала бы быть уникальной.
    """
    budget = VoiceTurnBudget()
    settings = _settings(tts_max_chars=100)
    first_client, first_sink, _ = await _speak(
        _FIVE_SENTENCES[:2], settings=settings, budget=budget
    )
    second_client, second_sink, _ = await _speak(
        _FIVE_SENTENCES[2:], settings=settings, budget=budget
    )

    assert sum(len(t) for t in first_client.texts + second_client.texts) <= 100
    numbers = [end["segment"] for end in first_sink.ends + second_sink.ends]
    assert numbers == sorted(numbers)
    assert len(numbers) == len(set(numbers))


async def test_empty_after_cleaning_segment_is_not_synthesized_and_not_paid() -> None:
    """Пустой после чистки сегмент не отправляется, синтезатор не вызывается (`skipped_empty`)."""
    before = _segment_metric("skipped_empty")
    client, sink, _ = await _speak(["```\nprint(1)\nprint(2)\n```\n"])

    assert client.texts == []
    assert sink.ends == []
    assert _segment_metric("skipped_empty") == before + 1
