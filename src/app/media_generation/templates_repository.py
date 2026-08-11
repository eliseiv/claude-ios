"""Persistence for media gallery templates (ADR-066)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MediaTemplate


class MediaTemplatesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_kind(self, kind: str) -> list[MediaTemplate]:
        result = await self._session.scalars(
            select(MediaTemplate)
            .where(MediaTemplate.kind == kind)
            .order_by(MediaTemplate.sort_order.asc(), MediaTemplate.id.asc())
        )
        return list(result.all())

    async def get(self, template_id: str) -> MediaTemplate | None:
        return await self._session.get(MediaTemplate, template_id)

    async def next_sort_order(self, kind: str) -> int:
        current = await self._session.scalar(
            select(func.coalesce(func.max(MediaTemplate.sort_order), 0)).where(
                MediaTemplate.kind == kind
            )
        )
        return int(current or 0) + 10

    async def create(
        self,
        *,
        template_id: str,
        kind: str,
        title: str,
        prompt: str,
        model: str,
        required_input_images: int,
        parameters: dict[str, Any],
        cover_bytes: bytes,
        cover_media_type: str,
        sort_order: int,
    ) -> MediaTemplate:
        row = MediaTemplate(
            id=template_id,
            kind=kind,
            title=title,
            prompt=prompt,
            model=model,
            required_input_images=required_input_images,
            parameters=parameters,
            cover_bytes=cover_bytes,
            cover_media_type=cover_media_type,
            sort_order=sort_order,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def delete(self, template_id: str) -> bool:
        row = await self.get(template_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True
