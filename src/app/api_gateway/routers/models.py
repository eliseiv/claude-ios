"""Models catalog route: GET /v1/models (chat-orchestrator/02, ADR-034 / ADR-073 / ADR-075).

JWT-protected like GET /v1/tools (CurrentUser) — the list is not secret but the /v1/* auth contour
is uniform. Returns the instance catalog: credits chat models plus fal photo/video when
``FAL_API_KEY`` is set. Chat composition is still ``credits_providers()`` (opt-in
``LLM_PROVIDERS``); a leftover opposite LLM key does not add that provider. Read-only;
per-user rate limit as other reads.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.instance_catalog import build_instance_catalog
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError
from app.schemas.models import ModelsResponse

router = APIRouter(prefix="/v1/models", tags=["Models"])


@router.get(
    "",
    response_model=ModelsResponse,
    summary="Доступные модели инстанса",
    description=(
        "Модели, которые этот инстанс умеет обслужить. Chat — по включённым credits-провайдерам; "
        "photo/video — если задан ключ fal. У chat ровно одна `default:true` (дефолт инстанса), "
        "она первая. Поле `id` чата уходит в `POST /v1/chat/run` `model`; fal-id — endpoint "
        "генерации, не принимается как модель чата."
    ),
)
async def list_models(request: Request, current: CurrentUser) -> ModelsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    return ModelsResponse(models=build_instance_catalog(get_settings()))
