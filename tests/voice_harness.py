"""Стенд голосового режима: РАБОЧИЙ сокет `/v1/chat/voice`, а не хелпер вокруг обработчика.

Норма (`docs/modules/chat-orchestrator/09-testing.md §Голосовой режим`) требует «Integration —
сквозной путь сеанса (рабочий сокет, не хелпер)», поэтому кадры здесь ходят через настоящее
ASGI-приложение: `create_app()` вызывается со `scope["type"] == "websocket"`, а тест играет роль
транспорта. Это же даёт единственный способ наблюдать close-коды и отказ рукопожатия обычным HTTP
(расширение `websocket.http.response`).

Стенд остаётся в ОДНОМ событийном цикле с тестом — поэтому `TestClient` (портал в отдельном
потоке) здесь не годится: движок БД функционально-скоупный и привязан к циклу теста.

Подменяются ТОЛЬКО внешние границы: распознаватель, синтезатор, LLM-клиент и бакеты лимитов.
`app.db._sessionmaker` направляется на контейнерный движок, потому что обработчик сокета берёт
сессию БД прямым вызовом `session_scope()`, а не через `Depends` — `app.dependency_overrides` его
не перехватывает.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeAnthropicClient, make_jwt

# Значения окружения стенда. Голос сегментируется по предложениям (порог 5 вместо 80), иначе
# каждый нормальный кейс требовал бы абзаца текста, чтобы получить второй сегмент.
#
# ⚠️ ПОРОГИ ЗДЕСЬ ЗАВЫШЕНЫ НАМЕРЕННО, чтобы не мешать кейсам, предмет которых — не они. Ровно
# этим они и гасят кейсы о САМИХ порогах: на харнессном значении такой кейс зелен при ЛЮБОЙ
# реализации. Поэтому кейс, стерегущий порог (`TTS_RATE_LIMIT_PER_MIN`, `TTS_MAX_CHARS`,
# `VOICE_MODE_IDLE_TIMEOUT_SECONDS`, `VOICE_MODE_UTTERANCE_MAX_SECONDS`,
# `VOICE_MODE_SEGMENT_MIN_CHARS`, `ATTACHMENT_MAX_BYTES_AUDIO`), ОБЯЗАН задавать своё значение
# сам — `await voice_stand(TTS_MAX_CHARS="80")` (09-testing §Голосовой режим, врезка).
DEFAULT_ENV: dict[str, str] = {
    "VOICE_MODE_ENABLED": "true",
    "VOICE_INPUT_ENABLED": "true",
    "VOICE_OUTPUT_ENABLED": "true",
    "OPENAI_API_KEY": "sk-openai-voice-test",
    "TTS_DEFAULT_VOICE_ID": "default_female",
    "TTS_CREDIT_COST": "1",
    "TTS_MAX_CHARS": "700",
    "TTS_RATE_LIMIT_PER_MIN": "1000",
    "TTS_AUDIO_FORMAT": "mp3",
    "VOICE_MODE_SEGMENT_MIN_CHARS": "5",
    "VOICE_MODE_IDLE_TIMEOUT_SECONDS": "120",
    "VOICE_MODE_UTTERANCE_MAX_SECONDS": "60",
    "ATTACHMENT_MAX_BYTES_AUDIO": str(10 * 1024 * 1024),
    "RATE_LIMIT_CHAT_PER_USER": "1000",
    "CHARACTERS_ENABLED": "false",
}

# Дельты фейкового LLM-клиента, дающие ровно пять сегментов при пороге 5.
FIVE_SENTENCES: list[str] = [
    "Первое предложение ответа ассистента. ",
    "Второе предложение ответа ассистента. ",
    "Третье предложение ответа ассистента. ",
    "Четвёртое предложение ответа ассистента. ",
    "Пятое предложение ответа ассистента.",
]

_FRAME_WAIT_SECONDS = 20.0

# Прикладных close-кодов РОВНО ПЯТЬ (ADR-104 §9, 02-api-contracts §Close-коды). Проверка живёт
# здесь, а не в одном кейсе, и потому действует на КАЖДЫЙ тест стенда: обратная сторона нормы
# «ни один тест не наблюдает close-кода вне этих пяти» иначе стерегла бы только себя. Реализация,
# заведшая шестой код (например `4429`), уронит все кейсы, где он появится.
ALLOWED_CLOSE_CODES = frozenset({1000, 4400, 4401, 4408, 4500})


class VoiceLLMFake(FakeAnthropicClient):
    """Тот же дублёр LLM-границы, что и в общей сюите, плюс БАРЬЕР в потоке дельт.

    Барьер нужен кейсам прерывания: `interrupt` обязан прийти, пока генерация ещё идёт, а
    `_raise_if_interrupted` срабатывает ровно в точке очередной дельты. Элемент `stream_chunks`,
    оказавшийся вызываемым, ожидается вместо выдачи — так тест задаёт момент, не прибегая к
    реальным паузам (детерминированное событие вместо `sleep`).
    """

    async def stream_message(self, **kwargs: Any) -> Any:
        from app.chat.llm_client import StreamEvent

        result = await self.create_message(**kwargs)
        chunks = self.stream_chunks.pop(0) if self.stream_chunks else None
        if chunks is None:
            text = getattr(result, "text", "") or ""
            chunks = [text] if text else []
        for chunk in chunks:
            if callable(chunk):
                await chunk()
                continue
            yield StreamEvent.text_delta(chunk)
        yield StreamEvent.completed(result)


class VoiceHandshakeDenied(Exception):
    """Рукопожатие отклонено обычным HTTP: апгрейда не было."""

    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        super().__init__(f"handshake denied with {status}")
        self.status = status
        self.payload = payload


class VoiceSocket:
    """Клиентская сторона сокета: кадры внутрь, кадры наружу, close-код наружу."""

    def __init__(self, app: Any, *, token: str, device_id: str | None, path: str) -> None:
        self._app = app
        self._token = token
        self._device_id = device_id
        self._path = path
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.accepted = False
        self.close_code: int | None = None
        self.denial: tuple[int, dict[str, Any]] | None = None

    # ---- ASGI ----

    def _scope(self) -> dict[str, Any]:
        headers: list[tuple[bytes, bytes]] = [(b"host", b"testserver")]
        if self._token:
            headers.append((b"authorization", f"Bearer {self._token}".encode()))
        if self._device_id:
            headers.append((b"x-device-id", self._device_id.encode()))
        path, _, query = self._path.partition("?")
        return {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 51234),
            "root_path": "",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": headers,
            "subprotocols": [],
            "state": {},
            # Отказ ДО апгрейда приходит обычным HTTP только при этом расширении; без него
            # обработчик обязан корректно закрыть сокет, и это тоже наблюдаемо.
            "extensions": {"websocket.http.response": {}},
        }

    async def _receive(self) -> dict[str, Any]:
        return await self._to_app.get()

    async def _send(self, message: dict[str, Any]) -> None:
        await self._from_app.put(message)

    async def open(self) -> VoiceSocket:
        self._task = asyncio.create_task(self._app(self._scope(), self._receive, self._send))
        await self._to_app.put({"type": "websocket.connect"})
        first = await self._next_message()
        if first["type"] == "websocket.accept":
            self.accepted = True
            return self
        if first["type"] == "websocket.http.response.start":
            body = await self._next_message()
            payload = json.loads(body.get("body", b"") or b"{}")
            self.denial = (int(first["status"]), payload)
            raise VoiceHandshakeDenied(int(first["status"]), payload)
        if first["type"] == "websocket.close":  # pragma: no cover — сервер без расширения
            self.close_code = first.get("code")
            raise VoiceHandshakeDenied(0, {})
        raise AssertionError(f"unexpected first ASGI message: {first['type']}")

    async def _next_message(self) -> dict[str, Any]:
        return await asyncio.wait_for(self._from_app.get(), timeout=_FRAME_WAIT_SECONDS)

    async def aclose(self) -> None:
        if self._task is None:
            return
        if self.close_code is None:
            await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, timeout=_FRAME_WAIT_SECONDS)
        self._task = None

    # ---- клиент → сервер ----

    async def send_frame(self, frame: dict[str, Any]) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": json.dumps(frame)})

    async def send_audio(self, data: bytes) -> None:
        await self._to_app.put({"type": "websocket.receive", "bytes": data})

    async def disconnect(self) -> None:
        """Оборвать транспорт, как это делает ушедший клиент."""
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})

    # ---- сервер → клиент ----

    async def next(self) -> dict[str, Any]:
        """Следующий кадр сервера: управляющий JSON, бинарный или закрытие."""
        message = await self._next_message()
        if message["type"] == "websocket.send":
            if (data := message.get("bytes")) is not None:
                return {"type": "__bytes__", "data": bytes(data)}
            return dict(json.loads(message["text"]))
        if message["type"] == "websocket.close":
            self.close_code = message.get("code")
            assert (
                self.close_code in ALLOWED_CLOSE_CODES
            ), f"close-код {self.close_code} вне объявленных пяти (ADR-104 §9)"
            return {"type": "__close__", "code": self.close_code}
        raise AssertionError(f"unexpected ASGI message: {message['type']}")  # pragma: no cover

    async def collect_until(self, *types: str) -> list[dict[str, Any]]:
        """Читать кадры, пока не встретится один из `types` (он входит в результат)."""
        frames: list[dict[str, Any]] = []
        while True:
            frame = await self.next()
            frames.append(frame)
            if frame["type"] in types or frame["type"] == "__close__":
                return frames

    async def turn(self, **utterance: Any) -> list[dict[str, Any]]:
        """Одна реплика голосом: `utterance.begin` + байты + `utterance.end` → кадры до `done`."""
        await self.begin_utterance(**utterance)
        return await self.collect_until("done")

    async def begin_utterance(
        self,
        *,
        audio: bytes = b"voice-bytes",
        media_type: str = "audio/mp4",
        generation_mode: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self.send_frame({"type": "utterance.begin", "mediaType": media_type})
        await self.send_audio(audio)
        end: dict[str, Any] = {"type": "utterance.end"}
        if generation_mode is not None:
            end["generationMode"] = generation_mode
        if context is not None:
            end["context"] = context
        await self.send_frame(end)

    async def start(self, **fields: Any) -> dict[str, Any]:
        """Кадр `start` и ответ на него (`ready` либо `error`)."""
        await self.send_frame({"type": "start", **fields})
        return await self.next()


def upstream_failure() -> Callable[[], Any]:
    """Элемент потока дельт, роняющий генерацию ПОСЛЕ уже выданных дельт.

    Нужен там, где норма требует «сегменты доставлены, затем LLM-провайдер отказал»: флаг
    `raise_upstream` общего дублёра роняет вызов ДО первой дельты и такого пути не даёт.
    """

    async def _raise() -> None:
        from app.errors import UpstreamError

        raise UpstreamError("llm upstream error")

    return _raise


def interrupt_barrier(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Any]:
    """Барьер «дельта не выйдет, пока кадр `interrupt` не обработан».

    Кейсы прерывания требуют, чтобы `interrupt` пришёл, ПОКА генерация идёт, а решение о судьбе
    хода принимается ровно в точке очередной дельты. Барьер связывает эти два события причинно —
    вместо паузы, которая была бы гонкой, а не проверкой.

    Обработчик не подменяется: вокруг него ставится наблюдатель, который зовёт оригинал и
    отмечает событие. Поведение прод-кода при этом не меняется ни на шаг.
    """
    from app.api_gateway.routers import chat_voice

    seen = asyncio.Event()
    original = chat_voice._VoiceSession._on_interrupt  # noqa: SLF001

    async def _observed(self: Any, payload: dict[str, Any]) -> None:
        await original(self, payload)
        seen.set()

    monkeypatch.setattr(chat_voice._VoiceSession, "_on_interrupt", _observed)  # noqa: SLF001

    async def _wait() -> None:
        await seen.wait()

    return _wait


def frames_of(frames: list[dict[str, Any]], frame_type: str) -> list[dict[str, Any]]:
    return [frame for frame in frames if frame["type"] == frame_type]


def first_of(frames: list[dict[str, Any]], frame_type: str) -> dict[str, Any]:
    found = frames_of(frames, frame_type)
    assert found, f"кадр {frame_type!r} не пришёл; получено: {[f['type'] for f in frames]}"
    return found[0]


def delta_text(frames: list[dict[str, Any]]) -> str:
    return "".join(frame["text"] for frame in frames_of(frames, "delta"))


# ---------------------------------------------------------------------------------------------
# Дублёры внешних границ
# ---------------------------------------------------------------------------------------------


@dataclass
class TranscriptionRecorder:
    """Распознаватель: скрипт расшифровок + запись вызовов."""

    transcripts: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    default: str = "привет из голосового режима"

    def next_transcript(self) -> str:
        return self.transcripts.pop(0) if self.transcripts else self.default


@dataclass
class SpeechRecorder:
    """Синтезатор: запись вызовов, скриптуемый отказ и удержание N-го вызова.

    `hold_on` + `hold` дают единственный способ наблюдать исход сегмента `interrupted`: кадр
    `interrupt` обязан прийти, ПОКА сегмент синтезируется. Удержание причинное, не по времени.
    """

    texts: list[str] = field(default_factory=list)
    voices: list[str] = field(default_factory=list)
    fail_on: int | None = None
    hold_on: int | None = None
    hold: Callable[[], Any] | None = None

    @property
    def calls(self) -> int:
        return len(self.texts)


@dataclass
class SpeechBucket:
    """Бакет `rl:speech` вместо Redis — с ЖИВЫМ порогом `TTS_RATE_LIMIT_PER_MIN`.

    Подменяется внешняя граница (Redis), а НЕ правило: настоящий лимитер в тестовой среде
    fail-open, то есть пропускает всё, и кейс о бакете на нём зелен при любой реализации. Здесь
    порог читается из настроек в момент выдачи токена, поэтому кейс, задавший
    `TTS_RATE_LIMIT_PER_MIN` своим значением, получает ФАКТИЧЕСКОЕ поведение бакета.

    Бакет ОДИН на стенд и разделяется голосовым режимом и `POST /v1/chat/speech`: у них общий
    ключ, и второго бакета не заводится.
    """

    taken: int = 0

    async def take(self, **_kwargs: Any) -> bool:
        from app.config import get_settings

        limit = get_settings().tts_rate_limit_per_min
        if self.taken >= limit:
            return False
        self.taken += 1
        return True


@dataclass
class ChatBucket:
    """Бакет ходов `enforce_chat_limits` с ЖИВЫМ порогом `RATE_LIMIT_CHAT_PER_USER`.

    Нужен там, где предмет кейса — сам порог (§13.16: рукопожатие расходует токен). По
    умолчанию порог стенда завышен, поэтому прочим кейсам бакет не мешает.
    """

    taken: int = 0

    async def take(self, **_kwargs: Any) -> bool:
        from app.config import get_settings

        limit = get_settings().rate_limit_chat_per_user
        if self.taken >= limit:
            return False
        self.taken += 1
        return True


class FakeRedis:
    """Redis для замка сессии `voice:turn:{sessionId}` (ADR-104 §13.14) — в памяти процесса.

    Заведён потому, что настоящий Redis в тестовой среде недоступен, а обработчик при
    `RedisError` идёт fail-open: кейс о замке на живом отказе Redis проходил бы по ветке
    «замок не брался» и НЕ проверял бы ничего — ровно тот ложный зелёный, ради которого норма
    и написана. Подменяется ВНЕШНЯЯ граница, правило замка остаётся настоящим.

    `calls` фиксирует каждое взятие и его исход, поэтому кейс доказывает, что отказ пришёл ОТ
    ЗАМКА, а не от fail-open.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        taken = not (nx and key in self.store)
        if taken:
            self.store[key] = value
        self.calls.append({"key": key, "nx": nx, "ex": ex, "taken": taken})
        return True if taken else None

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    def held(self, key: str) -> bool:
        return key in self.store


