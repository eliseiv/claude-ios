"""Integration: ADR-110 — neutral path duplicates ``/v1/web/*`` of the RU-payment endpoints.

Each duplicate is the SAME handler registered a second time (ADR-110 §1), so every case below sends
the SAME input to the original and to the duplicate and asserts the SAME status and body (the
per-request ``requestId`` of an error envelope is the only field excluded). Hermetic: shared
testcontainers Postgres (``db_sessionmaker``), every outgoing broadapps call is faked at the
``httpx`` name inside ``checkout.py`` / ``experiments.py``, the webhook verify client is faked,
and the Redis sliding window is replaced by an in-process counter keyed by the SAME Redis key the
limiter builds (so the shared-bucket cases observe the real key, not a patched enforcer).

Covers ADR-110 §8 / billing-cloudpayments/09-testing.md «Пути-дубликаты»: parity of all five pairs
(success + 401/503/422/502 and ``{"logged": false}``), webhook on ``/v1/web/events`` (no 401,
forged callback credits nothing, empty token -> 500, malformed body -> 200 ``{"code":0}``), one
bucket per pair for ``rl:cpwebhook`` / ``rl:other`` / ``rl:experiments``, dedup across the two
webhook paths, OpenAPI without ``/v1/web/``, and ``/cancel`` (no active subscription, upstream
refusal, success keeps ``status``/``expires_at``). ``/cancel`` with ``canceled=false`` and an
EXISTING local subscription row is checked for pair parity only (TD-064: outcome not decided).
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

_OLD = "/v1/billing/cloudpayments"
_NEW = "/v1/web"
PAIRS: dict[str, tuple[str, str]] = {
    "checkout": (f"{_OLD}/checkout", f"{_NEW}/session"),
    "cancel": (f"{_OLD}/cancel", f"{_NEW}/cancel"),
    "webhook": (f"{_OLD}/webhook", f"{_NEW}/events"),
    "assign": (f"{_OLD}/experiments/assign", f"{_NEW}/offers/assign"),
    "shown": (f"{_OLD}/experiments/paywall-shown", f"{_NEW}/offers/shown"),
}

_APP_ID = "481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d"
_API_TOKEN = "broadapps-outgoing-bearer-secret"  # noqa: S105 - test-only static secret
_API_BASE = "https://pay.broadapps.dev/api/v1"
_TOKEN_CODE = "100_tokens_9.99"
_TOKEN_CREDITS = 100
_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"

_CHECKOUT_BODY = {"productId": "week_6.99_nottrial", "customerEmail": "user@example.com"}
_EXP_BODY = {"experimentCode": "exp_a", "segmentCode": "b", "placement": "onbording"}
_LINK_OK = {
    "payment_id": "e3d7ffe4-0000-0000-0000-000000000000",
    "payment_url": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc",
    "status": "pending",
    "expires_at": None,
}
_ASSIGN_OK = {
    "segment": {"code": "b", "is_control": False},
    "requested_segment_matches": True,
    "created": True,
}


# ------------------------------ fakes ------------------------------


class _Resp:
    def __init__(self, status_code: int, data: Any = None) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> Any:
        return self._data


class _Upstream:
    """Scripted broadapps: ``(method, url-suffix) -> (status, json)``; records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {}
        self.calls: list[tuple[str, str]] = []

    def on(self, method: str, suffix: str, status: int, data: Any = None) -> None:
        self.routes[(method, suffix)] = (status, data)

    def _answer(self, method: str, url: str) -> _Resp:
        self.calls.append((method, url))
        for (m, suffix), (status, data) in self.routes.items():
            if m == method and url.endswith(suffix):
                return _Resp(status, data)
        raise AssertionError(f"unscripted upstream call {method} {url}")


def _fake_httpx(up: _Upstream) -> SimpleNamespace:
    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def get(self, url: str, **_k: Any) -> _Resp:
            return up._answer("GET", url)

        async def post(self, url: str, **_k: Any) -> _Resp:
            return up._answer("POST", url)

    return SimpleNamespace(
        AsyncClient=_Client,
        TimeoutException=_httpx.TimeoutException,
        RequestError=_httpx.RequestError,
        ConnectError=_httpx.ConnectError,
    )


