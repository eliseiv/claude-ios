"""Integration: scheduled chats CRUD + worker (ADR-107 / modules/scheduled-chats/09-testing.md)."""

from __future__ import annotations

import asyncio
import datetime
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.orchestrator import ChatRunOut
from app.config import get_settings
from app.db import dispose_engine
from app.models import ScheduledChatTask
from app.scheduled_chats.repository import ScheduledChatsRepository
from app.scheduled_chats.worker import poll_once
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_URL = "/v1/scheduled-chats"


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def _run_at(*, seconds: int = 120) -> str:
    ts = _now() + datetime.timedelta(seconds=seconds)
    return ts.isoformat().replace("+00:00", "Z")


def _err(r: Any) -> str:
    return r.json()["error"]["code"]


async def _seed_session(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    mode: str = "credits",
    updated_at: datetime.datetime | None = None,
) -> uuid.UUID:
    sid = session_id or uuid.uuid4()
    ts = updated_at or _now()
    await s.execute(
        text(
            "INSERT INTO chat_sessions "
            "(id, user_id, project_id, mode, title, assistant_mode, is_pinned, "
            "created_at, updated_at) "
            "VALUES (:id, :uid, 'p', :mode, 't', 'chat', false, :cre, :upd)"
        ),
        {
            "id": str(sid),
            "uid": str(user_id),
            "mode": mode,
            "cre": ts,
            "upd": ts,
        },
    )
    return sid


async def _seed_task(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    prompt: str = "scheduled hello",
    status: str = "scheduled",
    run_at: datetime.datetime | None = None,
    session_id: uuid.UUID | None = None,
    mode: str = "credits",
    started_at: datetime.datetime | None = None,
    claimed_at: datetime.datetime | None = None,
    generation_mode: str | None = "general",
) -> uuid.UUID:
    tid = uuid.uuid4()
    now = _now()
    ra = run_at if run_at is not None else now - datetime.timedelta(seconds=5)
    await s.execute(
        text(
            """
            INSERT INTO scheduled_chat_tasks (
                id, user_id, session_id, prompt, mode, assistant_mode, model,
                generation_mode, run_at, status, claimed_at, started_at,
                created_at, updated_at
            ) VALUES (
                :id, :uid, :sid, :prompt, :mode, 'chat', NULL,
                :gm, :run_at, :status, :claimed, :started,
                :now, :now
            )
            """
        ),
        {
            "id": tid,
            "uid": user_id,
            "sid": session_id,
            "prompt": prompt,
            "mode": mode,
            "gm": generation_mode,
            "run_at": ra,
            "status": status,
            "claimed": claimed_at,
            "started": started_at,
            "now": now,
        },
    )
    return tid