@dataclass
class VoiceStand:
    """Всё, что кейсу нужно наблюдать и чем управлять."""

    app: Any
    http: AsyncClient
    llm: VoiceLLMFake
    speech: SpeechRecorder
    speech_bucket: SpeechBucket
    chat_bucket: ChatBucket
    redis: FakeRedis
    transcription: TranscriptionRecorder
    sessionmaker: async_sessionmaker[AsyncSession]
    _sockets: list[VoiceSocket] = field(default_factory=list)

    def token(self, user_id: uuid.UUID, **kwargs: Any) -> str:
        return make_jwt(user_id, **kwargs)

    async def connect(
        self,
        user_id: uuid.UUID | None = None,
        *,
        token: str | None = None,
        device_id: str | None = "dev-1",
        path: str = "/v1/chat/voice",
    ) -> VoiceSocket:
        assert user_id is not None or token is not None
        resolved = token if token is not None else make_jwt(user_id)  # type: ignore[arg-type]
        socket = VoiceSocket(self.app, token=resolved, device_id=device_id, path=path)
        self._sockets.append(socket)
        await socket.open()
        return socket

    async def session(
        self, user_id: uuid.UUID, **start_fields: Any
    ) -> tuple[VoiceSocket, dict[str, Any]]:
        """Поднять сокет и открыть сеанс: возвращает сокет и кадр `ready`."""
        socket = await self.connect(user_id)
        ready = await socket.start(**start_fields)
        assert ready["type"] == "ready", ready
        return socket, ready

    def script(self, *deltas: Any, text: str | None = None) -> None:
        """Задать ОДИН ответ фейкового LLM-клиента: его дельты и итоговый текст.

        Вызываемый элемент среди дельт — БАРЬЕР: фейк дождётся его вместо выдачи (см.
        `VoiceLLMFake`). Так кейс прерывания задаёт момент, не прибегая к паузам.
        """
        answer = text if text is not None else "".join(d for d in deltas if isinstance(d, str))
        self.llm.responses.append(self.llm.text_result(answer))
        self.llm.stream_chunks.append(list(deltas))

    def script_tool_call(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *deltas: Any,
        tool_ids: list[str] | None = None,
    ) -> None:
        """Задать ОДИН ответ с клиентскими вызовами инструментов и сопутствующим текстом."""
        text = "".join(d for d in deltas if isinstance(d, str))
        self.llm.responses.append(
            self.llm.parallel_tool_result(calls, text=text, tool_ids=tool_ids)
        )
        self.llm.stream_chunks.append(list(deltas))

    async def aclose(self) -> None:
        for socket in self._sockets:
            await socket.aclose()


