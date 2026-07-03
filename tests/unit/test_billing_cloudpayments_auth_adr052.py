"""Unit: ADR-052 — lenient ``Authorization`` parsing + safe 401 diagnostic log (CP webhook).

Drives the isolated per-route verifier ``require_cloudpayments_webhook`` and its pure helpers
``_extract_webhook_credential`` / ``_auth_scheme_label`` DIRECTLY, with a fake ``Request`` carrying
only the raw headers the verifier reads. Fully hermetic: NO DB, NO network, NO LLM; the secret is
injected via ``CLOUDPAYMENTS_WEBHOOK_TOKEN`` + ``get_settings.cache_clear()`` (restored afterwards),
so the suite passes with placeholder provider keys.

Invariants under test (ADR-052 / billing-cloudpayments/09-testing §Авторизация):
- Valid secret is accepted in EVERY wrapping: ``Bearer``/``Token`` (case-insensitive word) and raw.
- Unrecognised scheme (``Basic``), wrong token (any form) and a missing header all -> 401.
- Unset secret -> 500 misconfigured BEFORE any extraction/compare (fail-closed), NOT 401.
- On EVERY 401: exactly one WARNING ``cloudpayments_webhook_auth_denied`` whose fields are only the
  allowlist (``matched=False`` + scheme WORD + present header NAMES) — never the token/secret value
  or the full ``Authorization`` header. On success and on 500 the record is NOT emitted.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from starlette.datastructures import Headers

from app.billing_cloudpayments.auth import (
    CloudPaymentsWebhookMisconfiguredError,
    _auth_scheme_label,
    _extract_webhook_credential,
    require_cloudpayments_webhook,
)
from app.config import get_settings
from app.errors import UnauthorizedError
from app.observability.logging import JsonFormatter

_SECRET = "cloudpayments-webhook-secret-value-64chars-XXXXXXXXXXXXXXXXXXXXXXXX"
_MESSAGE = "cloudpayments_webhook_auth_denied"
_LOGGER = "app.billing_cloudpayments.auth"


def _request(headers: dict[str, str]) -> Request:
    """A minimal stand-in for ``Request`` exposing only ``.headers`` (all the verifier reads)."""
    return cast(Request, SimpleNamespace(headers=Headers(headers)))


@pytest.fixture
def _secret_set(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Configure the webhook secret for the verifier (lru-cached settings; restored on teardown)."""
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", _SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _denied_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.msg == _MESSAGE]


def _rendered(record: logging.LogRecord) -> dict[str, Any]:
    """Render through the REAL ``JsonFormatter`` — the exact line operators would see."""
    return cast(dict[str, Any], json.loads(JsonFormatter().format(record)))


# ============================ §1 Accepted formats (no raise) ============================


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {_SECRET}",
        f"bearer {_SECRET}",  # scheme word is case-insensitive
        f"Token {_SECRET}",
        f"token {_SECRET}",
        _SECRET,  # raw secret, no scheme
        f"  Bearer   {_SECRET}  ",  # surplus whitespace is trimmed on both sides
    ],
)
def test_valid_secret_accepted_in_every_wrapping(_secret_set: Any, header: str) -> None:
    # Returns None (authorised) and raises nothing.
    assert require_cloudpayments_webhook(_request({"authorization": header})) is None


# ============================ §1 Rejected -> 401 ============================


@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": f"Basic {_SECRET}"},  # unrecognised scheme => whole header != secret
        {"authorization": f"Bearer {_SECRET}-wrong"},  # wrong token, bearer form
        {"authorization": f"{_SECRET}-wrong"},  # wrong token, raw form
        {"authorization": "Bearer "},  # collapses to raw "Bearer" != secret -> fail-closed
        {},  # no Authorization header at all
    ],
)
def test_rejected_inputs_raise_401(_secret_set: Any, headers: dict[str, str]) -> None:
    with pytest.raises(UnauthorizedError):
        require_cloudpayments_webhook(_request(headers))


# ==================== §2 Fail-closed: unset secret -> 500 (not 401) ====================


def test_unset_secret_is_500_before_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", "")
    get_settings.cache_clear()
    try:
        # A syntactically valid header must NOT downgrade the misconfiguration to a 401.
        with pytest.raises(CloudPaymentsWebhookMisconfiguredError) as exc:
            require_cloudpayments_webhook(_request({"authorization": f"Bearer {_SECRET}"}))
        assert exc.value.status_code == 500
    finally:
        get_settings.cache_clear()


