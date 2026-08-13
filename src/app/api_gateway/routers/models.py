"""Models catalog route: GET /v1/models (chat-orchestrator/02, ADR-034 / ADR-073).

JWT-protected like GET /v1/tools (CurrentUser) — the list is not secret but the /v1/* auth contour
is uniform. Returns ``Settings.catalog_models()`` as ``{id, displayName, default, provider}``, with
the instance default marked ``default=true`` and emitted FIRST. Dual-credits instances (opt-in
``LLM_PROVIDERS``) include both providers; unset LLM_PROVIDERS keeps the single-provider catalog.
Read-only; per-user rate limit as other reads.
"""

from __future__ import annotations

from typing import Literal, cast

from fastapi import APIRouter, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError
from app.schemas.models import ModelInfo, ModelsResponse

router = APIRouter(prefix="/v1/models", tags=["Models"])


def _build_models() -> list[ModelInfo]:
    """Ordered model list: default first, then allowlists (ADR-034 §2 / ADR-073)."""
    models: list[ModelInfo] = []
    for model_id, display_name, is_default, provider in get_settings().catalog_models():
        models.append(
            ModelInfo(
                id=model_id,
                displayName=display_name,
                default=is_default,
                provider=cast(Literal["openai", "anthropic"], provider),
            )
        )
    return models


@router.get(
    "",
    response_model=ModelsResponse,
    summary="Доступные модели инстанса",
    description=(
        "Возвращает модели инстанса для селектора. Ровно одна помечена `default:true` "
        "(дефолт активного провайдера) и идёт первой. Аддитивное поле `provider` "
        "(`openai`/`anthropic`). На инстансе без `LLM_PROVIDERS` список — как раньше, только "
        "активный провайдер. `id` передаётся обратно в `POST /v1/chat/run` поле `model`."
    ),
)
async def list_models(request: Request, current: CurrentUser) -> ModelsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    return ModelsResponse(models=_build_models())