def _install_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    from app.config import get_settings

    for key, value in {**DEFAULT_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture
async def voice_stand(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[Callable[..., Any]]:
    """Фабрика стенда: `stand = await voice_stand(VOICE_MODE_ENABLED="false")`.

    Фабрика, а не готовый объект, потому что половина кейсов гейтов входа отличается ровно
    ОДНОЙ переменной окружения, и она обязана быть выставлена ДО создания приложения.
    """
    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import chat as chat_router
    from app.api_gateway.routers import chat_voice
    from app.api_gateway.routers import voices as voices_router
    from app.chat import anthropic_client as anthropic_mod

    stands: list[VoiceStand] = []

    async def _make(**env: str) -> VoiceStand:
        import app.db as db_mod
        from app.chat.speech import SpeechClient
        from app.main import create_app

        _install_env(monkeypatch, env)

        # Обработчик сокета берёт сессию прямым вызовом `session_scope()`, поэтому
        # `dependency_overrides` его не перехватывает: направляем сам процессный sessionmaker.
        monkeypatch.setattr(db_mod, "_sessionmaker", db_sessionmaker, raising=False)

        llm = VoiceLLMFake()
        anthropic_mod._anthropic_singleton = llm  # type: ignore[assignment]

        transcription = TranscriptionRecorder()
        speech = SpeechRecorder()

        class _FakeTranscriptionClient:
            def __init__(self) -> None:
                pass

            async def transcribe(
                self, audio: bytes, media_type: str, language: str | None = None
            ) -> str:
                transcription.calls.append(
                    {"bytes": len(audio), "mediaType": media_type, "language": language}
                )
                return transcription.next_transcript()

        async def _fake_synthesize(_self: Any, *, text: str, voice: Any) -> bytes:
            speech.texts.append(text)
            speech.voices.append(voice.id)
            if speech.hold_on is not None and len(speech.texts) == speech.hold_on:
                assert speech.hold is not None
                await speech.hold()
            if speech.fail_on is not None and len(speech.texts) == speech.fail_on:
                from app.errors import UpstreamError

                raise UpstreamError("speech provider error")
            return f"audio-{len(speech.texts)}".encode()

        monkeypatch.setattr(chat_voice, "TranscriptionClient", _FakeTranscriptionClient)
        monkeypatch.setattr(SpeechClient, "synthesize", _fake_synthesize)
        deps.get_speech_client.cache_clear()

        bucket = SpeechBucket()
        chat_bucket = ChatBucket()
        fake_redis = FakeRedis()

        async def _allow(**_kwargs: Any) -> bool:
            return True

        # Замок сессии `voice:turn:{sessionId}` обязан браться ПО-НАСТОЯЩЕМУ: на живом отказе
        # Redis обработчик уходит в fail-open, и кейс о замке не проверял бы ничего.
        monkeypatch.setattr(chat_voice, "get_redis", lambda: fake_redis)

        # Бакет синтеза — ЖИВОЙ (порог из настроек), остальные лимитеры открыты: их пороги
        # предметом голосовых кейсов не являются и задаются кейсом точечно, где нужны.
        for module, name in (
            (rate_limit, "enforce_speech_limits"),
            (chat_voice, "enforce_speech_limits"),
            (voices_router, "enforce_speech_limits"),
        ):
            monkeypatch.setattr(module, name, bucket.take)

        for module, name in (
            (rate_limit, "enforce_chat_limits"),
            (chat_router, "enforce_chat_limits"),
            (chat_voice, "enforce_chat_limits"),
        ):
            monkeypatch.setattr(module, name, chat_bucket.take)

        for module, name in (
            (rate_limit, "enforce_other_limits"),
            (voices_router, "enforce_other_limits"),
        ):
            monkeypatch.setattr(module, name, _allow)

        async def _override_db() -> AsyncIterator[AsyncSession]:
            async with db_sessionmaker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

        app = create_app()
        app.dependency_overrides[deps.get_db] = _override_db
        http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        stand = VoiceStand(
            app=app,
            http=http,
            llm=llm,
            speech=speech,
            speech_bucket=bucket,
            chat_bucket=chat_bucket,
            redis=fake_redis,
            transcription=transcription,
            sessionmaker=db_sessionmaker,
        )
        stands.append(stand)
        return stand

    yield _make

    for stand in stands:
        await stand.aclose()
        await stand.http.aclose()
    from app.config import get_settings as _get_settings

    _get_settings.cache_clear()
    deps.get_speech_client.cache_clear()
