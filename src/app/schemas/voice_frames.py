"""Кадры WebSocket-ручки `/v1/chat/voice` (ADR-104 §4, chat-orchestrator/02 §`/v1/chat/voice`).

Управление — **текстовые JSON**-кадры `{"type": …}`; звук — **бинарные** кадры между открывающим
и закрывающим управляющим кадром. WebSocket различает их на уровне протокола, поэтому ни
префикса, ни поля-дискриминатора не требуется.

Схемы здесь описывают только направление **клиент → сервер**: их надо ВАЛИДИРОВАТЬ, и валидация
обязана быть той же строгой (`extra='forbid'`), что у HTTP-тел, — иначе опечатка в имени поля
молча превратилась бы в умолчание. Кадры сервер → клиент собираются обработчиком прямо: у них нет
внешнего ввода, а `done` несёт `ChatResponse` целиком и своей схемы не имеет по построению
(ADR-104 §2).

Эти схемы в OpenAPI не попадают: WebSocket-маршруты FastAPI в схему не выводит. Клиентский вид
контракта живёт в `docs/API-REFERENCE.md §31`.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import Field

from app.schemas.chat import DEFAULT_GENERATION_MODE, AttachmentMediaType, GenerationMode
from app.schemas.common import StrictModel

# Значения `type` кадров клиент → сервер. Перечень ЗАКРЫТ контрактом; неизвестный тип — отказ,
# а не молчаливое игнорирование (тот же принцип явного намерения, что у ADR-034 §3/ADR-097 §7).
FRAME_START = "start"
FRAME_UTTERANCE_BEGIN = "utterance.begin"
FRAME_UTTERANCE_END = "utterance.end"
FRAME_TEXT = "text"
FRAME_INTERRUPT = "interrupt"
FRAME_TOOL_RESULT = "tool.result"
FRAME_PING = "ping"
# У `ping` схемы нет намеренно: полей у него тоже нет, а объявленная и никем не читаемая
# модель — мёртвая декларация. Keepalive только сбрасывает отсчёт idle-таймаута.


class VoiceStartFrame(StrictModel):
    """Первый кадр сеанса. Шесть session-fixed полей — весь действующий набор (ADR-104 §3).

    Принимаются ТОЛЬКО при создании сессии и на resume игнорируются — дословно правило
    ADR-034 §3/ADR-097 §4. `context` здесь НЕ принимается: он per-message и приходит на кадре
    хода; вторая форма того же поля с другой областью действия не заводится.
    """

    type: Literal["start"]
    sessionId: uuid.UUID | None = None
    mode: Literal["credits", "byok"] = "credits"
    assistantMode: Literal["chat", "code"] | None = None
    model: str | None = None
    characterId: str | None = None
    workspaceProjectId: uuid.UUID | None = None
    projectId: str | None = None


class VoiceUtteranceBeginFrame(StrictModel):
    """Пользователь заговорил; далее идут бинарные кадры ОДНОГО файла."""

    type: Literal["utterance.begin"]
    # Набор ВХОДА — тот же, что у голосового вложения (ADR-095 §2). Набор ВЫХОДА
    # (`audio.begin.mediaType`) другой, и один в другой не переносится.
    mediaType: AttachmentMediaType


class VoiceUtteranceEndFrame(StrictModel):
    """Устройство решило, что реплика окончена → распознавание → ход. Серверного VAD нет."""

    type: Literal["utterance.end"]
    generationMode: GenerationMode = DEFAULT_GENERATION_MODE
    context: dict[str, Any] | None = None


class VoiceTextFrame(StrictModel):
    """Набранное сообщение внутри голосового сеанса — тот же ход, просто без распознавания."""

    type: Literal["text"]
    text: str = Field(min_length=1)
    generationMode: GenerationMode = DEFAULT_GENERATION_MODE
    context: dict[str, Any] | None = None


class VoiceInterruptFrame(StrictModel):
    """Пользователь перебил ассистента (ADR-104 §5). Инициатор — только устройство."""

    type: Literal["interrupt"]
    turnId: uuid.UUID
    reason: Literal["barge_in", "user_stop"]


class VoiceToolResultItem(StrictModel):
    """Один результат клиентского инструмента.

    Форма ИДЕНТИЧНА элементу `results[]` у `POST /v1/chat/tool-result` (ADR-025 §B2). Тела
    целиком не идентичны и не могут быть: HTTP-тело несёт ещё `userId` и `sessionId`, а кадр
    вместо них несёт `turnId` — сеанс и пользователь на сокете уже установлены рукопожатием и
    кадром `start`.
    """

    toolCallId: uuid.UUID
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class VoiceToolResultFrame(StrictModel):
    """Результаты клиентских инструментов. Барьер хода не меняется (ADR-025 §B3)."""

    type: Literal["tool.result"]
    turnId: uuid.UUID
    results: list[VoiceToolResultItem] = Field(min_length=1)
