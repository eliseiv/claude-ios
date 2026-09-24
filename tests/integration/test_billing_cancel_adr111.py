"""ADR-111: ``/cancel`` writes ``will_renew=false`` ONLY when the provider found and canceled an
active RU subscription (``found=True``); ``willRenew`` echoes the flag after the operation.

Both paths of the ADR-110 pair are exercised. Fakes and the app fixture are shared with
``test_billing_web_aliases_adr110`` (the broadapps HTTP boundary is scripted there).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user
from tests.integration.test_billing_web_aliases_adr110 import (  # noqa: F401
    PAIRS,
    _Upstream,
    buckets,
    client,
    upstream,
    verify,
)

_ACTIVE = {"data": [{"subscription_id": "s1", "status": "active"}]}


async def _seed_renewing(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    """A user with an existing ``subscriptions`` row whose ``will_renew`` is TRUE (e.g. Apple)."""
    async with maker() as s:
        uid = await seed_user(s, subscription="active")
        await s.execute(
            text("UPDATE subscriptions SET will_renew = true WHERE user_id = :u"), {"u": str(uid)}
        )
        await s.commit()
    return uid


async def _sub(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> Any:
    async with maker() as s:
        return (
            await s.execute(
                text("SELECT status, expires_at, will_renew FROM subscriptions WHERE user_id = :u"),
                {"u": str(uid)},
            )
        ).one_or_none()


@pytest.mark.parametrize("url", PAIRS["cancel"])
async def test_cancel_not_found_keeps_existing_will_renew_true(
    client: AsyncClient,  # noqa: F811
    upstream: _Upstream,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    url: str,
) -> None:
    # Invariant (b): nothing was canceled upstream -> the local flag is not touched.
    upstream.on("GET", "/subscriptions", 200, {"data": []})
    uid = await _seed_renewing(db_sessionmaker)
    before = await _sub(db_sessionmaker, uid)

    r = await client.post(url, headers=auth_headers(uid))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canceled"] is False
    assert body["willRenew"] is True
    after = await _sub(db_sessionmaker, uid)
    assert after.will_renew is True
    assert (after.status, after.expires_at) == (before.status, before.expires_at)


@pytest.mark.parametrize("url", PAIRS["cancel"])
async def test_cancel_found_sets_will_renew_false_from_true(
    client: AsyncClient,  # noqa: F811
    upstream: _Upstream,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    url: str,
) -> None:
    # Invariant (a): provider confirmed the cancel -> the flag becomes false; status/expiry kept.
    upstream.on("GET", "/subscriptions", 200, _ACTIVE)
    upstream.on("POST", "/subscriptions/s1/cancel", 200, {"status": "active"})
    uid = await _seed_renewing(db_sessionmaker)
    before = await _sub(db_sessionmaker, uid)

    r = await client.post(url, headers=auth_headers(uid))

    assert r.status_code == 200, r.text
    assert r.json()["canceled"] is True and r.json()["willRenew"] is False
    after = await _sub(db_sessionmaker, uid)
    assert after.will_renew is False
    assert (after.status, after.expires_at) == (before.status, before.expires_at)


@pytest.mark.parametrize("url", PAIRS["cancel"])
async def test_cancel_not_found_without_row_reports_false_and_creates_nothing(
    client: AsyncClient,  # noqa: F811
    upstream: _Upstream,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    url: str,
) -> None:
    upstream.on("GET", "/subscriptions", 200, {"data": []})
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    r = await client.post(url, headers=auth_headers(uid))

    assert r.status_code == 200, r.text
    assert r.json()["canceled"] is False and r.json()["willRenew"] is False
    assert await _sub(db_sessionmaker, uid) is None


@pytest.mark.parametrize("url", PAIRS["cancel"])
async def test_cancel_upstream_502_leaves_local_state_untouched(
    client: AsyncClient,  # noqa: F811
    upstream: _Upstream,  # noqa: F811
    db_sessionmaker: async_sessionmaker[AsyncSession],
    url: str,
) -> None:
    upstream.on("GET", "/subscriptions", 500, {})
    uid = await _seed_renewing(db_sessionmaker)
    before = await _sub(db_sessionmaker, uid)

    r = await client.post(url, headers=auth_headers(uid))

    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "upstream_error"
    assert tuple(await _sub(db_sessionmaker, uid)) == tuple(before)
