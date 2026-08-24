"""ORM table definitions mirroring 03-data-model.md (9 tables, enums, indexes)."""

from __future__ import annotations

import datetime
import decimal
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import BIGINT, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - dev without sync dep
    Vector = None  # type: ignore[misc, assignment]

from app.models.base import Base

# --- Enum value tuples (match CREATE TYPE in 03-data-model.md) ---
SUBSCRIPTION_STATUS = ("active", "expired", "none")
LEDGER_TX_TYPE = ("credit", "debit")
# ADR-016: extended BYOK statuses (validating/offline/expired) added in migration 0004.
BYOK_KEY_STATUS = ("valid", "invalid", "missing", "validating", "offline", "expired")
CHAT_MODE = ("credits", "byok")
CHAT_ROLE = ("user", "assistant", "tool")
TOOL_CALL_STATUS = ("pending", "completed", "errored")
# ADR-012: assistant type (chat|code) — orthogonal to chat_mode (billing).
ASSISTANT_MODE = ("chat", "code")

_subscription_status_enum = Enum(
    *SUBSCRIPTION_STATUS, name="subscription_status", create_type=False
)
_ledger_tx_type_enum = Enum(*LEDGER_TX_TYPE, name="ledger_tx_type", create_type=False)
_byok_key_status_enum = Enum(*BYOK_KEY_STATUS, name="byok_key_status", create_type=False)
_chat_mode_enum = Enum(*CHAT_MODE, name="chat_mode", create_type=False)
_chat_role_enum = Enum(*CHAT_ROLE, name="chat_role", create_type=False)
_tool_call_status_enum = Enum(*TOOL_CALL_STATUS, name="tool_call_status", create_type=False)
_assistant_mode_enum = Enum(*ASSISTANT_MODE, name="assistant_mode", create_type=False)

_uuid_default = sa_text("gen_random_uuid()")
_now = sa_text("now()")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    trial_used: Mapped[bool] = mapped_column(nullable=False, server_default=sa_text("false"))
    # ADR Figma-gap (migration 0004): human-readable profile name (Profile screen), nullable.
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        _subscription_status_enum, nullable=False, server_default=sa_text("'none'")
    )
    plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Auto-renew intent (ADR-047): true = will renew, false = cancelled/expiring, null = unknown
    # (RU/broadapps path does not report it). Set from Adapty events; surfaced in /policy/effective.
    will_renew: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (Index("ix_subscriptions_expires_at", "expires_at"),)


class Wallet(Base):
    __tablename__ = "wallets"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=sa_text("0"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (CheckConstraint("balance >= 0", name="ck_wallets_balance_nonneg"),)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(_ledger_tx_type_enum, nullable=False)
    amount: Mapped[int] = mapped_column(BIGINT, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
        UniqueConstraint("user_id", "idempotency_key", name="ux_ledger_idempotency"),
        Index("ix_ledger_user_created", "user_id", "created_at", postgresql_using="btree"),
    )


class BYOKKey(Base):
    __tablename__ = "byok_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_status: Mapped[str] = mapped_column(
        _byok_key_status_enum, nullable=False, server_default=sa_text("'missing'")
    )
    enabled: Mapped[bool] = mapped_column(nullable=False, server_default=sa_text("false"))
    # ADR-044 §4 (migration 0013): provider detected from the key prefix at set time. NULL = legacy
    # row (pre-migration) or an unrecognized key format; the service detects on the fly from the
    # decrypted key as a fallback (ADR-044 §5/§6) and writes the fresh value on the next set. TEXT,
    # not an enum — allowed values {anthropic, openai} are enforced by the detector, not the DB.
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # ADR-022 (migration 0007): nullable. NULL = «чистый чат» without website-builder (server-side
    # site.* tools are NOT offered to Claude); a non-empty string = website-builder available.
    # Fixed at session creation; on resume it is read from the session (request field ignored).
    # NOT to be confused with workspace_project_id (workspace, ADR-013).
    project_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str] = mapped_column(_chat_mode_enum, nullable=False)  # billing_mode (ADR-012)
    # ADR-034 (migration 0010): user-selected model, session-fixed at creation. nullable; NULL =
    # «дефолтная модель инстанса» (the active provider's default, resolved by the client at
    # generation time). Validated against allowed_models() before write; on resume not re-written.
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- Figma-gap extension (migration 0004), chats/preferences modules ---
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ADR-012: assistant type fixed at session creation (chat|code), distinct from `mode`.
    assistant_mode: Mapped[str] = mapped_column(
        _assistant_mode_enum, nullable=False, server_default=sa_text("'chat'")
    )
    # ADR-036 (migration 0011): workspace («рабочее пространство») binding, nullable. NULL = chat
    # without a workspace (backward-compatible). Session-fixed at creation (like mode/model). FK to
    # workspace_projects with ON DELETE SET NULL (deleting a workspace keeps its chats as «чистые»).
    # NOT to be confused with project_id (Text, website-builder — ADR-022); different field/meaning.
    workspace_project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Turn generation modes are stored on chat_steps, while this session-level provider state keeps
    # opaque continuation handles that make a later turn cheaper/simpler to send to the provider.
    # OpenAI stores the latest Responses API response id here; Anthropic Messages stays stateless.
    provider_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Nullable for existing rows. NULL and "legacy" both mean the original /v1/chat/run backend;
    # "v2" means the session is owned by /v1/chat/v2/* and can use provider-side continuation
    # state such as OpenAI Responses API previous_response_id.
    generation_backend: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_pinned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    # Temporary chat (v2): session-fixed at create via POST /v1/chat/v2/run ``temporary``.
    # Hidden from GET /v1/chats; still addressable by id for multi-turn / DELETE.
    is_temporary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        Index("ix_sessions_user_updated", "user_id", "updated_at"),
        # chats list: pinned first, then recency (BR-CH-3).
        Index(
            "ix_sessions_user_pinned_updated",
            "user_id",
            sa_text("is_pinned DESC"),
            sa_text("updated_at DESC"),
        ),
        # List path only reads non-temporary sessions.
        Index(
            "ix_sessions_user_non_temporary_updated",
            "user_id",
            sa_text("updated_at DESC"),
            postgresql_where=sa_text("is_temporary = false"),
        ),
        # ADR-036: filter «чаты проекта» (GET /v1/chats?workspaceProjectId=) and chatCount.
        Index("ix_sessions_workspace", "workspace_project_id"),
    )


