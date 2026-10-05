"""Outgoing broadapps ``/payments/link`` call that creates a RU payment link (ADR-051 §3,
billing-cloudpayments/03 §Checkout).

``CloudPaymentsCheckoutClient`` performs a single per-call ``httpx.AsyncClient`` POST to
``{settings.cloudpayments_api_base}/payments/link`` with a ``multipart/form-data`` body (via
``files=``, NOT ``data=``) and a server-held ``Authorization: Bearer <api_token>``. Every upstream
failure (timeout / connect / non-2xx / malformed body) is mapped to a generic ``UpstreamError``
(502) that NEVER leaks the upstream body/status or our token to the client. Exactly one structured
log ``"cloudpayments_checkout_outcome"`` is emitted per call with an allowlist of fields —
``customer_email`` (PII), the Bearer token and ``app_id`` are never logged (ADR-051 §6).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

import httpx
import redis.asyncio as redis

from app.api_gateway.rate_limit import get_redis
from app.billing_cloudpayments.parser import KIND_TOKENS, KIND_UNKNOWN, classify_product
from app.billing_cloudpayments.pay_page import SKIP_DISABLED, rewrite_payment_url
from app.config import Settings
from app.errors import UpstreamError, ValidationFailedError
from app.instance_config import one_time_credits, one_time_product_ids
from app.observability.logging import log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.checkout"

# Connect+read timeout for the outgoing broadapps call (ADR-051 §3). No dedicated env — the three
# CLOUDPAYMENTS_API_* configs are sufficient.
_CHECKOUT_TIMEOUT_SECONDS = 15.0

# Unpaid-link reuse (CHECKOUT_LINK_REUSE_SECONDS). One Redis hash per user (field = product + email
# digest) so a credited payment drops every reusable link of that user with a single DEL. The lock
# outlives the upstream call, so its holder always finishes (stores or fails) before it expires.
_LINK_LOCK_TTL_SECONDS = int(_CHECKOUT_TIMEOUT_SECONDS) + 5
_LINK_LOCK_POLL_SECONDS = 0.25
# A link the provider says expires sooner than this is not handed out again.
_LINK_EXPIRY_MARGIN_SECONDS = 60


def _links_key(user_id: uuid.UUID) -> str:
    return f"cp:link:{user_id}"


def _link_field(product_id: str, customer_email: str) -> str:
    # The email is part of the identity (a link carries the receipt address) but is never stored.
    digest = hashlib.sha256(customer_email.strip().lower().encode("utf-8")).hexdigest()
    return f"{product_id}:{digest}"


async def forget_reusable_links(user_id: uuid.UUID) -> None:
    """Drop the user's reusable checkout links after a credited payment (next purchase = new link).

    Redis unavailability is logged and swallowed: the webhook outcome must not depend on the cache.
    """
    try:
        await get_redis().delete(_links_key(user_id))
    except redis.RedisError as exc:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_checkout_link_reuse_unavailable",
            op="invalidate",
            userId=str(user_id),
            error=type(exc).__name__,
        )


@dataclass(frozen=True)
class CheckoutResult:
    """Passthrough of the broadapps payment-link response (ADR-051 §4)."""

    payment_id: str
    payment_url: str
    status: str
    expires_at: str | None


@dataclass(frozen=True)
class CancelResult:
    """Passthrough of the broadapps subscription-cancel response.

    ``found`` is False when the user has no active broadapps subscription (nothing to cancel).
    ``status`` / ``canceled_at`` / ``already_canceled`` echo the broadapps cancel response.
    """

    found: bool
    status: str | None = None
    canceled_at: str | None = None
    already_canceled: bool | None = None


class CloudPaymentsCheckoutClient:
    """Creates a RU payment link via broadapps. No DB; recent unpaid links are reused via Redis."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def validate_product(self, product_id: str) -> None:
        """Allowlist gate symmetric with the webhook (ADR-051 §2, 03 §Валидация productId).

        Reuses ``classify_product`` (billing_interval_unit unknown at checkout => None): we only
        issue a link for a product the webhook could later credit. Does NOT size any grant.
        ``unknown`` OR a ``tokens`` product with a non-positive credit value => 422.
        """
        # ADR-099: тот же overlay-aware источник, что у вебхука. Этот гейт объявляет себя
        # СИММЕТРИЧНЫМ вебхуку, и симметрия обязана держаться на одном множестве: иначе продукт,
        # заведённый оператором и у нас, и в панели поставщика, вебхук зачёл бы, а ссылку на
        # оплату мы бы не выдали. На пустом оверлее оба выражения равны сегодняшним.
        one_time_ids = one_time_product_ids(settings=self._settings)
        kind = classify_product(product_id, None, one_time_ids)
        if kind == KIND_UNKNOWN:
            raise ValidationFailedError("unknown_product")
        if (
            kind == KIND_TOKENS
            and (one_time_credits(product_id, settings=self._settings) or 0) <= 0
        ):
            raise ValidationFailedError("unknown_product")

    async def list_products(self) -> list[dict[str, Any]] | None:
        """Fetch the broadapps product catalog for this app (GET /apps/{app_id}/products).

        Returns the raw ``data`` list of product dicts, or ``None`` on any failure / unconfigured
        app (the caller then falls back to the static catalog). Never raises; token never logged.
        """
        settings = self._settings
        if not settings.cloudpayments_app_id or not settings.cloudpayments_api_token:
            return None
        url = f"{settings.cloudpayments_api_base}/apps/{settings.cloudpayments_app_id}/products"
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                resp = await client.get(url, headers=headers)
            if not (200 <= resp.status_code < 300):
                return None
            body = resp.json()
        except (httpx.HTTPError, ValueError, UnicodeDecodeError):
            return None
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else None

    async def cancel_subscription(self, *, user_id: uuid.UUID) -> CancelResult:
        """Cancel the user's active broadapps recurring subscription (auto-renew off, access kept).

        Two upstream calls: GET ``/users/{user_id}/subscriptions`` to find the active
        ``subscription_id``, then POST ``/subscriptions/{id}/cancel``. ``user_id`` is the JWT
        subject; the broadapps account must match the id the payment was made under. No active
        subscription -> ``found=False`` (no upstream cancel, caller no-ops). Any upstream failure
        maps to ``UpstreamError`` (502) and never leaks the upstream body/status or our token.
        """
        settings = self._settings
        base = settings.cloudpayments_api_base
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                listing = await client.get(f"{base}/users/{user_id}/subscriptions", headers=headers)
                if not (200 <= listing.status_code < 300):
                    raise self._cancel_upstream_error("list_status", user_id)
                sub_id = self._active_subscription_id(listing.json())
                if sub_id is None:
                    log_event(
                        logger,
                        logging.INFO,
                        "cloudpayments_cancel_outcome",
                        result="no_active_subscription",
                        userId=str(user_id),
                    )
                    return CancelResult(found=False)
                cancel = await client.post(
                    f"{base}/subscriptions/{sub_id}/cancel", json={}, headers=headers
                )
                if not (200 <= cancel.status_code < 300):
                    raise self._cancel_upstream_error("cancel_status", user_id)
                body = cancel.json()
        except httpx.TimeoutException as exc:
            raise self._cancel_upstream_error("timeout", user_id) from exc
        except httpx.RequestError as exc:
            raise self._cancel_upstream_error("connect_error", user_id) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._cancel_upstream_error("malformed_response", user_id) from exc

        status = body.get("status") if isinstance(body, dict) else None
        canceled_at = body.get("canceled_at") if isinstance(body, dict) else None
        already = body.get("already_canceled") if isinstance(body, dict) else None
        log_event(
            logger,
            logging.INFO,
            "cloudpayments_cancel_outcome",
            result="canceled",
            userId=str(user_id),
            alreadyCanceled=already,
        )
        return CancelResult(
            found=True,
            status=status if isinstance(status, str) else None,
            canceled_at=canceled_at if isinstance(canceled_at, str) else None,
            already_canceled=already if isinstance(already, bool) else None,
        )

    @staticmethod
    def _active_subscription_id(listing: Any) -> str | None:
        """Extract the first active subscription_id from a broadapps subscriptions listing."""
        if not isinstance(listing, dict):
            return None
        for item in listing.get("data", []):
            if not isinstance(item, dict):
                continue
            sub_id = item.get("subscription_id")
            if item.get("status") == "active" and isinstance(sub_id, str) and sub_id:
                return sub_id
        return None

    def _cancel_upstream_error(self, reason: str, user_id: uuid.UUID) -> UpstreamError:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_cancel_outcome",
            result="error",
            reason=reason,
            userId=str(user_id),
        )
        return UpstreamError("payment provider unavailable")

    async def create_payment_link(
        self, *, user_id: uuid.UUID, product_id: str, customer_email: str
    ) -> CheckoutResult:
        """Return a payment link for (user, product, email), reusing a recent unpaid one.

        Within ``CHECKOUT_LINK_REUSE_SECONDS`` a repeated call returns the stored result without a
        broadapps call. Concurrent calls are serialised by a short Redis lock: the loser waits for
        the winner's stored result, or takes the lock itself if the winner failed. Upstream errors
        are never stored. Redis unavailability fails open to a plain upstream call.
        """
        reuse_seconds = self._settings.checkout_link_reuse_seconds
        if reuse_seconds <= 0:
            return await self._issue_link(
                user_id=user_id, product_id=product_id, customer_email=customer_email
            )
        key = _links_key(user_id)
        field = _link_field(product_id, customer_email)
        lock_key = f"cp:link:lock:{user_id}:{field}"
        client = get_redis()
        locked = False
        try:
            deadline = time.monotonic() + _LINK_LOCK_TTL_SECONDS
            while True:
                cached = await self._cached_link(client, key, field)
                if cached is not None:
                    self._log_created(
                        cached, user_id=user_id, product_id=product_id, rewritten=None, reused=True
                    )
                    return cached
                if await client.set(lock_key, "1", nx=True, ex=_LINK_LOCK_TTL_SECONDS):
                    locked = True
                    break
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(_LINK_LOCK_POLL_SECONDS)
        except redis.RedisError as exc:
            self._log_reuse_unavailable("lookup", exc, user_id=user_id, product_id=product_id)
            return await self._issue_link(
                user_id=user_id, product_id=product_id, customer_email=customer_email
            )
        try:
            result = await self._issue_link(
                user_id=user_id, product_id=product_id, customer_email=customer_email
            )
            ttl = self._reuse_ttl(result, reuse_seconds)
            if ttl > 0:
                try:
                    await self._store_link(client, key, field, result, ttl, reuse_seconds)
                except redis.RedisError as exc:
                    self._log_reuse_unavailable(
                        "store", exc, user_id=user_id, product_id=product_id
                    )
            return result
        finally:
            if locked:
                # On a Redis error the lock TTL releases it; the store step logs the failure.
                with contextlib.suppress(redis.RedisError):
                    await client.delete(lock_key)

    @staticmethod
    async def _cached_link(client: redis.Redis, key: str, field: str) -> CheckoutResult | None:
        raw = await client.hget(key, field)  # type: ignore[misc]
        if not isinstance(raw, str):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        expires = data.get("exp")
        payment_url = data.get("paymentUrl")
        if not isinstance(expires, int | float) or expires <= time.time():
            return None
        if not isinstance(payment_url, str) or not payment_url:
            return None
        expires_at = data.get("expiresAt")
        return CheckoutResult(
            payment_id=str(data.get("paymentId") or ""),
            payment_url=payment_url,
            status=str(data.get("status") or ""),
            expires_at=expires_at if isinstance(expires_at, str) else None,
        )

    @staticmethod
    async def _store_link(
        client: redis.Redis,
        key: str,
        field: str,
        result: CheckoutResult,
        ttl: int,
        reuse_seconds: int,
    ) -> None:
        value = json.dumps(
            {
                "paymentId": result.payment_id,
                "paymentUrl": result.payment_url,
                "status": result.status,
                "expiresAt": result.expires_at,
                "exp": time.time() + ttl,
            }
        )
        async with client.pipeline(transaction=True) as pipe:
            pipe.hset(key, field, value)
            pipe.expire(key, reuse_seconds)
            await pipe.execute()

    @staticmethod
    def _reuse_ttl(result: CheckoutResult, reuse_seconds: int) -> int:
        """Reuse window, capped by the provider's own link expiry when it is a parseable instant."""
        if result.expires_at is None:
            return reuse_seconds
        try:
            expires_at = datetime.datetime.fromisoformat(result.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return reuse_seconds
        if expires_at.tzinfo is None:
            return reuse_seconds
        left = (expires_at - datetime.datetime.now(datetime.UTC)).total_seconds()
        return min(reuse_seconds, int(left) - _LINK_EXPIRY_MARGIN_SECONDS)

    @staticmethod
    def _log_reuse_unavailable(
        op: str, exc: redis.RedisError, *, user_id: uuid.UUID, product_id: str
    ) -> None:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_checkout_link_reuse_unavailable",
            op=op,
            userId=str(user_id),
            productId=product_id,
            error=type(exc).__name__,
        )

    @staticmethod
    def _log_created(
        result: CheckoutResult,
        *,
        user_id: uuid.UUID,
        product_id: str,
        rewritten: bool | None,
        reused: bool,
    ) -> None:
        log_event(
            logger,
            logging.INFO,
            "cloudpayments_checkout_outcome",
            result="created",
            userId=str(user_id),
            productId=product_id,
            status=result.status,
            paymentId=result.payment_id,
            paymentUrlRewritten=rewritten,
            reused=reused,
        )

    async def _issue_link(
        self, *, user_id: uuid.UUID, product_id: str, customer_email: str
    ) -> CheckoutResult:
        """POST broadapps ``/payments/link`` and return the created link (ADR-051 §3).

        ``user_id`` is the authenticated subject (from JWT ``sub``), never a client-supplied value.
        Any upstream failure maps to ``UpstreamError`` (502) without leaking upstream detail/token.
        """
        settings = self._settings
        url = f"{settings.cloudpayments_api_base}/payments/link"
        # multipart/form-data via files= with (None, value) tuples — httpx sets the Content-Type
        # boundary itself. Do NOT set Content-Type by hand, and do NOT use data= (urlencoded).
        files: dict[str, tuple[None, str]] = {
            "app_id": (None, settings.cloudpayments_app_id),
            "product_id": (None, product_id),
            "user_id": (None, str(user_id)),
            "customer_email": (None, customer_email),
        }
        headers = {
            "Authorization": f"Bearer {settings.cloudpayments_api_token}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=_CHECKOUT_TIMEOUT_SECONDS) as client:
                response = await client.post(url, files=files, headers=headers)
        except httpx.TimeoutException as exc:
            raise self._upstream_error("timeout", user_id=user_id, product_id=product_id) from exc
        except httpx.RequestError as exc:
            raise self._upstream_error(
                "connect_error", user_id=user_id, product_id=product_id
            ) from exc

        # broadapps success = 201; accept any 2xx defensively. A non-2xx never proxies the upstream
        # status/body outward — only a generic 502.
        if not (200 <= response.status_code < 300):
            raise self._upstream_error("upstream_status", user_id=user_id, product_id=product_id)

        try:
            body = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise self._upstream_error(
                "malformed_response", user_id=user_id, product_id=product_id
            ) from exc

        result = self._to_result(body)
        if result is None:
            raise self._upstream_error("malformed_response", user_id=user_id, product_id=product_id)

        # ADR-113 §2: the ONE place both paths of the pair (/v1/billing/cloudpayments/checkout and
        # /v1/web/session) go through. A broadapps payment-page link moves onto SERVICE_DOMAIN; any
        # other host (YooMoney, T-Bank) passes through untouched.
        rewrite = rewrite_payment_url(result.payment_url, settings)
        if rewrite.skip_reason is not None:
            # A broadapps link that stays on the broadapps host must be visible (ADR-113 §7); the
            # URL itself is not logged. ``disabled`` is the operator's deliberate state -> INFO;
            # ``path_not_proxied`` / ``service_domain_unset`` are misconfiguration -> WARNING.
            log_event(
                logger,
                logging.INFO if rewrite.skip_reason == SKIP_DISABLED else logging.WARNING,
                "cloudpayments_pay_page_rewrite_skipped",
                reason=rewrite.skip_reason,
                userId=str(user_id),
                productId=product_id,
            )
        if rewrite.rewritten:
            result = replace(result, payment_url=rewrite.url)

        self._log_created(
            result,
            user_id=user_id,
            product_id=product_id,
            rewritten=rewrite.rewritten,
            reused=False,
        )
        return result

    @staticmethod
    def _to_result(body: Any) -> CheckoutResult | None:
        """Project the broadapps JSON body into a ``CheckoutResult`` or None if malformed.

        Requires a non-empty ``payment_url``; ``expires_at`` is passed through as a string or None
        without parsing (upstream format is not fixed — passthrough is safer, ADR-051 §4).
        """
        if not isinstance(body, dict):
            return None
        payment_url = body.get("payment_url")
        if not isinstance(payment_url, str) or not payment_url:
            return None
        payment_id = body.get("payment_id")
        status = body.get("status")
        expires_at = body.get("expires_at")
        return CheckoutResult(
            payment_id=str(payment_id) if payment_id is not None else "",
            payment_url=payment_url,
            status=str(status) if status is not None else "",
            expires_at=expires_at if isinstance(expires_at, str) else None,
        )

    def _upstream_error(self, reason: str, *, user_id: uuid.UUID, product_id: str) -> UpstreamError:
        """Log the error outcome (allowlist) and build the generic 502 raised to the client."""
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_checkout_outcome",
            result="error",
            reason=reason,
            userId=str(user_id),
            productId=product_id,
        )
        return UpstreamError("payment provider unavailable")