# ============================ §1 _extract_webhook_credential (pure) ============================


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),  # case-insensitive scheme word
        ("Token abc123", "abc123"),
        ("token abc123", "abc123"),
        ("abc123", "abc123"),  # single part -> whole trimmed header (raw token)
        ("  abc123  ", "abc123"),  # raw token, surplus whitespace trimmed
        ("  Bearer   abc123  ", "abc123"),  # scheme + value with surplus whitespace
        (None, None),
        ("", None),
        ("   ", None),  # blank after strip -> None
        ("Bearer ", "Bearer"),  # trailing space collapses to one word -> raw "Bearer" (won't match)
        ("Basic abc123", "Basic abc123"),  # unrecognised scheme -> whole header (won't match)
    ],
)
def test_extract_webhook_credential(raw: str | None, expected: str | None) -> None:
    assert _extract_webhook_credential(raw) == expected


# ============================ §3 _auth_scheme_label (pure, no value) ============================


@pytest.mark.parametrize(
    "raw, label",
    [
        (None, "none"),
        ("", "empty"),
        ("   ", "empty"),
        ("Bearer x", "bearer"),
        ("bearer x", "bearer"),
        ("Token x", "token"),
        ("Basic x", "basic"),
        ("abc123", "raw"),  # single part (raw token) -> "raw", value never surfaced
        ("  abc123  ", "raw"),
    ],
)
def test_auth_scheme_label_never_leaks_value(raw: str | None, label: str) -> None:
    result = _auth_scheme_label(raw)
    assert result == label
    # The token value is never the label.
    assert "abc123" not in result


# ============================ §3 Diagnostic log on 401 ============================


def test_denied_log_emitted_once_with_allowlist_fields(
    _secret_set: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # Migrations in other test modules can disable this module logger; re-enable defensively so the
    # WARNING is captured. Capture at DEBUG globally (logger-scoped capture misses structured logs).
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)

    wrong = f"{_SECRET}-wrong"
    with pytest.raises(UnauthorizedError):
        require_cloudpayments_webhook(
            _request({"authorization": f"Bearer {wrong}", "x-api-key": "secret-key-value"})
        )

    recs = _denied_records(caplog)
    assert len(recs) == 1, "exactly one auth_denied record per 401"
    rec = recs[0]
    assert rec.levelno == logging.WARNING

    fields = _rendered(rec)
    assert fields["message"] == _MESSAGE
    assert fields["matched"] is False
    assert fields["authScheme"] == "bearer"
    # present header NAMES only (both known allowlist headers are present here).
    assert set(fields["presentAuthHeaders"]) == {"authorization", "x-api-key"}

    # No secret / token VALUE, no full Authorization header value in the rendered line.
    blob = json.dumps(fields)
    assert _SECRET not in blob
    assert wrong not in blob
    assert "secret-key-value" not in blob  # x-api-key VALUE never logged, only its NAME


@pytest.mark.parametrize(
    "headers, scheme",
    [
        ({"authorization": f"{_SECRET}-wrong"}, "raw"),  # raw token mismatch
        ({}, "none"),  # no header
        ({"authorization": ""}, "empty"),  # present but blank
        ({"authorization": f"Basic {_SECRET}"}, "basic"),  # unrecognised scheme
    ],
)
def test_denied_log_auth_scheme_variants(
    _secret_set: Any, caplog: pytest.LogCaptureFixture, headers: dict[str, str], scheme: str
) -> None:
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)

    with pytest.raises(UnauthorizedError):
        require_cloudpayments_webhook(_request(headers))

    recs = _denied_records(caplog)
    assert len(recs) == 1
    fields = _rendered(recs[0])
    assert fields["authScheme"] == scheme
    assert fields["matched"] is False


def test_no_denied_log_on_success(_secret_set: Any, caplog: pytest.LogCaptureFixture) -> None:
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    require_cloudpayments_webhook(_request({"authorization": f"Bearer {_SECRET}"}))
    assert _denied_records(caplog) == []


def test_no_denied_log_on_misconfigured_500(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger(_LOGGER).disabled = False
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", "")
    get_settings.cache_clear()
    try:
        with pytest.raises(CloudPaymentsWebhookMisconfiguredError):
            require_cloudpayments_webhook(_request({"authorization": f"Bearer {_SECRET}"}))
        assert _denied_records(caplog) == []
    finally:
        get_settings.cache_clear()
