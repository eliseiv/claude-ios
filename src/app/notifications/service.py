"""Device-token registration use-cases (notifications/03-architecture.md)."""

from __future__ import annotations

import uuid

from app.errors import ValidationFailedError
from app.notifications.repository import DevicePushTokensRepository


class NotificationsService:
    def __init__(self, repo: DevicePushTokensRepository) -> None:
        self._repo = repo

    @staticmethod
    def resolve_device_id(
        *,
        body_device_id: str | None,
        jwt_device_id: str | None,
        header_device_id: str | None,
    ) -> str:
        """Body → JWT claim → ``X-Device-Id``; missing everywhere → 422."""
        for candidate in (body_device_id, jwt_device_id, header_device_id):
            if candidate is not None and candidate.strip():
                return candidate.strip()
        raise ValidationFailedError(
            "deviceId is required (body, JWT device_id claim, or X-Device-Id header)"
        )

    async def register(
        self,
        *,
        user_id: uuid.UUID,
        device_id: str,
        push_token: str,
        platform: str = "ios",
    ) -> None:
        token = push_token.strip()
        if not token:
            raise ValidationFailedError("pushToken must be a non-empty string")
        await self._repo.upsert(
            user_id=user_id,
            device_id=device_id,
            push_token=token,
            platform=platform,
        )

    async def delete(self, *, user_id: uuid.UUID, device_id: str) -> bool:
        return await self._repo.delete(user_id=user_id, device_id=device_id)
