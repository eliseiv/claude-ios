"""Unified instance catalog for GET /v1/models (ADR-075).

Chat rows come from the instance-config layer (credits_providers + allowlists + the operator
showcase, ADR-034/073/099).
Fal rows are appended only when ``FAL_API_KEY`` is non-empty (ADR-060 gate). Leftover opposite
LLM keys do not add a chat provider — that still requires ``LLM_PROVIDERS``.
"""

from __future__ import annotations

from typing import Literal, cast

from app import instance_config
from app.config import Settings
from app.media_generation.catalog import fal_catalog_entries
from app.schemas.models import ModelInfo


def build_instance_catalog(settings: Settings) -> list[ModelInfo]:
    """Витрина чата (дефолт первым), затем fal-эндпоинты, если у инстанса есть ключ fal.

    Состав chat-строк берётся из слоя оверлеев: оператор может снять модель с витрины из
    панели. Это гейт КАТАЛОГА, а не бэкенда — уже созданная сессия на снятой модели продолжает
    работать и тарифицироваться, поэтому её строка тарифа остаётся в admin-контракте.
    """
    models: list[ModelInfo] = []
    for model_id, display_name, is_default, provider in instance_config.chat_catalog_rows(
        settings=settings
    ):
        models.append(
            ModelInfo(
                id=model_id,
                displayName=display_name,
                name=display_name,
                default=is_default,
                provider=cast(Literal["openai", "anthropic"], provider),
                modality="chat",
                variant=None,
                family=None,
                # Тот же единственный резолвер, что питает гейт баланса и списание.
                creditCost=instance_config.chat_turn_credit_cost(model_id, settings=settings),
            )
        )
    if not settings.fal_api_key.strip():
        return models
    for entry in fal_catalog_entries():
        models.append(
            ModelInfo(
                id=entry.id,
                displayName=entry.name,
                name=entry.name,
                default=entry.default,
                provider="fal",
                modality=cast(Literal["photo", "video"], entry.modality),
                variant=entry.variant,
                family=entry.family,
            )
        )
    return models
