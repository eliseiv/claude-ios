"""Integration: CRM admin /v1/admin/* (broad-crm contract v1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, seed_user

_ADMIN_SECRET = "crm-admin-key-integration-0123456789abcdef0123456789"
_ADMIN_HEADERS = {"X-Admin-Key": _ADMIN_SECRET}


@pytest.fixture
async def crm_admin_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    settings = get_settings()
    orig_secret = settings.admin_api_secret
    orig_key = settings.admin_api_key
    settings.admin_api_secret = _ADMIN_SECRET
    settings.admin_api_key = ""
    settings.token_products_raw = '{"tokens_100": 100}'

    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import admin as admin_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    async def _allow_admin(**_kwargs: Any) -> bool:
        return True

    orig_admin = rate_limit.enforce_admin_limits
    rate_limit.enforce_admin_limits = _allow_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = _allow_admin  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    settings.admin_api_secret = orig_secret
    settings.admin_api_key = orig_key
    rate_limit.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]


async def test_crm_no_admin_key_403(crm_admin_client: AsyncClient) -> None:
    r = await crm_admin_client.get("/v1/admin/users")
    assert r.status_code == 403


async def test_crm_wrong_admin_key_401(crm_admin_client: AsyncClient) -> None:
    r = await crm_admin_client.get("/v1/admin/users", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401


async def test_crm_users_list_and_detail(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    listed = await crm_admin_client.get("/v1/admin/users", headers=_ADMIN_HEADERS)
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] >= 1
    assert any(item["id"] == str(uid) for item in body["items"])

    detail = await crm_admin_client.get(f"/v1/admin/users/{uid}", headers=_ADMIN_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["id"] == str(uid)
    assert detail.json()["balance"]["tokens"] == 0


async def test_crm_adjust_tokens_and_subscription(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    products = await crm_admin_client.get("/v1/admin/products", headers=_ADMIN_HEADERS)
    assert products.status_code == 200
    product_id = products.json()["items"][0]["product_id"]

    tok = await crm_admin_client.post(
        f"/v1/admin/users/{uid}/tokens",
        headers=_ADMIN_HEADERS,
        json={"amount": 50},
    )
    assert tok.status_code == 200
    assert tok.json()["tokens"] == 50

    sub = await crm_admin_client.post(
        f"/v1/admin/users/{uid}/subscription",
        headers=_ADMIN_HEADERS,
        json={"product_id": product_id, "expires_in_days": 7, "grant_id": "crm-grant-1"},
    )
    assert sub.status_code == 200
    assert sub.json()["applied"] is True
    assert sub.json()["subscription_active"] is True

    sub2 = await crm_admin_client.post(
        f"/v1/admin/users/{uid}/subscription",
        headers=_ADMIN_HEADERS,
        json={"product_id": product_id, "expires_in_days": 7, "grant_id": "crm-grant-1"},
    )
    assert sub2.status_code == 200
    assert sub2.json()["applied"] is False


async def test_crm_empty_endpoints(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    stats = await crm_admin_client.get("/v1/admin/stats", headers=_ADMIN_HEADERS)
    assert stats.status_code == 200
    assert stats.json()["users_total"] >= 1

    payments = await crm_admin_client.get(
        f"/v1/admin/users/{uid}/payments", headers=_ADMIN_HEADERS
    )
    assert payments.status_code == 200
    assert payments.json() == {"total": 0, "items": []}

    requests = await crm_admin_client.get(
        f"/v1/admin/users/{uid}/requests", headers=_ADMIN_HEADERS
    )
    assert requests.status_code == 200
    assert requests.json()["total"] == 0


async def test_crm_user_not_found_404(crm_admin_client: AsyncClient) -> None:
    missing = uuid.uuid4()
    r = await crm_admin_client.get(f"/v1/admin/users/{missing}", headers=_ADMIN_HEADERS)
    assert r.status_code == 404
    assert r.json()["detail"] == "user not found"