class ChatStep(Base):
    __tablename__ = "chat_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    # ADR-021 (migration 0006): monotonic global identity. Step order in a session is determined
    # by `seq` (insertion order), NOT `created_at`. `seq` guarantees tool_use < tool_result for
    # the server-side tool-loop (same transaction → equal created_at, random UUID tie-break →
    # orphan tool_result → Anthropic 400, BUG-5). Assigned by the DB on INSERT; never set in code.
    seq: Mapped[int] = mapped_column(
        BIGINT,
        Identity(always=True),
        nullable=False,
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    message_step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(_chat_role_enum, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        # ADR-021: reconstruction / next-step lookup order by seq (NOT created_at).
        Index("ix_steps_session_seq", "session_id", "seq"),
        Index("ix_steps_message_step", "message_step_id"),
    )


class ChatChunk(Base):
    """Indexed chat step fragment for cross-session semantic search (RAG memory)."""

    __tablename__ = "chat_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    chat_step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workspace_project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    session_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(1536), nullable=False)  # type: ignore[misc]
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        UniqueConstraint("chat_step_id", "chunk_index", name="uq_chat_chunks_step_chunk"),
        Index("ix_chat_chunks_user_created", "user_id", "created_at"),
        Index("ix_chat_chunks_session", "session_id"),
        Index("ix_chat_chunks_workspace", "user_id", "workspace_project_id"),
    )


class UserMemory(Base):
    """Explicit user-provided facts (global or workspace-scoped)."""

    __tablename__ = "user_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    workspace_project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'explicit'"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (Index("ix_user_memories_user", "user_id", "created_at"),)


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    message_step_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    # ADR-008: raw Anthropic tool_use.id ("toolu_..."), opaque (NOT a UUID). Internal-only;
    # used as tool_result.tool_use_id on continuation so the id pair in Anthropic history
    # matches. The public toolCallId stays the domain UUID (id above).
    provider_tool_use_id: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        _tool_call_status_enum, nullable=False, server_default=sa_text("'pending'")
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (Index("ix_tool_calls_session", "session_id", "created_at"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_sessions.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        Index("ix_audit_user_created", "user_id", "created_at"),
        Index("ix_audit_event_type", "event_type", "created_at"),
    )


class RequestLog(Base):
    """One CRM-visible backend request (ADR-077).

    Unlike ``AuditLog``, this row models an API lifecycle and may be updated
    exactly to a terminal state. Provider USD stays nullable until measured.
    """

    __tablename__ = "request_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    # Soft reference by verified JWT sub. A FK would make the independent
    # writer deadlock against the uncommitted lazy-provision INSERT on a
    # user's first request (ADR-077).
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'started'"))
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("202"))
    tokens_spent: Mapped[decimal.Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    provider_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    refunded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    message_step_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    media_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'queued', 'completed', 'failed')",
            name="ck_request_logs_status",
        ),
        Index("ix_request_logs_user_started", "user_id", sa_text("started_at DESC")),
        Index(
            "ux_request_logs_media_job",
            "media_job_id",
            unique=True,
            postgresql_where=sa_text("media_job_id IS NOT NULL"),
        ),
    )


