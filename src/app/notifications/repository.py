"""Persistence for ``device_push_tokens`` (notifications/03-architecture.md)."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DevicePushToken


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class DevicePushTokensRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        device_id: str,
        push_token: str,
        platform: str = "ios",
    ) -> None:
        """Insert or refresh the token for ``(user_id, device_id)``."""
        await self._session.execute(
            text(
                """
                INSERT INTO device_push_tokens
                    (user_id, device_id, push_token, platform, updated_at)
                VALUES (:user_id, :device_id, :push_token, :platform, now())
                ON CONFLICT (user_id, device_id) DO UPDATE
                SET push_token = EXCLUDED.push_token,
                    platform = EXCLUDED.platform,
                    updated_at = now()
                """
            ),
            {
                "user_id": user_id,
                "device_id": device_id,
                "push_token": push_token,
                "platform": platform,
            },
        )

    async def delete(self, *, user_id: uuid.UUID, device_id: str) -> bool:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(DevicePushToken).where(
                    DevicePushToken.user_id == user_id,
                    DevicePushToken.device_id == device_id,
                )
            ),
        )
        return (result.rowcount or 0) > 0

    async def delete_by_push_token(self, *, push_token: str) -> None:
        """Drop every row with this APNs token (410 Unregistered cleanup)."""
        await self._session.execute(
            delete(DevicePushToken).where(DevicePushToken.push_token == push_token)
        )

    async def list_for_user(self, *, user_id: uuid.UUID) -> list[DevicePushToken]:
        rows = await self._session.scalars(
            select(DevicePushToken).where(DevicePushToken.user_id == user_id)
        )
        return list(rows.all())
