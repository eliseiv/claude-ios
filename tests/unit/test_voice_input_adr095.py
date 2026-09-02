"""Голосовые сообщения (ADR-095): аудио становится текстом ДО хода.

Проверяется не «распознавание вызвано», а четыре вещи, каждая из которых ломается молча:

1. ГРАНИЦА КЛАССА. Аудио не должно доходить до сборщиков блоков контента: там оно попало бы в
   текстовую ветку и умерло на декодировании UTF-8, а сообщение указывало бы на «битый
   текстовый файл» — то есть увело бы разбор в сторону от настоящей причины.
2. ГЕЙТ. На инстансе без голоса класс объявлен контрактом, но прислать его некому; отказ обязан
   называть причину, иначе она неотличима от «формат не тот».
3. ЗАМЕНА, А НЕ ДОБАВЛЕНИЕ. После распознавания аудиовложения в ходе быть не должно: иначе оно
   уедет провайдеру, который его не ждёт.
4. ПУСТАЯ ЗАПИСЬ. Молчание в микрофон — обычное дело; отправлять модели пустоту нельзя, но и
   падать 500-й нельзя.
"""

from __future__ import annotations

import base64

import pytest

from app.chat.attachments import prepare_attachments
from app.config import Settings
from app.errors import UnsupportedMediaTypeError
from app.schemas.chat import AttachmentIn

_AUDIO = base64.b64encode(b"\x00\x01fake-audio-bytes").decode()


def _audio(media_type: str = "audio/m4a") -> AttachmentIn:
    return AttachmentIn(type="audio", mediaType=media_type, data=_AUDIO, filename="voice.m4a")


@pytest.mark.parametrize(
    "media_type", ["audio/mp4", "audio/m4a", "audio/mpeg", "audio/wav", "audio/webm", "audio/ogg"]
)
def test_contract_accepts_the_formats_phones_actually_record(media_type: str) -> None:
    """Схема обязана принимать то, во что пишут диктофоны, а не абстрактный список."""
    assert AttachmentIn(type="audio", mediaType=media_type, data=_AUDIO).mediaType == media_type


def test_audio_never_reaches_block_assembly() -> None:
    """Аудио до сборки блоков доходить не должно — и отказ обязан говорить ПОЧЕМУ.

    Без явной проверки запись ушла бы в текстовую ветку и упала на декодировании UTF-8: отказ
    был бы про «битый текст», то есть про несуществующую причину.
    """
    with pytest.raises(UnsupportedMediaTypeError, match="transcribed"):
        prepare_attachments([_audio()], Settings(), "openai")


def test_audio_has_its_own_size_ceiling() -> None:
    """Потолок аудио отдельный от картиночного.

    Минута речи в m4a — около мегабайта. Общий с картинками потолок в 20 МиБ пропустил бы
    получасовую запись, и распознавание молотило бы её дольше, чем живёт ход.
    """
    s = Settings()
    assert s.attachment_max_bytes_audio < s.attachment_max_bytes_image


def test_voice_is_off_by_default() -> None:
    """Дефолт — выключено: класс объявлен в контракте раньше, чем клиент научился его слать."""
    assert Settings().voice_input_enabled is False


