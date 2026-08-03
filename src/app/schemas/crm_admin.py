"""CRM admin API schemas (broad-crm «Пользователи бэков», v1).

Wire format uses snake_case JSON keys per the CRM contract. Responses are separate from the
legacy camelCase /v1/admin/wallet/* schemas.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel


class CrmUserListItem(StrictModel):
    id: str
    external_id: str | None = None
    is_paid: bool
    payments_count: int
    renewals_count: int
    tokens: float
    subscription_active: bool
    subscription_expires_at: str | None = None
    plan_id: str | None = None
    registered_at: str


class CrmUserListResponse(StrictModel):
    total: int
    items: list[CrmUserListItem]


class CrmUserBalance(StrictModel):
    tokens: float
    credited_total: float | None = None
    spent_total: float | None = None


class CrmUserSubscription(StrictModel):
    plan_id: str | None = None
    plan_name: str | None = None
    price: str | None = None
    active: bool
    expires_at: str | None = None
    last_payment_at: str | None = None
    last_payment_method: str | None = None


class CrmUserRevenue(StrictModel):
    income_usd: float
    api_cost_usd: float
    providers: dict[str, float]


class CrmMediaBucket(StrictModel):
    total: int
    success: int
    failed: int


class CrmMediaAvgSec(StrictModel):
    photo: float | None = None
    video: float | None = None
    overall: float | None = None


class CrmMediaStats(StrictModel):
    photos: CrmMediaBucket
    videos: CrmMediaBucket
    avg_generation_sec: CrmMediaAvgSec


class CrmUserDetailResponse(StrictModel):
    id: str
    external_id: str | None = None
    registered_at: str
    balance: CrmUserBalance
    subscription: CrmUserSubscription
    revenue: CrmUserRevenue | None = None
    media_stats: CrmMediaStats | None = None


class CrmPaymentItem(StrictModel):
    title: str
    description: str | None = None
    amount: float
    currency: str
    status: Literal["success", "failed"]
    occurred_at: str


class CrmPaymentListResponse(StrictModel):
    total: int
    items: list[CrmPaymentItem]


class CrmRequestItem(StrictModel):
    endpoint: str
    prompt_preview: str | None = None
    status_code: int
    status: Literal["ok", "slow", "error"]
    duration_sec: float | None = None
    sent_at: str


class CrmRequestListResponse(StrictModel):
    total: int
    items: list[CrmRequestItem]


class CrmStatsResponse(StrictModel):
    users_total: int
    paid_users: int
    payments_sum_usd: float


class CrmProductItem(StrictModel):
    product_id: str
    name: str
    price: str | None = None
    period: str | None = None


class CrmProductListResponse(StrictModel):
    items: list[CrmProductItem]


class CrmTokensAdjustRequest(StrictModel):
    amount: int = Field(description="Positive to credit, negative to debit.")


class CrmTokensAdjustResponse(StrictModel):
    id: str
    tokens: float


class CrmSubscriptionGrantRequest(StrictModel):
    product_id: str = Field(min_length=1, max_length=255)
    expires_in_days: int = Field(gt=0)
    grant_id: str = Field(min_length=1, max_length=255)


class CrmSubscriptionGrantResponse(StrictModel):
    id: str
    tokens: float
    subscription_active: bool
    subscription_expires_at: str | None = None
    applied: bool
