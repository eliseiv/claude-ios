"""Schemas for /v1/notifications/* (notifications/02-api-contracts.md, ADR-067)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel


class DeviceTokenRegisterRequest(StrictModel):
    deviceId: str | None = Field(
        default=None,
        description=(
            "Стабильный id устройства. Опционален: иначе JWT claim `device_id` или заголовок "
            "`X-Device-Id`. Если нигде нет — 422."
        ),
    )
    pushToken: str = Field(
        min_length=1,
        max_length=512,
        description="APNs device token (hex или raw string от iOS).",
    )
    platform: Literal["ios"] = Field(
        default="ios",
        description="Платформа push-доставки. MVP — только iOS/APNs.",
    )


class DeviceTokenRegisterResponse(StrictModel):
    registered: bool = True


class DeviceTokenDeleteRequest(StrictModel):
    deviceId: str | None = Field(
        default=None,
        description=(
            "Стабильный id устройства. Опционален: иначе JWT claim / `X-Device-Id`. "
            "Если нигде нет — 422."
        ),
    )


class DeviceTokenDeleteResponse(StrictModel):
    deleted: bool = True