class _Buckets:
    """In-process stand-in for the Redis sliding window: counts hits per REAL limiter key."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = defaultdict(int)
        self.limit: int | None = None  # None = unlimited

    async def allow(self, _client: Any, key: str, limit: int, window_seconds: int) -> bool:
        self.hits[key] += 1
        cap = self.limit if self.limit is not None else limit
        return self.hits[key] <= cap


class _Verify:
    def __init__(self) -> None:
        self.payments: list[dict[str, Any]] = []

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        return [dict(p) for p in self.payments]


def _payment(payment_id: str = "pay-1") -> dict[str, Any]:
    return {
        "payment_id": payment_id,
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": _TOKEN_CODE, "payment_type": "one_time"},
    }


def _webhook_body(account_id: str = _UID_UPPER) -> bytes:
    return json.dumps(
        {
            "Status": "Completed",
            "OperationType": "Payment",
            "Amount": 3990,
            "Currency": "RUB",
            "AccountId": account_id,
        }
    ).encode()


# ------------------------------ fixtures ------------------------------


@pytest.fixture
def upstream() -> _Upstream:
    return _Upstream()


@pytest.fixture
def buckets() -> _Buckets:
    return _Buckets()


@pytest.fixture
def verify() -> _Verify:
    return _Verify()


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
    buckets: _Buckets,
    verify: _Verify,
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.api_gateway import rate_limit
    from app.billing_cloudpayments import checkout as checkout_mod
    from app.billing_cloudpayments import experiments as experiments_mod
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", _APP_ID)
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", _API_TOKEN)
    monkeypatch.setenv("CLOUDPAYMENTS_API_BASE", _API_BASE)
    monkeypatch.setenv("TOKEN_PRODUCTS", json.dumps({_TOKEN_CODE: _TOKEN_CREDITS}))
    get_settings.cache_clear()

    fake = _fake_httpx(upstream)
    monkeypatch.setattr(checkout_mod, "httpx", fake)
    monkeypatch.setattr(experiments_mod, "httpx", fake)
    monkeypatch.setattr(rate_limit, "_allow", buckets.allow)
    monkeypatch.setattr(deps, "get_cloudpayments_verify_client", lambda: verify)

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    get_settings.cache_clear()


def _norm(resp: _httpx.Response) -> tuple[int, Any]:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        body = {**body, "error": {k: v for k, v in body["error"].items() if k != "requestId"}}
    return resp.status_code, body


async def _both(
    client: AsyncClient, pair: str, *, uid: uuid.UUID | None = None, **kwargs: Any
) -> tuple[int, Any]:
    """Send the same request to both paths of ``pair``; assert identical status + body."""
    old, new = PAIRS[pair]
    base_headers = dict(kwargs.pop("headers", {}) or {})
    results = []
    for url in (old, new):
        headers = dict(base_headers)
        if uid is not None:
            headers.update(auth_headers(uid))
        results.append(_norm(await client.post(url, headers=headers, **kwargs)))
    assert results[0] == results[1], f"{pair}: old={results[0]} new={results[1]}"
    return results[0]


def _unconfigure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", "")
    get_settings.cache_clear()


# ========================= parity: /checkout <-> /v1/web/session =========================


@pytest.mark.parametrize("scenario", ["success", "401", "503", "422", "502"])
async def test_alias_parity_checkout_session(
    client: AsyncClient, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    uid: uuid.UUID | None = uuid.uuid4()
    body: dict[str, Any] = dict(_CHECKOUT_BODY)
    upstream.on("POST", "/payments/link", 201, _LINK_OK)
    expected = 200
    if scenario == "401":
        uid, expected = None, 401
    elif scenario == "503":
        _unconfigure(monkeypatch)
        expected = 503
    elif scenario == "422":
        body["productId"] = "totally-not-a-product"
        expected = 422
    elif scenario == "502":
        upstream.on("POST", "/payments/link", 500, {"detail": "boom"})
        expected = 502
    status, resp = await _both(client, "checkout", uid=uid, json=body)
    assert status == expected, resp
    if scenario == "success":
        assert resp["paymentUrl"] == _LINK_OK["payment_url"]
    if scenario == "503":
        assert resp["error"]["code"] == "cloudpayments_checkout_not_configured"


# ========================= parity: /cancel <-> /v1/web/cancel =========================


@pytest.mark.parametrize("scenario", ["success", "no_active", "401", "503", "502"])
async def test_alias_parity_cancel(
    client: AsyncClient, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    uid: uuid.UUID | None = uuid.uuid4()
    upstream.on(
        "GET", "/subscriptions", 200, {"data": [{"subscription_id": "s1", "status": "active"}]}
    )
    upstream.on(
        "POST", "/subscriptions/s1/cancel", 200, {"status": "active", "already_canceled": False}
    )
    expected = 200
    if scenario == "no_active":
        upstream.on("GET", "/subscriptions", 200, {"data": []})
    elif scenario == "401":
        uid, expected = None, 401
    elif scenario == "503":
        _unconfigure(monkeypatch)
        expected = 503
    elif scenario == "502":
        upstream.on("GET", "/subscriptions", 500, {})
        expected = 502
    status, resp = await _both(client, "cancel", uid=uid)
    assert status == expected, resp
    if scenario == "success":
        assert resp["canceled"] is True
    if scenario == "no_active":
        assert resp["canceled"] is False
    if scenario == "502":
        assert resp["error"]["code"] == "upstream_error"


async def test_alias_parity_cancel_not_found_with_local_row_parity_only(
    client: AsyncClient,
    upstream: _Upstream,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # TD-064: the will_renew outcome here is NOT fixed by a test — only pair parity is checked.
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active")
    upstream.on("GET", "/subscriptions", 200, {"data": []})
    status, resp = await _both(client, "cancel", uid=uid)
    assert status == 200 and resp["canceled"] is False


# ========================= parity: experiments =========================


@pytest.mark.parametrize("scenario", ["success", "401", "503", "422", "502"])
async def test_alias_parity_offers_assign(
    client: AsyncClient, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    uid: uuid.UUID | None = uuid.uuid4()
    body: dict[str, Any] = dict(_EXP_BODY)
    upstream.on("POST", "/experiments/assignments", 200, _ASSIGN_OK)
    expected = 200
    if scenario == "401":
        uid, expected = None, 401
    elif scenario == "503":
        _unconfigure(monkeypatch)
        expected = 503
    elif scenario == "422":
        body["userId"] = str(uuid.uuid4())
        expected = 422
    elif scenario == "502":
        upstream.on("POST", "/experiments/assignments", 500, {})
        expected = 502
    status, resp = await _both(client, "assign", uid=uid, json=body)
    assert status == expected, resp
    if scenario == "success":
        assert resp["segment"]["code"] == "b"


@pytest.mark.parametrize("scenario", ["success", "401", "503", "422", "logged_false"])
async def test_alias_parity_offers_shown(
    client: AsyncClient, upstream: _Upstream, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    uid: uuid.UUID | None = uuid.uuid4()
    body: dict[str, Any] = dict(_EXP_BODY)
    upstream.on("POST", "/experiments/paywall-shown", 200, {})
    expected: tuple[int, Any] | None = (200, {"logged": True})
    if scenario == "401":
        uid, expected = None, None
    elif scenario == "503":
        _unconfigure(monkeypatch)
        expected = None
    elif scenario == "422":
        body["deviceId"] = "x"
        expected = None
    elif scenario == "logged_false":
        upstream.on("POST", "/experiments/paywall-shown", 500, {})
        expected = (200, {"logged": False})
    status, resp = await _both(client, "shown", uid=uid, json=body)
    if expected is not None:
        assert (status, resp) == expected
    else:
        assert status == {"401": 401, "503": 503, "422": 422}[scenario], resp


# ========================= webhook: /webhook <-> /v1/web/events =========================


@pytest.mark.parametrize("scenario", ["malformed", "empty", "misconfigured"])
async def test_alias_parity_webhook_events(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    content = {"malformed": b"{not json", "empty": b"", "misconfigured": _webhook_body()}[scenario]
    if scenario == "misconfigured":
        monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "")
        get_settings.cache_clear()
    status, resp = await _both(client, "webhook", content=content)
    if scenario == "misconfigured":
        assert status == 500
        assert resp["error"]["code"] == "cloudpayments_webhook_misconfigured"
    else:
        assert (status, resp) == (200, {"code": 0})


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int | None:
    async with maker() as s:
        bal = await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
        return None if bal is None else int(bal)


async def test_web_events_without_authorization_is_not_401_and_credits(
    client: AsyncClient, verify: _Verify, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    verify.payments = [_payment()]
    r = await client.post(PAIRS["webhook"][1], content=_webhook_body())
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert await _balance(db_sessionmaker, uid) == _TOKEN_CREDITS


async def test_web_events_forged_callback_credits_nothing(
    client: AsyncClient, verify: _Verify, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    verify.payments = []  # the provider confirms NO payment: the callback is forged
    r = await client.post(PAIRS["webhook"][1], content=_webhook_body())
    assert r.status_code == 200 and r.json() == {"code": 0}
    assert not await _balance(db_sessionmaker, uid)


async def test_webhook_dedup_across_old_then_new_path_credits_once(
    client: AsyncClient, verify: _Verify, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    verify.payments = [_payment("pay-dedup")]
    old, new = PAIRS["webhook"]
    assert (await client.post(old, content=_webhook_body())).status_code == 200
    assert await _balance(db_sessionmaker, uid) == _TOKEN_CREDITS
    assert (await client.post(new, content=_webhook_body())).status_code == 200
    assert await _balance(db_sessionmaker, uid) == _TOKEN_CREDITS


# ========================= one bucket per pair =========================


@pytest.mark.parametrize(
    ("pair", "key_prefix"),
    [
        ("webhook", "rl:cpwebhook:"),
        ("checkout", "rl:other:"),
        ("cancel", "rl:other:"),
        ("assign", "rl:experiments:"),
        ("shown", "rl:experiments:"),
    ],
)
async def test_alternating_old_and_new_path_drain_one_bucket(
    client: AsyncClient, upstream: _Upstream, buckets: _Buckets, pair: str, key_prefix: str
) -> None:
    upstream.on("POST", "/payments/link", 201, _LINK_OK)
    upstream.on("GET", "/subscriptions", 200, {"data": []})
    upstream.on("POST", "/experiments/assignments", 200, _ASSIGN_OK)
    upstream.on("POST", "/experiments/paywall-shown", 200, {})
    buckets.limit = 2
    uid = uuid.uuid4()
    headers = {} if pair == "webhook" else auth_headers(uid)
    kwargs: dict[str, Any] = {"headers": headers}
    if pair == "webhook":
        kwargs["content"] = b""
    elif pair == "checkout":
        kwargs["json"] = _CHECKOUT_BODY
    elif pair in ("assign", "shown"):
        kwargs["json"] = _EXP_BODY
    old, new = PAIRS[pair]
    codes = [(await client.post(url, **kwargs)).status_code for url in (old, new, old, new)]
    assert codes == [200, 200, 429, 429], codes
    keys = [k for k in buckets.hits if k.startswith(key_prefix)]
    assert len(keys) == 1, buckets.hits
    assert buckets.hits[keys[0]] == 4


# ========================= OpenAPI =========================


async def test_openapi_hides_web_aliases_and_keeps_originals(client: AsyncClient) -> None:
    from app.api_gateway.routers.billing_cloudpayments import _ROUTES

    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    assert not [p for p in paths if p.startswith("/v1/web/") or p == "/v1/web"]
    assert "/v1/web/" not in json.dumps(schema)
    for spec in _ROUTES:
        op = paths[f"{_OLD}{spec.billing_path}"]
        assert set(op) == {"post"}
        assert op["post"]["tags"] == ["Billing (CloudPayments)"]
        assert op["post"]["summary"] == spec.summary


# ========================= /cancel semantics (ADR-110 §8) =========================


async def test_cancel_success_sets_will_renew_false_and_keeps_status_expiry(
    client: AsyncClient,
    upstream: _Upstream,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    upstream.on(
        "GET", "/subscriptions", 200, {"data": [{"subscription_id": "s1", "status": "active"}]}
    )
    upstream.on("POST", "/subscriptions/s1/cancel", 200, {"status": "active"})
    for url in PAIRS["cancel"]:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active")
            before = (
                await s.execute(
                    text("SELECT status, expires_at FROM subscriptions WHERE user_id=:u"),
                    {"u": str(uid)},
                )
            ).one()
        r = await client.post(url, headers=auth_headers(uid))
        assert r.status_code == 200, r.text
        assert r.json()["canceled"] is True and r.json()["willRenew"] is False
        async with db_sessionmaker() as s:
            after = (
                await s.execute(
                    text(
                        "SELECT status, expires_at, will_renew FROM subscriptions WHERE user_id=:u"
                    ),
                    {"u": str(uid)},
                )
            ).one()
        assert (after.status, after.expires_at) == (before.status, before.expires_at)
        assert after.will_renew is False