class Project(Base):
    """Website-builder project: one backend project per (user, external_project_id)."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # client-side projectId from the chat session (chat_sessions.project_id).
    external_project_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        UniqueConstraint("user_id", "external_project_id", name="ux_projects_user_external"),
        Index("ix_projects_user", "user_id", "updated_at"),
    )


class SiteFile(Base):
    """A stored file of a website-builder project (BYTEA content; TD-009)."""

    __tablename__ = "site_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # normalized relative path (no ".."/absolute/NUL).
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BIGINT, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        CheckConstraint("size >= 0", name="ck_site_files_size_nonneg"),
        UniqueConstraint("project_id", "path", name="ux_site_files_project_path"),
        Index("ix_site_files_project", "project_id"),
    )


class UserPreferences(Base):
    """Per-user preferences (ADR-012, preferences module). One row per user (lazy upsert)."""

    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # ADR-012: default assistant type (chat|code) — orthogonal to billing_mode.
    default_assistant_mode: Mapped[str] = mapped_column(
        _assistant_mode_enum, nullable=False, server_default=sa_text("'chat'")
    )
    notifications_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    # Code-context defaults (language etc.); no secrets (validated + redacted).
    code_defaults: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    memory_search_scope: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'global'")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )


class AdaptyWebhookEvent(Base):
    """Processed Adapty subscription webhook events (ADR-029, billing-adapty/04, migration 0008).

    Single deduplication point: ``event_id`` (Adapty's external id) is the PRIMARY KEY, enabling
    ``INSERT ... ON CONFLICT (event_id) DO NOTHING RETURNING event_id`` so a replayed event is
    detected and short-circuited to ``duplicate`` with no side effects. ``payload`` stores the
    PARSED event object (not raw bytes); the bearer secret lives in the header, never the body.
    """

    __tablename__ = "adapty_webhook_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (Index("ix_adapty_webhook_events_user_id", "user_id"),)


class CloudPaymentsWebhookEvent(Base):
    """Processed RU (broadapps/CloudPayments) payment events (ADR-050, billing-cloudpayments/04,
    migration 0014).

    Single deduplication point: ``transaction_id`` (CloudPayments ``TransactionId``) is the PRIMARY
    KEY, enabling ``INSERT ... ON CONFLICT (transaction_id) DO NOTHING RETURNING transaction_id`` so
    a replayed callback is detected and short-circuited to ``duplicate`` with no side effects.
    ``payload`` stores ONLY a SANITIZED allowlist projection (no card PAN/issuer/type, no bearer) —
    unlike ``adapty_webhook_events`` (which has no card data and stores the full parsed object).
    """

    __tablename__ = "cloudpayments_webhook_events"

    transaction_id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (Index("ix_cloudpayments_webhook_events_user_id", "user_id"),)


class MediaTemplate(Base):
    """Gallery generation template with BYTEA cover (ADR-066, migration 0021).

    List endpoints project metadata + absolute ``coverUrl``; the cover bytes are served by
    ``GET /v1/media/templates/{id}/cover``. Operators create/delete rows via admin API.
    """

    __tablename__ = "media_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    required_input_images: Mapped[int] = mapped_column(nullable=False, server_default=sa_text("0"))
    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    cover_bytes: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cover_media_type: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        CheckConstraint("kind IN ('image', 'video')", name="ck_media_templates_kind"),
        CheckConstraint(
            "required_input_images >= 0 AND required_input_images <= 14",
            name="ck_media_templates_required_input_images",
        ),
        CheckConstraint(
            "cover_media_type IN ('image/jpeg', 'image/png', 'image/webp')",
            name="ck_media_templates_cover_media_type",
        ),
        Index("ix_media_templates_kind_sort", "kind", "sort_order"),
    )


class MediaJob(Base):
    """One image/video generation run submitted to the fal.ai queue (ADR-060 §4, migration 0018).

    A row is created *after* the credits are debited and the run is accepted upstream, so its
    existence means "the user paid for this run and fal owns it". ``fal_request_id`` plus the two
    upstream polling URLs are the handle ``GET /v1/media/jobs/{id}`` uses; the URLs are stored
    rather than rebuilt because nested endpoints (``kling-video/v3/pro/...``) do not yield their
    queue path from the endpoint id alone.

    ``status`` is a terminal-state cache: once ``completed``/``failed`` no further upstream call is
    made. ``credits_refunded`` makes the refund of a failed run idempotent even if two pollers race
    (the wallet grant is itself keyed by the job id, this flag just avoids the redundant call).
    """

    __tablename__ = "media_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Public model id from the server-side catalog (e.g. "veo-3.1"), not the fal endpoint.
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    fal_endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    fal_request_id: Mapped[str] = mapped_column(Text, nullable=False)
    status_url: Mapped[str] = mapped_column(Text, nullable=False)
    response_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    credits_charged: Mapped[int] = mapped_column(nullable=False, server_default=sa_text("0"))
    credits_refunded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    # What fal charges US for this run, in USD (ADR-079). Stamped at submit, where the values
    # that move fal's bill — resolution, duration, audio, image count — are known; they are not
    # persisted anywhere else, so without this column the cost of a run is only recoverable up
    # to the credit pack it was billed in. NULL means "not measured" (job predates the column,
    # or its model has no purchase price on file) and must never be read as $0.
    provider_cost_usd: Mapped[decimal.Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    # Normalized output ({"assets": [...]}) — not the raw upstream body.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Edit chain (ADR-063 §1): the job this run was made from, if any. SET NULL rather than
    # CASCADE — deleting a bad source frame means "take it out of the feed", not "erase everything
    # that grew from it".
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("media_jobs.id", ondelete="SET NULL"), nullable=True
    )
    # The reference-image URLs actually sent upstream. Persisted rather than derived from the
    # parent: the parent may be deleted, and the feed still has to show what this was made from.
    input_image_urls: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # ADR-067: stamped once when a media-ready push is claimed (poll or reconciler).
    push_sent_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed')",
            name="ck_media_jobs_status",
        ),
        # Owner-scoped listing, newest first (GET /v1/media/jobs).
        Index("ix_media_jobs_user_created", "user_id", "created_at"),
        # Background reconciler (ADR-067 / Q-060-2): non-terminal jobs only.
        Index(
            "ix_media_jobs_non_terminal",
            "created_at",
            postgresql_where=sa_text("status IN ('queued', 'running')"),
        ),
    )


class DevicePushToken(Base):
    """APNs device token registered for a (user, device) pair (ADR-067, notifications)."""

    __tablename__ = "device_push_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    push_token: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'ios'"))
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        UniqueConstraint("user_id", "device_id", name="ux_push_tokens_user_device"),
        Index("ix_push_tokens_user", "user_id"),
    )


class WorkspaceProject(Base):
    """A workspace («рабочее пространство», iOS «Project») — ADR-036 §2.

    Name + optional description + optional custom ``instructions`` (project system-prompt) +
    knowledge files (``workspace_files``) shared as context across the project's chats. NOT the
    website-builder ``projects`` table (ADR-013): a different entity. Owner-scoped by ``user_id``.
    """

    __tablename__ = "workspace_projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Custom project system-prompt; injected AFTER the base assistant_mode prompt (ADR-036 §3).
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        # Cursor-paginated list (updated_at DESC) scoped to the owner (ADR-036 §8).
        Index("ix_workspace_projects_user_updated", "user_id", "updated_at"),
    )


class WorkspaceFile(Base):
    """A knowledge file of a workspace (BYTEA content; ADR-036 §4, TD-027).

    Stored by the same pattern as ``site_files`` (own table, raw bytes in ``content``). For
    document/text the extracted text is kept in ``extracted_text`` at upload time (used for the
    provider-agnostic context injection — ADR-036 §6); for images ``extracted_text`` is NULL
    (the image is injected as a vision block). The API never returns ``content``/``extracted_text``.
    """

    __tablename__ = "workspace_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    workspace_project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    media_type: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BIGINT, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        CheckConstraint("size >= 0", name="ck_workspace_files_size_nonneg"),
        Index("ix_workspace_files_project", "workspace_project_id"),
    )


class AuthIdentity(Base):
    """External identity-provider link (Sign in with Apple on start) — ADR-043 §4, migration 0012.

    ``UNIQUE(provider, subject)`` is the cross-device resolution point (one Apple account = one
    ``userId``) and the race-safety anchor (``ON CONFLICT (provider, subject) DO NOTHING`` +
    re-read, like ``auth_devices``). ``ix_auth_identities_user`` powers the reverse lookup "does
    this userId already have an Apple identity" (account-linking, ADR-043 §5). FK ON DELETE
    CASCADE (identities live while the user lives). ``users``/``auth_devices``/
    ``auth_refresh_tokens`` are NOT changed.
    """

    __tablename__ = "auth_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=_uuid_default
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)  # 'apple' (extensible)
    subject: Mapped[str] = mapped_column(Text, nullable=False)  # provider-stable id (apple sub)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)  # optional (private-relay)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_now
    )

    __table_args__ = (
        # Unique INDEX (not a table constraint) to match the DDL/migration name exactly
        # (ux_auth_identities_provider_subject), like auth_refresh_tokens' ux_refresh_token_hash.
        Index(
            "ux_auth_identities_provider_subject",
            "provider",
            "subject",
            unique=True,
        ),
        Index("ix_auth_identities_user", "user_id"),
    )