async def _bind_worker_db(fake: FakeAnthropicClient) -> None:
    """Worker uses global get_sessionmaker(); rebind to testcontainer + fake LLM."""
    from app.byok import service as byok_service
    from app.chat import anthropic_client as anthropic_mod

    await dispose_engine()
    # dispose drops the global engine; next get_sessionmaker() must re-read DATABASE_URL
    # from the testcontainer fixture — a stale get_settings() cache keeps the default
    # localhost URL and fails DNS (gaierror) when this test runs alone.
    get_settings.cache_clear()
    anthropic_mod._anthropic_singleton = fake  # type: ignore[assignment]
    byok_service.AnthropicClient = type(fake)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CRUD / validation / isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crud_happy_path_and_foreign_404(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        owner = await seed_user(s, subscription="active", balance=20)
        other = await seed_user(s, subscription="active", balance=20)

    headers = auth_headers(owner)
    created = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "  remind me  ", "runAt": _run_at()},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "scheduled"
    assert body["prompt"] == "remind me"
    assert body["mode"] == "credits"
    tid = body["id"]

    got = await client.get(f"{_URL}/{tid}", headers=headers)
    assert got.status_code == 200
    assert got.json()["id"] == tid

    listed = await client.get(_URL, headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == tid for item in listed.json()["items"])

    patched = await client.patch(
        f"{_URL}/{tid}",
        headers=headers,
        json={"prompt": "updated prompt"},
    )
    assert patched.status_code == 200
    assert patched.json()["prompt"] == "updated prompt"

    foreign = await client.get(f"{_URL}/{tid}", headers=auth_headers(other))
    assert foreign.status_code == 404
    assert _err(foreign) == "scheduled_chat_not_found"

    cancelled = await client.delete(f"{_URL}/{tid}", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json() == {"deleted": True, "status": "cancelled"}


@pytest.mark.asyncio
async def test_create_validation_codes(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    headers = auth_headers(uid)

    empty = await client.post(_URL, headers=headers, json={"prompt": "   ", "runAt": _run_at()})
    assert empty.status_code == 422
    assert _err(empty) == "prompt_required"

    monkeypatch.setenv("SCHEDULED_CHAT_PROMPT_MAX_CHARS", "10")
    get_settings.cache_clear()
    long = await client.post(
        _URL, headers=headers, json={"prompt": "x" * 11, "runAt": _run_at()}
    )
    assert long.status_code == 422
    assert _err(long) == "prompt_too_long"
    monkeypatch.delenv("SCHEDULED_CHAT_PROMPT_MAX_CHARS", raising=False)
    get_settings.cache_clear()

    naive = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": "2099-01-01T12:00:00"},
    )
    assert naive.status_code == 422
    assert _err(naive) == "run_at_timezone_required"

    past = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": (_now() - datetime.timedelta(minutes=1)).isoformat()},
    )
    assert past.status_code == 422
    assert _err(past) == "run_at_not_in_future"

    soon = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": _run_at(seconds=30)},
    )
    assert soon.status_code == 422
    assert _err(soon) == "run_at_too_soon"

    far = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": _run_at(seconds=100 * 24 * 3600)},
    )
    assert far.status_code == 422
    assert _err(far) == "run_at_too_far"

    bad_am = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": _run_at(), "assistantMode": "wizard"},
    )
    assert bad_am.status_code == 422
    assert _err(bad_am) == "unsupported_assistant_mode"

    bad_mode = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": _run_at(), "mode": "paypal"},
    )
    assert bad_mode.status_code == 422
    assert _err(bad_mode) == "unsupported_mode"


