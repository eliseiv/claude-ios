"""Browser routes of the broadapps payment page on the instance's own domain (ADR-113 §3).

``/cp/pay/{rest}`` (GET/HEAD/POST), ``/payment/return``, ``/main.css``, ``/main.js`` (GET/HEAD) —
outside ``/v1`` (browser pages, not API) and ``include_in_schema=False`` (not a client contract).
The browser opens them by the rewritten ``paymentUrl``; no JWT.

Order of checks: (1) the instance has checkout configured (``cloudpayments_checkout_configured()``)
AND ``CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED`` is on, else the same ``404`` without an upstream call
(the flag gates both the link rewrite and these routes); (2) the RAW path matches the whitelist
else ``404`` without an upstream call; (3) per-source-IP limit ``rl:cppage:{ip}`` else ``429``
neutral HTML; (4) forward to the fixed upstream host (``PayPageProxy``).
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from app.api_gateway.rate_limit import enforce_cloudpayments_pay_page_limits
from app.billing_cloudpayments.pay_page import (
    PATH_CLASS_ASSET,
    PATH_CLASS_PAY,
    PATH_CLASS_RETURN,
    PAY_PAGE_RATE_LIMIT_PER_IP,
    PayPageProxy,
    log_rate_limited,
    not_found_response,
    path_allowed,
    raw_request_path,
    unavailable_response,
    upstream_host,
)
from app.config import get_settings
from app.deps import client_ip

router = APIRouter(include_in_schema=False)


async def _serve(request: Request, path_class: str) -> Response:
    settings = get_settings()
    if (
        not settings.cloudpayments_checkout_configured()
        or not settings.cloudpayments_pay_page_proxy_enabled
    ):
        return not_found_response()
    upstream = upstream_host(settings)
    raw_path = raw_request_path(request.scope)
    if not upstream or raw_path is None or not path_allowed(raw_path, path_class):
        return not_found_response()
    if not await enforce_cloudpayments_pay_page_limits(
        ip=client_ip(request), limit=PAY_PAGE_RATE_LIMIT_PER_IP
    ):
        log_rate_limited(path_class=path_class, method=request.method.upper())
        return unavailable_response(429)
    return await PayPageProxy(settings).forward(
        request, raw_path=raw_path, path_class=path_class, upstream=upstream
    )


@router.api_route("/cp/pay/{rest:path}", methods=["GET", "HEAD", "POST"])
async def pay_page(request: Request, rest: str) -> Response:
    # ``rest`` (decoded) is deliberately unused: the whitelist and the upstream path are built from
    # the RAW path only (ADR-113 §3).
    del rest
    return await _serve(request, PATH_CLASS_PAY)


@router.api_route("/payment/return", methods=["GET", "HEAD"])
async def payment_return(request: Request) -> Response:
    return await _serve(request, PATH_CLASS_RETURN)


@router.api_route("/main.css", methods=["GET", "HEAD"])
async def pay_page_asset_css(request: Request) -> Response:
    return await _serve(request, PATH_CLASS_ASSET)


@router.api_route("/main.js", methods=["GET", "HEAD"])
async def pay_page_asset_js(request: Request) -> Response:
    return await _serve(request, PATH_CLASS_ASSET)
