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
from sqlalchemy.orm.attributes import flag_modified

from app.config import get_settings
from app.models import ChatSession, ChatStep, ToolCall

# Default max length of an auto-generated chat title (chats/03-architecture.md).
_TITLE_MAX_CHARS = 60

# Optional members of one media job ref (ADR-068 §2). jobId is mandatory and handled separately.
_MEDIA_JOB_REF_KEYS = ("kind", "status", "model", "creditsCharged")


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


def _media_job_ref(raw: Any) -> dict[str, Any] | None:
    """Normalize ONE stored media job ref (ADR-068 §2 shape); ``None`` when it has no ``jobId``.

    A ref without an addressable ``jobId`` is dropped: the client can neither poll
    ``GET /v1/media/jobs/{jobId}`` for it nor collapse it against a ref of another leg by the key
    of ADR-103 §2, so it would only add a phantom card.
    """
    if not isinstance(raw, dict):
        return None
    job_id = raw.get("jobId")
    if not job_id:
        return None
    ref: dict[str, Any] = {"jobId": str(job_id)}
    for key in _MEDIA_JOB_REF_KEYS:
        value = raw.get(key)
        if value is not None:
            ref[key] = value
    return ref


def _media_wizard_job_ref(raw: Any) -> dict[str, Any] | None:
    """Job ref of a wizard-submit user step (ADR-070 §3), as stored in ``payload.mediaWizard``.

    The wizard writes the submitted job on the user step itself, so this is the snapshot taken at
    submit time — ``status`` is ``queued`` by ADR-068 §1, ``model`` comes from the answered wizard.
    Same shape the history anchor builds from this very source
    (modules/chats/02-api-contracts.md §``GET /v1/chats/{id}``), so the recovery of ADR-103 §1 is
    not narrower than the anchor on this path either.
    """
    if not isinstance(raw, dict) or not raw.get("jobId"):
        return None
    answers = raw.get("answers")
    return _media_job_ref(
        {
            "jobId": raw["jobId"],
            "kind": raw.get("kind"),
            "model": answers.get("model") if isinstance(answers, dict) else None,
            "status": "queued",
        }
    )


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

    def is_expired(self, session: ChatSession) -> bool:
        """True when the session has exceeded the soft TTL (Q-001-1) → a new session on resume.

        Public so callers that need the same resume rule WITHOUT writing (e.g. the ADR-034 model
        gate that must know whether get_or_create_session would create) can reuse it.
        """
        ttl = get_settings().session_soft_ttl_seconds
        updated = session.updated_at
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=datetime.UTC)
        return (_now() - updated).total_seconds() > ttl

    async def get_or_create_session(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        mode: str,
        session_id: uuid.UUID | None,
        assistant_mode: str = "chat",
        title: str | None = None,
        model: str | None = None,
        character_id: str | None = None,
        workspace_project_id: uuid.UUID | None = None,
        generation_backend: str | None = None,
        temporary: bool = False,
    ) -> SessionContext:
        """Resume an owned, non-expired session or create a new one.

        ``project_id`` (ADR-022), ``assistant_mode`` (ADR-012), the auto-generated ``title``
        (chats/03), ``model`` (ADR-034) and ``workspace_project_id`` (ADR-036) are fixed at creation
        only — a single source of truth, never re-written here for an existing session (rename is
        handled by the chats module). ``project_id=None`` creates a «чистый чат» session
        (``chat_sessions.project_id = NULL``; ``site.*`` tools not offered). ``model=None`` stores
        ``chat_sessions.model = NULL`` (= the instance default model, resolved by the client at
        generation time — ADR-034 §3). ``character_id`` (ADR-097) is fixed the same way:
        ``None`` stores ``chat_sessions.character_id = NULL`` (= a chat without a character, the
        pre-feature behaviour); membership in the registry and the instance flag are validated by
        the caller BEFORE creation, so no invalid character is ever written.
        ``workspace_project_id=None`` creates a chat without a
        workspace (``chat_sessions.workspace_project_id = NULL`` — ADR-036; NOT the website-builder
        ``project_id``). ``generation_backend`` is also fixed on create so legacy `/v1/chat/*`
        sessions and `/v1/chat/v2/*` sessions do not accidentally mix provider-state and billing
        contracts. ``temporary`` (v2) is session-fixed the same way: hidden from ``GET /v1/chats``,
        still addressable by id. Ownership of the workspace is validated by the caller before
        creation.
        """
        if session_id is not None:
            existing = await self.get_session(session_id, user_id)
            if existing is not None and not self.is_expired(existing):
                return SessionContext(session=existing, is_new=False)
            # Missing or expired → new session (mode/assistant_mode/title fixed at creation).
        new_session = ChatSession(
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            assistant_mode=assistant_mode,
            title=title,
            model=model,
            character_id=character_id,
            workspace_project_id=workspace_project_id,
            generation_backend=generation_backend,
            is_temporary=temporary,
        )
        self._session.add(new_session)
        await self._session.flush()
        return SessionContext(session=new_session, is_new=True)

    async def set_title_if_absent(self, session: ChatSession, title: str | None) -> None:
        """Проставить автозаголовок чата, если его ещё нет (chats/03).

        Существует ради ОДНОГО случая — голосового сеанса (ADR-104 §3): там сессия создаётся
        кадром `start`, когда реплики ещё нет, поэтому `get_or_create_session` получает
        ``title=None``. Правило «автозаголовок из ПЕРВОГО сообщения» при этом остаётся тем же и
        той же функцией `derive_title` — меняется лишь момент, когда первое сообщение становится
        известно. Без этого голосовые чаты приходили бы в список без заголовка.

        Идемпотентно и НЕ переименовывает: непустой заголовок (в том числе заданный
        пользователем через chats) не трогается.
        """
        if session.title or not title:
            return
        session.title = title
        await self._session.flush()

    async def touch_session(self, session: ChatSession) -> None:
        session.updated_at = _now()
        await self._session.flush()

    async def set_generation_backend(
        self, session: ChatSession | uuid.UUID, generation_backend: str | None
    ) -> None:
        """Persist the public chat backend contract used by a session.

        Existing rows may have NULL because this field was introduced after the legacy endpoint.
        The orchestrator treats NULL as legacy unless a caller explicitly enters `/v1/chat/v2/*`,
        in which case the session is upgraded to `v2` before provider state is used.
        """
        if isinstance(session, uuid.UUID):
            row = await self._session.get(ChatSession, session)
            if row is None:  # pragma: no cover - callers operate on an existing session
                return
        else:
            row = session
        row.generation_backend = generation_backend
        await self._session.flush()

    async def set_provider_state(
        self, session: ChatSession | uuid.UUID, provider_state: dict[str, Any] | None
    ) -> None:
        """Persist opaque provider continuation state for a chat session.

        The payload is intentionally provider-owned JSON. Today OpenAI stores the latest
        Responses API ``response.id`` here, but NO turn reads it back: provider-side continuation
        is switched off (``_CONTINUATION_ENABLED`` in ``app.chat.openai_responses_client``,
        TD-032), and every v2 turn replays the full local history instead. The write keeps the
        handle current for the day that switch is flipped; it does not shorten a later request.
        Anthropic Messages API calls are still stateless, so they normally leave this unchanged or
        empty. The repository remains the single writer for ``chat_sessions``.
        """
        if isinstance(session, uuid.UUID):
            row = await self._session.get(ChatSession, session)
            if row is None:  # pragma: no cover - callers operate on an existing session
                return
        else:
            row = session
        row.provider_state = provider_state
        await self._session.flush()

    async def clear_provider_state(self, session_id: uuid.UUID) -> None:
        """Drop provider continuation state when local history is rewritten.

        edit+regenerate truncates ``chat_steps`` locally; any remote chain id that points to the
        old suffix is no longer a faithful representation of the chat, so it is dropped rather
        than left to name a history that no longer exists.

        This does not change what the next call sends while continuation is off
        (``_CONTINUATION_ENABLED``, TD-032): that call rebuilds from local history either way,
        because it never reads the stored handle. Clearing matters for the stored value itself —
        and for the moment the switch makes it live again.
        """
        await self.set_provider_state(session_id, None)

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
        # ADR-021: order by the monotonic `seq` (insertion order), NOT (created_at, id).
        # In the server-side tool-loop tool_use + tool_result are written in one transaction →
        # equal transaction-time created_at; the UUID-id tie-break is random and could place
        # tool_result before its tool_use → orphan tool_result → Anthropic 400 (BUG-5). `seq`
        # guarantees tool_use < tool_result by insertion order.
        return list(
            await self._session.scalars(
                select(ChatStep)
                .where(ChatStep.session_id == session_id)
                .order_by(ChatStep.seq.asc())
            )
        )

    async def get_assistant_step(
        self, session_id: uuid.UUID, step_id: uuid.UUID
    ) -> ChatStep | None:
        """One ``role='assistant'`` step of THIS session, or ``None`` (ADR-100, 06-rbac).

        ``session_id`` is part of the predicate, never an afterthought: the caller has already
        resolved the session by ``(id, user_id)``, so scoping the step to it means a ``stepId``
        from someone else's chat is unreachable even when the UUID is known. ``role`` is matched
        strictly — a user step or a tool step is «not an assistant answer», not «forbidden», and
        both map to the same ``404 step_not_found``: nothing about a foreign row is revealed.
        """
        row: ChatStep | None = await self._session.scalar(
            select(ChatStep).where(
                ChatStep.id == step_id,
                ChatStep.session_id == session_id,
                ChatStep.role == "assistant",
            )
        )
        return row

    async def has_assistant_step(self, session_id: uuid.UUID, message_step_id: uuid.UUID) -> bool:
        """Есть ли у ЭТОГО хода хоть один шаг ассистента.

        Нужен ровно для одного решения: помечать ли ход, упавший после записи реплики
        пользователя. Шаг пользователя коммитится ДО сетевого вызова намеренно — чтобы не держать
        соединение с базой открытым всю генерацию, — поэтому отказ провайдера оставляет реплику в
        истории без ответа, и на следующем ходу модель отвечает на неё, а не на новую.
        Существующий шаг ассистента означает, что ход что-то уже ответил (например, виток с
        вызовом инструмента), и вторая пометка была бы ложью о состоянии.
        """
        found: uuid.UUID | None = await self._session.scalar(
            select(ChatStep.id)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "assistant",
            )
            .limit(1)
        )
        return found is not None

    async def generation_mode_for_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> str:
        """Return the turn-scoped generation mode stored on the user step.

        ``/v1/chat/v2/tool-result`` has no generationMode field by design. When it continues a
        pending tool-use turn, it must reuse the exact mode chosen by the original
        ``/v1/chat/v2/run`` request so provider options and wallet billing stay stable across the
        whole message step.

        The accepted set MUST list ALL four modes (ADR-064 §12). A mode missing here degrades
        SILENTLY to ``general``: nothing raises, but the continuation of a quiz turn loses BOTH its
        price AND ``quiz.generate`` from the offered tool-set (axis C is computed from this value).
        """
        value = await self._session.scalar(
            select(ChatStep.payload["generationMode"].astext)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "user",
            )
            .order_by(ChatStep.seq.asc())
            .limit(1)
        )
        return (
            value
            if isinstance(value, str)
            and value in {"general", "research", "reasoning", "study_learn"}
            else "general"
        )

    async def find_media_wizard_state(
        self, session_id: uuid.UUID, selection_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Latest persisted mediaChoices wizard state for ``selectionId`` (ADR-070).

        Prefers the ``media.ask_params`` tool result (answers are patched in place during the
        wizard so intermediate taps do not create chat bubbles). Falls back to a completed
        user-step ``mediaWizard`` summary.
        """
        from app.chat.tools import TOOL_MEDIA_ASK_PARAMS

        sid = str(selection_id)
        steps = await self.list_steps(session_id)
        for step in reversed(steps):
            payload = step.payload if isinstance(step.payload, dict) else {}
            if step.role == "tool" and payload.get("toolName") == TOOL_MEDIA_ASK_PARAMS:
                result = payload.get("result")
                if isinstance(result, dict) and result.get("selectionId") == sid:
                    return result
            if step.role == "user":
                wizard = payload.get("mediaWizard")
                if isinstance(wizard, dict) and wizard.get("selectionId") == sid:
                    return wizard
        return None

    async def patch_media_ask_params_result(
        self,
        session_id: uuid.UUID,
        selection_id: uuid.UUID,
        *,
        answers: dict[str, str],
        step: str,
        questions: list[dict[str, Any]],
    ) -> ChatStep | None:
        """Update the ask_params tool-result in place (wizard progress without new history rows)."""
        from app.chat.tools import TOOL_MEDIA_ASK_PARAMS

        sid = str(selection_id)
        steps = await self.list_steps(session_id)
        for step_row in reversed(steps):
            payload = step_row.payload if isinstance(step_row.payload, dict) else {}
            if step_row.role != "tool" or payload.get("toolName") != TOOL_MEDIA_ASK_PARAMS:
                continue
            result = payload.get("result")
            if not isinstance(result, dict) or result.get("selectionId") != sid:
                continue
            new_result = {
                **result,
                "answers": answers,
                "step": step,
                "questions": questions,
            }
            step_row.payload = {**payload, "result": new_result}
            flag_modified(step_row, "payload")
            await self._session.flush()
            return step_row
        return None

    async def recent_user_payloads(
        self, session_id: uuid.UUID, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Newest-first user-step payloads (for recent chat-photo reuse / ask-first hint)."""
        steps = await self.list_steps(session_id)
        payloads: list[dict[str, Any]] = []
        for step in reversed(steps):
            if step.role != "user":
                continue
            if isinstance(step.payload, dict):
                payloads.append(step.payload)
            if len(payloads) >= limit:
                break
        return payloads

    async def last_media_job_ref(self, session_id: uuid.UUID) -> dict[str, Any] | None:
        """Most recent media jobId recorded in this chat (assistant mediaJobs or generate_* result).

        Used to steer edit follow-ups toward image-to-image via ``sourceJobId``.
        """
        return await self._last_media_job_ref(session_id, kind=None)

    async def last_image_job_ref(self, session_id: uuid.UUID) -> dict[str, Any] | None:
        """Most recent **image** media job in this chat (for Use-last-photo video wizard step)."""
        return await self._last_media_job_ref(session_id, kind="image")

    async def _last_media_job_ref(
        self, session_id: uuid.UUID, *, kind: str | None
    ) -> dict[str, Any] | None:
        from app.chat.tools import TOOL_MEDIA_GENERATE_IMAGE, TOOL_MEDIA_GENERATE_VIDEO

        generate_tools = {TOOL_MEDIA_GENERATE_IMAGE, TOOL_MEDIA_GENERATE_VIDEO}
        steps = await self.list_steps(session_id)
        for step in reversed(steps):
            payload = step.payload if isinstance(step.payload, dict) else {}
            if step.role == "assistant":
                jobs = payload.get("mediaJobs")
                if isinstance(jobs, list):
                    for job in reversed(jobs):
                        if not isinstance(job, dict) or not job.get("jobId"):
                            continue
                        if kind is not None and job.get("kind") != kind:
                            continue
                        return job
            if step.role == "tool" and payload.get("toolName") in generate_tools:
                result = payload.get("result")
                if not isinstance(result, dict) or not result.get("jobId"):
                    continue
                if kind is not None and result.get("kind") != kind:
                    continue
                return result
        return None

    async def last_tool_result_for_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID, tool_name: str
    ) -> dict[str, Any] | None:
        """Last non-empty ``result`` of ``tool_name`` within ONE turn (``message_step_id``).

        Turn-scoped fallback producer for ``ChatResponse.quiz`` (ADR-064 §7): when the accumulator
        of the CURRENT call is empty, the pool of the TURN is recovered from its tool steps, so
        every leg of the turn (continuation, idempotent replay, ``blocked+max_tokens``) carries the
        same pool and the ``assistantMessage`` suppression keyed on it never silently lapses.

        Ordered by ``seq`` DESC (last wins, ADR-021 insertion order). ``payload->>'result'`` is SQL
        NULL both for a missing key and for a JSON ``null``, so the filter means «has a real
        result» — an errored quiz round (``error`` set, ``result`` null) is never returned.

        The caller MUST gate this read by the effective turn mode: outside quiz turns (all other
        modes and the whole legacy path) it is not executed at all.
        """
        value = await self._session.scalar(
            select(ChatStep.payload["result"])
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "tool",
                ChatStep.payload["toolName"].astext == tool_name,
                ChatStep.payload["result"].astext.isnot(None),
            )
            .order_by(ChatStep.seq.desc())
            .limit(1)
        )
        return value if isinstance(value, dict) else None

    async def tool_results_for_message_step(
        self,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_names: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Successful tool ``result`` payloads for ``tool_names`` within ONE turn, seq ASC.

        Producer 2 of ``ChatResponse.documents`` (ADR-101 §4): recover every successful
        document.create / document.update of this ``message_step_id`` so continuations / replay /
        ``blocked+max_tokens`` carry the turn's cards. Errored rounds (``result`` null) are
        excluded — same SQL null semantics as ``last_tool_result_for_message_step``.

        ``mediaJobs`` does NOT read through here: its recovery may not be narrower than the history
        anchor (ADR-103 §1), which also reads assistant ``payload.mediaJobs`` and the wizard's user
        step — see ``media_job_refs_for_message_step``.
        """
        if not tool_names:
            return []
        rows = (
            await self._session.execute(
                select(ChatStep.payload["result"])
                .where(
                    ChatStep.session_id == session_id,
                    ChatStep.message_step_id == message_step_id,
                    ChatStep.role == "tool",
                    ChatStep.payload["toolName"].astext.in_(tuple(tool_names)),
                    ChatStep.payload["result"].astext.isnot(None),
                )
                .order_by(ChatStep.seq.asc())
            )
        ).scalars()
        return [value for value in rows if isinstance(value, dict)]

    async def media_job_refs_for_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """Media job refs already recorded by the steps of ONE turn, ``seq ASC`` (ADR-103 §1).

        Producer 2 of ``ChatResponse.mediaJobs``. The source list is exactly the one normatively
        fixed for the history anchor (modules/chats/02-api-contracts.md §``GET /v1/chats/{id}``)
        and may NOT be narrower than it:

        * ``role='tool'`` — successful ``media.generate_image`` / ``media.generate_video`` result
          (an errored round has a null ``result`` and creates no job, ADR-103 §4);
        * ``role='assistant'`` — ``payload.mediaJobs`` published by an earlier leg of the turn;
        * ``role='user'`` — ``payload.mediaWizard.jobId``: the wizard submit (ADR-070 §3) runs
          BEFORE the LLM and may leave no ``media.generate_*`` tool step at all, so a recovery built
          on tool results alone would lose exactly the jobs it exists to recover.

        ONE query per turn on ``(session_id, message_step_id)`` — the same read and the same key
        ADR-101 §4 already spends on ``documents`` (ADR-103 §8). Refs are returned RAW, in step
        order and without dedup: the fold by ``jobId`` and the ``creditsCharged`` projection rule
        (ADR-103 §2–3) belong to the response assembly, not to persistence — the history anchor
        reads the same rows and must NOT inherit the zeroing.
        """
        from app.chat.tools import TOOL_MEDIA_GENERATE_IMAGE, TOOL_MEDIA_GENERATE_VIDEO

        generate_tools = {TOOL_MEDIA_GENERATE_IMAGE, TOOL_MEDIA_GENERATE_VIDEO}
        rows = (
            await self._session.execute(
                select(ChatStep.role, ChatStep.payload)
                .where(
                    ChatStep.session_id == session_id,
                    ChatStep.message_step_id == message_step_id,
                )
                .order_by(ChatStep.seq.asc())
            )
        ).all()
        refs: list[dict[str, Any]] = []
        for role, raw_payload in rows:
            payload = raw_payload if isinstance(raw_payload, dict) else {}
            ref: dict[str, Any] | None
            if role == "tool" and payload.get("toolName") in generate_tools:
                ref = _media_job_ref(payload.get("result"))
                if ref is not None:
                    refs.append(ref)
            elif role == "assistant":
                jobs = payload.get("mediaJobs")
                if isinstance(jobs, list):
                    refs.extend(r for job in jobs if (r := _media_job_ref(job)) is not None)
            elif role == "user":
                ref = _media_wizard_job_ref(payload.get("mediaWizard"))
                if ref is not None:
                    refs.append(ref)
        return refs

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

    async def list_tool_calls_for_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> list[ToolCall]:
        """All tool_calls of one assistant turn (ADR-025 barrier). Single query, no N+1.

        Ordered by creation order (id is insertion-stable enough here; the orchestrator filters
        client-side rows and checks their status to decide whether the barrier is closed).

        ``populate_existing=True`` (CRITICAL identity-map fix): ``complete_tool_call()`` flips the
        status with a raw SQL ``UPDATE ... RETURNING`` that does NOT touch the ORM identity-map, so
        ToolCall rows already loaded earlier in this session (e.g. via ``get_tool_call`` in
        ``tool_result``) keep their stale ``status='pending'``. Without this option the barrier
        SELECT would re-return those cached objects unchanged and the continuation would never run.
        ``populate_existing`` forces the freshly-SELECTed DB values (status='completed'/'errored')
        to overwrite the cached attributes, so the barrier sees the actual statuses.
        """
        return list(
            await self._session.scalars(
                select(ToolCall)
                .where(
                    ToolCall.session_id == session_id,
                    ToolCall.message_step_id == message_step_id,
                )
                .order_by(ToolCall.created_at.asc(), ToolCall.id.asc())
                .execution_options(populate_existing=True)
            )
        )

    async def complete_tool_call(
        self,
        *,
        tool_call_id: uuid.UUID,
        status: str,
        result: dict[str, Any] | None,
    ) -> bool:
        """Atomic pending → completed/errored. True if this call performed the transition.

        The raw SQL ``UPDATE`` bypasses the ORM identity-map: any ToolCall instance already loaded
        in this session keeps a stale ``status='pending'``. The freshness guarantee the ADR-025
        barrier relies on is provided by ``list_tool_calls_for_step`` (``populate_existing=True``),
        which re-populates the exact rows the barrier reads from the DB. We intentionally do NOT
        ``expire`` the cached instance here: an expired ToolCall would lazy-refresh on the next
        attribute access (e.g. the audit step reading ``tool_name``/``id`` right after this call),
        and that synchronous refresh outside a greenlet context raises ``MissingGreenlet`` in the
        async engine.
        """
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

    async def truncate_from_message_step(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> int | None:
        """Truncate session history from a turn for edit+regenerate (ADR-040 §2).

        Deletes the user-step identified by ``message_step_id`` and EVERYTHING after it (its
        assistant/tool steps and all later turns), plus the ``tool_calls`` of the truncated turns.
        Returns the number of deleted ``chat_steps`` (>= 1 when the anchor is found), or ``None``
        when no ``role='user'`` step with ``message_step_id`` exists in the session (caller →
        404 message_not_found).

        - **anchor** = min ``chat_steps.seq`` of the session's step with this ``message_step_id``
          AND ``role='user'`` (ADR-021 monotonic seq is the only reliable order key; ADR-040 §4в:
          the anchor is matched STRICTLY by ``role='user'`` — an assistant/tool-only message_step_id
          resolves to None → 404). None → return None.
        - ``tool_calls`` of the truncated turns are deleted EXPLICITLY (ADR-040 §2 step 3): their FK
          is on ``chat_sessions`` (session_id), NOT on ``chat_steps`` — deleting steps does NOT
          cascade them, so without this they would be orphaned. The subquery reads the
          STILL-EXISTING steps (seq >= anchor) before the steps are deleted.
        - All DELETEs are scoped by ``session_id`` (an already ownership-checked session — the
          caller only truncates a resumed, owned session, ADR-040 §5). No cross-session deletion.
        - ``flush()`` only — the surrounding /chat/run request transaction commits as one unit with
          the new turn's generation (ADR-040 §2), so truncation + new user-step are atomic.
        """
        anchor = await self._session.scalar(
            text(
                "SELECT min(seq) FROM chat_steps "
                "WHERE session_id = :sid AND message_step_id = :msid AND role = 'user'"
            ),
            {"sid": str(session_id), "msid": str(message_step_id)},
        )
        if anchor is None:
            return None
        # Delete tool_calls of the truncated turns FIRST (FK is on chat_sessions, not chat_steps →
        # no cascade). The subquery reads the still-existing chat_steps (seq >= anchor).
        await self._session.execute(
            text(
                "DELETE FROM tool_calls WHERE session_id = :sid AND message_step_id IN ("
                "SELECT DISTINCT message_step_id FROM chat_steps "
                "WHERE session_id = :sid AND seq >= :anchor)"
            ),
            {"sid": str(session_id), "anchor": anchor},
        )
        result = await self._session.execute(
            text("DELETE FROM chat_steps WHERE session_id = :sid AND seq >= :anchor RETURNING id"),
            {"sid": str(session_id), "anchor": anchor},
        )
        deleted = len(result.fetchall())
        await self._session.flush()
        return deleted

    async def assistant_tool_step_id(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID
    ) -> uuid.UUID | None:
        """ADR-025: id of the assistant step carrying the current turn's tool_use blocks.

        For a status=tool_call response on /chat/tool-result with the barrier still open, stepId
        must point at the assistant step whose payload holds the (still-pending) tool_use blocks —
        the latest assistant step of this turn (greatest ``seq``). Returns None if absent.
        """
        step_id: uuid.UUID | None = await self._session.scalar(
            select(ChatStep.id)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "assistant",
            )
            .order_by(ChatStep.seq.desc())
            .limit(1)
        )
        return step_id

    async def next_step_after(
        self, session_id: uuid.UUID, message_step_id: uuid.UUID, after_tool_call: uuid.UUID
    ) -> ChatStep | None:
        """For idempotent replay: the assistant step persisted right after a completed tool-result.

        ADR-021: anchored to the monotonic ``seq``, NOT ``created_at``. The tool step recording
        this tool-call's tool_result has a deterministic ``seq``; the next assistant step in this
        message-step with a strictly greater ``seq`` is the round's continuation. ``created_at`` is
        unreliable as an order key (transaction-time ``now()`` is equal for steps of one
        transaction; the UUID-id tie-break is random), so it is not used here.

        Multi-round tool-loop safe: a later round's assistant step has a greater ``seq`` than this
        round's tool step, but the FIRST (smallest seq) assistant step after the anchor is this
        round's step (ASC ``.first()``). Falls back to the latest assistant step (max seq) if the
        anchor tool step is unavailable.
        """
        anchor_seq = await self._session.scalar(
            select(ChatStep.seq)
            .where(
                ChatStep.session_id == session_id,
                ChatStep.message_step_id == message_step_id,
                ChatStep.role == "tool",
                ChatStep.payload["toolCallId"].astext == str(after_tool_call),
            )
            .order_by(ChatStep.seq.asc())
            .limit(1)
        )
        query = select(ChatStep).where(
            ChatStep.session_id == session_id,
            ChatStep.message_step_id == message_step_id,
            ChatStep.role == "assistant",
        )
        if anchor_seq is not None:
            rows = await self._session.scalars(
                query.where(ChatStep.seq > anchor_seq).order_by(ChatStep.seq.asc())
            )
            return rows.first()
        rows = await self._session.scalars(query.order_by(ChatStep.seq.desc()))
        return rows.first()


def _json_or_null(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value)