@pytest.mark.asyncio
async def test_session_id_foreign_404_and_mode_from_session(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
        other = await seed_user(s, subscription="active", balance=20)
        owned = await _seed_session(s, user_id=uid, mode="byok")
        foreign = await _seed_session(s, user_id=other, mode="credits")
        await s.commit()

    headers = auth_headers(uid)
    missing = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": _run_at(), "sessionId": str(uuid.uuid4())},
    )
    assert missing.status_code == 404
    assert _err(missing) == "session_not_found"

    alien = await client.post(
        _URL,
        headers=headers,
        json={"prompt": "x", "runAt": _run_at(), "sessionId": str(foreign)},
    )
    assert alien.status_code == 404
    assert _err(alien) == "session_not_found"

    # Body mode ignored when sessionId set — session mode wins.
    ok = await client.post(
        _URL,
        headers=headers,
        json={
            "prompt": "x",
            "runAt": _run_at(),
            "sessionId": str(owned),
            "mode": "credits",
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["mode"] == "byok"
    assert ok.json()["sessionId"] == str(owned)


@pytest.mark.asyncio
async def test_active_limit_and_patch_delete_guards(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHEDULED_CHAT_MAX_ACTIVE_PER_USER", "2")
    get_settings.cache_clear()

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    headers = auth_headers(uid)

    r1 = await client.post(_URL, headers=headers, json={"prompt": "a", "runAt": _run_at()})
    r2 = await client.post(_URL, headers=headers, json={"prompt": "b", "runAt": _run_at(seconds=130)})
    assert r1.status_code == 201 and r2.status_code == 201
    over = await client.post(_URL, headers=headers, json={"prompt": "c", "runAt": _run_at(seconds=140)})
    assert over.status_code == 409
    assert _err(over) == "active_limit_exceeded"

    tid = uuid.UUID(r1.json()["id"])
    async with db_sessionmaker() as s:
        await s.execute(
            text(
                "UPDATE scheduled_chat_tasks SET status='running', "
                "claimed_at=now(), started_at=now() WHERE id=:id"
            ),
            {"id": tid},
        )
        await s.commit()

    patch_running = await client.patch(
        f"{_URL}/{tid}", headers=headers, json={"prompt": "nope"}
    )
    assert patch_running.status_code == 409
    assert _err(patch_running) == "not_patchable"

    del_running = await client.delete(f"{_URL}/{tid}", headers=headers)
    assert del_running.status_code == 409
    assert _err(del_running) == "not_cancellable"

    empty = await client.patch(f"{_URL}/{r2.json()['id']}", headers=headers, json={})
    assert empty.status_code == 422
    assert _err(empty) == "empty_patch"

    monkeypatch.delenv("SCHEDULED_CHAT_MAX_ACTIVE_PER_USER", raising=False)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Claim / stuck TTL / soft-TTL resume / deleted session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_skip_locked_parallel(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await dispose_engine()
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
        tid = await _seed_task(s, user_id=uid)
        await s.commit()

    now = _now()

    async def _claim() -> list[uuid.UUID]:
        async with db_sessionmaker() as session:
            repo = ScheduledChatsRepository(session)
            rows = await repo.claim_due(now=now, batch_size=10)
            await session.commit()
            return [r.id for r in rows]

    a, b = await asyncio.gather(_claim(), _claim())
    claimed = a + b
    assert claimed.count(tid) == 1
    assert len(claimed) == 1

    # Second claim of already-running must return empty.
    async with db_sessionmaker() as session:
        repo = ScheduledChatsRepository(session)
        again = await repo.claim_due(now=now, batch_size=10)
        await session.commit()
    assert again == []


@pytest.mark.asyncio
async def test_stuck_ttl_worker_interrupted_not_rescheduled(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropicClient()
    await _bind_worker_db(fake)
    monkeypatch.setenv("SCHEDULED_CHAT_RUNNING_TTL_SECONDS", "60")
    get_settings.cache_clear()

    stale = _now() - datetime.timedelta(seconds=120)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
        tid = await _seed_task(
            s,
            user_id=uid,
            status="running",
            run_at=stale,
            claimed_at=stale,
            started_at=stale,
        )
        await s.commit()

    n = await poll_once(get_settings())
    assert n == 0  # recovered, nothing newly claimed

    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "worker_interrupted"
        assert row.finished_at is not None

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_push_once_on_stuck_ttl_worker_interrupted(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stuck TTL → failed/worker_interrupted + push once (09-testing.md)."""
    fake = FakeAnthropicClient()
    await _bind_worker_db(fake)
    monkeypatch.setenv("SCHEDULED_CHAT_RUNNING_TTL_SECONDS", "60")
    get_settings.cache_clear()

    sends: list[dict[str, Any]] = []

    class _Apns:
        configured = True

        def build_scheduled_chat_ready_payload(self, push: Any) -> dict[str, Any]:
            from app.notifications.apns_client import ApnsClient

            return ApnsClient(get_settings()).build_scheduled_chat_ready_payload(push)

        async def send(self, *, device_token: str, payload: dict[str, Any]) -> str:
            sends.append({"device_token": device_token, "payload": payload})
            return "sent"

    monkeypatch.setattr("app.scheduled_chats.worker.ApnsClient", lambda _settings: _Apns())

    stale = _now() - datetime.timedelta(seconds=120)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
        await s.execute(
            text(
                "INSERT INTO user_preferences (user_id, notifications_enabled) VALUES (:u, true)"
            ),
            {"u": uid},
        )
        await s.execute(
            text(
                "INSERT INTO device_push_tokens (user_id, device_id, push_token, platform) "
                "VALUES (:u, 'd1', 'tok-stuck', 'ios')"
            ),
            {"u": uid},
        )
        tid = await _seed_task(
            s,
            user_id=uid,
            status="running",
            run_at=stale,
            claimed_at=stale,
            started_at=stale,
        )
        await s.commit()

    n = await poll_once(get_settings())
    assert n == 0  # recovered, nothing newly claimed
    assert len(sends) == 1
    payload = sends[0]["payload"]
    assert payload["type"] == "scheduled_chat_ready"
    assert payload["scheduledChatId"] == str(tid)
    assert payload["status"] == "failed"
    assert payload["errorCode"] == "worker_interrupted"

    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "worker_interrupted"
        assert row.push_sent_at is not None
        first_push = row.push_sent_at

    # Second notify must be a no-op (claim on push_sent_at).
    from app.notifications.push_service import ScheduledChatPushService

    async with db_sessionmaker() as s:
        svc = ScheduledChatPushService(s, apns=_Apns())  # type: ignore[arg-type]
        await svc.notify_scheduled_chat_ready(
            task_id=tid,
            user_id=uid,
            status="failed",
            session_id=None,
            message_step_id=None,
            error_code="worker_interrupted",
        )
        await s.commit()
        row2 = await s.get(ScheduledChatTask, tid)
        assert row2 is not None
        assert row2.push_sent_at == first_push
    assert len(sends) == 1

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_soft_ttl_resume_keeps_same_session_id(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: resume_only must resume soft-expired owned UUID (not create-on-miss)."""
    fake = FakeAnthropicClient()
    fake.responses = [fake.text_result("still same session")]
    await _bind_worker_db(fake)
    monkeypatch.setenv("SESSION_SOFT_TTL_SECONDS", "60")
    get_settings.cache_clear()

    old = _now() - datetime.timedelta(hours=2)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
        sid = await _seed_session(s, user_id=uid, updated_at=old)
        tid = await _seed_task(s, user_id=uid, session_id=sid, prompt="resume soft ttl")
        await s.commit()

    async with db_sessionmaker() as s:
        before = int(
            await s.scalar(text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": uid})
            or 0
        )

    n = await poll_once(get_settings())
    assert n == 1

    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.status == "completed", (row.status, row.error_code, row.error_message)
        assert row.result_session_id == sid
        after = int(
            await s.scalar(text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": uid})
            or 0
        )
    assert after == before  # no silent create

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_deleted_planned_session_fails_without_create(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeAnthropicClient()
    fake.responses = [fake.text_result("should not run")]
    await _bind_worker_db(fake)

    ghost = uuid.uuid4()
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
        tid = await _seed_task(s, user_id=uid, session_id=ghost, prompt="gone")
        await s.commit()

    async with db_sessionmaker() as s:
        before = int(
            await s.scalar(text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": uid})
            or 0
        )

    n = await poll_once(get_settings())
    assert n == 1
    assert not fake.calls  # orchestrator never reached

    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "session_not_found"
        assert row.result_session_id is None
        after = int(
            await s.scalar(text("SELECT count(*) FROM chat_sessions WHERE user_id=:u"), {"u": uid})
            or 0
        )
    assert after == before


# ---------------------------------------------------------------------------
# Worker ChatResponse mapping + push once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_completed_sets_result_ids(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeAnthropicClient()
    fake.responses = [fake.text_result("done")]
    await _bind_worker_db(fake)

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
        tid = await _seed_task(s, user_id=uid, prompt="new session run", session_id=None)
        await s.commit()

    assert await poll_once(get_settings()) == 1

    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.status == "completed"
        assert row.result_session_id is not None
        assert row.result_message_step_id is not None
        assert row.error_code is None


@pytest.mark.asyncio
async def test_worker_blocked_policy_and_max_tokens_and_tool_call(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1) credits_empty via real policy (active sub, zero balance) — no LLM call needed.
    fake = FakeAnthropicClient()
    await _bind_worker_db(fake)
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
        tid_empty = await _seed_task(s, user_id=uid, prompt="no credits")
        await s.commit()
    assert await poll_once(get_settings()) == 1
    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid_empty)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "credits_empty"

    # 2) max_tokens via fake LLM
    fake.responses = [fake.max_tokens_result(text="partial")]
    await _bind_worker_db(fake)
    async with db_sessionmaker() as s:
        uid2 = await seed_user(s, subscription="active", balance=50)
        tid_mt = await _seed_task(s, user_id=uid2, prompt="truncate me")
        await s.commit()
    assert await poll_once(get_settings()) == 1
    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid_mt)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "max_tokens"

    # 3) tool_call → tool_loop_unsupported (stub ChatRunOut; tool catalog varies by mode)
    await dispose_engine()

    class _ToolOrch:
        async def run(self, **kwargs: Any) -> ChatRunOut:
            return ChatRunOut(status="tool_call", session_id=kwargs["session_id"] or uuid.uuid4())

    monkeypatch.setattr(
        "app.scheduled_chats.worker.get_v2_orchestrator",
        lambda _session: _ToolOrch(),
    )
    async with db_sessionmaker() as s:
        uid3 = await seed_user(s, subscription="active", balance=50)
        tid_tool = await _seed_task(s, user_id=uid3, prompt="write file", session_id=None)
        await s.commit()
    assert await poll_once(get_settings()) == 1
    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid_tool)
        assert row is not None
        assert row.status == "failed"
        assert row.error_code == "tool_loop_unsupported"


@pytest.mark.asyncio
async def test_push_once_on_completed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAnthropicClient()
    fake.responses = [fake.text_result("pushed")]
    await _bind_worker_db(fake)

    sends: list[dict[str, Any]] = []

    class _Apns:
        configured = True

        def build_scheduled_chat_ready_payload(self, push: Any) -> dict[str, Any]:
            from app.notifications.apns_client import ApnsClient

            return ApnsClient(get_settings()).build_scheduled_chat_ready_payload(push)

        async def send(self, *, device_token: str, payload: dict[str, Any]) -> str:
            sends.append({"device_token": device_token, "payload": payload})
            return "sent"

    monkeypatch.setattr("app.scheduled_chats.worker.ApnsClient", lambda _settings: _Apns())

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
        await s.execute(
            text(
                "INSERT INTO user_preferences (user_id, notifications_enabled) VALUES (:u, true)"
            ),
            {"u": uid},
        )
        await s.execute(
            text(
                "INSERT INTO device_push_tokens (user_id, device_id, push_token, platform) "
                "VALUES (:u, 'd1', 'tok-sc', 'ios')"
            ),
            {"u": uid},
        )
        tid = await _seed_task(s, user_id=uid, prompt="notify me")
        await s.commit()

    assert await poll_once(get_settings()) == 1
    assert len(sends) == 1
    payload = sends[0]["payload"]
    assert payload["type"] == "scheduled_chat_ready"
    assert payload["scheduledChatId"] == str(tid)
    assert payload["status"] == "completed"
    assert payload["sessionId"] is not None

    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.push_sent_at is not None
        first_push = row.push_sent_at

    # Second notify must be a no-op (claim on push_sent_at).
    from app.notifications.push_service import ScheduledChatPushService

    async with db_sessionmaker() as s:
        svc = ScheduledChatPushService(s, apns=_Apns())  # type: ignore[arg-type]
        await svc.notify_scheduled_chat_ready(
            task_id=tid,
            user_id=uid,
            status="completed",
            session_id=row.result_session_id,
            message_step_id=row.result_message_step_id,
            error_code=None,
        )
        await s.commit()
        row2 = await s.get(ScheduledChatTask, tid)
        assert row2 is not None
        assert row2.push_sent_at == first_push
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_map_outcome_via_monkeypatched_orchestrator(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense: worker maps ChatRunOut statuses even when orchestrator is stubbed."""
    await dispose_engine()
    mid = uuid.uuid4()
    planned_holder: dict[str, uuid.UUID] = {}

    class _Orch:
        async def run(self, **kwargs: Any) -> ChatRunOut:
            return ChatRunOut(
                status="assistant_message",
                session_id=kwargs["session_id"],
                message_step_id=mid,
                assistant_message="stub",
            )

    monkeypatch.setattr(
        "app.scheduled_chats.worker.get_v2_orchestrator",
        lambda _session: _Orch(),
    )

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
        planned = await _seed_session(s, user_id=uid)
        planned_holder["sid"] = planned
        tid = await _seed_task(s, user_id=uid, session_id=planned)
        await s.commit()

    assert await poll_once(get_settings()) == 1
    async with db_sessionmaker() as s:
        row = await s.get(ScheduledChatTask, tid)
        assert row is not None
        assert row.status == "completed"
        assert row.result_session_id == planned_holder["sid"]
        assert row.result_message_step_id == mid
