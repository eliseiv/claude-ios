"""WebSocket `/v1/chat/voice` — живой голосовой диалог (ADR-104).

Устройство шлёт речь, сервер исполняет **обычный ход** тем же `ChatOrchestrator.run(...)` и
озвучивает ответ **по мере генерации**, пользователь вправе перебить. Транспорт — WebSocket,
потому что прерывание обязано быть **сообщением**, а не обрывом соединения: обрыв неотличим от
потери сети и приходит как `CancelledError`, то есть мимо ветки закрытия хода.

**Существующие контракты не меняются ни на байт.** Ни `POST /v1/chat/run`, ни `/v1/chat/v2/run`,
ни SSE-поток, ни `POST /v1/chat/speech` этот модуль не трогает: он переиспользует их части
(`ChatOrchestrator.run`, `TranscriptionClient`, `to_spoken_text`/`apply_speech_cap`,
`resolve_voice`, `enforce_chat_limits`, конверт ошибок ADR-004), а не дублирует их.

**Что живёт здесь и только здесь** — транспорт: рукопожатие и его отказы, диспетчеризация кадров,
последовательность ходов, idle-таймаут и прикладные close-коды. Сегментация и потоковый синтез
живут в `app.chat.speech` рядом с чисткой; закрытие прерванного хода — в оркестраторе. Граница
проведена так, что каждая часть проверяема без сокета.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_gateway.rate_limit import enforce_chat_limits, enforce_speech_limits
from app.audit.service import AuditService
from app.chat.attachments import AUDIO_MEDIA_TYPES
from app.chat.orchestrator import (
    ChatOrchestrator,
    ChatRunOut,
    ToolResultIn,
    TurnInterrupted,
    _language_of,
)
from app.chat.repository import ChatRepository, derive_title
from app.chat.speech import VoiceTurnBudget, VoiceTurnSpeech
from app.chat.transcription import TranscriptionClient
from app.chat.voice_mode import voice_mode_available, voice_mode_missing_flags
from app.chat.voices import Voice, resolve_voice
from app.config import get_settings
from app.db import session_scope
from app.deps import (
    client_ip,
    get_speech_client,
    get_v2_orchestrator,
    provision_user,
    verify_bearer_token,
)
from app.errors import (
    AppError,
    RateLimitedError,
    UnauthorizedError,
    VoiceModeDisabledError,
    VoiceModeNotConfiguredError,
)
from app.observability.context import set_session_id, set_user_id
from app.observability.logging import get_logger, log_event
from app.observability.metrics import voice_mode_connections, voice_mode_turns_total
from app.preferences.service import PreferencesService
from app.schemas.chat import GenerationMode
from app.schemas.voice_frames import (
    FRAME_INTERRUPT,
    FRAME_PING,
    FRAME_START,
    FRAME_TEXT,
    FRAME_TOOL_RESULT,
    FRAME_UTTERANCE_BEGIN,
    FRAME_UTTERANCE_END,
    VoiceInterruptFrame,
    VoiceStartFrame,
    VoiceTextFrame,
    VoiceToolResultFrame,
    VoiceUtteranceBeginFrame,
    VoiceUtteranceEndFrame,
)
from app.wallet.service import WalletService

logger = get_logger(__name__)

router = APIRouter(tags=["Chat"])

# Прикладные close-коды (ADR-104 §9). Их РОВНО ПЯТЬ, и у каждого есть производящее событие в
# таблице отказов; объявленный код без производящего события был бы мёртвой декларацией.
# Причину несёт предшествующий кадр `error` (поле `code`), close-код — КЛАСС случившегося,
# поэтому отдельного close-кода на каждый `error.code` не заводится. `4429` не вводится: отказ
# лимита ходов внутри сеанса — отказ ХОДА, соединение живёт, а лимита соединений нет вовсе.
CLOSE_NORMAL = 1000
CLOSE_START_REJECTED = 4400
CLOSE_UNAUTHORIZED = 4401
CLOSE_IDLE_TIMEOUT = 4408
CLOSE_INTERNAL = 4500

# `error.scope` — К ЧЕМУ относится отказ, а не терминален ли он (ADR-104 §4). Признак
# терминальности для клиента — приход close-кадра, а НЕ значение `scope`.
SCOPE_SESSION = "session"
SCOPE_TURN = "turn"
SCOPE_SPEECH = "speech"


def _envelope(status_code: int, code: str, message: str) -> Any:
    """Единый конверт ошибки ADR-004 для отказа ДО апгрейда.

    Рукопожатие — последняя точка, где конверт ещё применим: после `accept` его не существует, и
    отказ становится кадром `error`.
    """
    from fastapi.responses import JSONResponse

    from app.observability.context import get_request_id

    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "requestId": get_request_id()}},
    )


class _Denied(Exception):
    """Отказ рукопожатия: обычный HTTP до апгрейда."""

    def __init__(self, error: AppError) -> None:
        super().__init__(error.code)
        self.error = error


class _VoiceSession:
    """Состояние ОДНОГО соединения. Одно соединение — одна сессия, ходы строго последовательны.

    Мультиплексирование чатов отвергнуто (ADR-104 §3): оно потребовало бы `sessionId` в каждом
    кадре и параллельных ходов, а `AsyncSession` не многозадачна — два одновременных хода одной
    сессии реплеили бы провайдеру одну историю дважды и разъехались бы на барьере хода.
    """

    def __init__(self, websocket: WebSocket, user_id: uuid.UUID, device_id: str | None) -> None:
        self._ws = websocket
        self._user_id = user_id
        self._device_id = device_id
        self._send_lock = asyncio.Lock()
        self._alive = True

        self._settings = get_settings()
        self._start: VoiceStartFrame | None = None
        self._session_id: uuid.UUID | None = None
        self._voice: Voice | None = None

        # Открытая реплика: кадры звука между `utterance.begin` и `utterance.end`.
        self._utterance_media_type: str | None = None
        self._utterance: bytearray = bytearray()
        self._utterance_started: float = 0.0
        self._utterance_rejected = False

        # Текущий ход. Ходы строго последовательны, поэтому единственная задача, а не набор.
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_id: uuid.UUID | None = None
        self._turn_text = ""
        self._turn_speech: VoiceTurnSpeech | None = None
        # Величины, принадлежащие ХОДУ, а не его ноге: совокупный бюджет `TTS_MAX_CHARS`,
        # сквозная нумерация сегментов, число дослушанных сегментов хода и признаки «дальше не
        # синтезируем». Живут ЗДЕСЬ, потому что ход с клиентскими инструментами исполняется
        # несколькими ногами с одним `turnId`, а синтез создаётся на ногу (ADR-104 §6, §7).
        # Пересоздаётся ровно там, где начинается НОВЫЙ ход, и переживает ноги continuation.
        self._turn_budget = VoiceTurnBudget()
        # Произвёл ли ассистент хоть один символ текста в ЭТОМ ХОДЕ — на ЛЮБОЙ его ноге
        # (ADR-104 §13.11). Величина ХОДА, а не ноги, и это вторая половина решения, а не
        # деталь: снимок с поножного `_turn_text` ответил бы на вопрос «сказала ли что-нибудь
        # ЭТА нога» и на continuation дал бы «пусто» ровно там, где сопутствующий текст ноги
        # `tool_call` пользователь уже УСЛЫШАЛ, — ассистент договорил бы целый новый ответ
        # после просьбы замолчать.
        self._turn_spoke = False
        self._interrupt_reason: str | None = None
        # СНИМОК предиката §5, снятый В МОМЕНТ кадра `interrupt`. Живёт по правилам
        # `_interrupt_reason`: прерывание — событие ХОДА, поэтому на ноге `tool.result` не
        # сбрасывается, только в `_new_turn`.
        self._interrupt_had_text = False
        # Причина, по которой у ЭТОГО хода не будет звука. Решается ДО первого сегмента
        # (балансовый гейт) или по его концу (произносить оказалось нечего), а отправляется
        # ОДНИМ кадром при закрытии хода: раньше `turnId` ещё не выпущен оркестратором, а кадр
        # без него был бы вторым, «безадресным» пространством идентификаторов.
        self._speech_skipped: str | None = None

    # ---- транспорт ----

    async def _send(self, frame: dict[str, Any]) -> None:
        """Отправить управляющий кадр. Сериализация одна на все кадры сервер → клиент."""
        if not self._alive:
            return
        async with self._send_lock:
            if not self._alive:
                return
            try:
                await self._ws.send_json(frame)
            except (WebSocketDisconnect, RuntimeError):
                # Клиент ушёл. Ход при этом НЕ отменяется (ADR-104 §9, строка 1): он доходит до
                # конца, персистится и тарифицируется; теряется только звук в полёте.
                self._alive = False

    async def _send_bytes(self, data: bytes) -> None:
        if not self._alive:
            return
        async with self._send_lock:
            if not self._alive:
                return
            try:
                await self._ws.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError):
                self._alive = False

    async def _skipped(self) -> None:
        """Кадр `speech.skipped`: ход исполнен, но звука не будет. Ход при этом НЕ теряется."""
        if self._speech_skipped is None:
            return
        await self._send(
            {
                "type": "speech.skipped",
                "turnId": str(self._turn_id),
                "reason": self._speech_skipped,
            }
        )

    async def _error(
        self, *, code: str, message: str, scope: str, turn_id: uuid.UUID | None = None
    ) -> None:
        """Кадр `error`. Соединение НЕ закрывается — закрытие делает вызывающий, если отказ
        терминален: терминальность из `scope` не выводится."""
        frame: dict[str, Any] = {"type": "error", "code": code, "message": message, "scope": scope}
        if turn_id is not None:
            frame["turnId"] = str(turn_id)
        await self._send(frame)

    # ---- сеанс ----

    async def run(self) -> None:
        """Цикл чтения кадров до закрытия. Возвращает управление, закрыв сокет прикладным кодом."""
        idle = self._settings.voice_mode_idle_timeout_seconds
        try:
            while True:
                try:
                    message = await asyncio.wait_for(self._ws.receive(), timeout=idle)
                except TimeoutError:
                    if self._turn_task is not None and not self._turn_task.done():
                        # Сеанс не простаивает: сервер ГЕНЕРИРУЕТ, и кадров от клиента в это
                        # время не ждут по построению. Таймаут закрывает МОЛЧАЩИЙ сокет, а не
                        # работающий, иначе долгий ответ выглядел бы разрывом сети.
                        continue
                    # Единственная мера против накопления простаивающих соединений: лимита
                    # сокетов на пользователя нет намеренно (ADR-104 §10).
                    await self._close(CLOSE_IDLE_TIMEOUT)
                    return
                if message["type"] == "websocket.disconnect":
                    self._alive = False
                    return
                if (data := message.get("bytes")) is not None:
                    await self._on_binary(bytes(data))
                    continue
                text = message.get("text")
                if text is None:  # pragma: no cover — кадр без обеих полезных нагрузок
                    continue
                if await self._on_text(text):
                    return
        finally:
            # Обрыв ход не отменяет: дожидаемся его конца, чтобы шаг и списание не потерялись
            # вместе с задачей при закрытии процесса запроса.
            await self._drain_turn()

    async def _close(self, code: int) -> None:
        self._alive = False
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self._ws.close(code=code)

    async def _drain_turn(self) -> None:
        task, self._turn_task = self._turn_task, None
        if task is None:
            return
        if not task.done() and self._turn_speech is not None:
            # Звук отбрасывается: серверного буфера «переиграть» нет и не заводится.
            self._turn_speech.interrupt()
        with contextlib.suppress(Exception):
            await task

    # ---- диспетчеризация ----

    async def _on_text(self, raw: str) -> bool:
        """Обработать управляющий кадр. `True` — соединение закрыто и цикл обязан кончиться."""
        try:
            payload = _parse_json(raw)
        except ValueError:
            await self._error(
                code="validation_error", message="frame is not valid JSON", scope=SCOPE_SESSION
            )
            return False
        frame_type = payload.get("type") if isinstance(payload, dict) else None
        if frame_type == FRAME_PING:
            return False
        if frame_type == FRAME_START:
            return await self._on_start(payload)
        if self._start is None:
            # `start` — ПЕРВЫЙ кадр сеанса: до него сеанс не привязан ни к сессии, ни к голосу.
            await self._error(
                code="validation_error",
                message="the first frame of a voice session must be 'start'",
                scope=SCOPE_SESSION,
            )
            return False
        if frame_type == FRAME_UTTERANCE_BEGIN:
            await self._on_utterance_begin(payload)
            return False
        if frame_type == FRAME_UTTERANCE_END:
            await self._on_utterance_end(payload)
            return False
        if frame_type == FRAME_TEXT:
            await self._on_text_frame(payload)
            return False
        if frame_type == FRAME_INTERRUPT:
            await self._on_interrupt(payload)
            return False
        if frame_type == FRAME_TOOL_RESULT:
            await self._on_tool_result(payload)
            return False
        await self._error(
            code="validation_error",
            message="unknown frame type",
            scope=SCOPE_SESSION,
        )
        return False

    async def _on_start(self, payload: dict[str, Any]) -> bool:
        if self._start is not None:
            await self._error(
                code="validation_error",
                message="'start' is accepted once per connection",
                scope=SCOPE_SESSION,
            )
            return False
        try:
            frame = VoiceStartFrame.model_validate(payload)
        except ValidationError:
            await self._error(
                code="validation_error", message="invalid 'start' frame", scope=SCOPE_SESSION
            )
            return False
        if frame.assistantMode == "code":
            # ADR-104 §7: подсказка озвучки в код-режим намеренно не добавляется, и такой сеанс
            # молчал бы КАЖДЫМ ходом. Поле session-fixed ⇒ отказ терминален.
            await self._error(
                code="unsupported_assistant_mode",
                message="voice mode does not support the code assistant mode",
                scope=SCOPE_SESSION,
            )
            await self._close(CLOSE_START_REJECTED)
            return True

        try:
            session_id, voice = await self._open_session(frame)
        except AppError as exc:
            # Чужая и несуществующая сессия неотличимы: тот же код и тот же текст, поэтому
            # существование чужой не раскрывается (05-security §Голосовой режим).
            await self._error(code=exc.code, message=exc.message, scope=SCOPE_SESSION)
            await self._close(CLOSE_START_REJECTED)
            return True

        self._start = frame
        self._session_id = session_id
        self._voice = voice
        set_session_id(str(session_id))
        await self._send({"type": "ready", "sessionId": str(session_id), "voiceId": voice.id})
        return False

    async def _open_session(self, frame: VoiceStartFrame) -> tuple[uuid.UUID, Voice]:
        """Резолв-или-создание сессии и резолв голоса — по одному разу на сеанс.

        `ready {sessionId, voiceId}` уходит СРАЗУ после `start`, поэтому сессия обязана
        существовать уже здесь; создаётся она общим с HTTP-ходом путём (`open_session`), со всеми
        проверками session-fixed полей. Голос резолвится той же единственной функцией и той же
        тройкой ступеней, что у `POST /v1/chat/speech`.
        """
        resolved: tuple[uuid.UUID, Voice] | None = None
        async for db in session_scope():
            orchestrator = get_v2_orchestrator(db)
            repo = ChatRepository(db)
            if frame.sessionId is not None:
                existing = await repo.get_session(frame.sessionId, self._user_id)
                if existing is None:
                    raise _SessionNotFound("session not found")
                session = existing
            else:
                # `open_session` коммитит САМ, и это не украшение: выход из `session_scope`
                # через `break` закрывает генератор, не доходя до его коммита, — тот же приём и
                # то же обязательство, что у продюсера SSE-роута.
                session = await orchestrator.open_session(
                    user_id=self._user_id,
                    project_id=frame.projectId,
                    session_id=None,
                    mode=frame.mode,
                    assistant_mode=frame.assistantMode,
                    model=frame.model,
                    character_id=frame.characterId,
                    workspace_project_id=frame.workspaceProjectId,
                )
            # Значения снимаются, пока сессия БД жива: наружу уходят `id` и голос, а не
            # ORM-объект, переживающий свою транзакцию.
            resolved = (
                session.id,
                resolve_voice(
                    character_id=session.character_id,
                    user_default_voice_id=await PreferencesService(db).get_default_voice_id(
                        self._user_id
                    ),
                ),
            )
            break
        if resolved is None:  # pragma: no cover — генератор сессии всегда даёт ровно одну
            raise RuntimeError("session scope produced no session")
        return resolved

    # ---- реплика ----

    async def _on_utterance_begin(self, payload: dict[str, Any]) -> None:
        try:
            frame = VoiceUtteranceBeginFrame.model_validate(payload)
        except ValidationError:
            await self._error(
                code="validation_error",
                message="invalid 'utterance.begin' frame",
                scope=SCOPE_SESSION,
            )
            return
        if frame.mediaType not in AUDIO_MEDIA_TYPES:
            # Набор ВХОДА — тот же, что у голосового вложения; набор ВЫХОДА другой и сюда не
            # переносится. Общий allowlist вложений шире, поэтому класс проверяется отдельно.
            await self._error(
                code="unsupported_media_type",
                message="unsupported audio media type",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return
        self._utterance_media_type = frame.mediaType
        self._utterance = bytearray()
        self._utterance_started = time.monotonic()
        self._utterance_rejected = False

    async def _on_binary(self, data: bytes) -> None:
        if self._utterance_media_type is None:
            # Принадлежность бинарного кадра задаётся ОБЪЕМЛЮЩИМ сегментом, а не заголовком в
            # самом кадре: кадр вне открытого сегмента отбрасывается, соединение живёт.
            await self._error(
                code="unexpected_binary_frame",
                message="binary frame outside an open utterance",
                scope=SCOPE_SESSION,
            )
            return
        if self._utterance_rejected:
            return
        self._utterance.extend(data)
        if self._utterance_too_large():
            # Действуют ОБА потолка, что сработает раньше: байты ограничивают трафик, секунды —
            # время распознавания, и на сильно сжатом кодеке одно не выводится из другого.
            self._utterance_rejected = True
            self._utterance = bytearray()
            await self._error(
                code="attachment_too_large",
                message="utterance exceeds the size or duration limit",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )

    def _utterance_too_large(self) -> bool:
        """Оба потолка реплики: байты и секунды. Что сработает раньше, то и отказывает.

        Секунды меряются ПО ЧАСАМ — от `utterance.begin` до последнего пришедшего кадра звука.
        Декодера у сервера нет и заводить его нельзя (это новая зависимость), а на живом потоке
        длительность записи и есть время, которое она шла: реплика передаётся по мере
        произнесения, а не загружается файлом.
        """
        if len(self._utterance) > self._settings.attachment_max_bytes_audio:
            return True
        elapsed = time.monotonic() - self._utterance_started
        return elapsed > self._settings.voice_mode_utterance_max_seconds

    async def _on_utterance_end(self, payload: dict[str, Any]) -> None:
        try:
            frame = VoiceUtteranceEndFrame.model_validate(payload)
        except ValidationError:
            await self._error(
                code="validation_error",
                message="invalid 'utterance.end' frame",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return
        audio, media_type = bytes(self._utterance), self._utterance_media_type
        rejected = self._utterance_rejected
        too_long = self._utterance_media_type is not None and self._utterance_too_large()
        self._utterance = bytearray()
        self._utterance_media_type = None
        self._utterance_rejected = False
        if media_type is None:
            await self._error(
                code="validation_error",
                message="'utterance.end' without an open utterance",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return
        if rejected:
            # Отказ по потолку уже отправлен на кадре, который его превысил; ход не начинается.
            return
        if too_long:
            # Потолок СЕКУНД переспрашивается здесь, а не только на кадрах звука: клиент,
            # выдержавший паузу дольше потолка между последним куском и `utterance.end`,
            # провёл бы реплику мимо ограничения — проверка по часам обязана быть и в точке,
            # где реплика закрывается.
            await self._error(
                code="attachment_too_large",
                message="utterance exceeds the size or duration limit",
                scope=SCOPE_TURN,
            )
            return
        if not await self._claim_turn():
            return
        self._spawn_turn(
            self._turn_from_utterance(
                audio=audio,
                media_type=media_type,
                generation_mode=frame.generationMode,
                context=frame.context,
            )
        )

    async def _on_text_frame(self, payload: dict[str, Any]) -> None:
        try:
            frame = VoiceTextFrame.model_validate(payload)
        except ValidationError:
            await self._error(
                code="validation_error",
                message="invalid 'text' frame",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return
        if not await self._claim_turn():
            return
        self._spawn_turn(
            self._turn_from_text(
                text=frame.text,
                generation_mode=frame.generationMode,
                context=frame.context,
            )
        )

    async def _claim_turn(self) -> bool:
        """Ходы строго последовательны: второй ход при незакрытом первом отклоняется."""
        if self._turn_task is not None and not self._turn_task.done():
            await self._error(
                code="turn_in_progress",
                message="a turn of this session is still running",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return False
        return True

    # ---- прерывание ----

    async def _on_interrupt(self, payload: dict[str, Any]) -> None:
        try:
            frame = VoiceInterruptFrame.model_validate(payload)
        except ValidationError:
            await self._error(
                code="validation_error",
                message="invalid 'interrupt' frame",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return
        if self._turn_id is None or frame.turnId != self._turn_id:
            # Прерывать нечего: ход с этим `turnId` уже закрыт (кадр разошёлся с `done` в сети)
            # либо назван чужой. Кадра отказа здесь НЕТ намеренно: намерение пользователя уже
            # исполнено — ассистент молчит, — а чужого кода из таблицы отказов под этот случай
            # не заводится: `turn_in_progress` означает «ход ещё идёт», то есть ровно обратное.
            return
        # Синтез прекращается ВСЕГДА и немедленно — это ровно то, о чём просил пользователь.
        self._interrupt_reason = frame.reason
        # СНИМОК предиката §5 берётся ЗДЕСЬ, в момент кадра, а не при следующей дельте
        # (ADR-104 §13.11): вопрос нормы — «было ли уже что-то сказано, когда человек перебил»,
        # и ответ на него обязан быть свойством ХОДА, а не размера кусков, которыми провайдер
        # отдаёт текст, и не скорости сети. Иначе двое, перебившие на одном и том же слове,
        # получили бы разные истории, и один и тот же — разные между попытками.
        self._interrupt_had_text = self._turn_spoke
        if self._turn_speech is not None:
            self._turn_speech.interrupt()

    def _raise_if_interrupted(self) -> None:
        """Предикат ADR-104 §5 по СНИМКУ момента кадра `interrupt` (уточнён §13.11).

        Вопрос предиката — «было ли уже что-то сказано в ЭТОМ ХОДЕ в момент, когда человек
        перебил», а НЕ «пуст ли накопитель ноги сейчас». Второе читалось бы как «пришла ли ещё
        одна дельта после кадра», то есть вычислялось бы из размера кусков провайдера и скорости
        сети, а не из наблюдаемых фактов пути: одно и то же действие пользователя давало бы
        разную историю от прогона к прогону.

        Снимок непуст → генерация отменяется исключением в точке очередной дельты. Снимок пуст →
        генерация НЕ отменяется и доходит до штатного конца: отмена оставила бы ход без шага
        ассистента (уже случившийся прод-дефект) либо потребовала бы писать пустой assistant-шаг,
        который провайдер отвергает при реплее. Кадр `interrupted` уходит в ОБЕИХ строках — он
        подтверждает намерение, а не отмену.

        Величина снимка — ХОДА (`_turn_spoke`), а не ноги: сопутствующий текст ноги `tool_call`
        человек уже услышал, и на continuation поножный накопитель дал бы ложное «пусто».

        Остаточный риск назван и не устраняется: отмена возможна только в точке очередной
        дельты, поэтому непустой снимок при отсутствии следующих дельт оставит ход без пометки —
        прерывать на произвольном сетевом ожидании §5 запрещает.
        """
        if self._interrupt_reason is None or not self._interrupt_had_text:
            return
        raise TurnInterrupted(
            text=self._turn_text,
            reason=self._interrupt_reason,
            # ЧИСЛО ДОСЛУШАННЫХ СЕГМЕНТОВ ХОДА, а не ноги: пользователь слушал ход целиком, и
            # ноги continuation для него неразличимы. Предикат СПИСАНИЯ синтеза — другая
            # величина (`delivered_segments` ноги), единица там шаг; не путать.
            spoken_segments=self._turn_budget.heard_segments,
        )

    # ---- ход ----

    def _spawn_turn(self, coro: Coroutine[Any, Any, None]) -> None:
        self._turn_task = asyncio.create_task(coro)

    def _new_turn(self) -> None:
        """Сбросить состояние НОВОГО хода, включая потурновый бюджет синтеза.

        Вызывается ровно из двух точек — реплики и набранного текста, — и НЕ вызывается на ноге
        `tool.result`: та продолжает ТОТ ЖЕ ход под тем же `turnId`, и обнуление бюджета там
        было бы ровно тем дефектом, ради которого бюджет вынесен из объекта ноги.
        """
        self._turn_text = ""
        self._turn_spoke = False
        self._interrupt_reason = None
        self._interrupt_had_text = False
        self._speech_skipped = None
        self._turn_id = None
        self._turn_budget = VoiceTurnBudget()

    async def _turn_from_utterance(
        self,
        *,
        audio: bytes,
        media_type: str,
        generation_mode: GenerationMode,
        context: dict[str, Any] | None,
    ) -> None:
        """Распознать реплику и исполнить ход. Речь вложением НЕ становится (ADR-095 §1).

        Распознавание идёт ТЕМ ЖЕ `TranscriptionClient` и тем же ключом, что у голосового
        вложения; байты записи нигде не персистятся, а пользовательский шаг хода — обычный
        текстовый шаг с расшифровкой, неотличимый от набранного руками.

        Отказы ЭТОГО шага приходят БЕЗ `turnId`, и поле объявлено необязательным именно для них:
        `turnId` — это `messageStepId` хода, второго пространства идентификаторов не заводится, а
        ход, который не начался, своего `messageStepId` не имеет.
        """
        self._new_turn()
        locale = (context or {}).get("locale")
        try:
            transcript = await TranscriptionClient().transcribe(
                # Подсказка языка — ТА ЖЕ функция, что у голосового вложения (ADR-095 §6), и
                # именно она, а не «похожая»: правило «только явные две буквы, иначе
                # автоопределение» нормативно, потому что НЕВЕРНАЯ подсказка хуже её отсутствия —
                # распознаватель начинает слышать несуществующий язык (прод 2026-09-02).
                audio,
                media_type,
                language=_language_of(locale if isinstance(locale, str) else None),
            )
        except AppError as exc:
            await self._error(code=exc.code, message=exc.message, scope=SCOPE_TURN)
            return
        if not transcript:
            # Пустая реплика — ПРЕДМЕТНЫЙ отказ, а не пустой ход: отправить в модель тишину
            # нельзя, промолчать в ответ — тоже. Код `empty_audio` вводится ADR-104 §9 и на
            # HTTP-путь не переносится (там та же запись даёт `422 validation_error`).
            await self._error(
                code="empty_audio",
                message="the utterance contains no recognizable speech",
                scope=SCOPE_TURN,
            )
            return
        await self._execute_turn(
            message=transcript,
            generation_mode=generation_mode,
            context=context,
            transcript=transcript,
        )

    async def _turn_from_text(
        self, *, text: str, generation_mode: GenerationMode, context: dict[str, Any] | None
    ) -> None:
        self._new_turn()
        await self._execute_turn(
            message=text, generation_mode=generation_mode, context=context, transcript=None
        )

    async def _execute_turn(
        self,
        *,
        message: str,
        generation_mode: GenerationMode,
        context: dict[str, Any] | None,
        transcript: str | None,
    ) -> None:
        if generation_mode == "study_learn":
            # Дельт в этом режиме нет вовсе, а `assistantMessage` подавлен при непустом `quiz` —
            # озвучивать нечего ПО ПОСТРОЕНИЮ. Режим приходит per-turn, поэтому соединение живёт
            # и следующий ход в допустимом режиме проходит.
            await self._error(
                code="unsupported_generation_mode",
                message="voice mode does not support the study_learn generation mode",
                scope=SCOPE_TURN,
            )
            return
        if not await self._allow_turn():
            return
        assert self._start is not None
        start = self._start
        started = time.monotonic()

        async def _call(orchestrator: ChatOrchestrator) -> ChatRunOut:
            return await orchestrator.run(
                user_id=self._user_id,
                project_id=start.projectId,
                session_id=self._session_id,
                message=message,
                mode=start.mode,
                assistant_mode=start.assistantMode,
                model=start.model,
                character_id=start.characterId,
                workspace_project_id=start.workspaceProjectId,
                context=context,
                generation_mode=generation_mode,
                generation_backend="v2",
                on_text_delta=self._on_delta,
                on_turn_start=self._turn_started(transcript),
            )

        await self._run_leg(call=_call, started=started, title_source=message)

    async def _on_tool_result(self, payload: dict[str, Any]) -> None:
        try:
            frame = VoiceToolResultFrame.model_validate(payload)
        except ValidationError:
            await self._error(
                code="validation_error",
                message="invalid 'tool.result' frame",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
            return
        if not await self._claim_turn():
            return
        # `turnId` кадра НЕ принимается: `turnId` — это `messageStepId`, который выпустил
        # оркестратор и который транспорт уже знает (`_turn_id`). Взять его снаружи значило бы
        # позволить клиенту переименовать ключ связи, объявленный точным; ход же всё равно
        # резолвится оркестратором по самим `toolCallId`, а не по этому полю. Поле остаётся в
        # схеме кадра, потому что контракт обязывает клиента его слать.
        # `_interrupt_reason`, `_interrupt_had_text` и `_turn_spoke` здесь НЕ сбрасываются, и
        # это не пропуск: прерывание — событие ХОДА, а не ноги, и «ход уже что-то сказал» тоже.
        # Пользователь, перебивший ход на ноге `tool_call` при пустом накопителе (генерация
        # тогда не отменяется, ADR-104 §5), получил бы после `tool.result` ход, закрытый как
        # обычный: без кадра `interrupted` и без пометки `payload.interrupted`. А сброшенный
        # снимок дал бы на continuation ложное «ничего не сказано» там, где сопутствующий текст
        # ноги `tool_call` уже прозвучал (§13.11). Сбрасывается только то, что действительно
        # принадлежит ноге: её накопленный текст и причина, по которой звука не было на ней.
        self._turn_text = ""
        self._speech_skipped = None
        normalized = [
            ToolResultIn(
                tool_call_id=item.toolCallId,
                result=item.result,
                error=item.error,
            )
            for item in frame.results
        ]
        started = time.monotonic()

        async def _call(orchestrator: ChatOrchestrator) -> ChatRunOut:
            assert self._session_id is not None
            return await orchestrator.tool_result(
                user_id=self._user_id,
                session_id=self._session_id,
                results=normalized,
                generation_backend="v2",
                on_text_delta=self._on_delta,
            )

        self._spawn_turn(self._run_leg(call=_call, started=started, title_source=None))

    async def _allow_turn(self) -> bool:
        """Ось режима, свежесть JWT и лимит ХОДОВ (тот же `enforce_chat_limits`).

        Отказ лимита на уже поднятом сокете — отказ ХОДА: соединение живёт, пользователь вправе
        сказать следующую реплику позже. Лимита СОЕДИНЕНИЙ нет вовсе, поэтому `4429` не вводится.

        **Ось переспрашивается на КАЖДОМ ходе, а не только в рукопожатии.** Её половина
        `VOICE_INPUT_ENABLED` разрешается через реестр настроек инстанса и меняется из панели НА
        ЛЕТУ (ADR-104 §8), а обещание там дословно: оператор, снявший голосовой ввод, гасит
        голосовой режим НЕМЕДЛЕННО. Проверка только в рукопожатии оставляла бы открытый сокет
        обслуживать ходы неограниченно долго после снятия флага.
        """
        if not voice_mode_available():
            # Причину несёт кадр `error`, close-код — КЛАСС случившегося. Классов ровно пять, и
            # события «ось снята посреди сеанса» среди них нет; ближайший верный по смыслу —
            # ШТАТНОЕ завершение: сеанс закончился не сбоем и не выселением, а тем, что услуги
            # на инстансе больше нет. Шестой код не вводится (ADR-104 §9: «ровно пять»).
            await self._error(
                code="voice_mode_disabled",
                message="voice mode was disabled on this instance",
                scope=SCOPE_SESSION,
            )
            await self._close(CLOSE_NORMAL)
            return False
        try:
            verify_bearer_token(self._ws.headers.get("authorization"))
        except UnauthorizedError:
            # Текущий ход довести не надо — он ещё не начался; сокет закрывается прикладным
            # кодом, чтобы приложение отличило «нас выгнали» от «сеть пропала».
            await self._error(code="unauthorized", message="token expired", scope=SCOPE_SESSION)
            await self._close(CLOSE_UNAUTHORIZED)
            return False
        if not await enforce_chat_limits(
            user_id=self._user_id, device_id=self._device_id, ip=client_ip(self._ws)
        ):
            await self._error(code="rate_limited", message="rate limit exceeded", scope=SCOPE_TURN)
            return False
        return True

    async def _on_delta(self, text: str) -> None:
        """Приращение текста ответа: кадр `delta` + пища для сегментатора озвучки.

        Здесь же — единственная точка, где ход может быть прерван: исключение поднимается ровно
        в момент очередной дельты, а не на следующем сетевом ожидании.
        """
        self._turn_text += text
        # Потурновый признак «ход уже что-то сказал». Отдельно от `_turn_text` потому, что тот
        # принадлежит НОГЕ (закрывается шагом) и на continuation обнуляется, а этот — ходу.
        self._turn_spoke = True
        await self._send({"type": "delta", "turnId": str(self._turn_id), "text": text})
        if self._turn_speech is not None:
            self._turn_speech.feed_delta(text)
        # Порядок строк — инвариант: проверка идёт ПОСЛЕ накопления, поэтому `text` в
        # `TurnInterrupted` непуст по построению. Что решает СУДЬБУ хода — не он, а снимок.
        self._raise_if_interrupted()

    def _turn_started(self, transcript: str | None) -> Callable[[uuid.UUID], Awaitable[None]]:
        """`turnId` = `messageStepId` хода (ADR-104 §4): второго пространства id не заводится.

        Ключ хода выпускает ОРКЕСТРАТОР, а транспорт обязан знать его ДО первого кадра, потому
        что каждый кадр несёт `turnId`. Отсюда же место кадра `transcript`: он уходит здесь —
        после того, как ключ известен, и ДО обращения к модели, то есть с той же семантикой, что
        у одноимённого события SSE. Собственный идентификатор транспорт не выдумывает: выдуманный
        разошёлся бы с `ChatResponse.messageStepId` в кадре `done`.
        """

        async def _started(message_step_id: uuid.UUID) -> None:
            self._turn_id = message_step_id
            if transcript is not None:
                await self._send(
                    {"type": "transcript", "turnId": str(message_step_id), "text": transcript}
                )

        return _started

    async def _run_leg(
        self,
        *,
        call: Callable[[ChatOrchestrator], Awaitable[ChatRunOut]],
        started: float,
        title_source: str | None,
    ) -> None:
        """Одна нога хода: генерация, потоковый синтез, списание синтеза, `done`.

        Порядок — инвариант, а не деталь (ADR-104 §6): весь звук шага → списание синтеза →
        `done`. Отсюда следует, что исхода «списано, но не доставлено» не существует; обратный
        («доставлено, но не списано») возможен и назван прямо — транзакции запроса на сокете нет.
        """
        outcome = "ok"
        speech: VoiceTurnSpeech | None = None
        try:
            async for db in session_scope():
                orchestrator = get_v2_orchestrator(db)
                wallet = WalletService(db, AuditService(db))
                speech = await self._prepare_speech(wallet=wallet)
                self._turn_speech = speech
                out = await call(orchestrator)
                # Весь звук шага → списание синтеза → `done`. Порядок — инвариант, а не деталь:
                # он и делает невозможным исход «списано, но не доставлено».
                if speech is not None:
                    await speech.finish()
                await self._settle_speech(out=out, speech=speech, wallet=wallet)
                if title_source is not None:
                    await self._ensure_title(db, title_source)
                # Коммит ЯВНЫЙ: выход из `session_scope` через `break` закрывает генератор, не
                # доходя до его коммита, поэтому списание синтеза и заголовок иначе потерялись
                # бы вместе с транзакцией — шаг и списание хода коммитит сам оркестратор.
                await db.commit()
                outcome = await self._close_turn(out=out)
                break
        except AppError as exc:
            # Ход не дал результата: он закрыт пометкой `turnFailed` (`_mark_turn_failed`
            # срабатывает на любом исключении пути генерации) либо не начался вовсе. На этом
            # пути единственный производитель такого исхода — отказ поставщика, поэтому метка
            # `upstream_error`, а не `blocked`: `blocked` — это `status="blocked"` в ответе, и
            # он приходит `done`-ом, а не исключением.
            outcome = "upstream_error"
            if speech is not None:
                speech.interrupt()
                await speech.finish()
            # `turnId` привязывает отказ к тому же ходу, чьи `transcript` и `delta` клиент уже
            # получил: он выпущен колбэком `on_turn_start` задолго до отказа, и отправлять
            # безадресный отказ, когда адрес известен, нечем оправдать.
            await self._error(
                code=exc.code, message=exc.message, scope=SCOPE_TURN, turn_id=self._turn_id
            )
        except Exception:
            outcome = "upstream_error"
            logger.exception("voice_turn_failed")
            if speech is not None:
                speech.interrupt()
                await speech.finish()
            await self._error(
                code="internal_error",
                message="internal error",
                scope=SCOPE_TURN,
                turn_id=self._turn_id,
            )
        finally:
            self._turn_speech = None
            if outcome == "ok" and not self._alive:
                # Сокет закрылся до `done`, ход доведён до конца: наблюдение, а не авария. С
                # поломкой поставщика этот исход НЕ сливается — потому и только при `ok`:
                # ушедший клиент не отменяет того, что провайдер отказал.
                outcome = "disconnected"
            voice_mode_turns_total.labels(outcome=outcome).inc()
            log_event(
                logger,
                logging.INFO,
                "voice_mode_turn",
                sessionId=str(self._session_id),
                turnId=str(self._turn_id),
                voiceId=self._voice.id if self._voice is not None else None,
                outcome=outcome,
                answerChars=len(self._turn_text),
                segments=speech.delivered_segments if speech is not None else 0,
                latencyMs=int((time.monotonic() - started) * 1000),
                interruptReason=self._interrupt_reason,
            )

    async def _prepare_speech(self, *, wallet: WalletService) -> VoiceTurnSpeech | None:
        """Балансовый гейт синтеза ДО первого сегмента (ADR-104 §6, §9).

        Нехватка кредитов ход НЕ роняет: ответ приходит текстом, а звука нет — потерять ответ
        хуже, чем не услышать его. Правило `409 insufficient_credits` — принадлежность
        `POST /v1/chat/speech` и на сокет не переносится.
        """
        assert self._voice is not None
        if self._interrupt_reason is not None:
            # Ход уже перебит: «новых `audio.*` для ЭТОГО хода не будет» (ADR-104 §5) — значит и
            # на его следующей ноге тоже. Признак прерывания живёт в СЕАНСЕ, а синтез создаётся
            # на ногу, поэтому наследование нужно выразить явно: иначе нога continuation завела
            # бы синтез с чистым состоянием и заговорила после того, как её остановили.
            return None
        if await wallet.current_balance(self._user_id) < self._settings.tts_credit_cost:
            self._speech_skipped = "insufficient_credits"
            return None
        speech = VoiceTurnSpeech(
            client=get_speech_client(),
            settings=self._settings,
            voice=self._voice,
            sink=_SocketSpeechSink(self),
            # Бюджет, нумерация и счётчик дослушанного — ХОДА, поэтому передаются снаружи и
            # переживают ноги continuation.
            budget=self._turn_budget,
            # Бакет `rl:speech` (`TTS_RATE_LIMIT_PER_MIN`) — тот же, что у `POST /v1/chat/speech`:
            # он защищает наш счёт у поставщика СИНТЕЗА, а не право пользователя говорить.
            # Второго бакета под голосовой режим не заводится (ADR-104 §10).
            limiter=self._speech_limiter,
        )
        speech.start()
        return speech

    async def _speech_limiter(self) -> bool:
        return await enforce_speech_limits(user_id=self._user_id)

    async def _settle_speech(
        self, *, out: ChatRunOut, speech: VoiceTurnSpeech | None, wallet: WalletService
    ) -> None:
        """Одно списание на ОЗВУЧЕННЫЙ ШАГ ассистента, ключ тот же — `tts:{stepId}:{voiceId}`.

        Единица — ШАГ, а не ход, и это следует из ключа: в ходе с клиентскими инструментами
        озвученных шагов два, и у каждого свой ключ. Названное следствие — прослушав ответ
        голосом, пользователь жмёт «прослушать» на том же сообщении и получает
        `creditsCharged: 0`.

        Единственный путь, где ДОСТАВЛЕННЫЙ звук не оплачивается, — отказ LLM-провайдера: там ход
        закрывается пометкой `turnFailed`, до этой точки исполнение не доходит вовсе (исключение
        уходит в ветку отказа выше), и ключ на таком шаге открыл бы бесплатный синтез строки
        «ход не удался» по кнопке. Отказ СИНТЕЗАТОРА исключением НЕ является и оплачивается по
        общему условию: хотя бы один сегмент доставлен → списывается.
        """
        assert self._voice is not None
        if speech is None or speech.delivered_segments == 0:
            if speech is not None and not speech.had_speakable_text:
                # `nothing_to_speak` определён контрактом ДОСЛОВНО как «очищенный текст ответа
                # пуст», и предикат обязан быть именно этим, а не «доставлено ноль сегментов».
                # Молчание по любой другой причине имеет СВОЮ строку таблицы отказов: бакет
                # исчерпан → `error {rate_limited, scope:"speech"}`; потолок хода исчерпан
                # предыдущей ногой → `done.speechTruncated`; прерывание → кадр `interrupted`;
                # отказ синтезатора → `error {upstream_error, scope:"speech"}`. Отдать им общую
                # причину значило бы послать клиенту ложное значение из ЗАКРЫТОГО перечня;
                # третьего значения `reason` при этом не заводится.
                self._speech_skipped = "nothing_to_speak"
            return
        if out.step_id is None:  # pragma: no cover — шаг существует у любой озвученной ноги
            return
        await wallet.consume(
            user_id=self._user_id,
            amount=self._settings.tts_credit_cost,
            idempotency_key=f"tts:{out.step_id}:{self._voice.id}",
            meta={
                "source": "voice_mode_speech",
                "voiceId": self._voice.id,
                "stepId": str(out.step_id),
            },
            session_id=self._session_id,
        )

    async def _ensure_title(self, db: AsyncSession, message: str) -> None:
        """Автозаголовок из ПЕРВОЙ реплики сеанса (chats/03).

        Сессия голосового сеанса создаётся кадром `start`, когда реплики ещё нет, поэтому
        правило чатов применяется здесь — той же функцией `derive_title` и идемпотентно.
        """
        if self._session_id is None:
            return
        repo = ChatRepository(db)
        session = await repo.get_session(self._session_id, self._user_id)
        if session is not None:
            await repo.set_title_if_absent(session, derive_title(message))

    async def _close_turn(self, *, out: ChatRunOut) -> str:
        """Кадры `interrupted` (если было) и `done`; возвращает метку исхода хода."""
        from app.api_gateway.routers.chat import _to_response

        await self._skipped()
        if self._interrupt_reason is not None:
            # Приходит НЕПОСРЕДСТВЕННО перед `done` и несёт только то, что знает канал. Факт
            # прерывания в `ChatResponse` не кладётся: поле появилось бы на всех маршрутах
            # генерации разом. Сколько списано — уже в `ChatResponse.usage`.
            await self._send(
                {
                    "type": "interrupted",
                    "turnId": str(self._turn_id),
                    "reason": self._interrupt_reason,
                    "spokenSegments": self._turn_budget.heard_segments,
                }
            )
        response = _to_response(out)
        await self._send(
            {
                "type": "done",
                "turnId": str(self._turn_id),
                # ADR-104 §13.8: свойство ХОДА на момент этого `done` — «озвучено НЕ ВСЁ,
                # сработал совокупный TTS_MAX_CHARS». Истинно при ОБОИХ способах исчерпания,
                # включая точное, когда `audio.end.truncated` у всех сегментов остался `false`
                # и другого носителя признака нет. Из сегментного `truncated` не выводится и
                # его не выводит. Живёт на КАДРЕ: `ChatResponse` не меняется ни на байт —
                # транспортный факт не появляется на всех маршрутах генерации разом.
                "speechTruncated": self._turn_budget.capped,
                # Немодифицированный `ChatResponse` — тот же объект и та же сериализация, что в
                # кадре `done` SSE. Своей формы ответа у голосового канала нет: она удвоила бы
                # правила `toolCalls`/`quiz`/`mediaJobs`/`documents`/`blockReason`.
                "response": response.model_dump(by_alias=True, mode="json", exclude_none=False),
            }
        )
        if self._interrupt_reason is not None:
            return "interrupted"
        return "blocked" if out.status == "blocked" else "ok"


class _SocketSpeechSink:
    """Кадры звука одного хода. Синтезу о WebSocket знать нечего — он говорит с этим объектом.

    `turnId` читается у сеанса, а не хранится копией: он выпускается оркестратором и становится
    известен уже после создания синтезатора, а вторая копия одной величины разошлась бы с первой.
    """

    def __init__(self, session: _VoiceSession) -> None:
        self._session = session

    async def audio_begin(self, *, segment: int, media_type: str, voice_id: str) -> None:
        await self._session._send(  # noqa: SLF001 — sink принадлежит сеансу и живёт его жизнью
            {
                "type": "audio.begin",
                "turnId": str(self._session._turn_id),  # noqa: SLF001
                "segment": segment,
                "mediaType": media_type,
                "voiceId": voice_id,
            }
        )

    async def audio_chunk(self, data: bytes) -> None:
        await self._session._send_bytes(data)  # noqa: SLF001

    async def audio_end(self, *, segment: int, truncated: bool) -> None:
        await self._session._send(  # noqa: SLF001
            {
                "type": "audio.end",
                "turnId": str(self._session._turn_id),  # noqa: SLF001
                "segment": segment,
                "truncated": truncated,
            }
        )

    async def speech_rate_limited(self) -> None:
        # `scope:"speech"` — ход НЕ затронут, звука дальше не будет. Тот же `code`, что у отказа
        # лимита ХОДА, и различает их именно `scope`: со `scope:"turn"` это «ход не начался», со
        # `scope:"speech"` — «ход идёт, синтез исчерпал свой бакет». Отдельного кода не
        # заводится: таблица `error.code` общая, а `scope` для того и объявлен обязательным.
        await self._session._error(  # noqa: SLF001
            code="rate_limited",
            message="speech rate limit exceeded",
            scope=SCOPE_SPEECH,
            turn_id=self._session._turn_id,  # noqa: SLF001
        )

    async def speech_failed(self) -> None:
        # Ход НЕ затронут: `delta`/`done` идут дальше. Один и тот же `code` значит разное, и
        # различает их именно `scope`: `turn` — ход сломан, `speech` — ход цел, а звука нет.
        await self._session._error(  # noqa: SLF001
            code="upstream_error",
            message="speech provider error",
            scope=SCOPE_SPEECH,
            turn_id=self._session._turn_id,  # noqa: SLF001
        )


class _SessionNotFound(AppError):
    """404-семантика отказа владения на кадре `start`; наружу уходит кадром, а не HTTP."""

    status_code = 404
    code = "session_not_found"


def _parse_json(raw: str) -> dict[str, Any]:
    import json

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("frame must be a JSON object")
    return parsed


async def _handshake(websocket: WebSocket) -> tuple[uuid.UUID, str | None]:
    """Отказы ДО апгрейда — обычным HTTP в едином конверте (ADR-004).

    Порядок проверок: JWT → ось инстанса → ключ поставщика → лимит ходов. Токен берётся ТОЛЬКО
    из заголовка рукопожатия: query-строка попадает в access-логи прокси и наши, то есть
    равносильна записи JWT в лог.
    """
    try:
        user = verify_bearer_token(websocket.headers.get("authorization"))
    except UnauthorizedError as exc:
        raise _Denied(exc) from exc
    set_user_id(str(user.user_id))

    settings = get_settings()
    missing = voice_mode_missing_flags(settings=settings)
    if missing:
        log_event(
            logger,
            logging.WARNING,
            "voice_mode_misconfigured",
            missingFlags=missing,
        )
        raise _Denied(VoiceModeDisabledError("voice mode is not enabled on this instance"))
    if not settings.openai_api_key:
        # Через этот ключ идут ОБЕ половины режима — распознавание и синтез.
        raise _Denied(VoiceModeNotConfiguredError("voice mode is not configured"))

    device_id = websocket.headers.get("x-device-id") or user.device_id
    if not await enforce_chat_limits(
        user_id=user.user_id, device_id=device_id, ip=client_ip(websocket)
    ):
        raise _Denied(RateLimitedError("rate limit exceeded"))

    async for db in session_scope():
        # Ленивая идемпотентная провизия пользователя — тот же шаг, через который проходят все
        # авторизованные `/v1/*`; без него первая же вставка со ссылкой на `users` упала бы.
        # Коммит ЯВНЫЙ по той же причине, что и в ходе: выход через `break` закрывает генератор,
        # не доходя до его коммита.
        await provision_user(db, user.user_id)
        await db.commit()
        break
    return user.user_id, device_id


@router.websocket("/v1/chat/voice")
async def chat_voice(websocket: WebSocket) -> None:
    """Живой голосовой диалог (ADR-104). В OpenAPI не выводится: FastAPI не описывает WebSocket."""
    try:
        user_id, device_id = await _handshake(websocket)
    except _Denied as denied:
        await _deny(websocket, denied.error)
        return

    await websocket.accept()
    voice_mode_connections.inc()
    session = _VoiceSession(websocket, user_id, device_id)
    try:
        await session.run()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("voice_mode_socket_failed")
        await session._close(CLOSE_INTERNAL)  # noqa: SLF001
    else:
        await session._close(CLOSE_NORMAL)  # noqa: SLF001
    finally:
        voice_mode_connections.dec()


async def _deny(websocket: WebSocket, error: AppError) -> None:
    """Ответить отказом рукопожатия обычным HTTP; при отсутствии расширения — закрыть сокет.

    Расширение `websocket.http.response` поддерживает и uvicorn, и тестовый клиент, но оно
    ОПЦИОНАЛЬНО в ASGI: сервер без него обязан получить корректное закрытие, а не исключение
    внутри обработчика.
    """
    try:
        await websocket.send_denial_response(
            _envelope(error.status_code, error.code, error.message)
        )
    except RuntimeError:  # pragma: no cover — ASGI-сервер без расширения denial-response
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=CLOSE_START_REJECTED)
