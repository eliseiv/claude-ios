"""Integration: ADR-113 §3–§5, §7 — the broadapps payment page proxied on the instance domain.

Cases 11–23, 25–28 of ADR-113 §8 (case 24 — the access log — lives in
``tests/unit/test_cloudpayments_pay_page_access_log_adr113.py``). The REAL app (``create_app``,
all middleware) is driven by hand-built ASGI scopes, so the RAW request path (``raw_path``) and
the ``Host`` header are exactly what a server would pass — an HTTP client would normalise ``..``
and ``%2e`` before they ever reach the app. Hermetic: the upstream is an ``httpx.MockTransport``
injected through the ``httpx`` name inside ``pay_page.py`` (no socket opens), the per-IP limiter's
Redis window is replaced by an in-process counter keyed by the REAL limiter key, no database is
touched.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from app.config import get_settings

_APP_ID = "481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d"
_API_TOKEN = "broadapps-outgoing-bearer-secret"  # noqa: S105 - test-only static secret
_API_BASE = "https://pay.broadapps.dev/api/v1"
_UPSTREAM = "pay.broadapps.dev"
_DOMAIN = "shop.example"
_PAY_UUID = "3f2c9a1e-8b7d-4c6e-9a0f-1b2c3d4e5f60"
_PAY_PATH = f"/cp/pay/{_PAY_UUID}"
_CLIENT_IP = "203.0.113.7"
_NEUTRAL = "Страница оплаты временно недоступна. Повторите попытку позже."

_PROXY_EVENT = "cloudpayments_pay_page_proxy"


# ------------------------------ fakes ------------------------------


class _Upstream:
    """Records every outgoing request and answers with a scripted handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.client_kwargs: list[dict[str, Any]] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda _r: httpx.Response(
            200, headers={"Content-Type": "text/html; charset=utf-8"}, content=b"<p>ok</p>"
        )

    def reply(self, status: int, *, headers: Any = None, content: bytes = b"") -> None:
        self.handler = lambda _r: httpx.Response(status, headers=headers, content=content)

    def raise_(self, exc: Exception) -> None:
        def _h(_r: httpx.Request) -> httpx.Response:
            raise exc

        self.handler = _h

    def _transport_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        return self.handler(request)

    def fake_module(self) -> SimpleNamespace:
        upstream = self

        def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            upstream.client_kwargs.append(dict(kwargs))
            return httpx.AsyncClient(
                *args, transport=httpx.MockTransport(upstream._transport_handler), **kwargs
            )

        return SimpleNamespace(
            AsyncClient=_client,
            URL=httpx.URL,
            TimeoutException=httpx.TimeoutException,
            RequestError=httpx.RequestError,
            InvalidURL=httpx.InvalidURL,
        )