class _FakeTranscriber:
    """Распознаватель, который возвращает заранее заданный текст и считает вызовы."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[str] = []
        self.languages: list[str | None] = []

    async def transcribe(self, audio: bytes, media_type: str, language: str | None = None) -> str:
        self.calls.append(media_type)
        self.languages.append(language)
        return self.text


def _orchestrator(transcriber: _FakeTranscriber) -> object:
    """Оркестратор с подменённым распознавателем; остальные зависимости не нужны.

    `_transcribe_voice` — предшаг, он не касается ни базы, ни модели, поэтому проверяется без
    поднятия всего хода: тест остаётся быстрым и падает по существу, а не по обвязке.
    """
    from app.chat.orchestrator import ChatOrchestrator

    orch = object.__new__(ChatOrchestrator)

    class _D:
        transcription = transcriber

    orch._deps = _D()  # type: ignore[attr-defined]
    return orch


@pytest.mark.asyncio
async def test_audio_is_replaced_by_text_not_added_to_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ход дальше не должен видеть аудио: провайдер его не ждёт."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("VOICE_INPUT_ENABLED", "true")
    try:
        tr = _FakeTranscriber("привет из голосового")
        orch = _orchestrator(tr)
        image = AttachmentIn(type="image", mediaType="image/png", data=_AUDIO)
        msg, atts, transcript = await orch._transcribe_voice("", [_audio(), image])

        assert msg == "привет из голосового", "распознанное не стало текстом хода"
        assert transcript == "привет из голосового"
        assert atts is not None
        assert [a.type for a in atts] == ["image"], "аудио осталось во вложениях"
        assert tr.calls == ["audio/m4a"]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_disabled_instance_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Не включено» и «формат не тот» — разные причины, и клиент обязан их различать."""
    from app.config import get_settings
    from app.errors import ValidationFailedError

    get_settings.cache_clear()
    monkeypatch.setenv("VOICE_INPUT_ENABLED", "false")
    try:
        orch = _orchestrator(_FakeTranscriber("x"))
        with pytest.raises(ValidationFailedError, match="not enabled"):
            await orch._transcribe_voice("", [_audio()])
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_silent_recording_is_refused_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Молчание в микрофон: модели нечего отправить, но и 500 отдавать не за что."""
    from app.config import get_settings
    from app.errors import ValidationFailedError

    get_settings.cache_clear()
    monkeypatch.setenv("VOICE_INPUT_ENABLED", "true")
    try:
        orch = _orchestrator(_FakeTranscriber(""))
        with pytest.raises(ValidationFailedError, match="no recognizable speech"):
            await orch._transcribe_voice("", [_audio()])
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_typed_text_and_voice_both_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если человек и написал, и наговорил — потерять нельзя ни то, ни другое."""
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("VOICE_INPUT_ENABLED", "true")
    try:
        orch = _orchestrator(_FakeTranscriber("наговорённое"))
        msg, atts, _ = await orch._transcribe_voice("написанное", [_audio()])
        assert "написанное" in msg and "наговорённое" in msg
        assert atts is None
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("locale", "expected"),
    [("ru-RU", "ru"), ("ru_RU", "ru"), ("RU", "ru"), ("ru", "ru"), ("en-US", "en")],
)
def test_language_hint_derived_from_client_locale(locale: str, expected: str) -> None:
    """Язык распознавания берётся из локали, которую клиент и так присылает.

    Без подсказки распознаватель определяет язык сам и на коротких или шумных записях
    ошибается: прод 2026-09-02 — русское голосовое распозналось как ИСПАНСКОЕ, и модель,
    получив испанский текст, ответила по-испански. Отказ молчаливый вдвойне: и расшифровка, и
    ответ выглядят осмысленными, просто не на том языке.
    """
    from app.chat.orchestrator import _language_of

    assert _language_of(locale) == expected


@pytest.mark.parametrize("locale", [None, "", "x", "zzz", "123"])
def test_unknown_locale_leaves_autodetect_alone(locale: str | None) -> None:
    """Нераспознанная локаль даёт None, а не догадку.

    Неверная подсказка ХУЖЕ автоопределения: она заставляет распознаватель слышать язык,
    которого в записи нет. Поэтому подсказка только из явных двух букв.
    """
    from app.chat.orchestrator import _language_of

    assert _language_of(locale) is None


@pytest.mark.asyncio
async def test_locale_reaches_the_transcriber(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверяется не наличие параметра, а что он ДОХОДИТ до распознавателя.

    Между локалью и вызовом три передачи; обрыв на любой из них не виден ни в одном тесте,
    проверяющем звенья по отдельности.
    """
    from app.config import get_settings

    captured: dict[str, object] = {}

    class _Recorder:
        async def transcribe(self, audio: bytes, media_type: str, language: str | None = None):
            captured["language"] = language
            return "привет"

    get_settings.cache_clear()
    monkeypatch.setenv("VOICE_INPUT_ENABLED", "true")
    try:
        orch = _orchestrator(_Recorder())  # type: ignore[arg-type]
        await orch._transcribe_voice("", [_audio()], locale="ru-RU")
        assert captured["language"] == "ru"
    finally:
        get_settings.cache_clear()
