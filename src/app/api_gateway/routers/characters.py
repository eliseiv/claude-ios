"""Characters catalog route: GET /v1/characters (chat-orchestrator/02, ADR-097).

JWT-protected like GET /v1/tools / GET /v1/models / GET /v1/presets: the list is not secret,
but the /v1/* auth contour is uniform. Read-only — no session, no ledger, no audit; per-user
rate limit as the other reads. Source is the static registry ``app.chat.characters``.

Locale resolution REUSES the presets helper (``resolve_presets_locale``) and the same
per-instance variable: an instance has one catalog language, and a second env for the same
fact would drift from the first.

The instance flag gates the catalog and the behaviour on one axis (ADR-097 §7): when it is
off the endpoint still answers 200 with an empty list — a 404 would be indistinguishable from
"old backend / wrong path", and the app could not tell whether to hide the section for good.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.api_gateway.routers.presets import resolve_presets_locale
from app.chat.characters import character_catalog
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError
from app.schemas.characters import CharactersResponse

router = APIRouter(prefix="/v1/characters", tags=["Characters"])


@router.get(
    "",
    response_model=CharactersResponse,
    summary="Каталог персонажей",
    description=(
        "Возвращает персонажей для экрана выбора собеседника: `id` (стабильный slug), `name`, "
        "`tagline` (короткая подпись) и `icon` (имя SF Symbol). Выбранный `id` передаётся в "
        "`characterId` при создании чата — дальше ассистент отвечает голосом этого персонажа. "
        "Поле `enabled` сообщает, включён ли выбор персонажа на инстансе: при `false` список "
        "пуст и `characterId` в `/v1/chat/run` отклоняется (422). Тексты `name` и `tagline` "
        "отдаются на выбранном языке: приоритет у параметра `locale`, затем заголовок "
        "`Accept-Language`, затем язык по умолчанию для инстанса; при отсутствии перевода "
        "используется английский. Поле `locale` в ответе сообщает фактически применённый язык. "
        "Read-only, без состояния."
    ),
)
async def list_characters(
    request: Request,
    current: CurrentUser,
    locale: str | None = Query(
        default=None,
        description=(
            "Желаемый язык каталога (например `en`, `ru` или `zh-Hans`). Если не указан — язык "
            "определяется по заголовку `Accept-Language`, иначе используется язык по умолчанию "
            "для инстанса. Недопустимое значение возвращает ошибку 422."
        ),
        examples=["ru", "zh-Hans"],
    ),
    accept_language: str | None = Header(default=None),
) -> CharactersResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    settings = get_settings()
    resolved = resolve_presets_locale(
        query_locale=locale,
        accept_language=accept_language,
        default_locale=settings.resolved_presets_default_locale(),
    )
    # The locale is resolved even when the feature is off: `?locale=` outside the set stays a
    # 422 on every instance, and the client gets the same shape either way.
    enabled = settings.characters_enabled
    return CharactersResponse.model_validate(
        {
            "enabled": enabled,
            "locale": resolved,
            "characters": character_catalog(resolved) if enabled else [],
        }
    )
