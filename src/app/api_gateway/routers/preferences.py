"""Preferences routes: GET/PATCH /v1/preferences (preferences/02-api-contracts.md)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.chat.voices import is_selectable_voice
from app.config import get_settings
from app.deps import CurrentUser, get_preferences_service
from app.errors import (
    RateLimitedError,
    UnknownVoiceError,
    VoiceOutputDisabledError,
)
from app.preferences.service import UNSET, PreferencesService, PreferencesView
from app.schemas.preferences import PreferencesPatchRequest, PreferencesResponse

router = APIRouter(prefix="/v1/preferences", tags=["Preferences"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _to_response(view: PreferencesView) -> PreferencesResponse:
    return PreferencesResponse(
        defaultAssistantMode=view.default_assistant_mode,
        notificationsEnabled=view.notifications_enabled,
        codeDefaults=view.code_defaults,
        memoryEnabled=view.memory_enabled,
        memorySearchScope=view.memory_search_scope,
        defaultVoiceId=view.default_voice_id,
    )


def _validate_default_voice_id(value: str | None) -> None:
    """Gate a non-empty ``defaultVoiceId`` on the instance flag and the registry (ADR-100 §8).

    Order matters and mirrors the character gate (``characters_disabled`` before
    ``unknown_character``, ADR-097 §7): on an instance where the feature is off, EVERY value is
    equally impossible, and reporting `unknown_voice` there would send the client hunting for a
    valid id that does not exist on this instance. ``null`` is a legal value (reset to the
    instance voice) and passes both checks.

    Refusal rather than silent ignoring: the chosen voice is visible to the user in settings, and
    a silently dropped value would leave a screen that shows one voice and sounds like another —
    the kind of failure that cannot be diagnosed from outside.
    """
    if value is None:
        return
    if not get_settings().voice_output_enabled:
        raise VoiceOutputDisabledError("speech output is not enabled on this instance")
    if not is_selectable_voice(value):
        raise UnknownVoiceError(f"voice '{value}' is not available on this instance")


@router.get(
    "",
    response_model=PreferencesResponse,
    summary="Получить настройки",
    description=(
        "Возвращает пользовательские настройки: дефолтный тип ассистента (chat|code), "
        "toggle уведомлений, голос озвучки по умолчанию и дефолты Code-контекста. Если "
        "настройки ещё не заданы — возвращаются значения по умолчанию (chat / true / null / {})."
    ),
)
async def get_preferences(
    request: Request,
    current: CurrentUser,
    prefs: Annotated[PreferencesService, Depends(get_preferences_service)],
) -> PreferencesResponse:
    await _rate_limit(current.user_id)
    view = await prefs.get(current.user_id)
    return _to_response(view)


@router.patch(
    "",
    response_model=PreferencesResponse,
    summary="Обновить настройки",
    description=(
        "Частично обновляет настройки (любое подмножество полей). Создаёт строку при "
        "отсутствии (upsert). Возвращает полный актуальный объект настроек."
    ),
)
async def patch_preferences(
    body: PreferencesPatchRequest,
    request: Request,
    current: CurrentUser,
    prefs: Annotated[PreferencesService, Depends(get_preferences_service)],
) -> PreferencesResponse:
    await _rate_limit(current.user_id)
    # ADR-100: «поле прислали» определяется присутствием ключа, а не non-None — явный `null`
    # СБРАСЫВАЕТ голос к инстансному, и отличить его от «не прислали» по значению нельзя.
    voice_sent = "defaultVoiceId" in body.model_fields_set
    if voice_sent:
        _validate_default_voice_id(body.defaultVoiceId)
    view = await prefs.patch(
        current.user_id,
        default_assistant_mode=body.defaultAssistantMode,
        notifications_enabled=body.notificationsEnabled,
        code_defaults=body.codeDefaults,
        memory_search_scope=body.memorySearchScope,
        default_voice_id=body.defaultVoiceId if voice_sent else UNSET,
    )
    return _to_response(view)
