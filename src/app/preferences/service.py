"""Preferences service: lazy get + upsert patch over user_preferences (preferences/03).

GET without a row returns in-memory defaults (no DB write). PATCH upserts and only updates
the provided fields (COALESCE semantics at the use-case layer). assistant_mode is the
assistant type (chat|code) and is orthogonal to billing_mode (ADR-012).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import instance_config
from app.models import UserPreferences

DEFAULT_ASSISTANT_MODE = "chat"


class _Unset:
    """Sentinel type: «поле в PATCH не прислали» — в отличие от «прислали null»."""


UNSET = _Unset()
"""ADR-100: `defaultVoiceId` — первое поле настроек, у которого `null` ЗНАЧИМ.

Для остальных полей `None` означает «не прислали» (COALESCE-семантика частичного обновления), но
контракт `defaultVoiceId` требует, чтобы `null` СБРАСЫВАЛ выбор к голосу инстанса. Отличить два
случая по самому значению нельзя, поэтому «не прислали» несёт отдельный сентинел, а не `None`.
"""


@dataclass(frozen=True)
class PreferencesView:
    default_assistant_mode: str
    notifications_enabled: bool
    code_defaults: dict[str, Any]
    memory_enabled: bool
    memory_search_scope: Literal["global", "workspace"]
    default_voice_id: str | None


def _defaults() -> PreferencesView:
    return PreferencesView(
        default_assistant_mode=DEFAULT_ASSISTANT_MODE,
        notifications_enabled=False,
        code_defaults={},
        # ADR-091: значение ПРОИЗВОДНОЕ от инстансного флага, персональной настройки больше нет.
        # Колонка в БД сохранена (expand-only), но не читается — иначе у пользователей со старым
        # `false` память осталась бы выключенной навсегда.
        memory_enabled=instance_config.memory_enabled(),
        memory_search_scope="global",
        # ADR-100: NULL = голос инстанса (TTS_DEFAULT_VOICE_ID), а не «озвучки нет».
        default_voice_id=None,
    )


def _to_view(row: UserPreferences) -> PreferencesView:
    return PreferencesView(
        default_assistant_mode=row.default_assistant_mode,
        notifications_enabled=row.notifications_enabled,
        code_defaults=dict(row.code_defaults),
        # Не row.memory_enabled: см. _defaults() — значение производное от инстансного флага.
        memory_enabled=instance_config.memory_enabled(),
        memory_search_scope=cast(Literal["global", "workspace"], row.memory_search_scope),
        # ADR-100 §8: сохранённое значение отдаётся ДАЖЕ при VOICE_OUTPUT_ENABLED=false — как
        # сохраняется `characterId` в списке чатов при снятом флаге персонажей: выключатель гасит
        # поведение, но не стирает выбор пользователя.
        default_voice_id=row.default_voice_id,
    )


class PreferencesService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _load(self, user_id: uuid.UUID) -> UserPreferences | None:
        row: UserPreferences | None = await self._session.scalar(
            select(UserPreferences).where(UserPreferences.user_id == user_id)
        )
        return row

    async def get(self, user_id: uuid.UUID) -> PreferencesView:
        row = await self._load(user_id)
        if row is None:
            return _defaults()
        return _to_view(row)

    async def patch(
        self,
        user_id: uuid.UUID,
        *,
        default_assistant_mode: str | None = None,
        notifications_enabled: bool | None = None,
        code_defaults: dict[str, Any] | None = None,
        memory_search_scope: str | None = None,
        default_voice_id: str | None | _Unset = UNSET,
    ) -> PreferencesView:
        """Upsert preferences, updating only the provided fields.

        ``default_voice_id`` is the one parameter whose ``None`` is a VALUE (reset to the instance
        voice), so «not provided» is carried by ``UNSET`` instead. Validation of the slug against
        the registry happens at the router (the 422 codes are contract-level), not here.
        """
        row = await self._load(user_id)
        if row is None:
            defaults = _defaults()
            row = UserPreferences(
                user_id=user_id,
                default_assistant_mode=(
                    default_assistant_mode
                    if default_assistant_mode is not None
                    else defaults.default_assistant_mode
                ),
                notifications_enabled=(
                    notifications_enabled
                    if notifications_enabled is not None
                    else defaults.notifications_enabled
                ),
                code_defaults=(
                    code_defaults if code_defaults is not None else defaults.code_defaults
                ),
                memory_search_scope=(
                    memory_search_scope
                    if memory_search_scope is not None
                    else defaults.memory_search_scope
                ),
                default_voice_id=(
                    default_voice_id
                    if not isinstance(default_voice_id, _Unset)
                    else defaults.default_voice_id
                ),
            )
            self._session.add(row)
        else:
            if default_assistant_mode is not None:
                row.default_assistant_mode = default_assistant_mode
            if notifications_enabled is not None:
                row.notifications_enabled = notifications_enabled
            if code_defaults is not None:
                row.code_defaults = code_defaults
            if memory_search_scope is not None:
                row.memory_search_scope = memory_search_scope
            if not isinstance(default_voice_id, _Unset):
                row.default_voice_id = default_voice_id
        await self._session.flush()
        await self._session.commit()
        return _to_view(row)

    async def get_default_assistant_mode(self, user_id: uuid.UUID) -> str:
        """Orchestrator fallback: preferences default, or 'chat' if no row (ADR-012)."""
        row = await self._load(user_id)
        if row is None:
            return DEFAULT_ASSISTANT_MODE
        return row.default_assistant_mode

    async def get_default_voice_id(self, user_id: uuid.UUID) -> str | None:
        """Stored speech voice for the user, or ``None`` (= instance voice) — ADR-100 §5.

        Read on EVERY synthesis, not at session creation. The contrast with the neighbouring
        ``get_default_assistant_mode`` is deliberate: the assistant mode is read once because it
        is FIXED on the session, while the voice is not — a setting that only affected chats the
        user has yet to start would be indistinguishable from a broken setting for someone with
        fifty chats. One read per synthesis needs no cache: the endpoint already goes out to an
        external provider.
        """
        row = await self._load(user_id)
        if row is None:
            return None
        return row.default_voice_id
