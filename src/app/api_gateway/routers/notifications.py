"""Notifications routes: POST/DELETE /v1/notifications/device-token (ADR-067)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_notifications_service
from app.errors import RateLimitedError
from app.notifications.service import NotificationsService
from app.schemas.notifications import (
    DeviceTokenDeleteRequest,
    DeviceTokenDeleteResponse,
    DeviceTokenRegisterRequest,
    DeviceTokenRegisterResponse,
)

router = APIRouter(prefix="/v1/notifications", tags=["Notifications"])


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


@router.post(
    "/device-token",
    response_model=DeviceTokenRegisterResponse,
    summary="Зарегистрировать APNs device token",
    description=(
        "Upsert APNs device-токена для пары `(userId, deviceId)`. "
        "`deviceId` — из тела, иначе JWT claim `device_id`, иначе заголовок `X-Device-Id`. "
        "Токен обрабатывается как чувствительный идентификатор и не логируется. "
        "Фактическая отправка push уважает `notificationsEnabled` в preferences."
    ),
)
async def register_device_token(
    body: DeviceTokenRegisterRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[NotificationsService, Depends(get_notifications_service)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> DeviceTokenRegisterResponse:
    await _rate_limit(current.user_id)
    device_id = service.resolve_device_id(
        body_device_id=body.deviceId,
        jwt_device_id=current.device_id,
        header_device_id=x_device_id,
    )
    await service.register(
        user_id=current.user_id,
        device_id=device_id,
        push_token=body.pushToken,
        platform=body.platform,
    )
    return DeviceTokenRegisterResponse(registered=True)


@router.delete(
    "/device-token",
    response_model=DeviceTokenDeleteResponse,
    summary="Удалить APNs device token",
    description=(
        "Удаляет зарегистрированный токен устройства (logout / отписка). "
        "`deviceId` резолвится так же, как при регистрации."
    ),
)
async def delete_device_token(
    body: DeviceTokenDeleteRequest,
    request: Request,
    current: CurrentUser,
    service: Annotated[NotificationsService, Depends(get_notifications_service)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> DeviceTokenDeleteResponse:
    await _rate_limit(current.user_id)
    device_id = service.resolve_device_id(
        body_device_id=body.deviceId,
        jwt_device_id=current.device_id,
        header_device_id=x_device_id,
    )
    deleted = await service.delete(user_id=current.user_id, device_id=device_id)
    return DeviceTokenDeleteResponse(deleted=deleted)
