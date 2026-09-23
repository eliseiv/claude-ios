"""Integration: PATCH /v1/preferences defaultModel + its effect on GET /v1/models.

`defaultModel` overrides which chat row carries `default:true` in the models catalog. Covers:
- PATCH valid id → 200, saved; GET /v1/models moves default:true (and the row) to it;
- PATCH id not on this instance's chat catalog → 422 unsupported_model;
- PATCH null → resets to the instance default (both in preferences and in the catalog);
- fal photo/video rows (if any) are never affected by the chat-only override.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


async def _models(client: AsyncClient, uid: str) -> list[dict[str, object]]:
    r = await client.get("/v1/models", headers=auth_headers(uid))
    assert r.status_code == 200
    return list(r.json()["models"])


@pytest.mark.asyncio
async def test_default_model_patch_moves_default_true_and_order(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    before = await _models(client, str(uid))
    chat_before = [m for m in before if m["modality"] == "chat"]
    instance_default = next(m["id"] for m in chat_before if m["default"])
    other = next(m["id"] for m in chat_before if not m["default"])
    assert other != instance_default

    r = await client.patch(
        "/v1/preferences", json={"defaultModel": other}, headers=auth_headers(uid)
    )
    assert r.status_code == 200
    assert r.json()["defaultModel"] == other

    after = await _models(client, str(uid))
    chat_after = [m for m in after if m["modality"] == "chat"]
    assert chat_after[0]["id"] == other  # default is first
    assert chat_after[0]["default"] is True
    defaults = [m["id"] for m in chat_after if m["default"]]
    assert defaults == [other]  # exactly one default:true, and it's the chosen one
    # Photo/video rows (if fal is configured) keep their own default logic untouched.
    fal_before = [m for m in before if m["modality"] != "chat"]
    fal_after = [m for m in after if m["modality"] != "chat"]
    assert fal_before == fal_after


@pytest.mark.asyncio
async def test_default_model_patch_unknown_id_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.patch(
        "/v1/preferences",
        json={"defaultModel": "totally-not-a-real-model"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unsupported_model"
    # Rejected value must not have been persisted.
    g = await client.get("/v1/preferences", headers=auth_headers(uid))
    assert g.json()["defaultModel"] is None


@pytest.mark.asyncio
async def test_default_model_null_resets_to_instance_default(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    before = await _models(client, str(uid))
    instance_default = next(m["id"] for m in before if m["modality"] == "chat" and m["default"])
    other = next(m["id"] for m in before if m["modality"] == "chat" and m["id"] != instance_default)

    r1 = await client.patch(
        "/v1/preferences", json={"defaultModel": other}, headers=auth_headers(uid)
    )
    assert r1.json()["defaultModel"] == other

    r2 = await client.patch(
        "/v1/preferences", json={"defaultModel": None}, headers=auth_headers(uid)
    )
    assert r2.status_code == 200
    assert r2.json()["defaultModel"] is None

    after = await _models(client, str(uid))
    defaults = [m["id"] for m in after if m["modality"] == "chat" and m["default"]]
    assert defaults == [instance_default]


@pytest.mark.asyncio
async def test_default_model_absent_field_is_no_op_default_stays(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A PATCH that doesn't mention defaultModel at all must not touch a previously-set value."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    before = await _models(client, str(uid))
    other = next(m["id"] for m in before if m["modality"] == "chat" and not m["default"])
    r1 = await client.patch(
        "/v1/preferences", json={"defaultModel": other}, headers=auth_headers(uid)
    )
    assert r1.json()["defaultModel"] == other

    r2 = await client.patch(
        "/v1/preferences", json={"notificationsEnabled": True}, headers=auth_headers(uid)
    )
    assert r2.status_code == 200
    assert r2.json()["defaultModel"] == other  # untouched by an unrelated field patch
