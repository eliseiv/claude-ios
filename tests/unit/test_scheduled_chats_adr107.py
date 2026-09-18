"""Unit: scheduled chats mapping, push payload/skip, worker off (ADR-107 / 09-testing.md)."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat.orchestrator import ChatRunOut
from app.config import get_settings
from app.notifications.apns_client import (
    ApnsClient,
    ScheduledChatReadyPush,
    scheduled_chat_ready_copy,
)
from app.notifications.push_service import ScheduledChatPushService
from app.preferences.service import PreferencesView
from app.scheduled_chats.worker import _map_run_outcome, worker_loop


def test_map_run_outcome_assistant_message_completed() -> None:
    out = ChatRunOut(
        status="assistant_message",
        session_id=uuid.uuid4(),
        message_step_id=uuid.uuid4(),
    )
    assert _map_run_outcome(out) == ("completed", None, None)


def test_map_run_outcome_tool_call_unsupported() -> None:
    out = ChatRunOut(status="tool_call", session_id=uuid.uuid4())
    status, code, _msg = _map_run_outcome(out)
    assert status == "failed"
    assert code == "tool_loop_unsupported"


def test_map_run_outcome_blocked_policy_uses_block_reason() -> None:
    out = ChatRunOut(status="blocked", session_id=uuid.uuid4(), block_reason="credits_empty")
    status, code, _msg = _map_run_outcome(out)
    assert status == "failed"
    assert code == "credits_empty"


def test_map_run_outcome_blocked_max_tokens() -> None:
    out = ChatRunOut(status="blocked", session_id=uuid.uuid4(), block_reason="max_tokens")
    status, code, _msg = _map_run_outcome(out)
    assert status == "failed"
    assert code == "max_tokens"


def test_scheduled_chat_ready_payload_type_and_fields() -> None:
    client = ApnsClient(MagicMock())
    title, body = scheduled_chat_ready_copy(status="completed")
    sid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    tid = str(uuid.uuid4())
    payload = client.build_scheduled_chat_ready_payload(
        ScheduledChatReadyPush(
            scheduled_chat_id=tid,
            session_id=sid,
            message_step_id=mid,
            status="completed",
            error_code=None,
            title=title,
            body=body,
        )
    )
    assert payload["type"] == "scheduled_chat_ready"
    assert payload["scheduledChatId"] == tid
    assert payload["sessionId"] == sid
    assert payload["messageStepId"] == mid
    assert payload["status"] == "completed"
    assert payload["errorCode"] is None
    assert "mutable-content" not in payload["aps"]


@pytest.mark.asyncio
async def test_push_skips_when_notifications_disabled() -> None:
    session = AsyncMock()
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = uuid.uuid4()
    session.execute = AsyncMock(return_value=claim_result)

    prefs = AsyncMock()
    prefs.get = AsyncMock(
        return_value=PreferencesView(
            default_assistant_mode="chat",
            notifications_enabled=False,
            code_defaults={},
            memory_enabled=False,
            default_voice_id=None,
            memory_search_scope="global",
        )
    )
    apns = MagicMock()
    apns.configured = True
    apns.send = AsyncMock()
    tokens = AsyncMock()
    tokens.list_for_user = AsyncMock(return_value=[MagicMock(push_token="tok")])

    svc = ScheduledChatPushService(session, apns=apns, tokens=tokens, preferences=prefs)
    await svc.notify_scheduled_chat_ready(
        task_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="completed",
        session_id=uuid.uuid4(),
        message_step_id=uuid.uuid4(),
        error_code=None,
    )
    apns.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_skips_when_already_claimed() -> None:
    session = AsyncMock()
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=claim_result)

    prefs = AsyncMock()
    apns = MagicMock()
    apns.send = AsyncMock()
    tokens = AsyncMock()

    svc = ScheduledChatPushService(session, apns=apns, tokens=tokens, preferences=prefs)
    await svc.notify_scheduled_chat_ready(
        task_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="failed",
        session_id=None,
        message_step_id=None,
        error_code="worker_interrupted",
    )
    prefs.get.assert_not_awaited()
    apns.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_skips_when_apns_not_configured() -> None:
    session = AsyncMock()
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = uuid.uuid4()
    session.execute = AsyncMock(return_value=claim_result)

    prefs = AsyncMock()
    prefs.get = AsyncMock(
        return_value=PreferencesView(
            default_assistant_mode="chat",
            notifications_enabled=True,
            code_defaults={},
            memory_enabled=False,
            default_voice_id=None,
            memory_search_scope="global",
        )
    )
    apns = MagicMock()
    apns.configured = False
    apns.send = AsyncMock()
    tokens = AsyncMock()
    tokens.list_for_user = AsyncMock(return_value=[MagicMock(push_token="tok")])

    svc = ScheduledChatPushService(session, apns=apns, tokens=tokens, preferences=prefs)
    await svc.notify_scheduled_chat_ready(
        task_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="completed",
        session_id=uuid.uuid4(),
        message_step_id=None,
        error_code=None,
    )
    apns.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_loop_off_when_poll_seconds_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHEDULED_CHAT_POLL_SECONDS", "0")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.scheduled_chat_poll_seconds <= 0

    stop = asyncio.Event()
    # Must return immediately without waiting on stop.
    await asyncio.wait_for(worker_loop(stop, settings), timeout=1.0)
    get_settings.cache_clear()
