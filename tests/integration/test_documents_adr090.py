"""Интеграционные тесты документов чата (ADR-090).

Реальный PostgreSQL. Покрываются инварианты, нарушение которых делает фичу опасной или
бесполезной: изоляция владельца, полная замена вместо патча, потолки НА СЕССИЮ, каскад с чатом
и отдача файла с корректным `Content-Disposition`.
"""

from __future__ import annotations

import base64
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import auth_headers, seed_user


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _plain(b64: str) -> str:
    return base64.b64decode(b64).decode("utf-8")


async def _seed_session(s: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    sid = uuid.uuid4()
    await s.execute(
        text(
            "INSERT INTO chat_sessions (id, user_id, mode, assistant_mode) "
            "VALUES (:id, :uid, 'credits', 'chat')"
        ),
        {"id": sid, "uid": user_id},
    )
    await s.commit()
    return sid


async def _create(
    client: AsyncClient, uid: uuid.UUID, sid: uuid.UUID, **over: object
) -> dict[str, object]:
    body: dict[str, object] = {
        "filename": "report",
        "mediaType": "text/markdown",
        "content": _b64("# Report\n\nfirst"),
    }
    body.update(over)
    r = await client.post(f"/v1/chats/{sid}/documents", json=body, headers=auth_headers(uid))
    assert r.status_code == 201, r.text
    return r.json()


# --- жизненный цикл -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_read_update_roundtrip(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)

    created = await _create(client, uid, sid)
    assert created["version"] == 1
    assert created["createdBy"] == "user"
    assert created["filename"] == "report.md", "расширение приводится к mediaType"

    doc_id = created["documentId"]
    got = await client.get(f"/v1/chats/{sid}/documents/{doc_id}", headers=auth_headers(uid))
    assert got.status_code == 200
    assert _plain(got.json()["content"]) == "# Report\n\nfirst"

    upd = await client.patch(
        f"/v1/chats/{sid}/documents/{doc_id}",
        json={"content": _b64("# Report\n\nsecond")},
        headers=auth_headers(uid),
    )
    assert upd.status_code == 200
    assert upd.json()["version"] == 2, "version растёт — клиент по нему видит изменение"

    got2 = await client.get(f"/v1/chats/{sid}/documents/{doc_id}", headers=auth_headers(uid))
    assert _plain(got2.json()["content"]) == "# Report\n\nsecond", "замена ЦЕЛИКОМ, не патч"


@pytest.mark.asyncio
async def test_list_excludes_content(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    await _create(client, uid, sid)
    r = await client.get(f"/v1/chats/{sid}/documents", headers=auth_headers(uid))
    assert r.status_code == 200
    docs = r.json()["documents"]
    assert len(docs) == 1
    assert docs[0]["content"] is None, "список не тащит содержимое"


@pytest.mark.asyncio
async def test_download_serves_file(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    doc = await _create(client, uid, sid, filename="плана/../отчёт", mediaType="text/plain")
    r = await client.get(
        f"/v1/chats/{sid}/documents/{doc['documentId']}/download", headers=auth_headers(uid)
    )
    assert r.status_code == 200
    assert r.text == "# Report\n\nfirst"
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;")
    assert "/" not in disp.split("filename*=")[0], "разделители пути вырезаны в самом имени"
    assert (
        "filename*=UTF-8''" in disp
    ), "кириллица переживает latin-1 заголовок только через RFC 5987"
    assert (
        'filename=".md"' not in disp
    ), "ASCII-запас не вырождается в одно расширение: файл с именем из точки — скрытый на Unix"
    assert 'filename="document' in disp, "у запаса осмысленная основа"


@pytest.mark.asyncio
async def test_delete_then_404(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    doc = await _create(client, uid, sid)
    d = await client.delete(
        f"/v1/chats/{sid}/documents/{doc['documentId']}", headers=auth_headers(uid)
    )
    assert d.status_code == 200 and d.json()["deleted"] is True
    again = await client.get(
        f"/v1/chats/{sid}/documents/{doc['documentId']}", headers=auth_headers(uid)
    )
    assert again.status_code == 404


# --- изоляция -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_document_is_404_not_403(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Чужой и несуществующий неотличимы — существование чужого объекта не раскрывается."""
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        sid = await _seed_session(s, owner)
        stranger = await seed_user(s)
    doc = await _create(client, owner, sid)
    r = await client.get(
        f"/v1/chats/{sid}/documents/{doc['documentId']}", headers=auth_headers(stranger)
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cannot_put_document_into_foreign_session(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """У нового документа ещё нет пары (user, session) — сессию обязан проверить отдельный гейт."""
    async with db_sessionmaker() as s:
        owner = await seed_user(s)
        sid = await _seed_session(s, owner)
        stranger = await seed_user(s)
    r = await client.post(
        f"/v1/chats/{sid}/documents",
        json={"filename": "x", "mediaType": "text/plain", "content": _b64("hi")},
        headers=auth_headers(stranger),
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_documents_die_with_the_chat(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    await _create(client, uid, sid)
    async with db_sessionmaker() as s:
        await s.execute(text("DELETE FROM chat_sessions WHERE id = :id"), {"id": sid})
        await s.commit()
        left = await s.scalar(
            text("SELECT count(*) FROM chat_documents WHERE session_id = :id"), {"id": sid}
        )
    assert left == 0, "CASCADE: документ живёт ровно столько, сколько чат"


# --- лимиты и коды ошибок -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_document_has_its_own_code(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    r = await client.post(
        f"/v1/chats/{sid}/documents",
        json={
            "filename": "big",
            "mediaType": "text/plain",
            "content": _b64("x" * (256 * 1024 + 1)),
        },
        headers=auth_headers(uid),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "document_too_large", "не generic validation_error"


@pytest.mark.asyncio
async def test_unsupported_media_type_rejected(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    r = await client.post(
        f"/v1/chats/{sid}/documents",
        json={"filename": "a.pdf", "mediaType": "application/pdf", "content": _b64("x")},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422, "бинарные форматы не поддерживаются (ADR-090 §1)"


@pytest.mark.asyncio
async def test_invalid_base64_is_422_not_500(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    r = await client.post(
        f"/v1/chats/{sid}/documents",
        json={"filename": "a", "mediaType": "text/plain", "content": "!!!not base64!!!"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_count_limit_is_per_session(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Потолок считается на сессию: во втором чате того же пользователя место снова есть."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
        other = await _seed_session(s, uid)
    for i in range(20):
        await _create(client, uid, sid, filename=f"d{i}")
    over = await client.post(
        f"/v1/chats/{sid}/documents",
        json={"filename": "one-too-many", "mediaType": "text/plain", "content": _b64("x")},
        headers=auth_headers(uid),
    )
    assert over.status_code == 422
    assert over.json()["error"]["code"] == "too_many_documents"
    await _create(client, uid, other, filename="fresh")


@pytest.mark.asyncio
async def test_update_does_not_count_its_own_bytes_twice(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Правка почти-максимального документа не должна упираться в потолок,

    который он же и занимает.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        sid = await _seed_session(s, uid)
    doc = await _create(client, uid, sid, mediaType="text/plain", content=_b64("y" * 200_000))
    r = await client.patch(
        f"/v1/chats/{sid}/documents/{doc['documentId']}",
        json={"content": _b64("z" * 200_000)},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
