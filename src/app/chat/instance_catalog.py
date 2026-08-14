"""Unified instance catalog for GET /v1/models (ADR-075).

Chat rows come from ``Settings.catalog_models()`` (credits_providers + allowlists, ADR-034/073).
Fal rows are appended only when ``FAL_API_KEY`` is non-empty (ADR-060 gate). Leftover opposite
LLM keys do not add a chat provider — that still requires ``LLM_PROVIDERS``.
"""

from __future__ import annotations

from typing import Literal, cast

from app.config import Settings
from app.media_generation.catalog import fal_catalog_entries
from app.schemas.models import ModelInfo


def build_instance_catalog(settings: Settings) -> list[ModelInfo]:
    """Chat allowlists first (default first), then fal endpoints if the instance has a fal key."""
    models: list[ModelInfo] = []
    for model_id, display_name, is_default, provider in settings.catalog_models():
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
