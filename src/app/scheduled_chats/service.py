"""CRUD for scheduled chat tasks (modules/scheduled-chats/02-api-contracts.md)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app import instance_config
from app.chat.repository import ChatRepository
from app.config import Settings, get_settings
from app.errors import (
    ActiveLimitExceededError,
    EmptyPatchError,
    NotCancellableError,
    NotPatchableError,
    PromptRequiredError,
    PromptTooLongError,
    RunAtNotInFutureError,
    RunAtTimezoneRequiredError,
    RunAtTooFarError,
    RunAtTooSoonError,
    ScheduledChatNotFoundError,
    SessionNotFoundError,
    UnsupportedAssistantModeError,
    UnsupportedGenerationModeError,
    UnsupportedModeError,
    UnsupportedModelError,
    ValidationFailedError,
)
from app.models import ScheduledChatTask
from app.scheduled_chats.cursor import InvalidCursorError, ScheduledChatCursor
from app.scheduled_chats.repository import ScheduledChatsRepository
from app.schemas.chat import GENERATION_MODE_ORDER
from app.schemas.scheduled_chats import (
    ScheduledChatCreateRequest,
    ScheduledChatDeleteResponse,
    ScheduledChatListResponse,
    ScheduledChatPatchRequest,
    ScheduledChatResponse,
)

_VALID_STATUSES = frozenset({"scheduled", "running", "completed", "failed", "cancelled"})
_VALID_MODES = frozenset({"credits", "byok"})
_VALID_ASSISTANT_MODES = frozenset({"chat", "code"})
_VALID_GENERATION_MODES = frozenset(GENERATION_MODE_ORDER)


class ScheduledChatsService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        repo: ScheduledChatsRepository | None = None,
        chats: ChatRepository | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._repo = repo or ScheduledChatsRepository(session)
        self._chats = chats or ChatRepository(session)

    def to_response(self, row: ScheduledChatTask) -> ScheduledChatResponse:
        return ScheduledChatResponse(
            id=row.id,
            sessionId=row.session_id,
            prompt=row.prompt,
            mode=row.mode,  # credits | byok (CHECK)
            assistantMode=row.assistant_mode,
            model=row.model,
            generationMode=row.generation_mode,
            runAt=row.run_at,
            status=row.status,
            resultSessionId=row.result_session_id,
            resultMessageStepId=row.result_message_step_id,
            errorCode=row.error_code,
            errorMessage=row.error_message,
            claimedAt=row.claimed_at,
            startedAt=row.started_at,
            finishedAt=row.finished_at,
            pushSentAt=row.push_sent_at,
            createdAt=row.created_at,
            updatedAt=row.updated_at,
        )

    async def create(
        self, *, user_id: uuid.UUID, body: ScheduledChatCreateRequest
    ) -> ScheduledChatResponse:
        prompt = self._validate_prompt(body.prompt)
        run_at = self._validate_run_at(body.runAt)
        generation_mode = self._validate_generation_mode(body.generationMode)
        mode, assistant_mode, model = await self._resolve_create_fields(
            user_id=user_id,
            session_id=body.sessionId,
            mode=body.mode,
            assistant_mode=body.assistantMode,
            model=body.model,
        )

        active = await self._repo.count_active(user_id=user_id)
        if active >= self._settings.scheduled_chat_max_active_per_user:
            raise ActiveLimitExceededError("too many active scheduled chats")

        now = datetime.datetime.now(tz=datetime.UTC)
        row = ScheduledChatTask(
            user_id=user_id,
            session_id=body.sessionId,
            prompt=prompt,
            mode=mode,
            assistant_mode=assistant_mode,
            model=model,
            generation_mode=generation_mode,
            run_at=run_at,
            status="scheduled",
            created_at=now,
            updated_at=now,
        )
        row = await self._repo.create(row)
        return self.to_response(row)

    async def get(self, *, user_id: uuid.UUID, task_id: uuid.UUID) -> ScheduledChatResponse:
        row = await self._repo.get_for_user(task_id=task_id, user_id=user_id)
        if row is None:
            raise ScheduledChatNotFoundError("scheduled chat not found")
        return self.to_response(row)

    async def list(
        self,
        *,
        user_id: uuid.UUID,
        status: str | None,
        cursor: str | None,
        limit: int,
    ) -> ScheduledChatListResponse:
        if status is not None and status not in _VALID_STATUSES:
            raise ValidationFailedError("invalid status filter")
        decoded: ScheduledChatCursor | None = None
        if cursor is not None:
            try:
                decoded = ScheduledChatCursor.decode(cursor)
            except InvalidCursorError as exc:
                raise ValidationFailedError("invalid cursor") from exc
        rows = await self._repo.list_for_user(
            user_id=user_id, status=status, cursor=decoded, limit=limit + 1
        )
        next_cursor: str | None = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = ScheduledChatCursor(created_at=last.created_at, id=last.id).encode()
            rows = rows[:limit]
        return ScheduledChatListResponse(
            items=[self.to_response(r) for r in rows],
            nextCursor=next_cursor,
        )

    async def patch(
        self, *, user_id: uuid.UUID, task_id: uuid.UUID, body: ScheduledChatPatchRequest
    ) -> ScheduledChatResponse:
        if not body.model_fields_set:
            raise EmptyPatchError("empty patch")
        row = await self._repo.get_for_user(task_id=task_id, user_id=user_id)
        if row is None:
            raise ScheduledChatNotFoundError("scheduled chat not found")
        if row.status != "scheduled":
            raise NotPatchableError("scheduled chat is not patchable")

        if "prompt" in body.model_fields_set and body.prompt is not None:
            row.prompt = self._validate_prompt(body.prompt)
        if "runAt" in body.model_fields_set and body.runAt is not None:
            row.run_at = self._validate_run_at(body.runAt)
        if "sessionId" in body.model_fields_set:
            row.session_id = body.sessionId
        if "generationMode" in body.model_fields_set:
            row.generation_mode = self._validate_generation_mode(body.generationMode)
        if "assistantMode" in body.model_fields_set:
            row.assistant_mode = self._validate_assistant_mode(body.assistantMode)
        if "model" in body.model_fields_set:
            row.model = self._validate_model(body.model)

        # After applying sessionId: non-null → mode from session (body mode ignored).
        if row.session_id is not None:
            sess = await self._chats.get_session(row.session_id, user_id)
            if sess is None:
                raise SessionNotFoundError("session not found")
            row.mode = sess.mode
        elif "mode" in body.model_fields_set:
            row.mode = self._validate_mode(body.mode if body.mode is not None else "credits")

        row.updated_at = datetime.datetime.now(tz=datetime.UTC)
        await self._session.flush()
        await self._session.refresh(row)
        return self.to_response(row)

    async def delete(
        self, *, user_id: uuid.UUID, task_id: uuid.UUID
    ) -> ScheduledChatDeleteResponse:
        row = await self._repo.get_for_user(task_id=task_id, user_id=user_id)
        if row is None:
            raise ScheduledChatNotFoundError("scheduled chat not found")
        if row.status == "scheduled":
            now = datetime.datetime.now(tz=datetime.UTC)
            row.status = "cancelled"
            row.finished_at = now
            row.updated_at = now
            await self._session.flush()
            return ScheduledChatDeleteResponse(deleted=True, status="cancelled")
        if row.status == "running":
            raise NotCancellableError("running scheduled chat cannot be cancelled")
        if row.status in ("completed", "failed", "cancelled"):
            await self._repo.delete(row)
            return ScheduledChatDeleteResponse(deleted=True, status=None)
        raise ScheduledChatNotFoundError("scheduled chat not found")

    def _validate_prompt(self, prompt: str) -> str:
        stripped = prompt.strip()
        if not stripped:
            raise PromptRequiredError("prompt is required")
        if len(stripped) > self._settings.scheduled_chat_prompt_max_chars:
            raise PromptTooLongError("prompt is too long")
        return stripped

    def _validate_run_at(self, run_at: datetime.datetime) -> datetime.datetime:
        if run_at.tzinfo is None:
            raise RunAtTimezoneRequiredError("runAt must be timezone-aware")
        now = datetime.datetime.now(tz=datetime.UTC)
        aware = run_at.astimezone(datetime.UTC)
        if aware <= now:
            raise RunAtNotInFutureError("runAt must be in the future")
        min_lead = datetime.timedelta(seconds=self._settings.scheduled_chat_min_lead_seconds)
        if aware < now + min_lead:
            raise RunAtTooSoonError("runAt is too soon")
        max_lead = datetime.timedelta(days=self._settings.scheduled_chat_max_lead_days)
        if aware > now + max_lead:
            raise RunAtTooFarError("runAt is too far in the future")
        return aware

    def _validate_mode(self, mode: str) -> str:
        if mode not in _VALID_MODES:
            raise UnsupportedModeError("unsupported mode")
        return mode

    def _validate_assistant_mode(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in _VALID_ASSISTANT_MODES:
            raise UnsupportedAssistantModeError("unsupported assistantMode")
        return value

    def _validate_generation_mode(self, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in _VALID_GENERATION_MODES:
            raise UnsupportedGenerationModeError("unsupported generationMode")
        return value

    def _validate_model(self, model: str | None) -> str | None:
        if model is None:
            return None
        stripped = model.strip()
        if not stripped or not instance_config.model_is_selectable(stripped):
            raise UnsupportedModelError("unsupported model")
        return stripped

    async def _resolve_create_fields(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID | None,
        mode: str | None,
        assistant_mode: str | None,
        model: str | None,
    ) -> tuple[str, str | None, str | None]:
        if session_id is not None:
            sess = await self._chats.get_session(session_id, user_id)
            if sess is None:
                raise SessionNotFoundError("session not found")
            # Mode from session; assistant/model body values stored but ignored on resume at run.
            return (
                sess.mode,
                self._validate_assistant_mode(assistant_mode),
                self._validate_model(model) if model is not None else None,
            )
        return (
            self._validate_mode(mode if mode is not None else "credits"),
            self._validate_assistant_mode(assistant_mode),
            self._validate_model(model) if model is not None else None,
        )
