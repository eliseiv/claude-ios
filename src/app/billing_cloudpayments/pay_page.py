"""broadapps payment page served from the instance's own domain (ADR-113).

Two parts, one module so both read the SAME upstream host and the SAME host-token rule:

* ``rewrite_payment_url`` (ADR-113 §2) — the checkout response's ``paymentUrl`` for a broadapps
  payment page (host of ``CLOUDPAYMENTS_API_BASE``, path ``/cp/pay/``) is moved onto
  ``SERVICE_DOMAIN`` when ``CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED`` is on. Links to any other host
  (YooMoney, T-Bank) are passed through untouched.
* ``PayPageProxy`` (ADR-113 §3–§5, §7) — the browser-facing reverse proxy of ``/cp/pay/*``,
  ``/payment/return``, ``/main.css``, ``/main.js`` to that upstream host.

Security invariants (ADR-113 §4): the upstream host comes ONLY from configuration (never from the
request, the upstream body or ``payment_url``), the scheme is always ``https``, the path is checked
against a whitelist on the RAW request path, redirects are never followed, and ``Authorization``,
the API token, ``X-Forwarded-*``, ``X-Real-IP`` and ``Forwarded`` never go upstream (only an
allowlist of request headers is forwarded). Nothing identifying is logged: no ``/cp/pay/<uuid>``,
no query, no cookie, no body, no header value.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import Response

from app.config import Settings
from app.observability.logging import PAY_PAGE_PATH_PREFIX, PAY_PAGE_RETURN_PATH, log_event

logger = logging.getLogger(__name__)  # == "app.billing_cloudpayments.pay_page"

# --- Module constants (ADR-113 §5: no dedicated env, like _CHECKOUT_TIMEOUT_SECONDS) ---
PAY_PAGE_TIMEOUT_SECONDS: Final = 15.0
PAY_PAGE_MAX_BODY_BYTES: Final = 5 * 1024 * 1024
PAY_PAGE_RATE_LIMIT_PER_IP: Final = 120

# One declaration shared with the access-log masking (ADR-113 §4), like PROXY_WEBHOOK_PATH_PREFIX.
PAY_PATH_PREFIX: Final = PAY_PAGE_PATH_PREFIX
RETURN_PATH: Final = PAY_PAGE_RETURN_PATH
ASSET_PATHS: Final = frozenset({"/main.css", "/main.js"})

PATH_CLASS_PAY: Final = "pay"
PATH_CLASS_RETURN: Final = "return"
PATH_CLASS_ASSET: Final = "asset"

# ADR-113 §2 / §7: reasons of a broadapps link that was NOT rewritten.
SKIP_DISABLED: Final = "disabled"
SKIP_PATH_NOT_PROXIED: Final = "path_not_proxied"
SKIP_SERVICE_DOMAIN_UNSET: Final = "service_domain_unset"

_UNAVAILABLE_HTML: Final = (
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Оплата</title></head><body>"
    "<p>Страница оплаты временно недоступна. Повторите попытку позже.</p>"
    "</body></html>"
).encode()

# {rest} of /cp/pay/{rest}: one or more non-empty components of [A-Za-z0-9._~-] joined by "/".
# "%" and "\" are outside the class, so any percent-encoding and backslash are rejected; "." and
# ".." components are rejected separately (they match the class).
_PAY_REST_COMPONENT = re.compile(r"[A-Za-z0-9._~-]+")

# Request headers forwarded upstream (ADR-113 §3). Everything else — notably Authorization,
# X-Forwarded-*, X-Real-IP, Forwarded, Host — is dropped. Origin/Referer are forwarded with the
# instance host swapped for the upstream host.
_REQUEST_HEADER_ALLOWLIST: Final = frozenset(
    {
        "accept",
        "accept-language",
        "content-type",
        "user-agent",
        "cookie",
        "x-xsrf-token",
        "x-csrf-token",
        "x-requested-with",
    }
)
_REQUEST_HOST_SWAPPED: Final = frozenset({"origin", "referer"})

# Response headers passed to the browser as-is (ADR-113 §3).
_RESPONSE_PASSTHROUGH: Final = frozenset({"content-type", "cache-control", "expires"})
# Security policies: passed with the upstream host replaced in their value (today not sent).
_RESPONSE_POLICY_HEADERS: Final = frozenset(
    {
        "content-security-policy",
        "content-security-policy-report-only",
        "referrer-policy",
        "permissions-policy",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "cross-origin-resource-policy",
    }
)
# Headers the ADR names as dropped on purpose: our own value applies (HSTS / X-Frame-Options /
# nosniff from SecurityHeadersMiddleware), the length is recomputed, the body is already decoded,
# or they are hop-by-hop / fingerprinting. They are NOT reported in ``droppedHeaders`` — that field
# exists to make a NEW upstream header observable (ADR-113 §3 «Любой иной заголовок»).
_RESPONSE_KNOWN_DROPPED: Final = frozenset(
    {
        "strict-transport-security",
        "x-frame-options",
        "x-content-type-options",
        "content-encoding",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
        "server",
        "x-powered-by",
        "date",
    }
)

_TEXT_CONTENT_TYPES: Final = frozenset(
    {
        "text/html",
        "text/css",
        "text/javascript",
        "application/javascript",
        "application/x-javascript",
        "application/json",
    }
)
_CHARSET_RE = re.compile(r"charset\s*=\s*\"?([^\";\s]+)", re.IGNORECASE)
_BRAND_RE = re.compile("broadapps", re.IGNORECASE)


def upstream_host(settings: Settings) -> str:
    """The broadapps payment-page host = host of ``CLOUDPAYMENTS_API_BASE`` (ADR-113 §1), or ''."""
    return (urlsplit(settings.cloudpayments_api_base).hostname or "").lower()


@lru_cache(maxsize=16)
def _host_token_re(host: str) -> re.Pattern[str]:
    """Host as a TOKEN (ADR-113 §3), case-insensitive.

    Left boundary: start of text, a char outside ``[A-Za-z0-9.-]``, or ``%2F`` (any case).
    Right boundary: end of text or a char outside ``[A-Za-z0-9-]``; a ``.`` counts only when the
    char after it is outside ``[A-Za-z0-9-]`` (end of a sentence, not the next domain label).
    """
    return re.compile(
        r"(?:(?<![A-Za-z0-9.\-])|(?<=%2F))"
        + re.escape(host)
        + r"(?![A-Za-z0-9\-])(?!\.[A-Za-z0-9\-])",
        re.IGNORECASE,
    )


def replace_host_tokens(text: str, host: str, replacement: str) -> str:
    """Replace every token occurrence of ``host`` in ``text`` with ``replacement``."""
    if not host:
        return text
    return _host_token_re(host).sub(replacement, text)


def _swap_url_host(value: str, from_host: str, to_host: str) -> str:
    """Swap the host of an absolute (or protocol-relative) URL when it equals ``from_host``.

    The rest of the value is kept byte-for-byte; a relative URL or a URL on any other host is
    returned unchanged.
    """
    if not from_host or not to_host:
        return value
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        return value
    if not parts.netloc or hostname is None or hostname.lower() != from_host.lower():
        return value
    return value.replace("//" + parts.netloc, "//" + to_host, 1)


@dataclass(frozen=True, slots=True)
class PaymentUrlRewrite:
    """Outcome of the ADR-113 §2 predicate for one checkout response."""

    url: str
    rewritten: bool
    skip_reason: str | None  # set only for an upstream-host link that was NOT rewritten


def rewrite_payment_url(payment_url: str, settings: Settings) -> PaymentUrlRewrite:
    """Apply the ADR-113 §2 conjunction to ``payment_url``.

    (a) flag on, (b) host == upstream host (case-insensitive, host comparison — not substring),
    (c) path starts with ``/cp/pay/``, (d) ``normalized_service_domain()`` non-empty. All true →
    ``https://<SERVICE_DOMAIN><path>[?query][#fragment]`` (path, query, fragment byte-for-byte);
    otherwise the link unchanged. A link on another host (YooMoney, T-Bank) is the normal path and
    has no ``skip_reason``.
    """
    host = upstream_host(settings)
    try:
        parts = urlsplit(payment_url)
        link_host = parts.hostname
    except ValueError:
        return PaymentUrlRewrite(url=payment_url, rewritten=False, skip_reason=None)
    if not host or link_host is None or link_host.lower() != host:
        return PaymentUrlRewrite(url=payment_url, rewritten=False, skip_reason=None)
    if not settings.cloudpayments_pay_page_proxy_enabled:
        return PaymentUrlRewrite(url=payment_url, rewritten=False, skip_reason=SKIP_DISABLED)
    if not parts.path.startswith(PAY_PATH_PREFIX):
        return PaymentUrlRewrite(
            url=payment_url, rewritten=False, skip_reason=SKIP_PATH_NOT_PROXIED
        )
    domain = settings.normalized_service_domain()
    if not domain:
        return PaymentUrlRewrite(
            url=payment_url, rewritten=False, skip_reason=SKIP_SERVICE_DOMAIN_UNSET
        )
    url = "https://" + domain + parts.path
    if parts.query:
        url += "?" + parts.query
    if parts.fragment:
        url += "#" + parts.fragment
    return PaymentUrlRewrite(url=url, rewritten=True, skip_reason=None)


def raw_request_path(scope: Mapping[str, Any]) -> str | None:
    """The RAW request path (``scope["raw_path"]``, without the query), or None if unavailable.

    The whitelist is checked on the raw bytes, never on the decoded path, so ``%2e%2e`` / ``%2f``
    cannot slip through (ADR-113 §3). No raw path → None → the caller answers 404 (fail closed).
    """
    raw = scope.get("raw_path")
    if not isinstance(raw, bytes | bytearray):
        return None
    try:
        text = bytes(raw).decode("ascii")
    except UnicodeDecodeError:
        return None
    return text.split("?", 1)[0]


def path_allowed(raw_path: str, path_class: str) -> bool:
    """Whitelist of ADR-113 §3 on the raw path."""
    if path_class == PATH_CLASS_RETURN:
        return raw_path == RETURN_PATH
    if path_class == PATH_CLASS_ASSET:
        return raw_path in ASSET_PATHS
    if path_class != PATH_CLASS_PAY or not raw_path.startswith(PAY_PATH_PREFIX):
        return False
    rest = raw_path[len(PAY_PATH_PREFIX) :]
    if not rest:
        return False
    for component in rest.split("/"):
        if component in ("", ".", ".."):
            return False
        if _PAY_REST_COMPONENT.fullmatch(component) is None:
            return False
    return True


def not_found_response() -> Response:
    """404 for a path outside the whitelist or an instance without checkout — no upstream call."""
    return Response(content=b"Not Found", status_code=404, media_type="text/plain")


def unavailable_response(status_code: int) -> Response:
    """Neutral HTML (502 / 429): no mention of the provider or its domain (ADR-113 §5)."""
    return Response(
        content=_UNAVAILABLE_HTML,
        status_code=status_code,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def log_rate_limited(*, path_class: str, method: str) -> None:
    """``cloudpayments_pay_page_proxy`` for a request refused by the per-IP limit (INFO)."""
    log_event(
        logger,
        logging.INFO,
        "cloudpayments_pay_page_proxy",
        result="error",
        reason="rate_limited",
        pathClass=path_class,
        method=method,
    )


class _TooLargeError(Exception):
    """Upstream body exceeded PAY_PAGE_MAX_BODY_BYTES."""


def _strip_cookie_domain(value: str) -> str:
    """Drop the ``Domain`` attribute of a Set-Cookie value; every other segment is kept as-is."""
    segments = value.split(";")
    kept = [segments[0]] + [
        seg for seg in segments[1:] if seg.split("=", 1)[0].strip().lower() != "domain"
    ]
    return ";".join(kept)


def _content_type_parts(content_type: str) -> tuple[str, str | None]:
    mime = content_type.split(";", 1)[0].strip().lower()
    match = _CHARSET_RE.search(content_type)
    return mime, (match.group(1) if match else None)


class PayPageProxy:
    """Forwards one browser request to the fixed upstream host and rewrites its response."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def forward(
        self, request: Request, *, raw_path: str, path_class: str, upstream: str
    ) -> Response:
        """Proxy the request (path already whitelisted, instance gate and rate limit passed)."""
        method = request.method.upper()
        instance = self._settings.normalized_service_domain()
        query = request.scope.get("query_string", b"")
        raw_target = raw_path.encode("ascii")
        if isinstance(query, bytes) and query:
            raw_target += b"?" + query
        try:
            url = httpx.URL(scheme="https", host=upstream, raw_path=raw_target)
        except (ValueError, httpx.InvalidURL):
            # A query that cannot form the upstream URL (non-ASCII byte -> UnicodeDecodeError,
            # "#" -> InvalidURL) is refused BEFORE any outgoing call, with the same 404 as a
            # whitelist refusal and, like every pre-call refusal, without a proxy log.
            return not_found_response()
        headers = self._upstream_headers(request, instance=instance, upstream=upstream)
        body = await request.body() if method == "POST" else None

        started = time.monotonic()
        try:
            status, response_headers, content = await self._call(method, url, headers, body)
        except httpx.TimeoutException:
            return self._fail("timeout", path_class, method, started)
        except httpx.RequestError:
            return self._fail("connect_error", path_class, method, started)
        except _TooLargeError:
            return self._fail("too_large", path_class, method, started)

        out_headers, dropped = self._response_headers(response_headers, instance, upstream)
        content = self._rewrite_body(content, response_headers, instance, upstream, path_class)
        response = Response(content=b"" if method == "HEAD" else content, status_code=status)
        if method == "HEAD":
            # No body on HEAD, and the length of the rewritten GET body cannot be known without it:
            # Content-Length is NOT sent at all (never a false "0", ADR-113 §3).
            response.raw_headers[:] = [
                (k, v) for k, v in response.raw_headers if k.lower() != b"content-length"
            ]
        response.raw_headers.extend(out_headers)

        log_event(
            logger,
            logging.DEBUG if status < 500 else logging.INFO,
            "cloudpayments_pay_page_proxy",
            result="ok",
            pathClass=path_class,
            method=method,
            upstreamStatus=status,
            durationMs=int((time.monotonic() - started) * 1000),
            droppedHeaders=dropped,
        )
        return response

    async def _call(
        self,
        method: str,
        url: httpx.URL,
        headers: list[tuple[str, str]],
        body: bytes | None,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        # Redirects are NEVER followed: the browser gets the 3xx (ADR-113 §3). Per-call client.
        async with (
            httpx.AsyncClient(timeout=PAY_PAGE_TIMEOUT_SECONDS, follow_redirects=False) as client,
            client.stream(method, url, headers=headers, content=body) as upstream_resp,
        ):
            chunks: list[bytes] = []
            total = 0
            async for chunk in upstream_resp.aiter_bytes():
                total += len(chunk)
                if total > PAY_PAGE_MAX_BODY_BYTES:
                    raise _TooLargeError
                chunks.append(chunk)
            # latin-1 on both ends = a lossless byte round-trip of every header value.
            raw_headers = [
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in upstream_resp.headers.raw
            ]
            return upstream_resp.status_code, raw_headers, b"".join(chunks)

    @staticmethod
    def _upstream_headers(
        request: Request, *, instance: str, upstream: str
    ) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for name, value in request.headers.items():
            lowered = name.lower()
            if lowered in _REQUEST_HEADER_ALLOWLIST:
                out.append((lowered, value))
            elif lowered in _REQUEST_HOST_SWAPPED:
                out.append((lowered, _swap_url_host(value, instance, upstream)))
        out.append(("accept-encoding", "identity"))
        return out

    @staticmethod
    def _response_headers(
        headers: list[tuple[str, str]], instance: str, upstream: str
    ) -> tuple[list[tuple[bytes, bytes]], list[str]]:
        out: list[tuple[bytes, bytes]] = []
        dropped: set[str] = set()
        for name, value in headers:
            lowered = name.lower()
            if lowered in _RESPONSE_PASSTHROUGH:
                new_value = value
            elif lowered == "set-cookie":
                new_value = _strip_cookie_domain(value)
            elif lowered == "location":
                new_value = _swap_url_host(value, upstream, instance)
            elif lowered in _RESPONSE_POLICY_HEADERS:
                new_value = replace_host_tokens(value, upstream, instance) if instance else value
            else:
                if lowered not in _RESPONSE_KNOWN_DROPPED:
                    dropped.add(lowered)
                continue
            out.append((lowered.encode("latin-1"), new_value.encode("latin-1")))
        return out, sorted(dropped)

    @staticmethod
    def _rewrite_body(
        content: bytes,
        headers: list[tuple[str, str]],
        instance: str,
        upstream: str,
        path_class: str,
    ) -> bytes:
        content_type = next((v for k, v in headers if k.lower() == "content-type"), "")
        mime, charset = _content_type_parts(content_type)
        if mime not in _TEXT_CONTENT_TYPES or not content:
            return content
        encoding = charset or "utf-8"
        try:
            text = content.decode(encoding)
            if instance:
                text = replace_host_tokens(text, upstream, instance)
            rewritten = text.encode(encoding)
        except (LookupError, UnicodeError):
            log_event(
                logger,
                logging.WARNING,
                "cloudpayments_pay_page_rewrite_failed",
                pathClass=path_class,
                contentType=mime,
            )
            return content
        residual = len(_BRAND_RE.findall(text))
        if residual > 0:
            log_event(
                logger,
                logging.WARNING,
                "cloudpayments_pay_page_residual_brand",
                pathClass=path_class,
                count=residual,
            )
        return rewritten

    @staticmethod
    def _fail(reason: str, path_class: str, method: str, started: float) -> Response:
        log_event(
            logger,
            logging.WARNING,
            "cloudpayments_pay_page_proxy",
            result="error",
            reason=reason,
            pathClass=path_class,
            method=method,
            durationMs=int((time.monotonic() - started) * 1000),
        )
        return unavailable_response(502)
