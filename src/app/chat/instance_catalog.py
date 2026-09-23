"""Unified instance catalog for GET /v1/models (ADR-075).

Chat rows come from the instance-config layer (credits_providers + allowlists + the operator
showcase, ADR-034/073/099).
Fal rows are appended only when media generation is configured on the instance —
``Settings.media_generation_configured()`` (ADR-108 §1: ``proxy_configured ∨ fal_configured``).
Leftover opposite LLM keys do not add a chat provider — that still requires ``LLM_PROVIDERS``.
"""

from __future__ import annotations

from typing import Literal, cast

from app import instance_config
from app.config import Settings
from app.media_generation.catalog import fal_catalog_entries
from app.schemas.models import ModelInfo


def build_instance_catalog(
    settings: Settings, *, user_default_model: str | None = None
) -> list[ModelInfo]:
    """Витрина чата (дефолт первым), затем fal-эндпоинты, если у инстанса есть ключ fal.

    Состав chat-строк берётся из слоя оверлеев: оператор может снять модель с витрины из
    панели. Это гейт КАТАЛОГА, а не бэкенда — уже созданная сессия на снятой модели продолжает
    работать и тарифицироваться, поэтому её строка тарифа остаётся в admin-контракте.

    ``user_default_model`` (preferences ``defaultModel``, персональный) переставляет
    ``default:true`` с дефолта инстанса на выбранную пользователем модель — только среди chat-строк
    (fal photo/video не затрагиваются, у них своя per-modality default-логика). Если сохранённое
    значение не входит в ТЕКУЩУЮ витрину (оператор снял модель после того, как пользователь её
    выбрал) — молча деградирует к дефолту инстанса, как и `defaultVoiceId` при выключенной озвучке
    (ADR-100 §8): сохранённое значение не стирается, просто временно не действует.
    """
    models: list[ModelInfo] = []
    chat_ids: list[str] = []
    for model_id, display_name, is_default, provider in instance_config.chat_catalog_rows(
        settings=settings
    ):
        chat_ids.append(model_id)
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
    if user_default_model is not None and user_default_model in chat_ids:
        for m in models:
            if m.modality == "chat":
                m.default = m.id == user_default_model
        # Chat-раздел переупорядочивается так, чтобы default:true осталась ПЕРВОЙ (контракт
        # "дефолт первым" не завязан на ТО, чей это дефолт — инстансный или персональный).
        chat_models = [m for m in models if m.modality == "chat"]
        rest = [m for m in models if m.modality != "chat"]
        chat_models.sort(key=lambda m: m.id != user_default_model)
        models = chat_models + rest
    if not settings.media_generation_configured():
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
