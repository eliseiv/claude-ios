"""RU payment routes under /v1/billing/cloudpayments.

- POST /webhook (ADR-054, revises ADR-050): called by the payment aggregator (server-to-server),
  NOT by the iOS client. PUBLIC — broadapps sends no auth, so ``require_cloudpayments_webhook`` is
  only an observational (non-blocking) dependency; the callback is merely a TRIGGER and crediting
  happens only after verifying the payment via the broadapps API. A per-source-IP rate limit
  (anti-amplification of the outgoing verification GET) is the only throttle. The body is read RAW
  (``await request.body()``) with NO Pydantic body model: a malformed callback must yield 2xx,
  never 422 (which the aggregator would retry). Every processed outcome is HTTP 200 ``{"code": 0}``;
  429 on flood, and 500 (misconfigured / verification unavailable / DB failure) makes the aggregator
  retry -> clean reprocessing (idempotent by broadapps payment_id).
- POST /checkout (ADR-051): called by the iOS client (JWT). Creates a payment link via broadapps;
  the ``userId`` sent upstream is the authenticated subject (never the client body), which is the
  key fix for "lost payments". Active only where CLOUDPAYMENTS_APP_ID / CLOUDPAYMENTS_API_TOKEN are
  set (else 503).
- POST /experiments/assign, POST /experiments/paywall-shown (ADR-098): called by the iOS client
  (JWT) around paywall rendering; passthrough to broadapps, no money and no DB. Same instance gate
  as /checkout (503), but a SEPARATE rate-limit bucket so frequent impressions cannot lock the
  payment path. The upstream ``user_id`` is the JWT subject on both, like every other outgoing
  broadapps call. Deliberate asymmetry: /assign answers 502 when the provider fails (the segment is
  never invented), /paywall-shown answers 200 {"logged": false} and NEVER 502.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api_gateway.rate_limit import (
    enforce_cloudpayments_webhook_limits,
    enforce_experiment_limits,
    enforce_other_limits,
)
from app.billing_cloudpayments.auth import require_cloudpayments_webhook
from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
from app.billing_cloudpayments.experiments import (
    BroadappsExperimentsClient,
    resolve_experiment_locale,
)
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import Settings, get_settings
from app.deps import (
    CurrentUser,
    DbSession,
    client_ip,
    get_broadapps_experiments_client,
    get_cloudpayments_checkout_client,
    get_cloudpayments_webhook_service,
)
from app.errors import CloudPaymentsCheckoutNotConfiguredError, RateLimitedError
from app.models import Subscription
from app.schemas.billing_cloudpayments import (
    CloudPaymentsCancelResponse,
    CloudPaymentsCheckoutRequest,
    CloudPaymentsCheckoutResponse,
    CloudPaymentsWebhookResponse,
    ExperimentAssignRequest,
    ExperimentAssignResponse,
    ExperimentSegment,
    PaywallShownRequest,
    PaywallShownResponse,
)

router = APIRouter(prefix="/v1/billing/cloudpayments", tags=["Billing (CloudPayments)"])


@router.post(
    "/webhook",
    response_model=CloudPaymentsWebhookResponse,
    dependencies=[Depends(require_cloudpayments_webhook)],
    summary="Приём платежа RU (webhook)",
    description=(
        "Серверный вебхук платёжного агрегатора (вызывает агрегатор, не клиент). Публичный: "
        "событие лишь ТРИГГЕР — начисление выполняется только после подтверждения платежа через "
        "платёжный сервис. Тело читается сырым, без валидации схемы. Ответ всегда `200`, тело "
        '`{"code": 0}` (событие принято: платёж начислен либо проигнорирован). `429` — при частых '
        "вызовах с одного IP; `500` — если способ оплаты не сконфигурирован, при недоступности "
        "верификации или сбое БД (тогда агрегатор повторяет доставку)."
    ),
)
async def cloudpayments_webhook(
    request: Request,
    service: Annotated[CloudPaymentsWebhookService, Depends(get_cloudpayments_webhook_service)],
) -> JSONResponse:
    # Public endpoint (broadapps sends no auth) => per-source-IP rate limit is the only throttle;
    # its purpose is anti-amplification of the outgoing verification GET (ADR-054 §1).
    if not await enforce_cloudpayments_webhook_limits(ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")
    raw = await request.body()
    # The outcome (applied | duplicate | ignored/*) is emitted to logs/audit by the service; the
    # aggregator receives only {"code": 0} for every processed callback (ADR-054 §2).
    await service.handle(raw)
    return JSONResponse({"code": 0}, status_code=200)


@router.post(
    "/checkout",
    response_model=CloudPaymentsCheckoutResponse,
    summary="Создать ссылку на оплату (RU)",
    description=(
        "Создаёт платёжную ссылку для российской оплаты и возвращает `paymentUrl` — откройте его "
        "для оплаты. Требуется авторизация (JWT). Укажите `productId` и `customerEmail`. Доступно "
        "не на всех инсталляциях (`503`, если способ оплаты недоступен)."
    ),
)
async def cloudpayments_checkout(
    body: CloudPaymentsCheckoutRequest,
    current: CurrentUser,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CloudPaymentsCheckoutResponse:
    # userId comes ONLY from the verified JWT subject (never the request body) — the core fix that
    # guarantees the callback (ADR-050) can find this user and credit the right account.
    if not settings.cloudpayments_checkout_configured():
        raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    client.validate_product(body.productId)
    result = await client.create_payment_link(
        user_id=current.user_id,
        product_id=body.productId,
        customer_email=body.customerEmail,
    )
    return CloudPaymentsCheckoutResponse(
        paymentId=result.payment_id,
        paymentUrl=result.payment_url,
        status=result.status,
        expiresAt=result.expires_at,
    )


@router.post(
    "/experiments/assign",
    response_model=ExperimentAssignResponse,
    summary="Назначить сегмент эксперимента",
    description=(
        "Назначает пользователя в сегмент эксперимента пейволла и возвращает **действующий** "
        "сегмент. Требуется JWT; пользователь берётся из токена. Пришлите `experimentCode`, "
        "`segmentCode` и `placement` — значения передаются как есть. Рисуйте пейволл по "
        "`segment.code` из ответа, а не по запрошенному `segmentCode`: при "
        "`requestedSegmentMatches=false` у пользователя уже есть другое назначение. Повторный "
        "вызов безопасен (`created=false`). При `502`/`429`/`503` покажите свой пейволл по "
        "умолчанию — это не ошибка для пользователя. Доступно не на всех инсталляциях (`503`)."
    ),
)
async def experiments_assign(
    body: ExperimentAssignRequest,
    current: CurrentUser,
    client: Annotated[BroadappsExperimentsClient, Depends(get_broadapps_experiments_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    accept_language: str | None = Header(default=None),
) -> ExperimentAssignResponse:
    # Same order of checks on both experiment endpoints: JWT (dependency) -> instance gate ->
    # rate limit -> locale -> upstream call. The user id sent upstream is the verified JWT
    # subject, never a body field: every outgoing broadapps call carries that one identity.
    if not settings.cloudpayments_checkout_configured():
        raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")
    if not await enforce_experiment_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    locale = resolve_experiment_locale(accept_language, settings.presets_default_locale)
    result = await client.assign(
        user_id=current.user_id,
        experiment_code=body.experimentCode,
        segment_code=body.segmentCode,
        placement=body.placement,
        locale=locale,
    )
    return ExperimentAssignResponse(
        segment=ExperimentSegment(code=result.segment_code, isControl=result.is_control),
        requestedSegmentMatches=result.requested_segment_matches,
        created=result.created,
    )


@router.post(
    "/experiments/paywall-shown",
    response_model=PaywallShownResponse,
    summary="Записать показ пейволла",
    description=(
        "Записывает показ пейволла. Тело — такое же, как у назначения сегмента. Вызывается на "
        'каждый показ; события намеренно не дедуплицируются. Ответ — `{"logged": true|false}`: '
        "`false` означает, что событие не принято, но на показ пейволла это не влияет и ответа "
        "можно не дожидаться. Доступно не на всех инсталляциях (`503`)."
    ),
)
async def experiments_paywall_shown(
    body: PaywallShownRequest,
    current: CurrentUser,
    client: Annotated[BroadappsExperimentsClient, Depends(get_broadapps_experiments_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    accept_language: str | None = Header(default=None),
) -> PaywallShownResponse:
    # Deliberately asymmetric with /experiments/assign and /checkout: whatever the provider does,
    # this endpoint answers 200 {"logged": bool} and NEVER 502. A refused impression log must not
    # break the impression, and a false 502 here would devalue the code that means "payment link
    # not created" on the same prefix. The refusal is not lost — it is a WARNING in our log.
    if not settings.cloudpayments_checkout_configured():
        raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")
    if not await enforce_experiment_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    locale = resolve_experiment_locale(accept_language, settings.presets_default_locale)
    logged = await client.paywall_shown(
        user_id=current.user_id,
        experiment_code=body.experimentCode,
        segment_code=body.segmentCode,
        placement=body.placement,
        locale=locale,
    )
    return PaywallShownResponse(logged=logged)


@router.post(
    "/cancel",
    response_model=CloudPaymentsCancelResponse,
    summary="Отменить подписку (RU)",
    description=(
        "Отменяет автопродление активной RU-подписки у провайдера (broadapps). Доступ сохраняется "
        "до конца оплаченного периода (`status`/`expiresAt` не меняются), а `willRenew` становится "
        "`false`. Требуется JWT. Если активной подписки у провайдера нет — `canceled=false`."
    ),
)
async def cloudpayments_cancel(
    current: CurrentUser,
    session: DbSession,
    client: Annotated[CloudPaymentsCheckoutClient, Depends(get_cloudpayments_checkout_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CloudPaymentsCancelResponse:
    if not settings.cloudpayments_checkout_configured():
        raise CloudPaymentsCheckoutNotConfiguredError("cloudpayments checkout not configured")
    if not await enforce_other_limits(user_id=current.user_id):
        raise RateLimitedError("rate limit exceeded")
    result = await client.cancel_subscription(user_id=current.user_id)
    # Reflect auto-renew off locally (keep status/expires_at) so /policy/effective shows willRenew.
    sub = await session.scalar(select(Subscription).where(Subscription.user_id == current.user_id))
    if sub is not None:
        sub.will_renew = False
    return CloudPaymentsCancelResponse(
        canceled=result.found,
        status=result.status,
        canceledAt=result.canceled_at,
        alreadyCanceled=result.already_canceled,
        willRenew=False,
    )
