"""Models-catalog schema for GET /v1/models (chat-orchestrator/02, ADR-034 / ADR-073).

Provider-agnostic response contract: ``{id, displayName, default}`` plus additive ``provider``.
Exactly one item has ``default=true`` (the instance default model), which is emitted first. An
empty allowlist yields a single default item (displayName = id) — backward compatibility
(ADR-034 §1–2). ``provider`` is additive JSON (old iOS decoders ignore unknown keys).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel


class ModelInfo(StrictModel):
    id: str = Field(
        description=(
            "Провайдерный id модели. Передаётся обратно в `POST /v1/chat/run` поле `model`."
        )
    )
    displayName: str = Field(
        description="Человекочитаемое имя модели для UI (из allowlist `id→displayName`)."
    )
    default: bool = Field(
        description=(
            "Дефолтная модель инстанса. Ровно у одного элемента `true`; этот элемент идёт первым."
        )
    )
    provider: Literal["openai", "anthropic"] = Field(
        description=(
            "Сервисный провайдер этой модели. Аддитивное поле: старые клиенты его игнорируют. "
            "На однопровайдерном инстансе совпадает с активным провайдером инстанса."
        )
    )


class ModelsResponse(StrictModel):
    models: list[ModelInfo] = Field(
        description="Доступные модели инстанса (дефолт первым; dual-credits — оба провайдера)."
    )
