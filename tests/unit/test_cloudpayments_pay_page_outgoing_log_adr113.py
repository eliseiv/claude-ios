"""Unit: ADR-113 §4 — httpx's outgoing ``HTTP Request:`` line never carries the payment-page uuid.

The records are produced by httpx ITSELF (a real ``httpx.AsyncClient`` over ``httpx.MockTransport``
— httpx logs the line in the client, not in the transport, so no network is needed), with the REAL
``configure_logging`` wiring the filter. One end-to-end test drives the proxy route of the real app
so the record comes from the exact producer that leaked on the instance. One test per invariant:

* ``/cp/pay/<uuid>`` without and with a query -> ``/cp/pay/*``, no uuid, no query;
* ``/payment/return?x=1`` -> no query;
* any other outgoing URL -> line byte-for-byte, level INFO kept;
* the record is rewritten, never dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.config import get_settings

_PAY_UUID = "3f2c9a1e-8b7d-4c6e-9a0f-1b2c3d4e5f60"
_UPSTREAM = "https://pay.broadapps.dev"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


@pytest.fixture
def httpx_log() -> Iterator[_Capture]:
    """REAL ``configure_logging`` + a capture handler on the ``httpx`` logger; restored after."""
    from app.observability.logging import (
        AccessLogQueryRedactionFilter,
        OutgoingRequestLogRedactionFilter,
        configure_logging,
    )

    outgoing = logging.getLogger("httpx")
    access = logging.getLogger("uvicorn.access")
    saved = (list(outgoing.filters), list(access.filters), outgoing.level, outgoing.disabled)
    for item in list(outgoing.filters):
        if isinstance(item, OutgoingRequestLogRedactionFilter):
            outgoing.removeFilter(item)
    for item in list(access.filters):
        if isinstance(item, AccessLogQueryRedactionFilter):
            access.removeFilter(item)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging("INFO")
    capture = _Capture()
    outgoing.addHandler(capture)
    outgoing.setLevel(logging.INFO)
    outgoing.disabled = False
    try:
        yield capture
    finally:
        outgoing.removeHandler(capture)
        root.handlers[:] = handlers
        root.setLevel(level)
        outgoing.filters[:], access.filters[:], outgoing.level, outgoing.disabled = saved


async def _get(url: str) -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, content=b"ok"))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get(url)


def _only(capture: _Capture) -> tuple[int, str]:
    lines = [r for r in capture.records if r[1].startswith("HTTP Request:")]
    assert len(lines) == 1, capture.records  # rewritten, never dropped
    return lines[0]


async def test_pay_page_url_without_query_is_masked(httpx_log: _Capture) -> None:
    await _get(f"{_UPSTREAM}/cp/pay/{_PAY_UUID}")

    level, line = _only(httpx_log)
    assert _PAY_UUID not in line
    assert line == f'HTTP Request: GET {_UPSTREAM}/cp/pay/* "HTTP/1.1 200 OK"'
    assert level == logging.INFO


async def test_pay_page_url_with_query_masks_uuid_and_query(httpx_log: _Capture) -> None:
    await _get(f"{_UPSTREAM}/cp/pay/{_PAY_UUID}/confirm?x=1&s=QVAL")

    _level, line = _only(httpx_log)
    assert _PAY_UUID not in line
    assert "QVAL" not in line
    assert line == f'HTTP Request: GET {_UPSTREAM}/cp/pay/* "HTTP/1.1 200 OK"'


async def test_payment_return_loses_only_its_query(httpx_log: _Capture) -> None:
    await _get(f"{_UPSTREAM}/payment/return?x=1&TransactionId=42")

    _level, line = _only(httpx_log)
    assert line == f'HTTP Request: GET {_UPSTREAM}/payment/return "HTTP/1.1 200 OK"'


@pytest.mark.parametrize(
    "url",
    [
        f"{_UPSTREAM}/api/v1/payments/link",
        f"{_UPSTREAM}/main.js?v=3",
        f"{_UPSTREAM}/payment/return",
        f"{_UPSTREAM}/cp/payx/{_PAY_UUID}",
        "https://api.example.test/v1/things?limit=5",
    ],
)
async def test_other_outgoing_urls_are_byte_for_byte_at_info(httpx_log: _Capture, url: str) -> None:
    await _get(url)

    level, line = _only(httpx_log)
    assert line == f'HTTP Request: GET {url} "HTTP/1.1 200 OK"'
    assert level == logging.INFO


async def test_proxy_route_end_to_end_logs_no_uuid(
    httpx_log: _Capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The producer that leaked on the instance: PayPageProxy's own outgoing call."""
    from app.api_gateway import rate_limit
    from app.billing_cloudpayments import pay_page as pay_page_mod
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_APP_ID", "481d10b0-c7ee-4eeb-8618-d3a6cd7f7b9d")
    monkeypatch.setenv("CLOUDPAYMENTS_API_TOKEN", "broadapps-outgoing-bearer-secret")
    monkeypatch.setenv("CLOUDPAYMENTS_API_BASE", f"{_UPSTREAM}/api/v1")
    monkeypatch.setenv("SERVICE_DOMAIN", "shop.example")
    monkeypatch.setenv("CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED", "true")
    get_settings.cache_clear()

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda _r: httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"ok")
        )
        return httpx.AsyncClient(*args, transport=transport, **kwargs)

    monkeypatch.setattr(
        pay_page_mod,
        "httpx",
        SimpleNamespace(
            AsyncClient=_client,
            URL=httpx.URL,
            TimeoutException=httpx.TimeoutException,
            RequestError=httpx.RequestError,
            InvalidURL=httpx.InvalidURL,
        ),
    )

    async def _allow(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(rate_limit, "_allow", _allow)
    try:
        app = create_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://shop.example"
        ) as client:
            resp = await client.get(f"/cp/pay/{_PAY_UUID}")
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 200
    upstream_lines = [line for _lvl, line in httpx_log.records if "pay.broadapps.dev" in line]
    assert upstream_lines == [f'HTTP Request: GET {_UPSTREAM}/cp/pay/* "HTTP/1.1 200 OK"']
    assert all(_PAY_UUID not in line for _lvl, line in httpx_log.records)