class _Buckets:
    """In-process stand-in for the Redis sliding window: counts hits per REAL limiter key."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = defaultdict(int)
        self.limit: int | None = None  # None = the limiter's own limit

    async def allow(self, _client: Any, key: str, limit: int, _window: int) -> bool:
        self.hits[key] += 1
        cap = self.limit if self.limit is not None else limit
        return self.hits[key] <= cap


class _Resp(SimpleNamespace):
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]

    def one(self, name: str) -> str | None:
        values = self.all(name)
        assert len(values) <= 1, (name, values)
        return values[0] if values else None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


# ------------------------------ fixtures ------------------------------


@pytest.fixture
def upstream() -> _Upstream:
    return _Upstream()


@pytest.fixture
def buckets() -> _Buckets:
    return _Buckets()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, upstream: _Upstream, buckets: _Buckets) -> Iterator[Any]:
    from app.api_gateway import rate_limit
    from app.billing_cloudpayments import pay_page as pay_page_mod
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", _APP_ID)
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", _API_TOKEN)
    monkeypatch.setenv("CLOUDPAYMENTS_API_BASE", _API_BASE)
    monkeypatch.setenv("SERVICE_DOMAIN", _DOMAIN)
    # ADR-113 §3: the routes serve only with checkout configured AND the flag on.
    monkeypatch.setenv("CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(pay_page_mod, "httpx", upstream.fake_module())
    monkeypatch.setattr(rate_limit, "_allow", buckets.allow)
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    # A migration run earlier in the same session disables pre-existing loggers
    # (disable_existing_loggers); re-enable the one under test (harness artifact only).
    logging.getLogger("app.billing_cloudpayments.pay_page").disabled = False
    caplog.set_level(logging.DEBUG)
    return caplog


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def _call(
    app: Any,
    method: str,
    raw_path: str,
    *,
    query: str | bytes = "",
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
    host: str = _DOMAIN,
) -> _Resp:
    """One request through the real ASGI app, with the RAW path passed as a server would."""
    raw = raw_path.encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": unquote(raw_path),
        "raw_path": raw,
        "root_path": "",
        "query_string": query if isinstance(query, bytes) else query.encode("ascii"),
        "headers": [(b"host", host.encode())]
        + [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or [])],
        "client": (_CLIENT_IP, 50000),
        "server": (_DOMAIN, 443),
    }
    sent = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m["type"] == "http.response.start")
    return _Resp(
        status=start["status"],
        headers=[(k.decode("latin-1").lower(), v.decode("latin-1")) for k, v in start["headers"]],
        body=b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"),
    )


def _proxy_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _PROXY_EVENT]


_HTML = {"Content-Type": "text/html; charset=utf-8"}


# ================================ case 11 ================================


@pytest.mark.parametrize(
    "raw_path",
    [
        "/cp/pay/x/../../api",
        "/cp/pay/%2e%2e/x",
        "/cp/pay//x",
        "/cp/pay/a%2Fb",
        "/cp/pay/",
        # Same rule, forms not named in the ADR list:
        "/cp/pay/./x",
        "/cp/pay/..",
        "/cp/pay/a\\b",
        "/cp/pay/x/",
        f"/cp/pay/{_PAY_UUID}%20",
    ],
)
async def test_case11_path_outside_whitelist_is_404_without_upstream_call(
    app: Any, upstream: _Upstream, raw_path: str
) -> None:
    resp = await _call(app, "GET", raw_path)

    assert resp.status == 404
    assert upstream.requests == []


async def test_case11_whitelisted_path_does_reach_upstream(app: Any, upstream: _Upstream) -> None:
    # Against over-blocking: a legal multi-component path passes.
    resp = await _call(app, "GET", f"{_PAY_PATH}/step-2/a.b~c_d")

    assert resp.status == 200
    assert len(upstream.requests) == 1


# ================================ case 12 ================================


@pytest.mark.parametrize(
    ("raw_path", "query"),
    [
        (_PAY_PATH, "a=b&c=%2Fx%20y&d="),
        (f"{_PAY_PATH}/confirm", ""),
        ("/payment/return", "x=1"),
        ("/main.css", ""),
        ("/main.js", "v=3"),
    ],
)
async def test_case12_upstream_url_is_config_host_same_path_same_query(
    app: Any, upstream: _Upstream, raw_path: str, query: str
) -> None:
    resp = await _call(
        app,
        "GET",
        raw_path,
        query=query,
        headers=[("X-Forwarded-Host", "evil.test"), ("Forwarded", "host=evil.test")],
        host="evil.test",
    )

    assert resp.status == 200
    (request,) = upstream.requests
    assert request.url.scheme == "https"
    assert request.url.host == _UPSTREAM
    expected = raw_path.encode() + (b"?" + query.encode() if query else b"")
    assert request.url.raw_path == expected
    assert request.headers["host"] == _UPSTREAM


async def test_case12_upstream_host_follows_cloudpayments_api_base(
    app: Any, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(monkeypatch, "CLOUDPAYMENTS_API_BASE", "https://pages.upstream-alt.test/api/v1")

    await _call(app, "GET", _PAY_PATH, host="pay.broadapps.dev")

    (request,) = upstream.requests
    assert request.url.host == "pages.upstream-alt.test"
    assert request.url.scheme == "https"


# ================================ case 13 ================================


async def test_case13_request_header_allowlist(app: Any, upstream: _Upstream) -> None:
    await _call(
        app,
        "POST",
        _PAY_PATH,
        headers=[
            ("Authorization", "Bearer user.jwt.token"),
            ("X-Forwarded-For", "198.51.100.1"),
            ("X-Forwarded-Host", "shop.example"),
            ("X-Forwarded-Proto", "https"),
            ("X-Real-IP", "198.51.100.1"),
            ("Forwarded", "for=198.51.100.1"),
            ("Cookie", "pay-session=abc; XSRF-TOKEN=tok"),
            ("X-XSRF-TOKEN", "tok"),
            ("Origin", f"https://{_DOMAIN}"),
            ("Referer", f"https://{_DOMAIN}{_PAY_PATH}?step=1"),
            ("Content-Type", "application/x-www-form-urlencoded"),
        ],
        body=b"a=1&b=2",
    )

    (request,) = upstream.requests
    sent = {k.lower() for k in request.headers}
    for forbidden in (
        "authorization",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "forwarded",
    ):
        assert forbidden not in sent, forbidden
    assert _API_TOKEN not in str(request.headers.raw)
    assert request.headers["cookie"] == "pay-session=abc; XSRF-TOKEN=tok"
    assert request.headers["x-xsrf-token"] == "tok"
    assert request.headers["origin"] == f"https://{_UPSTREAM}"
    assert request.headers["referer"] == f"https://{_UPSTREAM}{_PAY_PATH}?step=1"
    assert upstream.bodies == [b"a=1&b=2"]


# ================================ case 14 ================================


_BODY_IN = (
    'var returnUrl = "https:\\/\\/pay.broadapps.dev\\/payment\\/return";\n'
    '<link rel="stylesheet" href="https://pay.broadapps.dev/main.css">\n'
    '<a href="//pay.broadapps.dev/x">x</a>\n'
    "u1=https%3A%2F%2Fpay.broadapps.dev%2Fcp\n"
    "u2=https%3a%2f%2fpay.broadapps.dev%2fcp\n"
    '<script src="https://widget.cloudpayments.ru/bundles/cloudpayments.js"></script>\n'
    "Оплата на pay.broadapps.dev.\n"
    "xpay.broadapps.dev pay.broadapps.dev.evil.test pay.broadapps.devx a.pay.broadapps.dev\n"
)
_BODY_OUT = (
    'var returnUrl = "https:\\/\\/shop.example\\/payment\\/return";\n'
    '<link rel="stylesheet" href="https://shop.example/main.css">\n'
    '<a href="//shop.example/x">x</a>\n'
    "u1=https%3A%2F%2Fshop.example%2Fcp\n"
    "u2=https%3a%2f%2fshop.example%2fcp\n"
    '<script src="https://widget.cloudpayments.ru/bundles/cloudpayments.js"></script>\n'
    "Оплата на shop.example.\n"
    "xpay.broadapps.dev pay.broadapps.dev.evil.test pay.broadapps.devx a.pay.broadapps.dev\n"
)


async def test_case14_host_token_replaced_in_every_form_and_only_as_token(
    app: Any, upstream: _Upstream
) -> None:
    upstream.reply(200, headers=_HTML, content=_BODY_IN.encode())

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.status == 200
    assert resp.text == _BODY_OUT
    assert resp.one("content-length") == str(len(_BODY_OUT.encode()))


@pytest.mark.parametrize(
    "content_type",
    ["text/css", "application/javascript", "application/json", "text/javascript"],
)
async def test_case14_other_text_types_are_rewritten_too(
    app: Any, upstream: _Upstream, content_type: str
) -> None:
    upstream.reply(
        200, headers={"Content-Type": content_type}, content=b'"https://pay.broadapps.dev/x"'
    )

    resp = await _call(app, "GET", "/main.js")

    assert resp.body == b'"https://shop.example/x"'


async def test_case14_non_text_body_is_not_rewritten(app: Any, upstream: _Upstream) -> None:
    raw = b"\x89PNG https://pay.broadapps.dev/x"
    upstream.reply(200, headers={"Content-Type": "image/png"}, content=raw)

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.body == raw


# ================================ case 15 ================================


async def test_case15_set_cookie_domain_is_stripped_other_attributes_kept(
    app: Any, upstream: _Upstream
) -> None:
    upstream.reply(
        200,
        headers=[
            ("Content-Type", "text/html"),
            (
                "Set-Cookie",
                "XSRF-TOKEN=t1; expires=Thu, 01 Jan 2099 00:00:00 GMT; Max-Age=7200; "
                "path=/; Domain=pay.broadapps.dev; secure; samesite=lax",
            ),
            ("Set-Cookie", "pay-session=s1; path=/; secure; httponly; samesite=lax"),
        ],
        content=b"<p>ok</p>",
    )

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.all("set-cookie") == [
        "XSRF-TOKEN=t1; expires=Thu, 01 Jan 2099 00:00:00 GMT; Max-Age=7200; "
        "path=/; secure; samesite=lax",
        "pay-session=s1; path=/; secure; httponly; samesite=lax",
    ]


# ================================ case 16 ================================


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        (
            "https://pay.broadapps.dev/payment/return?ok=1",
            "https://shop.example/payment/return?ok=1",
        ),
        ("https://bank.example/3ds?md=1", "https://bank.example/3ds?md=1"),
        ("/payment/return", "/payment/return"),
    ],
)
async def test_case16_location_host_swapped_only_for_upstream_and_not_followed(
    app: Any, upstream: _Upstream, location: str, expected: str
) -> None:
    upstream.reply(302, headers={"Location": location})

    resp = await _call(app, "POST", _PAY_PATH)

    assert resp.status == 302
    assert resp.one("location") == expected
    assert len(upstream.requests) == 1  # the redirect was not followed by the server
    assert upstream.client_kwargs[0]["follow_redirects"] is False
    assert upstream.client_kwargs[0]["timeout"] == 15.0


# ================================ case 17 ================================


@pytest.mark.parametrize("status", [404, 419, 500])
async def test_case17_upstream_status_passed_as_is(
    app: Any, upstream: _Upstream, status: int
) -> None:
    upstream.reply(status, headers=_HTML, content=b"<p>expired</p>")

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.status == status
    assert resp.body == b"<p>expired</p>"


# ================================ case 18 ================================


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [("timeout", "timeout"), ("connect", "connect_error"), ("too_large", "too_large")],
)
async def test_case18_unavailable_upstream_gives_neutral_502(
    app: Any,
    upstream: _Upstream,
    logs: pytest.LogCaptureFixture,
    outcome: str,
    reason: str,
) -> None:
    if outcome == "timeout":
        upstream.raise_(httpx.ReadTimeout("slow"))
    elif outcome == "connect":
        upstream.raise_(httpx.ConnectError("refused"))
    else:
        upstream.reply(
            200,
            headers={"Content-Type": "application/octet-stream"},
            content=b"a" * (5 * 1024 * 1024 + 1),
        )

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.status == 502
    assert resp.one("content-type") == "text/html; charset=utf-8"
    assert resp.one("cache-control") == "no-store"
    assert _NEUTRAL in resp.text
    assert "broadapps" not in resp.text.lower()
    (record,) = _proxy_records(logs)
    assert record.levelno == logging.WARNING
    fields = record.extra_fields  # type: ignore[attr-defined]
    assert fields["result"] == "error"
    assert fields["reason"] == reason


async def test_case18_body_exactly_at_the_limit_is_served(app: Any, upstream: _Upstream) -> None:
    # Against over-refusal: 5 MiB exactly is within the limit.
    upstream.reply(
        200,
        headers={"Content-Type": "application/octet-stream"},
        content=b"a" * (5 * 1024 * 1024),
    )

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.status == 200
    assert len(resp.body) == 5 * 1024 * 1024


# ================================ case 19 ================================


async def test_case19_per_ip_limit_gives_429_in_its_own_bucket(
    app: Any, upstream: _Upstream, buckets: _Buckets, logs: pytest.LogCaptureFixture
) -> None:
    from app.api_gateway.rate_limit import enforce_cloudpayments_webhook_limits

    buckets.limit = 2
    codes = [(await _call(app, "GET", _PAY_PATH)).status for _ in range(3)]
    limited = await _call(app, "GET", _PAY_PATH)

    assert codes == [200, 200, 429]
    assert limited.status == 429
    assert _NEUTRAL in limited.text
    assert "broadapps" not in limited.text.lower()
    assert len(upstream.requests) == 2  # refused requests never reach upstream
    assert dict(buckets.hits) == {f"rl:cppage:{_CLIENT_IP}": 4}
    limited_logs = [
        r
        for r in _proxy_records(logs)
        if r.extra_fields.get("reason") == "rate_limited"  # type: ignore[attr-defined]
    ]
    assert len(limited_logs) == 2
    assert all(r.levelno == logging.INFO for r in limited_logs)

    # The webhook bucket is untouched by the page and is not drained by it either.
    assert await enforce_cloudpayments_webhook_limits(ip=_CLIENT_IP) is True
    assert buckets.hits[f"rl:cpwebhook:{_CLIENT_IP}"] == 1
    assert buckets.hits[f"rl:cppage:{_CLIENT_IP}"] == 4


async def test_case19_webhook_traffic_does_not_spend_the_page_bucket(
    app: Any, upstream: _Upstream, buckets: _Buckets
) -> None:
    from app.api_gateway.rate_limit import enforce_cloudpayments_webhook_limits

    buckets.limit = 1
    for _ in range(3):
        await enforce_cloudpayments_webhook_limits(ip=_CLIENT_IP)

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.status == 200
    assert buckets.hits[f"rl:cppage:{_CLIENT_IP}"] == 1


# ================================ case 20 ================================


_GATE_PATHS = [_PAY_PATH, "/payment/return", "/main.css", "/main.js"]


@pytest.mark.parametrize("path", _GATE_PATHS)
async def test_case20_flag_off_answers_404_without_call(
    app: Any, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _set(monkeypatch, "CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "false")

    resp = await _call(app, "GET", path)

    assert resp.status == 404
    assert upstream.requests == []


@pytest.mark.parametrize("blank", ["CLOUDPAYMENTS_APP_ID", "CLOUDPAYMENTS_API_TOKEN"])
@pytest.mark.parametrize("path", _GATE_PATHS)
async def test_case20_instance_without_checkout_answers_404_without_call(
    app: Any, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, blank: str, path: str
) -> None:
    # Flag stays "true" (fixture): the checkout half of the gate refuses on its own.
    _set(monkeypatch, blank, "")

    resp = await _call(app, "GET", path)

    assert resp.status == 404
    assert upstream.requests == []


@pytest.mark.parametrize("path", _GATE_PATHS)
async def test_case20_both_refusals_are_byte_identical(
    app: Any, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _set(monkeypatch, "CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "false")
    flag_off = await _call(app, "GET", path)
    _set(monkeypatch, "CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "true")
    _set(monkeypatch, "CLOUDPAYMENTS_API_TOKEN", "")
    unconfigured = await _call(app, "GET", path)

    assert flag_off.status == unconfigured.status == 404
    assert flag_off.body == unconfigured.body

    # x-request-id is the per-request correlation id (CorrelationIdMiddleware), not the answer.
    def _answer(r: _Resp) -> list[tuple[str, str]]:
        return [(k, v) for k, v in r.headers if k != "x-request-id"]

    assert _answer(flag_off) == _answer(unconfigured)
    assert upstream.requests == []


@pytest.mark.parametrize("path", _GATE_PATHS)
async def test_case20_flag_on_and_checkout_configured_proxies(
    app: Any, upstream: _Upstream, path: str
) -> None:
    resp = await _call(app, "GET", path)

    assert resp.status == 200
    assert len(upstream.requests) == 1


# ================================ case 21 ================================


async def test_case21_residual_brand_warns_with_exact_count(
    app: Any, upstream: _Upstream, logs: pytest.LogCaptureFixture
) -> None:
    upstream.reply(
        200,
        headers=_HTML,
        content=b"<p>BroadApps Ltd, support@broadapps.com</p><a href='https://pay.broadapps.dev/x'>",
    )

    resp = await _call(app, "GET", _PAY_PATH)

    assert b"https://shop.example/x" in resp.body
    records = [r for r in logs.records if r.msg == "cloudpayments_pay_page_residual_brand"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    fields = records[0].extra_fields  # type: ignore[attr-defined]
    assert fields["count"] == 2
    assert fields["pathClass"] == "pay"


async def test_case21_no_warning_when_only_the_host_token_was_present(
    app: Any, upstream: _Upstream, logs: pytest.LogCaptureFixture
) -> None:
    upstream.reply(200, headers=_HTML, content=b"<a href='https://pay.broadapps.dev/x'>")

    await _call(app, "GET", _PAY_PATH)

    assert [r for r in logs.records if r.msg == "cloudpayments_pay_page_residual_brand"] == []


# ================================ case 22 ================================


@pytest.mark.parametrize("outcome", ["ok", "error"])
async def test_case22_proxy_log_has_no_uuid_query_or_cookie(
    app: Any, upstream: _Upstream, logs: pytest.LogCaptureFixture, outcome: str
) -> None:
    if outcome == "error":
        upstream.raise_(httpx.ConnectError("refused"))
    else:
        upstream.reply(200, headers={**_HTML, "X-Leak": "HDRVAL"}, content=b"<p>ok</p>")

    await _call(
        app,
        "GET",
        _PAY_PATH,
        query="secret=QUERYVAL",
        headers=[("Cookie", "pay-session=COOKIEVAL")],
    )

    records = _proxy_records(logs)
    assert len(records) == 1
    fields = records[0].extra_fields  # type: ignore[attr-defined]
    assert fields["pathClass"] == "pay"
    assert fields["method"] == "GET"
    dump = f"{records[0].getMessage()} {fields}"
    for leaked in (_PAY_UUID, "QUERYVAL", "secret=", "COOKIEVAL", "pay-session", "HDRVAL"):
        assert leaked not in dump, leaked
    if outcome == "ok":
        assert fields["result"] == "ok"
        assert fields["upstreamStatus"] == 200
        assert records[0].levelno == logging.DEBUG


async def test_case22_upstream_5xx_is_logged_at_info(
    app: Any, upstream: _Upstream, logs: pytest.LogCaptureFixture
) -> None:
    upstream.reply(503, headers=_HTML, content=b"down")

    await _call(app, "GET", _PAY_PATH)

    (record,) = _proxy_records(logs)
    assert record.levelno == logging.INFO
    assert record.extra_fields["upstreamStatus"] == 503  # type: ignore[attr-defined]


# ================================ case 23 ================================


def test_case23_openapi_has_no_proxy_paths(app: Any) -> None:
    paths = app.openapi()["paths"]

    for path in paths:
        assert not path.startswith("/cp/"), path
        assert path not in ("/payment/return", "/main.css", "/main.js"), path


# ================================ case 25 ================================


async def test_case25_upstream_security_headers_replaced_by_ours_exactly_once(
    app: Any, upstream: _Upstream
) -> None:
    upstream.reply(
        200,
        headers=[
            ("Content-Type", "text/html"),
            ("Strict-Transport-Security", "max-age=1"),
            ("X-Frame-Options", "SAMEORIGIN"),
            ("X-Content-Type-Options", "upstream-value"),
        ],
        content=b"<p>ok</p>",
    )

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.all("strict-transport-security") == ["max-age=63072000; includeSubDomains"]
    assert resp.all("x-frame-options") == ["DENY"]
    assert resp.all("x-content-type-options") == ["nosniff"]


# ================================ case 26 ================================


async def test_case26_policy_headers_passed_with_host_replaced(
    app: Any, upstream: _Upstream
) -> None:
    upstream.reply(
        200,
        headers=[
            ("Content-Type", "text/html"),
            (
                "Content-Security-Policy",
                "default-src 'self' https://pay.broadapps.dev; "
                "script-src https://widget.cloudpayments.ru https://pay.broadapps.dev",
            ),
            ("Referrer-Policy", "strict-origin-when-cross-origin"),
            ("Cross-Origin-Opener-Policy", "same-origin-allow-popups"),
        ],
        content=b"<p>ok</p>",
    )

    resp = await _call(app, "GET", _PAY_PATH)

    assert resp.all("content-security-policy") == [
        "default-src 'self' https://shop.example; "
        "script-src https://widget.cloudpayments.ru https://shop.example"
    ]
    assert resp.all("referrer-policy") == ["strict-origin-when-cross-origin"]
    assert resp.all("cross-origin-opener-policy") == ["same-origin-allow-popups"]


# ================================ case 27 ================================


async def test_case27_unknown_header_dropped_and_named_without_value(
    app: Any, upstream: _Upstream, logs: pytest.LogCaptureFixture
) -> None:
    upstream.reply(
        200,
        headers=[
            ("Content-Type", "text/html"),
            ("X-Test", "v"),
            ("Server", "nginx/1.25"),
            ("X-Powered-By", "PHP/8.3"),
        ],
        content=b"<p>ok</p>",
    )

    resp = await _call(app, "GET", _PAY_PATH)

    names = {k for k, _v in resp.headers}
    assert "x-test" not in names
    assert "server" not in names
    assert "x-powered-by" not in names
    (record,) = _proxy_records(logs)
    dropped = record.extra_fields["droppedHeaders"]  # type: ignore[attr-defined]
    assert "x-test" in dropped
    assert "v" not in dropped
    assert dropped == sorted(dropped)
    assert "nginx" not in str(dropped)
    assert "PHP" not in str(dropped)


# ================================ case 28 ================================


@pytest.mark.parametrize("status", [200, 410])
async def test_case28_head_passes_status_with_no_body_and_no_content_length(
    app: Any, upstream: _Upstream, status: int
) -> None:
    upstream.reply(status, headers={**_HTML, "Content-Length": "1234"})

    resp = await _call(app, "HEAD", _PAY_PATH)

    assert resp.status == status
    assert resp.body == b""
    assert resp.all("content-length") == []
    assert upstream.requests[0].method == "HEAD"


def test_case25_proxy_itself_drops_upstream_security_headers() -> None:
    """The proxy layer drops them on its own, not only thanks to the middleware overwriting them.

    ``SecurityHeadersMiddleware`` assigns its values with ``headers[...] = ...`` and would mask a
    proxy that passed the upstream ones through; this check pins the proxy's own allowlist.
    """
    from app.billing_cloudpayments.pay_page import PayPageProxy

    out, dropped = PayPageProxy._response_headers(  # noqa: SLF001
        [
            ("Strict-Transport-Security", "max-age=1"),
            ("X-Frame-Options", "SAMEORIGIN"),
            ("X-Content-Type-Options", "upstream-value"),
            ("Content-Type", "text/html"),
        ],
        _DOMAIN,
        _UPSTREAM,
    )

    assert out == [(b"content-type", b"text/html")]
    assert dropped == []  # known-dropped headers are not reported as new


# ================================ case 29 ================================


@pytest.mark.parametrize(
    "query",
    [
        "a=é".encode(),  # non-ASCII byte in the raw query
        b"a=b#frag",  # "#" cannot be part of an upstream query
    ],
    ids=["non_ascii", "hash"],
)
async def test_case29_query_that_cannot_form_upstream_url_is_404_without_call_or_log(
    app: Any, upstream: _Upstream, logs: pytest.LogCaptureFixture, query: bytes
) -> None:
    whitelist_refusal = await _call(app, "GET", "/cp/pay/%2e%2e/x")

    resp = await _call(app, "GET", _PAY_PATH, query=query)

    assert resp.status == 404
    assert resp.body == whitelist_refusal.body
    assert resp.one("content-type") == whitelist_refusal.one("content-type")
    assert upstream.requests == []
    assert _proxy_records(logs) == []
