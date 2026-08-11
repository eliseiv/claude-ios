"""Integration: GET /v1/tools (ADR-019, chat-orchestrator/02).

JWT-protected like all /v1/* reads. Uses the shared hermetic `client` (real PG container, faked
external clients, rate limits forced open). Verifies the auth gate (401 without token) and the
response contract (dotted domain names; mutating/execution flags; inputSchema present) and full
coverage of the tool registry. The catalog is the FULL technical registry and is not filtered by
generationMode, so quiz.generate is listed here as well (02-api-contracts §GET /v1/tools).

The number of entries is asserted **against the registry, never as a literal**
(06-testing-strategy.md §time.now: «ассерт — против реестра, не против литерала»).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user
from tests.tool_registry import ALL_REGISTERED_TOOL_NAMES

_EXPECTED_NAMES = {
    "files.read",
    "files.write",
    "files.list",
    "files.mkdir",
    "calendar.read",
    "calendar.create_events",
    "reminders.read",
    "reminders.create",
    "site.write_file",
    "site.preview",
    "site.list",
    "site.read",
    "site.delete",
    "time.now",
    "quiz.generate",
    "media.generate_image",
    "media.generate_video",
}
_MUTATING = {
    "files.write",
    "files.mkdir",
    "calendar.create_events",
    "reminders.create",
    "site.write_file",
    "site.delete",
}


@pytest.mark.asyncio
async def test_tools_requires_auth(client: AsyncClient) -> None:
    r = await client.get("/v1/tools")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tools_broken_bearer_401(client: AsyncClient) -> None:
    r = await client.get("/v1/tools", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tools_returns_the_whole_registry_with_token(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    tools = r.json()["tools"]
    # Composition against the doc-mirrored list AND the registry; the LIST length (not the set)
    # additionally rules out a duplicated entry in the serialized response.
    assert {t["name"] for t in tools} == _EXPECTED_NAMES == set(ALL_REGISTERED_TOOL_NAMES)
    assert len(tools) == len(ALL_REGISTERED_TOOL_NAMES)


@pytest.mark.asyncio
async def test_tools_descriptor_contract(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    r = await client.get("/v1/tools", headers=auth_headers(uid))
    assert r.status_code == 200
    by_name = {t["name"]: t for t in r.json()["tools"]}
    for name, tool in by_name.items():
        # Domain dotted name, never the anthropic underscore wire form (BUG-3).
        assert "." in name and "_" not in name.split(".")[0]
        assert set(tool.keys()) == {"name", "description", "mutating", "execution", "inputSchema"}
        assert tool["mutating"] is (name in _MUTATING), name
        # ADR-026/ADR-064: server-side == site.* (project-scoped) OR the global time.now /
        # quiz.generate; else client.
        expected_exec = (
            "server"
            if name.startswith("site.") or name in {"time.now", "quiz.generate", "media.generate_image", "media.generate_video"}
            else "client"
        )
        assert tool["execution"] == expected_exec, (name, tool["execution"])
        assert isinstance(tool["inputSchema"], dict) and tool["inputSchema"].get("type") == "object"
        assert tool["description"]


@pytest.mark.asyncio
async def test_tools_user_mismatch_in_token_still_serves_own_catalog(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # The catalog is identical for every authenticated user (no per-user data); a freshly-minted
    # token for an unprovisioned subject still gets a 200 (lazy provisioning, ADR-007).
    r = await client.get("/v1/tools", headers=auth_headers(uuid.uuid4()))
    assert r.status_code == 200
    assert len(r.json()["tools"]) == len(ALL_REGISTERED_TOOL_NAMES)
