"""Chat persistence: sessions, steps, tool_calls + context reconstruction (CO-3, chat/04).

Only this module writes chat_sessions / chat_steps / tool_calls. Context for Claude is
reconstructed from chat_steps on each step (TD-002). Soft TTL 24h by updated_at (Q-001-1):
continuing an expired session starts a new session.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ChatSession, ChatStep, ToolCall

# Default max length of an auto-generated chat title (chats/03-architecture.md).
_TITLE_MAX_CHARS = 60


@dataclass(frozen=True)
class SessionContext:
    session: ChatSession
    is_new: bool


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def derive_title(message: str, limit: int = _TITLE_MAX_CHARS) -> str | None:
    """Auto-generate a chat title from the first user message (chats/03, BR-CH-2).

    Whitespace-normalized and truncated to ``limit`` chars. Returns None for an
    empty/whitespace-only message (the list then falls back to preview).
    """
    normalized = " ".join(message.split())
    if not normalized:
        return None
    return normalized[:limit]


class ChatRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def mark_trial_used(self, user_id: uuid.UUID) -> bool:
        """Atomically consume the single lifetime trial (ADR-005, BR-1).

        UPDATE ... WHERE trial_used = FALSE → idempotent: returns True if this call flipped it,
        False if it was already used (concurrent retry / replay).
        """
        updated = await self._session.scalar(
            text(
                "UPDATE users SET trial_used = TRUE "
                "WHERE id = :uid AND trial_used = FALSE RETURNING id"
            ),
            {"uid": str(user_id)},
        )
        return updated is not None

    async def get_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession | None:
        row = await self._session.scalar(
            select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        )
        return row

    def _is_expired(self, session: ChatSession) -> bool:
        ttl = get_settings().session_soft_ttl_seconds
        updated = session.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=datetime.UTC)
        return (_now() - updated).total_seconds() > ttl

    async def get_or_create_session(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str,
        mode: str,
        session_id: uuid.UUID | None,
        assistant_mode: str = "chat",
        title: str | None = None,
    ) -> SessionContext:
        """Resume an owned, non-expired session or create a new one.

        ``assistant_mode`` (ADR-012) and the auto-generated ``title`` (chats/03) are fixed at
        creation only — they are a single source of truth and never re-written here for an
        existing session (rename is handled by the chats module).
        """
        if session_id is not None:
            existing = await self.get_session(session_id, user_id)
            if existing is not None and not self._is_expired(existing):
                return SessionContext(session=existing, is_new=False)
            # Missing or expired → new session (mode/assistant_mode/title fixed at creation).
        new_session = ChatSession(
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            assistant_mode=assistant_mode,
            title=title,
        )
        self._session.add(new_session)
        await self._session.flush()
        return SessionContext(session=new_session, is_new=True)

    async def touch_session(self, session: ChatSession) -> None:
        session.updated_at = _now()
        await self._session.flush()

    async def add_step(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        role: str,
        payload: dict[str, Any],
        usage: dict[str, Any] | None = None,
    ) -> ChatStep:
        step = ChatStep(
            session_id=session_id,
            message_step_id=message_step_id,
            role=role,
            payload=payload,
            usage=usage,
        )
        self._session.add(step)
        await self._session.flush()
        return step

    async def list_steps(self, session_id: uuid.UUID) -> list[ChatStep]:
        return list(
            await self._session.scalars(
                select(ChatStep)
                .where(ChatStep.session_id == session_id)
                .order_by(ChatStep.created_at, ChatStep.id)
            )
        )

    async def create_tool_call(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        tool_call_id: uuid.UUID,
        provider_tool_use_id: str,
    ) -> ToolCall:
        row = ToolCall(
            id=tool_call_id,
            session_id=session_id,
            message_step_id=message_step_id,
            tool_name=tool_name,
            provider_tool_use_id=provider_tool_use_id,
            args=args,
            status="pending",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_tool_call(self, tool_call_id: uuid.UUID) -> ToolCall | None:
        return await self._session.get(ToolCall, tool_call_id)

    async def complete_tool_call(
        self,
        *,
        tool_call_id: uuid.UUID,
        status: str,
        result: dict[str, Any] | None,
    ) -> bool:
        """Atomic pending → completed/errored. True if this call performed the transition."""
        updated = await self._session.scalar(
            text(
                "UPDATE tool_calls SET status = :status, result = CAST(:result AS JSONB), "
                "completed_at = now() WHERE id = :id AND status = 'pending' RETURNING id"
            ),
            {
                "status": status,
                "result": _json_or_null(result),
                "id": str(tool_call_id),
            },
        )
        return updated is not None

    async def next_step_after(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID, after_tool_call: uuid.UUID
    ) -> ChatStep | None:
        """For idempotent replay: the assistant step persisted right after a completed tool-result.

        Anchored to the tool-call's ``completed_at`` (the time of the /chat/tool-result
        transaction), NOT ``created_at``. ``created_at`` is unreliable here: in the /chat/run
        transaction the initiating tool_use assistant-step and the tool_call get an identical
        ``now()`` (Postgres ``now()`` = transaction-start time), so a ``created_at``-anchored
        filter catches the textless tool_use step instead of the later text step. The final
        assistant text step is written in the /chat/tool-result transaction, where its
        ``created_at`` equals that transaction's ``completed_at`` and is strictly later than the
        earlier /chat/run round — so ``>= completed_at`` deterministically selects this round's
        text step while excluding the prior tool_use step, independent of ``now()`` granularity.

        Multi-round tool-loop safe: a later round's assistant steps are written in a later
        transaction (strictly greater ``created_at``), so ASC ``.first()`` returns this round's
        step. Falls back to the latest assistant step if the tool_call (or its completion
        timestamp) is unavailable.
        """
        tool_call = await self._session.get(ToolCall, after_tool_call)
        query = select(ChatStep).where(
            ChatStep.session_id == session_id,
            ChatStep.message_step_id == message_step_id,
            ChatStep.role == "assistant",
        )
        if tool_call is not None and tool_call.completed_at is not None:
            rows = await self._session.scalars(
                query.where(ChatStep.created_at >= tool_call.completed_at).order_by(
                    ChatStep.created_at.asc(), ChatStep.id.asc()
                )
            )
            return rows.first()
        rows = await self._session.scalars(
            query.order_by(ChatStep.created_at.desc(), ChatStep.id.desc())
        )
        return rows.first()


def _json_or_null(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value)
