"""Chats use-cases: list/get/steps-view/rename-pin/delete (chats/03-architecture.md).

Owner isolation is enforced at the repository (WHERE user_id = sub); a missing/foreign chat
raises NotFoundError → 404 (BR-CH-1, never reveal foreign existence). steps-view exposes only
domain tool names and human summaries — NEVER raw provider tool_use.id (ADR-008).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from typing import Any

from app.chats.cursor import ChatCursor, InvalidCursorError
from app.chats.repository import ChatsRepository
from app.errors import NotFoundError, ValidationFailedError
from app.models import ChatSession, ChatStep, ToolCall

_SUMMARY_MAX_CHARS = 120


@dataclass(frozen=True)
class ChatListItemView:
    id: uuid.UUID
    title: str | None
    preview: str | None
    assistant_mode: str
    is_pinned: bool
    workspace_project_id: uuid.UUID | None
    updated_at: datetime.datetime


@dataclass(frozen=True)
class ChatListView:
    items: list[ChatListItemView]
    next_cursor: str | None


@dataclass(frozen=True)
class ChatStepView:
    id: uuid.UUID
    message_step_id: uuid.UUID
    role: str
    payload: dict[str, Any]
    usage: dict[str, Any] | None
    created_at: datetime.datetime


@dataclass(frozen=True)
class ChatHistoryView:
    id: uuid.UUID
    title: str | None
    assistant_mode: str
    mode: str
    steps: list[ChatStepView]


@dataclass(frozen=True)
class StepsViewStep:
    kind: str  # reasoning | tool_call | tool_result | assistant_message
    tool_name: str | None
    summary: str
    created_at: datetime.datetime


@dataclass(frozen=True)
class StepsView:
    message_step_id: uuid.UUID
    step_count: int
    steps: list[StepsViewStep]


def _truncate(value: str, limit: int = _SUMMARY_MAX_CHARS) -> str:
    collapsed = " ".join(value.split())
    return collapsed[:limit]


def _text_summary(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return _truncate(" ".join(p for p in parts if p))


class ChatsService:
    def __init__(self, repo: ChatsRepository) -> None:
        self._repo = repo

    async def list_chats(
        self,
        *,
        user_id: uuid.UUID,
        query: str | None,
        cursor: str | None,
        limit: int,
    ) -> ChatListView:
        decoded: ChatCursor | None = None
        if cursor:
            try:
                decoded = ChatCursor.decode(cursor)
            except InvalidCursorError as exc:
                raise ValidationFailedError("invalid cursor") from exc

        page = await self._repo.list_chats(
            user_id=user_id, query=query, cursor=decoded, limit=limit
        )
        items = [
            ChatListItemView(
                id=item.session.id,
                title=item.session.title,
                preview=item.preview,
                assistant_mode=item.session.assistant_mode,
                is_pinned=item.session.is_pinned,
                # workspace_project_id is a Sprint-2 column; not present yet → always null.
                workspace_project_id=None,
                updated_at=item.session.updated_at,
            )
            for item in page.items
        ]
        return ChatListView(items=items, next_cursor=page.next_cursor)

    async def _require_session(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession:
        session = await self._repo.get_session(session_id, user_id)
        if session is None:
            raise NotFoundError("chat not found")
        return session

    async def get_history(self, session_id: uuid.UUID, user_id: uuid.UUID) -> ChatHistoryView:
        session = await self._require_session(session_id, user_id)
        steps = await self._repo.list_steps(session_id)
        return ChatHistoryView(
            id=session.id,
            title=session.title,
            assistant_mode=session.assistant_mode,
            mode=session.mode,
            steps=[
                ChatStepView(
                    id=step.id,
                    message_step_id=step.message_step_id,
                    role=step.role,
                    payload=self._sanitize_payload(step),
                    usage=step.usage,
                    created_at=step.created_at,
                )
                for step in steps
            ],
        )

    @staticmethod
    def _sanitize_payload(step: ChatStep) -> dict[str, Any]:
        """Drop internal-only fields from a tool step payload before exposing it (ADR-008).

        Tool steps persist ``providerToolUseId`` (raw ``toolu_...``) for continuation replay; it
        must never surface in the public history. Other roles' payloads are content blocks only.
        """
        payload = dict(step.payload)
        payload.pop("providerToolUseId", None)
        return payload

    async def steps_view(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        message_step_id: uuid.UUID | None,
    ) -> StepsView:
        await self._require_session(session_id, user_id)
        target = message_step_id or await self._repo.latest_message_step_id(session_id)
        if target is None:
            raise NotFoundError("no steps for chat")

        steps = await self._repo.steps_for_message(session_id, target)
        if not steps:
            raise NotFoundError("message step not found")
        tool_calls = await self._repo.tool_calls_for_message(session_id, target)
        # Map raw provider tool_use.id → domain tool name so assistant tool_use blocks (which
        # carry the raw anthropic name/id) resolve to the public dotted name without exposing
        # the raw id (ADR-008).
        by_provider_id = {tc.provider_tool_use_id: tc for tc in tool_calls.values()}

        view_steps: list[StepsViewStep] = []
        for step in steps:
            view_steps.extend(self._render_step(step, tool_calls, by_provider_id))
        return StepsView(
            message_step_id=target,
            step_count=len(view_steps),
            steps=view_steps,
        )

    @staticmethod
    def _render_step(
        step: ChatStep,
        tool_calls: dict[uuid.UUID, ToolCall],
        by_provider_id: dict[str, ToolCall],
    ) -> list[StepsViewStep]:
        """Flatten one chat_step into UI steps (reasoning/tool_call/tool_result/assistant_message).

        Never emits secrets or raw provider tool_use.id — only domain tool names (with a dot)
        and short human summaries (ADR-008, chats/06-rbac).
        """
        out: list[StepsViewStep] = []
        if step.role == "assistant":
            content = step.payload.get("content", [])
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            summary = _text_summary(step.payload)
            if summary:
                out.append(
                    StepsViewStep(
                        kind="reasoning" if has_tool_use else "assistant_message",
                        tool_name=None,
                        summary=summary,
                        created_at=step.created_at,
                    )
                )
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    # Resolve the public dotted tool name via the raw provider id (never emitted).
                    match = by_provider_id.get(str(block.get("id")))
                    domain_name = match.tool_name if match is not None else None
                    out.append(
                        StepsViewStep(
                            kind="tool_call",
                            tool_name=domain_name,
                            summary=(
                                f"вызов {domain_name}" if domain_name else "вызов инструмента"
                            ),
                            created_at=step.created_at,
                        )
                    )
        elif step.role == "tool":
            tool_call_id = step.payload.get("toolCallId")
            tool_name = step.payload.get("toolName")
            if tool_name is None and tool_call_id is not None:
                try:
                    match = tool_calls.get(uuid.UUID(str(tool_call_id)))
                except ValueError:
                    match = None
                tool_name = match.tool_name if match is not None else None
            out.append(
                StepsViewStep(
                    kind="tool_result",
                    tool_name=tool_name,
                    summary=f"результат {tool_name}" if tool_name else "результат инструмента",
                    created_at=step.created_at,
                )
            )
        # role == "user" is not part of the assistant step-view (it is the trigger, not a step).
        return out

    async def rename_or_pin(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        title: str | None,
        set_title: bool,
        is_pinned: bool | None,
    ) -> ChatSession:
        session = await self._require_session(session_id, user_id)
        return await self._repo.update_metadata(
            session, title=title, set_title=set_title, is_pinned=is_pinned
        )

    async def delete_chat(self, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        deleted = await self._repo.delete_session(session_id, user_id)
        if not deleted:
            # Idempotent: a missing/foreign chat (incl. already-deleted) → 404 (chats/02).
            raise NotFoundError("chat not found")
