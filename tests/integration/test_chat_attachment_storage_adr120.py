"""Integration: хранение вложений хода чата (ADR-120).

Реальная PostgreSQL (testcontainers), LLM подделан на границе клиента. Покрыто:
§2 — `attachmentRefs` для ВСЕХ типов вложений (attachmentId/mediaType/filename/size + подписанный
url/expiresAt); §3 — скачивание по подписи 200 с теми же байтами, любая порча/чужой чат → 404;
§4 — edit без `attachments`/`message` наследует их у исходного хода; ADR-086 §7 — битая картинка
с верными magic bytes → 422 до модерации и LLM.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user
from tests.images import PNG

_TEXT = "заметка про релиз".encode()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _attachments() -> list[dict[str, str]]:
    return [
        {"type": "image", "mediaType": "image/png", "filename": "p.png", "data": _b64(PNG)},
        {"type": "text", "mediaType": "text/plain", "filename": "n.txt", "data": _b64(_TEXT)},
    ]


@pytest.fixture
def preview_secret() -> Iterator[None]:
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = "adr120-secret-0123456789abcdef0123456789abcdef01"
    yield
    settings.preview_url_secret = orig


async def _run(
    client: AsyncClient, uid: uuid.UUID, fake: FakeAnthropicClient, **body: Any
) -> dict[str, Any]:
    fake.responses = [fake.text_result("ok")]
    payload: dict[str, Any] = {"userId": str(uid), "mode": "credits", **body}
    r = await client.post("/v1/chat/run", json=payload, headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


async def _user_refs(client: AsyncClient, uid: uuid.UUID, sid: str) -> list[dict[str, Any]]:
    r = await client.get(f"/v1/chats/{sid}", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    users = [s for s in r.json()["steps"] if s["role"] == "user"]
    return list(users[-1]["payload"].get("attachmentRefs") or [])


async def _turn_files(maker: async_sessionmaker[AsyncSession], sid: str) -> list[tuple[str, str]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT message_step_id, filename FROM chat_attachments "
                    "WHERE session_id = :s ORDER BY position"
                ),
                {"s": sid},
            )
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


@pytest.mark.asyncio
async def test_attachment_refs_cover_all_types_and_signed_download_returns_bytes(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    preview_secret: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    out = await _run(client, uid, fake_anthropic, message="смотри", attachments=_attachments())
    sid = str(out["sessionId"])

    refs = await _user_refs(client, uid, sid)
    assert [(r["mediaType"], r["filename"], r["size"]) for r in refs] == [
        ("image/png", "p.png", len(PNG)),
        ("text/plain", "n.txt", len(_TEXT)),
    ]
    for ref, expected in zip(refs, [PNG, _TEXT], strict=True):
        uuid.UUID(ref["attachmentId"])
        assert ref["expiresAt"].endswith("Z")
        path = urlsplit(ref["url"]).path
        assert path.startswith(f"/v1/chats/{sid}/attachments/{ref['attachmentId']}/")
        got = await client.get(path)  # без JWT: авторизация в подписи пути
        assert got.status_code == 200, got.text
        assert got.content == expected
        assert got.headers["x-content-type-options"] == "nosniff"
    text_download = await client.get(urlsplit(refs[1]["url"]).path)
    assert text_download.headers["content-type"].startswith("text/plain; charset=utf-8")

    path = urlsplit(refs[0]["url"]).path
    base, token = path.rsplit("/", 1)
    other_chat = path.replace(sid, str(uuid.uuid4()))
    other_attachment = path.replace(refs[0]["attachmentId"], refs[1]["attachmentId"])
    for bad in (f"{base}/{token}x", f"{base}/garbage", other_chat, other_attachment):
        assert (await client.get(bad)).status_code == 404, bad


@pytest.mark.asyncio
async def test_attachment_refs_without_secret_carry_no_url(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    assert not get_settings().preview_url_secret
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    out = await _run(client, uid, fake_anthropic, message="x", attachments=_attachments())

    refs = await _user_refs(client, uid, str(out["sessionId"]))
    assert len(refs) == 2
    assert all("url" not in r and "expiresAt" not in r and r["attachmentId"] for r in refs)


@pytest.mark.asyncio
async def test_edit_without_message_and_attachments_inherits_both(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    first = await _run(
        client, uid, fake_anthropic, message="ORIGINAL_TEXT", attachments=_attachments()
    )
    sid = str(first["sessionId"])
    fake_anthropic.calls.clear()

    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="",
        sessionId=sid,
        editMessageStepId=str(first["messageStepId"]),
    )

    sent = str(fake_anthropic.calls[0]["messages"])
    assert "ORIGINAL_TEXT" in sent
    assert _b64(PNG) in sent, "картинка исходного хода снова ушла в модель"
    # Строки усечённого хода удалены, у нового хода — копии тех же файлов.
    new_msid = str(edited["messageStepId"])
    assert await _turn_files(db_sessionmaker, sid) == [(new_msid, "p.png"), (new_msid, "n.txt")]


@pytest.mark.asyncio
async def test_edit_with_new_attachments_replaces_inherited_ones(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=10)
    first = await _run(client, uid, fake_anthropic, message="a", attachments=_attachments())
    sid = str(first["sessionId"])
    replacement = {
        "type": "text",
        "mediaType": "text/plain",
        "filename": "new.txt",
        "data": _b64(b"new"),
    }
    edited = await _run(
        client,
        uid,
        fake_anthropic,
        message="b",
        sessionId=sid,
        editMessageStepId=str(first["messageStepId"]),
        attachments=[replacement],
    )
    assert await _turn_files(db_sessionmaker, sid) == [(str(edited["messageStepId"]), "new.txt")]


@pytest.mark.asyncio
async def test_broken_image_with_valid_magic_is_422_before_llm(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    broken = PNG[:16] + b"\x00" * 64
    attachment = {
        "type": "image",
        "mediaType": "image/png",
        "filename": "b.png",
        "data": _b64(broken),
    }
    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "mode": "credits",
            "message": "что тут?",
            "attachments": [attachment],
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "attachment_media_type_mismatch"
    assert fake_anthropic.calls == []
