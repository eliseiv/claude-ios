"""Integration: ADR-113 §2 — ``paymentUrl`` of a broadapps payment page moves onto SERVICE_DOMAIN.

Cases 1–10 of ADR-113 §8, each on BOTH paths of the ADR-110 pair
(``/v1/billing/cloudpayments/checkout`` and ``/v1/web/session``). Full HTTP path: real JWT auth +
lazy provisioning against the shared testcontainers Postgres; the single outgoing broadapps
``POST /payments/link`` is faked at the ``httpx`` name inside ``checkout.py`` (no network). Both
criteria of the predicate are checked: rewritten when all four conditions hold; untouched (with the
``cloudpayments_pay_page_rewrite_skipped`` record and the right ``reason`` — INFO for ``disabled``,
WARNING for ``path_not_proxied`` / ``service_domain_unset``) when (a), (c) or (d) is false;
untouched WITHOUT that record when the host is not the upstream host (YooMoney, T-Bank,
look-alike hosts).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers

_OLD = "/v1/billing/cloudpayments/checkout"
_NEW = "/v1/web/session"
PATHS = (_OLD, _NEW)

_APP_ID = "481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d"
_API_TOKEN = "broadapps-outgoing-bearer-secret"  # noqa: S105 - test-only static secret
_API_BASE = "https://pay.broadapps.dev/api/v1"
_DOMAIN = "shop.example"
_PRODUCT = "week_6.99_nottrial"
_PAY_UUID = "3f2c9a1e-8b7d-4c6e-9a0f-1b2c3d4e5f60"

_BROADAPPS_LINK = f"https://pay.broadapps.dev/cp/pay/{_PAY_UUID}?a=b"
_REWRITTEN = f"https://{_DOMAIN}/cp/pay/{_PAY_UUID}?a=b"

_SKIP_EVENT = "cloudpayments_pay_page_rewrite_skipped"
_OUTCOME_EVENT = "cloudpayments_checkout_outcome"


class _Upstream:
    """Scripts the faked broadapps ``POST /payments/link``."""

    def __init__(self) -> None:
        self.payment_url = _BROADAPPS_LINK
        self.calls = 0

    def body(self) -> dict[str, Any]:
        return {
            "payment_id": "e3d7ffe4-0000-0000-0000-000000000000",
            "payment_url": self.payment_url,
            "status": "pending",
            "expires_at": None,
        }


class _Resp:
    def __init__(self, data: Any) -> None:
        self.status_code = 201
        self._data = data

    def json(self) -> Any:
        return self._data


def _fake_httpx(upstream: _Upstream) -> SimpleNamespace:
    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def post(self, _url: str, **_k: Any) -> _Resp:
            upstream.calls += 1
            return _Resp(upstream.body())

    return SimpleNamespace(
        AsyncClient=_Client,
        TimeoutException=_httpx.TimeoutException,
        RequestError=_httpx.RequestError,
        ConnectError=_httpx.ConnectError,
    )


@pytest.fixture
def upstream() -> _Upstream:
    return _Upstream()


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    upstream: _Upstream,
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.api_gateway.routers import billing_cloudpayments as cp_router
    from app.billing_cloudpayments import checkout as checkout_mod
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", _APP_ID)
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", _API_TOKEN)
    monkeypatch.setenv("CLOUDPAYMENTS_API_BASE", _API_BASE)
    monkeypatch.setenv("SERVICE_DOMAIN", _DOMAIN)
    monkeypatch.setenv("CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(checkout_mod, "httpx", _fake_httpx(upstream))

    async def _allow(*, user_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(cp_router, "enforce_other_limits", _allow)

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


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    # The in-process Alembic migration disables loggers created before it ran
    # (disable_existing_loggers); re-enable the one under test (harness artifact only).
    logging.getLogger("app.billing_cloudpayments.checkout").disabled = False
    caplog.set_level(logging.DEBUG)
    return caplog


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def _checkout(client: AsyncClient, path: str, uid: uuid.UUID | None = None) -> Any:
    resp = await client.post(
        path,
        json={"productId": _PRODUCT, "customerEmail": "user@example.com"},
        headers=auth_headers(uid or uuid.uuid4()),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == event]


def _outcome_rewritten(caplog: pytest.LogCaptureFixture) -> bool:
    records = _records(caplog, _OUTCOME_EVENT)
    assert len(records) == 1
    fields = records[0].extra_fields  # type: ignore[attr-defined]
    assert fields["result"] == "created"
    value = fields["paymentUrlRewritten"]
    assert isinstance(value, bool)
    return value


# ---------------------------------- case 1 ----------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_case01_broadapps_link_is_rewritten_byte_for_byte_and_logged(
    client: AsyncClient, upstream: _Upstream, logs: pytest.LogCaptureFixture, path: str
) -> None:
    body = await _checkout(client, path)

    assert body["paymentUrl"] == _REWRITTEN
    assert _outcome_rewritten(logs) is True
    assert _records(logs, _SKIP_EVENT) == []
    # paymentUrl itself is never logged (neither the original nor the rewritten link).
    for record in _records(logs, _OUTCOME_EVENT):
        assert _PAY_UUID not in str(record.extra_fields)  # type: ignore[attr-defined]


@pytest.mark.parametrize("path", PATHS)
async def test_case01_query_and_fragment_are_carried_verbatim(
    client: AsyncClient, upstream: _Upstream, path: str
) -> None:
    upstream.payment_url = f"https://pay.broadapps.dev/cp/pay/{_PAY_UUID}/s~1.x?q=%2F%20a&b=#frag"

    body = await _checkout(client, path)

    assert body["paymentUrl"] == f"https://{_DOMAIN}/cp/pay/{_PAY_UUID}/s~1.x?q=%2F%20a&b=#frag"


# ---------------------------------- case 2 ----------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_case02_upstream_host_in_other_case_is_rewritten(
    client: AsyncClient, upstream: _Upstream, logs: pytest.LogCaptureFixture, path: str
) -> None:
    upstream.payment_url = f"https://PAY.BroadApps.dev/cp/pay/{_PAY_UUID}?a=b"

    body = await _checkout(client, path)

    assert body["paymentUrl"] == _REWRITTEN
    assert _outcome_rewritten(logs) is True


# ---------------------------------- case 3 ----------------------------------


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "foreign",
    [
        "https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc",
        "https://pay.tbank-online.com/Ab3dE9",
    ],
)
async def test_case03_yoomoney_and_tbank_untouched_without_warning(
    client: AsyncClient,
    upstream: _Upstream,
    logs: pytest.LogCaptureFixture,
    path: str,
    foreign: str,
) -> None:
    upstream.payment_url = foreign

    body = await _checkout(client, path)

    assert body["paymentUrl"] == foreign
    assert _outcome_rewritten(logs) is False
    assert _records(logs, _SKIP_EVENT) == []


# ------------------------------ cases 4, 5, 6 ------------------------------


def _assert_one_skip(
    caplog: pytest.LogCaptureFixture, reason: str, uid: uuid.UUID, level: int
) -> None:
    records = _records(caplog, _SKIP_EVENT)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == level
    fields = record.extra_fields  # type: ignore[attr-defined]
    assert fields["reason"] == reason
    assert fields["userId"] == str(uid)
    assert fields["productId"] == _PRODUCT
    assert set(fields) <= {"reason", "userId", "productId", "requestId"}
    assert _PAY_UUID not in str(fields)


@pytest.mark.parametrize("path", PATHS)
async def test_case04_flag_off_leaves_link_and_logs_disabled_at_info(
    client: AsyncClient,
    upstream: _Upstream,
    logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _set(monkeypatch, "CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "false")
    uid = uuid.uuid4()

    body = await _checkout(client, path, uid)

    assert body["paymentUrl"] == _BROADAPPS_LINK
    assert _outcome_rewritten(logs) is False
    _assert_one_skip(logs, "disabled", uid, logging.INFO)


@pytest.mark.parametrize("path", PATHS)
async def test_case05_upstream_host_outside_cp_pay_warns_path_not_proxied(
    client: AsyncClient, upstream: _Upstream, logs: pytest.LogCaptureFixture, path: str
) -> None:
    upstream.payment_url = f"https://pay.broadapps.dev/checkout/{_PAY_UUID}"
    uid = uuid.uuid4()

    body = await _checkout(client, path, uid)

    assert body["paymentUrl"] == upstream.payment_url
    assert _outcome_rewritten(logs) is False
    _assert_one_skip(logs, "path_not_proxied", uid, logging.WARNING)


@pytest.mark.parametrize("path", PATHS)
async def test_case06_empty_service_domain_warns_service_domain_unset(
    client: AsyncClient,
    upstream: _Upstream,
    logs: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    _set(monkeypatch, "SERVICE_DOMAIN", "")
    uid = uuid.uuid4()

    body = await _checkout(client, path, uid)

    assert body["paymentUrl"] == _BROADAPPS_LINK
    assert _outcome_rewritten(logs) is False
    _assert_one_skip(logs, "service_domain_unset", uid, logging.WARNING)


# ---------------------------------- case 7 ----------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_case07_service_domain_with_scheme_and_slash_gives_one_scheme(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _set(monkeypatch, "SERVICE_DOMAIN", "https://example.shop/")

    body = await _checkout(client, path)

    url = body["paymentUrl"]
    assert url == f"https://example.shop/cp/pay/{_PAY_UUID}?a=b"
    assert url.count("https://") == 1
    assert url.count("//") == 1  # only the scheme separator, none before the path


# ---------------------------------- case 8 ----------------------------------


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "lookalike",
    [
        f"https://xpay.broadapps.dev/cp/pay/{_PAY_UUID}",
        f"https://pay.broadapps.dev.evil.test/cp/pay/{_PAY_UUID}",
    ],
)
async def test_case08_lookalike_hosts_are_not_rewritten(
    client: AsyncClient,
    upstream: _Upstream,
    logs: pytest.LogCaptureFixture,
    path: str,
    lookalike: str,
) -> None:
    upstream.payment_url = lookalike

    body = await _checkout(client, path)

    assert body["paymentUrl"] == lookalike
    assert _outcome_rewritten(logs) is False
    # Not the upstream host => the normal pass-through path, no warning either.
    assert _records(logs, _SKIP_EVENT) == []


# ---------------------------------- case 9 ----------------------------------


_PAIR_SCENARIOS: dict[str, tuple[str, dict[str, str]]] = {
    "case1": (_BROADAPPS_LINK, {}),
    "case2": (f"https://PAY.BroadApps.dev/cp/pay/{_PAY_UUID}?a=b", {}),
    "case3_yoomoney": ("https://yoomoney.ru/checkout/payments/v2/contract?orderId=abc", {}),
    "case3_tbank": ("https://pay.tbank-online.com/Ab3dE9", {}),
    "case4": (_BROADAPPS_LINK, {"CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED": "false"}),
    "case5": (f"https://pay.broadapps.dev/checkout/{_PAY_UUID}", {}),
    "case6": (_BROADAPPS_LINK, {"SERVICE_DOMAIN": ""}),
}


@pytest.mark.parametrize("scenario", sorted(_PAIR_SCENARIOS))
async def test_case09_both_paths_of_the_pair_answer_identically(
    client: AsyncClient,
    upstream: _Upstream,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    link, env = _PAIR_SCENARIOS[scenario]
    upstream.payment_url = link
    for name, value in env.items():
        _set(monkeypatch, name, value)
    uid = uuid.uuid4()

    old = await _checkout(client, _OLD, uid)
    new = await _checkout(client, _NEW, uid)

    assert old == new
    assert upstream.calls == 2


# ---------------------------------- case 10 ----------------------------------


@pytest.mark.parametrize("path", PATHS)
async def test_case10_checkout_call_site_is_the_only_producer_of_the_rewrite(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Replacing the call in ``checkout.py`` by a pass-through leaves the link on broadapps.

    Wiring proof for case 1 (ADR-113 §8 п. 10): the rewrite seen by the client is produced by the
    ``rewrite_payment_url`` call inside ``CloudPaymentsCheckoutClient`` and nowhere else, so
    removing that call is observable on both paths. The source-level mutation (the call deleted)
    is reported separately in the QA report.
    """
    from app.billing_cloudpayments import checkout as checkout_mod
    from app.billing_cloudpayments.pay_page import PaymentUrlRewrite

    def _passthrough(payment_url: str, _settings: Any) -> PaymentUrlRewrite:
        return PaymentUrlRewrite(url=payment_url, rewritten=False, skip_reason=None)

    monkeypatch.setattr(checkout_mod, "rewrite_payment_url", _passthrough)

    body = await _checkout(client, path)

    assert body["paymentUrl"] == _BROADAPPS_LINK
