"""Integration: ADR-052 — lenient ``Authorization`` accepted through the FULL FastAPI stack.

Complements the pure-unit ADR-052 suite by driving the real webhook route
(``POST /v1/billing/cloudpayments/webhook``) so the DECORATIVE ``cloudPaymentsWebhook``
``HTTPBearer`` dependency (``auto_error=False``) is
exercised: a raw / ``Token`` / lower-case ``bearer`` header must NOT be rejected by the scheme and
must reach the handler (200). Hermetic: shared testcontainers Postgres (seeded), secret injected via
env + ``get_settings.cache_clear()``; no network, no LLM.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import seed_user

_SECRET = "cloudpayments-webhook-secret-value"
_URL = "/v1/billing/cloudpayments/webhook"
_UID_UPPER = "B284721F-C3E0-4446-B00F-3C6A21F32535"
_SUB_PRODUCT = "yearly_49.99_nottrial"


@pytest.fixture
async def cp_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.api_gateway import rate_limit
    from app.main import create_app

    monkeypatch.setenv("CLOUDPAYMENTS_WEBHOOK_TOKEN", _SECRET)
    monkeypatch.setenv("CLOUDPAYMENTS_PRODUCT_TOKENS", json.dumps({_SUB_PRODUCT: 12000}))
    monkeypatch.setenv("CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT", "1000")
    get_settings.cache_clear()

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _allow(**_kwargs: Any) -> bool:
        return True

    orig_other = rate_limit.enforce_other_limits
    rate_limit.enforce_other_limits = _allow  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    get_settings.cache_clear()


def _body() -> bytes:
    data = {
        "user_id": _UID_UPPER,
        "product_id": _SUB_PRODUCT,
        "billing_interval_unit": "year",
        "billing_interval_count": "1",
    }
    body = {
        "Status": "Completed",
        "OperationType": "Payment",
        "Amount": 4990,
        "Currency": "RUB",
        "TransactionId": "31d884c8-000f-5001-8000-1fb75b44e1d9",
        "AccountId": _UID_UPPER,
        "Data": json.dumps(data),
    }
    return json.dumps(body).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {_SECRET}",
        f"bearer {_SECRET}",  # lower-case scheme word
        f"Token {_SECRET}",
        _SECRET,  # raw secret, no scheme — the ADR-052 fix
    ],
)
async def test_valid_secret_passes_through_full_stack(
    cp_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], header: str
) -> None:
    uid = uuid.UUID(_UID_UPPER.lower())
    async with db_sessionmaker() as s:
        await seed_user(s, user_id=uid)
    r = await cp_client.post(_URL, content=_body(), headers={"Authorization": header})
    # Auth passed (NOT 401/403) and the handler collapsed to the CloudPayments ack.
    assert r.status_code == 200, r.text
    assert r.json() == {"code": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        f"Basic {_SECRET}",  # unrecognised scheme -> whole header != secret
        f"Bearer {_SECRET}-wrong",  # wrong token
        f"{_SECRET}-wrong",  # raw wrong token
    ],
)
async def test_bad_authorization_is_401_through_full_stack(
    cp_client: AsyncClient, header: str
) -> None:
    r = await cp_client.post(_URL, content=_body(), headers={"Authorization": header})
    assert r.status_code == 401, r.text
