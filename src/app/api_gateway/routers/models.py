"""Models catalog route: GET /v1/models (chat-orchestrator/02, ADR-034 / ADR-073 / ADR-075).

JWT-protected like GET /v1/tools (CurrentUser) — the list is not secret but the /v1/* auth contour
is uniform. Returns the instance catalog: credits chat models plus fal photo/video when media
generation is configured (ADR-108 §1: ``proxy_configured ∨ fal_configured``). Chat
composition is still ``credits_providers()`` (opt-in ``LLM_PROVIDERS``); a leftover opposite LLM
key does not add that provider. Read-only; per-user rate limit as other reads.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.instance_catalog import build_instance_catalog
from app.config import get_settings
from app.deps import CurrentUser, get_preferences_service
from app.errors import RateLimitedError
from app.preferences.service import PreferencesService
from app.schemas.models import ModelsResponse

router = APIRouter(prefix="/v1/models", tags=["Models"])


@router.get(
    "",
    response_model=ModelsResponse,
    summary="Доступные модели инстанса",
    description=(
        "Модели, которые этот инстанс умеет обслужить. Chat — по включённым credits-провайдерам; "
        "photo/video — если задан ключ fal. У chat ровно одна `default:true`, она первая: "
        "персональный выбор (`PATCH /v1/preferences` `defaultModel`), если задан и всё ещё есть "
        "на витрине, иначе дефолт инстанса. Поле `id` чата уходит в `POST /v1/chat/run` `model`; "
        "fal-id — endpoint генерации, не принимается как модель чата."
    ),
)
async def list_models(
    request: Request,
    current: CurrentUser,
    prefs: Annotated[PreferencesService, Depends(get_preferences_service)],
) -> ModelsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    user_default_model = await prefs.get_default_model(current.user_id)
    return ModelsResponse(
        models=build_instance_catalog(get_settings(), user_default_model=user_default_model)
    )
