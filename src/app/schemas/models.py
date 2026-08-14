"""Models-catalog schema for GET /v1/models (chat-orchestrator/02, ADR-034 / ADR-073 / ADR-075).

Provider-agnostic response contract: ``{id, displayName, default}`` plus additive ``provider``,
``name``, ``modality``, ``variant``, ``family``. Exactly one *chat* item has ``default=true``
(the instance default model) and is emitted first. When fal is configured, photo rows may carry
their own per-modality default. Chat rows are the built-in product catalog plus env extras
(ADR-076). New fields are additive JSON (old iOS decoders ignore unknown keys).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel

Provider = Literal["openai", "anthropic", "fal"]
Modality = Literal["chat", "photo", "video"]


class ModelInfo(StrictModel):
    id: str = Field(
        description=(
            "Id модели. Для chat — в `POST /v1/chat/run` поле `model`. "
            "Для photo/video — endpoint fal (параметры режима — `GET /v1/media/models`)."
        )
    )
    displayName: str = Field(
        description="Человекочитаемое имя модели для UI (из allowlist `id→displayName`)."
    )
    name: str = Field(
        description="То же, что `displayName`. Дубль для клиентов, которые читают `name`."
    )
    default: bool = Field(
        description=(
            "Дефолт в своей модальности. У chat ровно один `true` (дефолт инстанса), он первый "
            "в списке. У photo — дефолт генерации, если fal включён. У video — всегда `false`."
        )
    )
    provider: Provider = Field(
        description="Провайдер этой модели: `openai`, `anthropic` или `fal`."
    )
    modality: Modality = Field(
        description="Назначение: `chat`, `photo` или `video`. Селектор чата берёт только `chat`."
    )
    variant: str | None = Field(
        default=None,
        description="Режим fal (например `Text to Image`). У chat всегда `null`.",
    )
    family: str | None = Field(
        default=None,
        description="Семейство fal для группировки вариантов. У chat всегда `null`.",
    )


class ModelsResponse(StrictModel):
    models: list[ModelInfo] = Field(
        description=(
            "Модели, которые инстанс умеет обслужить: chat-провайдеры + fal, если задан ключ."
        )
    )
