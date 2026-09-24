"""Unit: ADR-113 §4 / §8 case 24 — the access log never carries the payment-page uuid.

The record is emitted through the REAL ``uvicorn.access`` logger in the exact form the server
writes (``'%s - "%s %s HTTP/%s" %d'``, path with query as the third argument), with the REAL
``configure_logging`` wiring the filter. One test per invariant, so each mutation of the filter
fails exactly its own test:

* ``/cp/pay/<uuid>`` WITHOUT a query is masked (the early "no ``?`` -> untouched" exit must not
  apply to this prefix);
* ``/cp/pay/<uuid>?x=1`` is masked — neither uuid nor query;
* ``/payment/return?x=1`` loses its query only;
* every other path (``/main.js``, API paths, look-alike prefixes) is byte-for-byte.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

_PAY_UUID = "3f2c9a1e-8b7d-4c6e-9a0f-1b2c3d4e5f60"


def _access_line(method: str, path: str, status: int = 200) -> str:
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    access = logging.getLogger("uvicorn.access")
    handler = _Capture()
    access.addHandler(handler)
    was_disabled, level = access.disabled, access.level
    access.disabled = False
    access.setLevel(logging.INFO)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5000", method, path, "1.1", status)
    finally:
        access.removeHandler(handler)
        access.disabled, access.level = was_disabled, level
    # The record is rewritten, never dropped.
    assert len(captured) == 1
    return captured[0]


@pytest.fixture(autouse=True)
def configured_logging() -> Iterator[None]:
    """Run the REAL ``configure_logging`` (it wires the filter); restore root and filters after."""
    from app.observability.logging import AccessLogQueryRedactionFilter, configure_logging

    access = logging.getLogger("uvicorn.access")
    before_filters = list(access.filters)
    for item in list(access.filters):
        if isinstance(item, AccessLogQueryRedactionFilter):
            access.removeFilter(item)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configure_logging("INFO")
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        access.filters[:] = before_filters


def test_case24_pay_page_without_query_is_masked() -> None:
    line = _access_line("GET", f"/cp/pay/{_PAY_UUID}")

    assert _PAY_UUID not in line
    assert line == '10.0.0.1:5000 - "GET /cp/pay/* HTTP/1.1" 200'


def test_case24_pay_page_subpath_without_query_is_masked() -> None:
    line = _access_line("POST", f"/cp/pay/{_PAY_UUID}/confirm", 302)

    assert _PAY_UUID not in line
    assert line == '10.0.0.1:5000 - "POST /cp/pay/* HTTP/1.1" 302'


def test_case24_pay_page_with_query_masks_uuid_and_query() -> None:
    line = _access_line("GET", f"/cp/pay/{_PAY_UUID}?x=1")

    assert _PAY_UUID not in line
    assert "x=1" not in line
    assert line == '10.0.0.1:5000 - "GET /cp/pay/* HTTP/1.1" 200'


def test_case24_payment_return_loses_only_its_query() -> None:
    line = _access_line("GET", "/payment/return?x=1&TransactionId=42")

    assert line == '10.0.0.1:5000 - "GET /payment/return HTTP/1.1" 200'


@pytest.mark.parametrize(
    "path",
    [
        "/main.js",
        "/main.js?v=3",
        "/main.css",
        "/payment/return",
        "/v1/chats?limit=5&kind=image",
        "/v1/billing/cloudpayments/checkout",
        "/cp/payx/abc?x=1",
        "/cp/pay",
        "/payment/returned?x=1",
    ],
)
def test_case24_other_paths_are_byte_for_byte(path: str) -> None:
    line = _access_line("GET", path, 404)

    assert line == f'10.0.0.1:5000 - "GET {path} HTTP/1.1" 404'
