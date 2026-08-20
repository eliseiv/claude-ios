"""Presets catalog route: GET /v1/presets (chat-orchestrator/02, ADR-035, localized ADR-049).

JWT-protected like GET /v1/tools and GET /v1/models (CurrentUser) — the list is not secret but
the /v1/* auth contour is uniform. Returns the prompt-preset registry sourced from
``app.chat.presets`` (single source of truth). Read-only, no state/DB/ledger; per-user rate limit
as other reads. Provider-agnostic — identical on every instance for a given locale (ADR-033).

Locale resolution (ADR-049 §3), first match wins: explicit ``?locale=`` (invalid → 422) →
``Accept-Language`` (lenient, silent fallback) → per-instance ``PRESETS_DEFAULT_LOCALE`` (graceful)
→ ``en`` (canon). Resolution is a pure helper (``resolve_presets_locale``) for testability.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Query, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.presets import (
    DEFAULT_PRESET_LOCALE,
    canonicalize_preset_locale,
    preset_catalog,
)
from app.config import get_settings
from app.deps import CurrentUser
from app.errors import RateLimitedError, ValidationFailedError
from app.schemas.presets import PresetsResponse

router = APIRouter(prefix="/v1/presets", tags=["Presets"])


def resolve_presets_locale(
    query_locale: str | None,
    accept_language: str | None,
    default_locale: str,
) -> str:
    """Resolve the catalog locale by priority (ADR-049 §3). Pure — no I/O, no settings access.

    Order (first match wins):
      1. ``query_locale`` (explicit ``?locale=``) — canonicalized via
         ``canonicalize_preset_locale`` (``zh-Hans`` / ``zh_Hans`` / ``zh-CN`` → ``zh-Hans``,
         ``ru-RU`` → ``ru``). Present-but-unsupported → ``ValidationFailedError`` (422):
         an explicit client intent must not be silently substituted (symmetric to unsupported_model,
         ADR-034 §3).
      2. ``accept_language`` — first supported tag (full BCP-47, then prefix/alias); ``q``-weights
         are dropped. No supported subtag / blank / unparseable → silent fallback (no error), the
         header is not a strict client intent.
      3. ``default_locale`` — the per-instance default (already graceful, ADR-049 §4), if supported.
      4. ``DEFAULT_PRESET_LOCALE`` (``"en"``) — final canon fallback.
    """
    if query_locale is not None:
        resolved = canonicalize_preset_locale(query_locale)
        if resolved is not None:
            return resolved
        raise ValidationFailedError(f"locale '{query_locale}' is not supported")

    header_locale = _first_supported_language(accept_language)
    if header_locale is not None:
        return header_locale

    default_resolved = canonicalize_preset_locale(default_locale)
    if default_resolved is not None:
        return default_resolved
    return DEFAULT_PRESET_LOCALE


def _first_supported_language(accept_language: str | None) -> str | None:
    """First supported locale from an ``Accept-Language`` header, else ``None`` (lenient).

    Splits on ``,``, drops the ``;q=...`` weight, and canonicalizes each tag
    (``zh-Hans-CN`` → ``zh-Hans``, ``ru-RU`` → ``ru``). A blank/unparseable header yields
    ``None`` (caller falls through). Standard content-negotiation leniency: never raises.
    """
    if not accept_language:
        return None
    for part in accept_language.split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        resolved = canonicalize_preset_locale(tag)
        if resolved is not None:
            return resolved
    return None


@router.get(
    "",
    response_model=PresetsResponse,
    summary="Каталог пресетов промтов",
    description=(
        "Возвращает список пресетов для чипов на главном экране чата и карточек на экране "
        "агентов: `id` (стабильный slug), `title`, `icon` (имя SF Symbol), `prompt` (текст для "
        "подстановки в композер), `description` (короткая подпись карточки), `category` "
        "(`work` / `life` / `entertainment`) и `subcategory` (карточка агента). Порядок "
        "элементов = порядок объявления. Тексты `title`, `prompt` и `description` отдаются "
        "на выбранном языке: приоритет у параметра `locale`, затем заголовок "
        "`Accept-Language`, затем язык по умолчанию для инстанса; при отсутствии перевода "
        "используется английский. Поле `locale` в ответе сообщает фактически применённый язык. "
        "Read-only, без состояния."
    ),
)
async def list_presets(
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
) -> PresetsResponse:
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    resolved = resolve_presets_locale(
        query_locale=locale,
        accept_language=accept_language,
        default_locale=get_settings().resolved_presets_default_locale(),
    )
    return PresetsResponse.model_validate({"locale": resolved, "presets": preset_catalog(resolved)})
