"""Persistence for operator media presets and reusable user avatars."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaFeaturePreset, UserAvatar


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


class MediaFeaturesRepository:
    """Request-scoped repository; transaction ownership stays with the API dependency."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_presets(
        self,
        *,
        feature: str,
        gender: str | None = None,
        style: str | None = None,
    ) -> list[MediaFeaturePreset]:
        stmt = select(MediaFeaturePreset).where(
            MediaFeaturePreset.feature == feature,
            MediaFeaturePreset.is_active.is_(True),
        )
        if gender is not None:
            stmt = stmt.where(MediaFeaturePreset.gender == gender)
        if style is not None:
            stmt = stmt.where(MediaFeaturePreset.style == style)
        rows = await self._session.scalars(
            stmt.order_by(MediaFeaturePreset.sort_order.asc(), MediaFeaturePreset.id.asc())
        )
        return list(rows.all())

    async def get_preset(
        self, preset_id: str, *, active_only: bool = True
    ) -> MediaFeaturePreset | None:
        row = await self._session.get(MediaFeaturePreset, preset_id)
        if row is None or (active_only and not row.is_active):
            return None
        return row

    async def next_preset_sort_order(self, feature: str) -> int:
        current = await self._session.scalar(
            select(func.coalesce(func.max(MediaFeaturePreset.sort_order), 0)).where(
                MediaFeaturePreset.feature == feature
            )
        )
        return int(current or 0) + 10

    async def create_preset(
        self,
        *,
        preset_id: str,
        feature: str,
        title: str,
        gender: str | None,
        style: str | None,
        provider_value: str | None,
        image_bytes: bytes,
        image_media_type: str,
        sort_order: int,
    ) -> MediaFeaturePreset:
        row = MediaFeaturePreset(
            id=preset_id,
            feature=feature,
            title=title,
            gender=gender,
            style=style,
            provider_value=provider_value,
            image_bytes=image_bytes,
            image_media_type=image_media_type,
            sort_order=sort_order,
            is_active=True,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete_preset(self, preset_id: str) -> bool:
        row = await self.get_preset(preset_id, active_only=False)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def count_user_avatars(self, user_id: uuid.UUID) -> int:
        value = await self._session.scalar(
            select(func.count(UserAvatar.id)).where(UserAvatar.user_id == user_id)
        )
        return int(value or 0)

    async def list_user_avatars(self, user_id: uuid.UUID) -> list[UserAvatar]:
        rows = await self._session.scalars(
            select(UserAvatar)
            .where(UserAvatar.user_id == user_id)
            .order_by(UserAvatar.created_at.desc(), UserAvatar.id.desc())
        )
        return list(rows.all())

    async def get_user_avatar(
        self, *, user_id: uuid.UUID, avatar_id: uuid.UUID
    ) -> UserAvatar | None:
        row: UserAvatar | None = await self._session.scalar(
            select(UserAvatar).where(
                UserAvatar.id == avatar_id,
                UserAvatar.user_id == user_id,
            )
        )
        return row

    async def create_user_avatar(
        self,
        *,
        user_id: uuid.UUID,
        title: str | None,
        source_preset_id: str | None,
        image_bytes: bytes,
        media_type: str,
    ) -> UserAvatar:
        row = UserAvatar(
            user_id=user_id,
            title=title,
            source_preset_id=source_preset_id,
            original_image_bytes=image_bytes,
            original_media_type=media_type,
            prepared_image_bytes=None,
            prepared_media_type=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete_user_avatar(self, row: UserAvatar) -> None:
        await self._session.delete(row)
        await self._session.flush()

    async def begin_preparation(
        self,
        row: UserAvatar,
        *,
        job_id: uuid.UUID,
        background_image_bytes: bytes | None,
        background_media_type: str | None,
        background_color: str | None,
    ) -> None:
        row.preparation_job_id = job_id
        row.background_image_bytes = background_image_bytes
        row.background_media_type = background_media_type
        row.background_color = background_color
        row.updated_at = _now()
        await self._session.flush()

    async def use_original(self, row: UserAvatar) -> None:
        row.prepared_image_bytes = None
        row.prepared_media_type = None
        row.preparation_job_id = None
        row.background_image_bytes = None
        row.background_media_type = None
        row.background_color = None
        row.updated_at = _now()
        await self._session.flush()

    async def cancel_preparation(self, row: UserAvatar, *, job_id: uuid.UUID) -> None:
        """Clear pending inputs but retain the last good prepared image, if one exists."""
        if row.preparation_job_id != job_id:
            return
        row.preparation_job_id = None
        row.background_image_bytes = None
        row.background_media_type = None
        row.background_color = None
        row.updated_at = _now()
        await self._session.flush()

    async def complete_preparation(
        self,
        row: UserAvatar,
        *,
        job_id: uuid.UUID,
        image_bytes: bytes,
        media_type: str,
    ) -> bool:
        """Persist only the newest preparation if two jobs finish out of order."""
        if row.preparation_job_id != job_id:
            return False
        row.prepared_image_bytes = image_bytes
        row.prepared_media_type = media_type
        row.preparation_job_id = None
        row.background_image_bytes = None
        row.background_media_type = None
        row.background_color = None
        row.updated_at = _now()
        await self._session.flush()
        return True
