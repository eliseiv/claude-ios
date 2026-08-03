"""CRM admin routes under /v1/admin (broad-crm «Пользователи бэков», v1).

Read/write endpoints for the CRM user-management panel. Authorization: X-Admin-Key (or legacy
X-Admin-Token) via the shared ``require_admin`` dependency on the parent admin router.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request

from app.admin.crm_service import CrmAdminService
from app.api_gateway.rate_limit import enforce_admin_limits
from app.deps import client_ip, get_crm_admin_service
from app.errors import RateLimitedError, UserNotFoundError
from app.schemas.crm_admin import (
    CrmPaymentListResponse,
    CrmProductListResponse,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmSubscriptionGrantRequest,
    CrmSubscriptionGrantResponse,
    CrmTokensAdjustRequest,
    CrmTokensAdjustResponse,
    CrmUserDetailResponse,
    CrmUserListResponse,
)

router = APIRouter(tags=["Admin (CRM)"])


async def _enforce_admin_rate_limit(request: Request) -> None:
    if not await enforce_admin_limits(ip=client_ip(request)):
        raise RateLimitedError("admin rate limit exceeded")


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid datetime") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


@router.get(
    "/users",
    response_model=CrmUserListResponse,
    summary="CRM: список пользователей",
)
async def crm_list_users(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    is_paid: bool | None = Query(default=None),
) -> CrmUserListResponse:
    await _enforce_admin_rate_limit(request)
    return await service.list_users(
        limit=limit,
        offset=offset,
        search=search,
        date_from=_parse_dt(date_from),
        date_to=_parse_dt(date_to),
        is_paid=is_paid,
    )


@router.get(
    "/users/{id}",
    response_model=CrmUserDetailResponse,
    summary="CRM: карточка пользователя",
)
async def crm_get_user(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
) -> CrmUserDetailResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.get_user(user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.get(
    "/users/{id}/payments",
    response_model=CrmPaymentListResponse,
    summary="CRM: история оплат пользователя",
)
async def crm_user_payments(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CrmPaymentListResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.list_payments(user_id, limit=limit, offset=offset)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.get(
    "/users/{id}/requests",
    response_model=CrmRequestListResponse,
    summary="CRM: история запросов пользователя",
)
async def crm_user_requests(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CrmRequestListResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.list_requests(user_id, limit=limit, offset=offset)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.get(
    "/stats",
    response_model=CrmStatsResponse,
    summary="CRM: сводная статистика",
)
async def crm_stats(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> CrmStatsResponse:
    await _enforce_admin_rate_limit(request)
    return await service.stats(
        date_from=_parse_dt(date_from),
        date_to=_parse_dt(date_to),
    )


@router.get(
    "/products",
    response_model=CrmProductListResponse,
    summary="CRM: каталог тарифов",
)
async def crm_products(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
) -> CrmProductListResponse:
    await _enforce_admin_rate_limit(request)
    return service.list_products()


@router.post(
    "/users/{id}/tokens",
    response_model=CrmTokensAdjustResponse,
    summary="CRM: начислить/списать токены",
)
async def crm_adjust_tokens(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    body: Annotated[CrmTokensAdjustRequest, Body()],
) -> CrmTokensAdjustResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.adjust_tokens(user_id, body.amount)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.post(
    "/users/{id}/subscription",
    response_model=CrmSubscriptionGrantResponse,
    summary="CRM: выдать/продлить подписку",
)
async def crm_grant_subscription(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    body: Annotated[CrmSubscriptionGrantRequest, Body()],
) -> CrmSubscriptionGrantResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.grant_subscription(
            user_id,
            product_id=body.product_id,
            expires_in_days=body.expires_in_days,
            grant_id=body.grant_id,
        )
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
