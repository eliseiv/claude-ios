"""Chat Orchestrator (CO-4..CO-7): policy → generate → tool-loop → debit → audit.

Implements /chat/run and /chat/tool-result. Single source of access truth is Policy Engine
(AC-6). messageStepId is the billing idempotency key, one per user message-step, reused
across all tool-rounds and re-entry (ADR-005/006). Debit happens exactly once on the final
assistant_message (mode=credits). BYOK plaintext key is in-memory only, never logged.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_CHAT_STEP,
    EVENT_POLICY_DECISION,
    EVENT_TOOL_CALL_COMPLETED,
    EVENT_TOOL_CALL_INITIATED,
    EVENT_TOOL_MUTATION,
    AuditEvent,
    AuditService,
)
from app.byok.service import BYOKService
from app.chat.anthropic_client import AnthropicAuthError
from app.chat.attachment_refs import (
    RECENT_USER_STEPS_SCAN,
    latest_alive_image_urls,
    recent_image_available,
    upload_turn_attachment_refs,
)
from app.chat.attachments import ImageAttachmentRef, PreparedAttachments, prepare_attachments
from app.chat.global_tools import (
    DOCUMENT_INVALID_ERROR_CODE,
    MEDIA_INVALID_ERROR_CODE,
    GlobalToolHandlers,
)
from app.chat.key_failover import (
    Attempt,
    build_attempt_chain,
    is_credential_failure,
    next_attempt_index,
)
from app.chat.llm_client import (
    STOP_REASON_MAX_TOKENS,
    STOP_REASON_TOOL_USE,
    LLMClient,
    LLMResult,
    NeutralMessage,
    generation_llm_client_for,
    llm_client_for,
)
from app.chat.openai_client import OpenAIAuthError
from app.chat.repository import ChatRepository, derive_title
from app.chat.tools import (
    ARGS_DEGRADE_TOOLS,
    GLOBAL_SERVER_SIDE_TOOLS,
    MEDIA_CHAT_TOOLS,
    MUTATING_TOOLS,
    PATCH_FORMAT_HINT,
    PATCH_INVALID_ERROR_CODE,
    QUIZ_CONSTRAINTS_HINT,
    QUIZ_INVALID_ERROR_CODE,
    SERVER_SIDE_TOOLS,
    TOOL_DOCUMENT_CREATE,
    TOOL_DOCUMENT_LIST,
    TOOL_DOCUMENT_READ,
    TOOL_DOCUMENT_UPDATE,
    TOOL_FILES_PATCH,
    TOOL_GENERATION_MODES,
    TOOL_MEDIA_ASK_PARAMS,
    TOOL_MEDIA_GENERATE_IMAGE,
    TOOL_MEDIA_GENERATE_VIDEO,
    TOOL_QUIZ_GENERATE,
    content_free_args_error,
    neutral_tool_definitions,
    offered_in_generation_mode,
    offered_media_chat_tool,
    offered_tool_family,
    validate_tool_args,
)
from app.chat.transcription import TranscriptionClient
from app.config import get_settings
from app.documents import DocumentsService
from app.errors import (
    ContentPolicyViolationError,
    InsufficientCreditsError,
    MediaGenerationNotConfiguredError,
    MessageNotFoundError,
    NotFoundError,
    UpstreamError,
    ValidationFailedError,
    WorkspaceNotFoundError,
)
from app.memory.indexer import schedule_delete_from_message_step, schedule_index_turn
from app.memory.service import MemoryService
from app.models import ChatSession, ChatStep, ToolCall
from app.moderation import ModerationService
from app.moderation.service import STAGE_INPUT, SURFACE_CHAT
from app.observability.logging import log_event
from app.observability.metrics import (
    blocked_requests_total,
    byok_usage_share,
    quiz_generate_total,
    token_usage_total,
)
from app.policy.engine import (
    BlockReason,
    Decision,
    Mode,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)
from app.policy.loader import load_policy_state
from app.preferences.service import PreferencesService
from app.pricing import report_chat_step_pricing
from app.schemas.chat import AttachmentIn, GenerationMode
from app.wallet.service import WalletService
from app.website.tools import SiteToolHandlers, ToolExecution
from app.workspaces.repository import WorkspacesRepository
from app.workspaces.service import WorkspacesService

# ADR-090: инструменты документов — их args-сбой вырождается в tool-ошибку (ARGS_DEGRADE_TOOLS).
_DOCUMENT_TOOL_NAMES = frozenset(
    {TOOL_DOCUMENT_CREATE, TOOL_DOCUMENT_LIST, TOOL_DOCUMENT_READ, TOOL_DOCUMENT_UPDATE}
)

logger = logging.getLogger("app.chat.orchestrator")

_MEDIA_TOOL_NAMES = frozenset({TOOL_MEDIA_GENERATE_IMAGE, TOOL_MEDIA_GENERATE_VIDEO})

# ADR-028 Решение 2: hard cap for serverTools[].summary (same value as steps-view summary).
# The summary is a COMPACT indicator only — it MUST NOT carry the raw tool result, paths, URLs,
# preview signed-tokens or any secret. Anything longer is truncated to this length.
_SUMMARY_MAX_CHARS = 120

# ADR-026 §7: static, date-FREE instruction telling Claude it has no built-in knowledge of the
# current date/time and must call the time.now tool. Identical in both modes. It is STATIC (no date
# is ever interpolated), so the system prompt stays stable between requests and the Anthropic prompt
# cache (cache_control: ephemeral) is NOT invalidated — the date arrives only in the time.now
# tool_result, outside the cached system prefix.
_TIME_NOW_INSTRUCTION = (
    "You do not have built-in knowledge of the current date or time. If the user's request "
    "depends on the current date, time, or day of the week, call the time.now tool to get it; "
    "do not guess."
)

# ADR-068 / ADR-070: prefer media.ask_params (catalog-backed taps) when model/quality unclear;
# media.generate_* only when parameters are already known. STATIC — no catalog dump.
_MEDIA_GENERATE_INSTRUCTION = (
    "When the user asks you to generate a photo or video and has not already chosen the model "
    "and quality (and duration for video), call media.ask_params once with kind and prompt, then "
    "wait — the app shows tappable choices. Do not invent model ids or resolutions. Only call "
    "media.generate_image or media.generate_video when those parameters are already known. "
    "Generate tools only queue a job and return a jobId — tell the user generation has started; "
    "do not claim the media is ready until the app reports it. "
    "Never repeat the generation prompt (the text you pass to media tools) in your visible reply "
    "or summarize it back to the user — keep that prompt internal to the tool call. "
    "First generation with no photo attached: omit sourceJobId (text-to-image / text-to-video). "
    "If the user attached a photo on THIS message and asks to generate/transform based on it "
    "(e.g. put them on a beach), call media.ask_params — the server uploads the attachment and "
    "runs image-to-image automatically; you do not pass imageUrls. "
    "If there is a recent photo from an earlier user message in this chat (see system hint) and "
    "the user did NOT attach a new photo on this message, FIRST ask in plain text whether they "
    "want to use that earlier photo — do NOT call media.ask_params or media.generate_* until they "
    "answer. If they say yes, call media.ask_params with useRecentImage true. If they say no, "
    "proceed without it (text-to-*). "
    "If the user asks to edit, change, redraw, add to, or refine a previously generated image or "
    "video in this chat, you MUST pass sourceJobId set to that job's jobId from history "
    "(tool results or assistant mediaJobs). Without sourceJobId (and with no attachment) the "
    "provider starts a NEW unrelated generation. Prefer media.ask_params with sourceJobId for "
    "edits when quality is unclear. "
    "Exception — video after a previously GENERATED photo in this chat (not a user attachment): "
    "call media.ask_params for kind=video WITHOUT sourceJobId and WITHOUT useRecentImage. The "
    "app shows a Yes/No card «Использовать последнее фото?» "
    "(same mediaChoices UI as duration/resolution). "
    "Do not ask that Yes/No in plain text for a generated photo — only the choices card."
)

# ADR-059: state, in the system prompt, that the assistant has the full conversation so far and
# must use it. Without this, gpt-4o (the OpenAI-instance provider, ADR-033) reads "remember X" as a
# request for cross-session persistent storage and replies with a canned "I can't store data /
# can't recall" disclaimer — EVEN THOUGH the prior turns are replayed to it (_build_messages) and
# it factually has them. The prior chat_steps ARE the memory; this line tells the model to treat
# them as such and never claim statelessness. STATIC (no per-request interpolation) so the Anthropic
# prompt cache prefix stays stable, exactly like _TIME_NOW_INSTRUCTION.
_CONVERSATION_MEMORY_INSTRUCTION = (
    "You have access to the full history of the current conversation; earlier messages in this "
    "chat are part of your context. When the user asks you to remember something or refers back "
    "to what was said, use that history to answer directly. Never claim you cannot remember, "
    "store, or recall information from this conversation."
)

# ADR-012: base system prompt selected by assistant_mode (chat vs code). Single source of truth
# for each mode's prompt (no scattered hardcoding). The set of tools offered to Claude is
# unchanged in this sprint (Q-012-1 default deferred); only the system prompt varies.
_SYSTEM_PROMPT_CHAT = (
    "You are a helpful assistant integrated into an iOS app. You can call tools that the "
    "user's device executes locally (files, calendar, reminders). Use tools when needed and "
    "respond concisely. " + _CONVERSATION_MEMORY_INSTRUCTION + " " + _TIME_NOW_INSTRUCTION
)
# Website-builder guidance: gpt-4o tends to "create" images by writing image files with a
# placeholder string as base64 ("base64 placeholder for dish1.jpg"), which site.write_file rejects
# as invalid base64 -> the page ships with broken <img> and a crooked layout. Steer the model to a
# self-contained page: no fake image bytes, no external URLs; use CSS/SVG/emoji for all visuals.
_SITE_BUILDER_INSTRUCTION = (
    "When building a website with the site tools, output a SELF-CONTAINED page. Do NOT create "
    "image or other binary files with placeholder, fake, or invented base64 content — such writes "
    "fail and leave broken images. If you have no real image data, do not write image files and do "
    "not reference external URLs; render every visual with CSS (gradients/backgrounds/shapes), "
    "inline SVG, or emoji instead."
)


_SYSTEM_PROMPT_CODE = (
    "You are a coding assistant integrated into an iOS app. Favor precise, technical answers: "
    "produce correct, idiomatic code with brief explanations. You can call tools that the "
    "user's device executes locally (files, calendar, reminders) and server-side site tools. "
    "Use tools when needed and respond concisely. "
    + _SITE_BUILDER_INSTRUCTION
    + " "
    + _CONVERSATION_MEMORY_INSTRUCTION
    + " "
    + _TIME_NOW_INSTRUCTION
)


# ADR-094: указания по работе с кодом. Живут ОТДЕЛЬНО от `_SYSTEM_PROMPT_CODE` и добавляются
# только там, где инструменты кода реально предложены (ось D). Иначе на инстансе с выключенным
# флагом модель в режиме `code` получала бы предписание звать files.search / git.*, которых у неё
# нет, — и обещала бы пользователю действия, выполнить которые не может.
# Указания задают ПОРЯДОК, а не перечисляют инструменты: модель, не посмотревшая на файл перед
# правкой, переписывает его по памяти и молча теряет чужие изменения — самый дорогой отказ здесь.
_CODE_TOOLS_INSTRUCTION = (
    "When working on code: locate files with files.search before assuming paths, read a file "
    "before changing it, and prefer files.patch over files.write so you change only the lines "
    "you addressed and never discard edits made elsewhere. "
    # ADR-094: набросок вместо заплатки — не редкость, а то, что модель выдаёт ПО УМОЛЧАНИЮ,
    # если формат не потребовать явно. Прод 2026-08-31: 8 вызовов из 8 отвергнуты `patch(1)`.
    "A files.patch diff is applied by patch(1): write real hunk headers like '@@ -12,7 +12,8 @@' "
    "and quote at least three unchanged context lines around each change, copied verbatim from "
    "the file you just read. The line numbers need not be exact — the hunk is located by its "
    "context — so never skip the header, and never spend the turn counting lines. "
    "Inspect the repository with git.status and git.diff before committing, and write commit "
    "messages that say WHY the change was made, not what the diff already shows. "
    "Push only when the user asked, and set force only when they explicitly asked to overwrite "
    "remote history. "
    "Changes to files and to the repository are confirmed by the user before they run, so "
    "propose the action rather than asking for permission in prose."
)


def _compose_system_prompt(assistant_mode: str, disabled: frozenset[str]) -> str:
    """Base assistant_mode prompt, with disabled tool families stripped (ADR-081).

    Empty ``disabled`` returns the canonical constants unchanged so existing instances keep
    a byte-identical prefix (prompt cache).
    """
    if not disabled:
        return _SYSTEM_PROMPT_CODE if assistant_mode == "code" else _SYSTEM_PROMPT_CHAT
    local = [name for name in ("files", "calendar", "reminders") if name not in disabled]
    site_on = "site" not in disabled
    parts: list[str] = []
    if assistant_mode == "code":
        parts.append(
            "You are a coding assistant integrated into an iOS app. Favor precise, technical "
            "answers: produce correct, idiomatic code with brief explanations."
        )
    else:
        parts.append("You are a helpful assistant integrated into an iOS app.")
    if local and site_on and assistant_mode == "code":
        parts.append(
            f"You can call tools that the user's device executes locally ({', '.join(local)}) "
            "and server-side site tools."
        )
    elif local:
        parts.append(
            f"You can call tools that the user's device executes locally ({', '.join(local)})."
        )
    elif site_on and assistant_mode == "code":
        parts.append("You can call server-side site tools.")
    parts.append("Use tools when needed and respond concisely.")
    if assistant_mode == "code" and site_on:
        parts.append(_SITE_BUILDER_INSTRUCTION)
    parts.append(_CONVERSATION_MEMORY_INSTRUCTION)
    parts.append(_TIME_NOW_INSTRUCTION)
    return " ".join(parts)


GenerationBackend = Literal["legacy", "v2"]

# ADR-064 §7 (soft level) / 03-architecture §Режим study_learn: static EN suffix appended to the
# base assistant_mode prompt ONLY on a study_learn turn. It is the SOFT half of the anti-spoiler
# guarantee (the hard half is the assistantMessage suppression in the single response mapping).
# STATIC — no date, no counters, no turn content — so the prompt prefix stays byte-stable inside the
# mode and the provider prompt cache is not invalidated per request. study_learn does get its OWN
# cache entry (its prefix differs from general by both this suffix and the tool-set): expected, not
# a defect. Workspace instructions (ADR-036 §3) are still appended AFTER this suffix.
_STUDY_LEARN_INSTRUCTION = (
    "This turn is a Study & Learn turn. Explain the topic briefly, then ask the learner questions "
    "ONLY by calling the quiz.generate tool with the whole pool of questions in a single call. "
    "Never repeat the question wording in your reply text, and never reveal the correct options or "
    "the explanations there — the app shows them to the learner after they answer. Keep any "
    "accompanying text short."
)

# ADR-084: research suffix. Hosted web_search is already attached by the provider client, but the
# base chat prompt only names device-local tools — gpt-5.1 then emits a dummy search
# (`calculator: 1+1`) and claims it has no internet. Soft half (like study_learn): tell the model
# the search is live and must be used for current facts. STATIC — no date/counters/turn content —
# so the prompt-cache prefix stays byte-stable inside the mode. Workspace instructions stay LAST.
_RESEARCH_INSTRUCTION = (
    "This turn is a Research turn. You have a live hosted web-search tool on this turn; it is not "
    "a device-local tool and it does reach the public internet. For questions that need current, "
    "dated, or source-backed facts — prices, exchange rates, news, laws, scores, availability, "
    "citations — you MUST use that web-search tool with a real query about the user's question. "
    "Never send a dummy, calculator, or no-op query. After search results arrive, answer from them "
    "and include working source links. Never claim you cannot look something up, have no internet "
    "access, or can only give generic advice on a Research turn."
)


def _effective_generation_mode(
    generation_mode: str,
    *,
    use_generation_v2: bool,
) -> GenerationMode:
    """ONE value for prompt / axis-C / provider / price (ADR-064 §3, ADR-082).

    v2 uses the request (or restored) mode. Legacy is ``general`` unless this instance
    opts into hosted web search on ``/v1/chat/*`` via ``CHAT_LEGACY_WEB_SEARCH_ENABLED``.
    """
    if use_generation_v2:
        known: dict[str, GenerationMode] = {
            "general": "general",
            "research": "research",
            "reasoning": "reasoning",
            "study_learn": "study_learn",
        }
        return known.get(generation_mode, "general")
    if get_settings().chat_legacy_web_search_enabled:
        return "research"
    return "general"


def _turn_credit_cost(effective_generation_mode: str, *, use_generation_v2: bool) -> int:
    """v2 (and opted-in legacy research) use the mode price; other legacy stays 1 credit."""
    if use_generation_v2 or get_settings().chat_legacy_web_search_enabled:
        return get_settings().chat_generation_credit_cost(effective_generation_mode)
    return 1


def _uses_generation_client(use_generation_v2: bool) -> bool:
    """OpenAI Chat Completions ignores ``generation_mode``; hosted web_search lives on Responses.

    v2 always uses the generation client. Legacy does too when this instance opted into
    ``CHAT_LEGACY_WEB_SEARCH_ENABLED`` — otherwise research mode is billed but never attached.
    Anthropic uses the same Messages client on both factories.
    """
    return use_generation_v2 or get_settings().chat_legacy_web_search_enabled


def _system_prompt_for(assistant_mode: str, generation_mode: str = "general") -> str:
    """Base system prompt for the turn: assistant_mode prompt + the generation-mode suffix.

    The mode suffix is added ONLY for the modes that declare one (``study_learn``, ADR-064;
    ``research``, ADR-084). ``generation_mode`` MUST be the EFFECTIVE mode of the turn. Legacy is
    ``general`` unless ``CHAT_LEGACY_WEB_SEARCH_ENABLED`` lifts it to ``research`` (ADR-082) — that
    path gets the research suffix and still no study_learn suffix. Workspace instructions are
    layered on top of this by ``_system_prompt_with_workspace`` and therefore stay LAST
    (ADR-036 §3).
    ADR-072: media-generate instruction is appended only when ``CHAT_MEDIA_TOOLS_ENABLED``.
    ADR-081: families in ``CHAT_DISABLED_TOOL_FAMILIES`` are omitted from the tool sentence.
    ADR-094: the code-work instruction is appended under the SAME condition that offers the code
    tools (``CODE_TOOLS_ENABLED`` and ``assistant_mode == "code"``) — never one without the other.
    """
    base = _compose_system_prompt(assistant_mode, get_settings().disabled_tool_families())
    # ADR-094 ось D: указания по работе с кодом добавляются ровно по тому же условию, по которому
    # предлагаются сами инструменты. Разойдись эти два условия — модель на инстансе без флага
    # получала бы предписание звать files.search / git.*, которых ей не дали.
    if get_settings().code_tools_enabled and assistant_mode == "code":
        base = f"{base} {_CODE_TOOLS_INSTRUCTION}"
    if get_settings().chat_media_tools_enabled:
        base = f"{base} {_MEDIA_GENERATE_INSTRUCTION}"
    if generation_mode == "study_learn":
        return f"{base}\n\n{_STUDY_LEARN_INSTRUCTION}"
    if generation_mode == "research":
        return f"{base}\n\n{_RESEARCH_INSTRUCTION}"
    return base


# ADR-037 §1,§3: allowlist for ChatRunRequest.context — a fixed registry of known per-message
# conversation settings, rendered into a compact text block prepended to the turn-0 user message.
# The rendered key order is FIXED (the order below), independent of the request dict's key order
# (deterministic block). Unknown keys are ignored (forward-compat); a key whose value fails its
# per-key validation is dropped (lenient, NOT a 422). Free-string keys have a length cap; enum keys
# must match a closed set; locale additionally enforces a character class. The whole context block
# is INJECTED INTO THE USER MESSAGE (never the system prompt) — so the Anthropic prompt cache
# (cache_control: ephemeral on system) is not invalidated and user data does not gain system
# authority (05-security.md).
_CONTEXT_FREE_STRING_MAX = {
    "codeLanguage": 40,
    "tone": 40,
    "locale": 35,
}
_CONTEXT_ENUMS = {
    "responseStyle": frozenset({"concise", "balanced", "detailed"}),
    "verbosity": frozenset({"low", "medium", "high"}),
}
# locale: BCP-47-like, restricted character class to keep arbitrary text out of the block (§1).
_CONTEXT_LOCALE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
# Deterministic render order (ADR-037 §3 = the allowlist-table order).
_CONTEXT_KEY_ORDER = ("codeLanguage", "responseStyle", "verbosity", "tone", "locale")


def _sanitize_context_value(value: str) -> str:
    """Strip block-structure characters from a free-string value (ADR-037 §3 escaping).

    Newlines / ``;`` / ``=`` would break the single-line ``k=v; k=v`` block structure, so they are
    replaced with a space and the result is collapsed/stripped. Defensive against a value smuggling
    its own delimiters into the conversation-settings block.
    """
    cleaned = value.replace("\n", " ").replace("\r", " ").replace(";", " ").replace("=", " ")
    return " ".join(cleaned.split())


def _validated_context_value(key: str, raw: Any) -> str | None:
    """Validate+normalize one context value for ``key`` per ADR-037 §1; None → drop the key.

    All values must be ``str`` and non-empty after ``strip``. Free-string keys are length-capped
    (chars, post-strip) then sanitized; enum keys are lower-cased and must be in the closed set;
    ``locale`` must match the restricted character class. A wrong type / out-of-range / out-of-enum
    value yields None (the key is ignored — lenient, never a 422).
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if key in _CONTEXT_ENUMS:
        lowered = value.lower()
        return lowered if lowered in _CONTEXT_ENUMS[key] else None
    if key == "locale":
        if len(value) > _CONTEXT_FREE_STRING_MAX["locale"]:
            return None
        if any(ch not in _CONTEXT_LOCALE_CHARS for ch in value):
            return None
        return value  # already constrained to a safe char class; no sanitize needed
    if key in _CONTEXT_FREE_STRING_MAX:
        if len(value) > _CONTEXT_FREE_STRING_MAX[key]:
            return None
        sanitized = _sanitize_context_value(value)
        return sanitized or None
    # Unknown key (not in any allowlist branch) → ignored by the caller's iteration over the fixed
    # key order; this path is unreachable for keys in _CONTEXT_KEY_ORDER. Defensive None.
    return None  # pragma: no cover


def _render_context_block(context: dict[str, Any] | None) -> str | None:
    """Render the deterministic per-message conversation-settings block (ADR-037 §3).

    Returns None when ``context`` is absent/empty or no allowlisted key survives validation (→ the
    turn behaves exactly as without ``context``). Otherwise returns a single-line block in FIXED key
    order with only the valid present keys, e.g.::

        [Conversation settings for this message: codeLanguage=Swift; responseStyle=concise]

    Unknown keys are ignored; per-key-invalid values are dropped (lenient). The content of
    ``context`` is NEVER logged (05-security.md) — this function neither logs nor raises.
    """
    if not context:
        return None
    parts: list[str] = []
    for key in _CONTEXT_KEY_ORDER:
        if key not in context:
            continue
        value = _validated_context_value(key, context[key])
        if value is not None:
            parts.append(f"{key}={value}")
    if not parts:
        return None
    return f"[Conversation settings for this message: {'; '.join(parts)}]"


def _compose_turn0_text(block: str | None, msg: str) -> str:
    """Compose the turn-0 user text from the context block (ADR-037) and the message (ADR-039 §3).

    Returns "" only when there is no text at all (empty/whitespace-only message AND no context
    block) — the caller then omits the text block entirely (image-only / file-only turn, §2). A
    whitespace-only message is treated as «no text» (``.strip()``, symmetric with the §1 validator)
    so a blank text block is never sent to the provider. No trailing ``"\\n\\n"`` is produced when
    the message is empty but a block is present.
    """
    if not msg.strip():
        return block or ""
    if block is not None:
        return f"{block}\n\n{msg}"
    return msg


def _system_prompt_with_workspace(
    assistant_mode: str, instructions: str | None, generation_mode: str = "general"
) -> str:
    """Compose the system prompt for a workspace session (ADR-036 §3).

    ``base(assistant_mode[, generation_mode])`` → ``\\n\\n`` → ``workspace.instructions`` when
    instructions are non-empty; otherwise the base prompt unchanged (so the prompt cache is not
    broken for sessions without instructions). Provider-agnostic (part of ``system``, identical for
    both providers). Layer order is normative: base prompt → generation-mode suffix
    (ADR-064 / ADR-084) → workspace instructions LAST (ADR-036 §3).
    """
    base = _system_prompt_for(assistant_mode, generation_mode)
    if instructions and instructions.strip():
        return f"{base}\n\n{instructions.strip()}"
    return base


def _merge_attachments(
    chat: PreparedAttachments | None, workspace: PreparedAttachments | None
) -> PreparedAttachments | None:
    """Merge workspace knowledge-file blocks with the request's inline attachment blocks (ADR-036).

    Both are injected into the last user turn on the first call only. Workspace context blocks are
    placed BEFORE the request attachments (project context first). placeholders come only from the
    request attachments (workspace files are never persisted as user-step placeholders — they are
    re-assembled from workspace_files on a new session's first turn).
    """
    if chat is None and workspace is None:
        return None
    chat_blocks = chat.content_blocks if chat is not None else []
    chat_placeholders = chat.placeholders if chat is not None else []
    # Media image-to-image bridging uses ONLY request images — not workspace knowledge files.
    chat_images = list(chat.images) if chat is not None else []
    ws_blocks = workspace.content_blocks if workspace is not None else []
    return PreparedAttachments(
        content_blocks=[*ws_blocks, *chat_blocks],
        placeholders=list(chat_placeholders),
        images=chat_images,
    )


def _active_provider() -> str:
    """Default credits provider (ADR-033 / ADR-073): ``LLM_PROVIDER``, anthropic if unset."""
    return get_settings().credits_provider_for_model(None)


def _credits_llm(*, provider: str, use_generation_v2: bool) -> LLMClient:
    """Credits-path client for ``provider`` (legacy Completions vs v2/opted-in Responses)."""
    if _uses_generation_client(use_generation_v2):
        return generation_llm_client_for(provider)
    return llm_client_for(provider)


def _provider_state_for_attempt(
    attempt: Attempt, stored: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Forward stored Responses state only to an OpenAI candidate — matched by PROVIDER, not key.

    A crossover to Anthropic must not send an OpenAI response id. A stored state from another
    provider is dropped rather than forwarded.

    The match is the provider NAME only: the key slot (service key vs ``OPENAI_API_KEY_BACKUP``,
    ADR-074) that minted the handle is not compared, so a handle from one OpenAI account would be
    forwarded to a candidate on another. That gap is one of the reasons provider-side continuation
    is off, and closing it is part of the work TD-032 describes. Today the forwarded state is
    inert: ``_usable_previous_response_id`` returns ``None`` regardless of what it receives.
    """
    if stored is None or attempt.provider != "openai":
        return None
    if stored.get("provider") not in (None, "openai"):
        return None
    return stored


async def _invoke_llm(
    llm: LLMClient,
    llm_kwargs: dict[str, Any],
    *,
    on_text_delta: Callable[[str], Awaitable[None]] | None,
    emit_text_deltas: bool,
) -> LLMResult:
    """One create_message / stream_message call. Failover wraps this, not the tool-loop."""
    if on_text_delta is not None:
        result: LLMResult | None = None
        async for event in llm.stream_message(**llm_kwargs):
            if event.kind == "text_delta" and event.text and emit_text_deltas:
                await on_text_delta(event.text)
            elif event.kind == "completed" and event.result is not None:
                result = event.result
        if result is None:
            raise RuntimeError("stream_message ended without completed event")
        return result
    return await llm.create_message(**llm_kwargs)


def _model_for_provider(model: str | None, provider: str) -> str | None:
    """Return ``model`` only if it is in ``provider``'s allowlist, else ``None`` (ADR-044).

    Shared stale-model guard for both billing modes:
    - credits (ADR-044 §Связанное / ADR-073): ``provider`` = the credits provider of this session
      model (``credits_provider_for_model``). Dual-credits routes Claude→Anthropic and GPT→OpenAI.
      A session model fixed for a provider that is no longer enabled (e.g. ``claude-*`` after
      dual-credits was turned off on an OpenAI instance) is NOT in that provider's allowlist →
      ``None`` → the client uses its provider default instead of failing with a foreign model id.
    - byok (ADR-044 §5.3): ``provider`` = the KEY's provider. A session model of another provider is
      never forwarded to the key's client.

    ``model is None`` (instance default) stays ``None``. The DB ``chat_sessions.model`` is never
    rewritten — only the value passed to the client on this call changes (expand-only, ADR-034).
    """
    if model is None:
        return None
    return model if model in get_settings().allowed_models_for(provider) else None


def _server_tool_summary(execution: ToolExecution) -> str | None:
    """Build the COMPACT serverTools[].summary for a server-side execution (ADR-028 Решение 2).

    MVP default (Q-028-1): a single compact summary, NOT the raw result. completed → "ok";
    errored → the short machine error code (e.g. "invalid_timezone"), never details/stacktraces.
    The raw result/path/URL/signed-token NEVER appears here (it stays only in /chats history,
    ADR-024). Defensively truncated to _SUMMARY_MAX_CHARS even though codes are already short.
    """
    if execution.is_error:
        code = execution.error_code or "errored"
        return code[:_SUMMARY_MAX_CHARS]
    return "ok"


@dataclass(frozen=True)
class ToolCallOut:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ServerToolExecutionOut:
    """One server-side tool execution of this /chat/run call (ADR-028 Решение 2).

    tool_name is the DOMAIN dotted name (anthropic_client already reverse-maps tool_use.name to
    domain before it reaches the orchestrator). summary is a COMPACT, already-truncated indicator
    (≤ _SUMMARY_MAX_CHARS) and NEVER the raw result / path / URL / signed-token.

    tool_call_id is the DOMAIN tool_calls.id (uuid4) of this server-side execution (ADR-030).
    It equals the toolCallId of the matching tool step in GET /v1/chats/{id} (correlation
    invariant) and is the same id domain as client-side toolCalls[].id — NOT the provider
    toolu_... id (ADR-008).
    """

    tool_call_id: uuid.UUID
    tool_name: str
    status: str  # completed | errored
    summary: str | None


@dataclass(frozen=True)
class ToolResultIn:
    """One normalized tool-result item (ADR-025 batch). error is the dumped ToolErrorBody dict."""

    tool_call_id: uuid.UUID
    result: dict[str, Any] | None
    error: dict[str, Any] | None


@dataclass(frozen=True)
class ChatStreamEvent:
    """One SSE event for ``/v1/chat/v2/run/stream`` (ADR-069)."""

    kind: Literal["delta", "done", "error"]
    text: str = ""
    out: ChatRunOut | None = None
    error_code: str | None = None
    error_message: str | None = None

    @classmethod
    def delta(cls, text: str) -> ChatStreamEvent:
        return cls(kind="delta", text=text)

    @classmethod
    def done(cls, out: ChatRunOut) -> ChatStreamEvent:
        return cls(kind="done", out=out)

    @classmethod
    def error(cls, code: str, message: str) -> ChatStreamEvent:
        return cls(kind="error", error_code=code, error_message=message)


@dataclass(frozen=True)
class ChatRunOut:
    status: str  # assistant_message | tool_call | blocked
    session_id: uuid.UUID
    assistant_message: str | None = None
    # ADR-025: ALL client-side tool calls of the turn (parallel tool use). tool_call (singular,
    # deprecated) = tool_calls[0]. Server-side site.* are executed on the backend and excluded.
    tool_calls: list[ToolCallOut] | None = None
    tool_call: ToolCallOut | None = None
    block_reason: str | None = None
    # ADR-095: что распознано из голосового сообщения этого хода. Возвращается, чтобы приложение
    # заменило свой локальный пузырёк «голосовое» на расшифровку СРАЗУ, не перезагружая историю:
    # иначе человек не видит, что именно услышал сервис, и не может поймать ошибку распознавания.
    transcript: str | None = None
    usage: dict[str, Any] | None = None
    # ADR-023: sync ids for chat history. message_step_id = the turn (one per user message-step,
    # reused across tool-rounds/re-entry); step_id = the id of the persisted assistant/tool step
    # this response represents (= ChatStep.id = ChatStepSchema.id). Both None for policy-blocked
    # (no step/turn is created — policy blocks before generation). For blocked+max_tokens (ADR-025)
    # both are set (the truncated assistant step IS created) and usage is present.
    message_step_id: uuid.UUID | None = None
    step_id: uuid.UUID | None = None
    # ADR-028 Решение 2: server-side tools (site.* / time.now) executed by the backend during THIS
    # /chat/run (or one /chat/tool-result continuation), in execution order. Always a list (possibly
    # empty). Empty for policy-blocked (tool-loop never ran); may be NON-empty for
    # blocked+max_tokens (server-side rounds could run before the final turn was truncated).
    server_tools: list[ServerToolExecutionOut] = field(default_factory=list)
    # ADR-064 §7: the quiz pool of THIS TURN (message_step_id) — turn-scoped CONTENT, not a per-call
    # indicator. Set on EVERY leg of a study_learn turn (run, each tool-result continuation,
    # idempotent replay, blocked+max_tokens) from the call accumulator or, when that is empty, from
    # the turn's persisted quiz tool-step. None = «no quiz in THIS TURN».
    # DELIBERATE CONTRAST with server_tools above, which is per-call and NOT reconstructed on
    # replay: server_tools answers «what ran in this call», quiz answers «what this turn contains».
    # Do not carry the rule of either field over to the other.
    quiz: dict[str, Any] | None = None
    # ADR-068: media jobs submitted in THIS TURN via media.generate_* — turn-scoped like quiz.
    # None = «no media jobs in this turn»; a list (possibly recovered from tool steps) otherwise.
    media_jobs: list[dict[str, Any]] | None = None
    # ADR-070: catalog-backed mediaChoices wizard step for this turn (from media.ask_params /
    # mediaSelection continuation). None = no picker in this response.
    media_choices: dict[str, Any] | None = None
    # Credits newly debited during THIS HTTP call. Idempotent replay is 0 even
    # when the saved usage contains historical creditsCharged (ADR-077).
    credits_spent: int = 0


@dataclass
class _QuizAccumulator:
    """Quiz pool produced by the CURRENT call (ADR-064 §7, producer 1).

    Mutable on purpose: it is threaded through the tool-loop so a pool produced in any round of this
    call reaches the terminal branch. LAST-WINS when the model calls the tool several times — pools
    are never merged (merging would give a non-deterministic size and duplicate questions).
    """

    pool: dict[str, Any] | None = None


@dataclass
class _MediaJobsAccumulator:
    """Media jobs submitted in the CURRENT call (ADR-068).

    APPEND (not last-wins): the model may queue several images/videos in one turn; the client needs
    every jobId. Threaded through the tool-loop like ``_QuizAccumulator``.
    """

    jobs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _MediaChoicesAccumulator:
    """mediaChoices wizard state produced in the CURRENT call (ADR-070). Last-wins."""

    state: dict[str, Any] | None = None


@dataclass(frozen=True)
class _TurnOutcome:
    """Result of processing one tool_use turn (ADR-011).

    client_out is set when the turn yields a client-side tool_call to hand off to iOS; None when
    the turn was purely server-side (site.*) and the orchestrator should continue the loop.
    """

    client_out: ChatRunOut | None


@dataclass(frozen=True)
class _BillingPlan:
    """How the final assistant_message must be billed (ADR-002 + ADR-005).

    Exactly one of the two flags is true when billing applies:
    - debit_credits: active subscription + mode=credits → consume credit_amount (idempotent).
    - mark_trial:    subscription=none + trial_used=false + mode=credits → free trial, flip
      users.trial_used (idempotent). No debit.
    BYOK and trial generations are free → both flags false.
    """

    debit_credits: bool
    mark_trial: bool
    credit_amount: int = 0
    expose_credit_amount: bool = False


def _billing_plan(
    mode: Mode,
    state: PolicyState,
    *,
    credit_amount: int,
    expose_credit_amount: bool = False,
) -> _BillingPlan:
    if mode is Mode.byok:
        return _BillingPlan(
            debit_credits=False,
            mark_trial=False,
            credit_amount=0,
            expose_credit_amount=expose_credit_amount,
        )
    # mode == credits
    if state.subscription_status is SubscriptionStatus.active:
        # ADR-002: active + enough credits → allow + debit. Generation modes calibrate amount.
        return _BillingPlan(
            debit_credits=True,
            mark_trial=False,
            credit_amount=max(1, credit_amount),
            expose_credit_amount=expose_credit_amount,
        )
    if state.subscription_status is SubscriptionStatus.none and not state.trial_used:
        # ADR-002: trial-allow has NO debit; instead the lifetime trial is consumed.
        return _BillingPlan(
            debit_credits=False,
            mark_trial=True,
            credit_amount=0,
            expose_credit_amount=expose_credit_amount,
        )
    # Any other credits state would have been blocked by policy before reaching here.
    return _BillingPlan(
        debit_credits=False,
        mark_trial=False,
        credit_amount=0,
        expose_credit_amount=expose_credit_amount,
    )


@dataclass
class _Deps:
    repo: ChatRepository
    wallet: WalletService
    byok: BYOKService
    audit: AuditService
    # ADR-033: provider-neutral LLM client (AnthropicClient | OpenAIClient). The orchestrator
    # depends only on the LLMClient contract and neutral types — never on a concrete provider.
    llm: LLMClient
    site_tools: SiteToolHandlers
    # ADR-026: project-independent global server-side tools (time.now), executed without a project.
    global_tools: GlobalToolHandlers
    preferences: PreferencesService
    # ADR-036: workspaces context provider (instructions + knowledge files) for workspace chats.
    workspaces: WorkspacesService
    memory: MemoryService | None = None
    # ADR-086: None ⇒ модерация не выполняется (вердикт unchecked), ход идёт как раньше.
    moderation: ModerationService | None = None
    # ADR-090: None ⇒ document.* недоступны, строка о документах в промт не добавляется.
    documents: DocumentsService | None = None
    # ADR-095: создаётся лениво и только там, где голос включён — конструктор клиента читает
    # ключ OpenAI, и на инстансе без голоса эта зависимость не нужна вовсе.
    transcription: TranscriptionClient | None = None


class ChatOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        repo: ChatRepository,
        wallet: WalletService,
        byok: BYOKService,
        audit: AuditService,
        anthropic_client: LLMClient,
        site_tools: SiteToolHandlers,
        preferences: PreferencesService,
        global_tools: GlobalToolHandlers | None = None,
        workspaces: WorkspacesService | None = None,
        memory: MemoryService | None = None,
        moderation: ModerationService | None = None,
        documents: DocumentsService | None = None,
    ) -> None:
        self._session = session
        self._deps = _Deps(
            repo=repo,
            wallet=wallet,
            byok=byok,
            audit=audit,
            # ADR-033: the injected client is the active provider's LLMClient. The param name is
            # kept (anthropic_client) for caller backward compatibility; the field is provider-
            # neutral (`llm`).
            llm=anthropic_client,
            site_tools=site_tools,
            # Default to a SystemClock-backed handler so existing callers keep working; the DI
            # factory (deps.py) wires an explicit instance (ADR-026 §5).
            global_tools=global_tools if global_tools is not None else GlobalToolHandlers(),
            preferences=preferences,
            # ADR-036: default to a session-backed WorkspacesService so existing callers keep
            # working; the DI factory (deps.py) wires the same instance explicitly.
            workspaces=(
                workspaces
                if workspaces is not None
                else WorkspacesService(WorkspacesRepository(session))
            ),
            memory=memory,
            # ADR-086: None ⇒ ход не модерируется (легаси-вызов без DI); фабрика в deps.py
            # передаёт общий экземпляр явно.
            moderation=moderation,
            documents=documents,
        )

    # ---- public entrypoints ----

    async def _transcribe_voice(
        self, message: str, attachments: list[AttachmentIn] | None
    ) -> tuple[str, list[AttachmentIn] | None, str | None]:
        """Заменить голосовые вложения расшифровкой (ADR-095).

        Вызывается ПЕРВЫМ делом в ходе — до модерации, сохранения шага и обращения к модели.
        После этой замены голосовой ход неотличим от набранного руками, и ни одна из
        нижележащих частей о голосе не знает: модерация проверяет тот же текст, история хранит
        тот же текст, реплей воспроизводит тот же текст.
        """
        if not attachments:
            return message, attachments, None
        voice = [a for a in attachments if a.type == "audio"]
        if not voice:
            return message, attachments, None
        if not get_settings().voice_input_enabled:
            # Отдельная причина, а не «неподдерживаемый тип»: класс объявлен в контракте, и
            # приложению нужно отличить «инстанс не умеет» от «формат не тот».
            raise ValidationFailedError("voice input is not enabled on this instance")

        client = self._deps.transcription or TranscriptionClient()
        parts: list[str] = []
        for att in voice:
            audio = base64.b64decode(att.data, validate=True)
            text = await client.transcribe(audio, att.mediaType)
            if text:
                parts.append(text)
        transcript = "\n".join(parts)

        rest = [a for a in attachments if a.type != "audio"]
        if not transcript and not message.strip() and not rest:
            # Пустая запись без текста и без других вложений: отправлять модели нечего. Молчание
            # в ответ выглядело бы зависанием, поэтому говорим прямо.
            raise ValidationFailedError("voice message contains no recognizable speech")
        combined = f"{message}\n{transcript}".strip() if message.strip() else transcript
        return combined, (rest or None), (transcript or None)

    async def run(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        session_id: uuid.UUID | None,
        message: str,
        mode: str,
        assistant_mode: str | None = None,
        attachments: list[AttachmentIn] | None = None,
        model: str | None = None,
        workspace_project_id: uuid.UUID | None = None,
        context: dict[str, Any] | None = None,
        edit_message_step_id: uuid.UUID | None = None,
        generation_mode: GenerationMode = "general",
        generation_backend: GenerationBackend = "legacy",
        temporary: bool = False,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        media_selection: dict[str, Any] | None = None,
        memory_search: bool | None = None,
    ) -> ChatRunOut:
        """Ход чата. Голос распознаётся ЗДЕСЬ, до самого хода (ADR-095).

        Распознавание вынесено в обёртку, а не в тело, ровно по двум причинам. Первая: после
        замены аудио текстом ход не отличает голосовое сообщение от набранного — модерация,
        история, реплей и тарификация работают с одним и тем же текстом, и ни одна из них о
        голосе не знает. Вторая: тело возвращает ответ из НЕСКОЛЬКИХ мест, и проставлять
        расшифровку в каждом значило бы терять её на следующей добавленной ветке — здесь же
        точка одна.
        """
        message, attachments, transcript = await self._transcribe_voice(message, attachments)
        out = await self._run_turn(
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            message=message,
            mode=mode,
            assistant_mode=assistant_mode,
            attachments=attachments,
            model=model,
            workspace_project_id=workspace_project_id,
            context=context,
            edit_message_step_id=edit_message_step_id,
            generation_mode=generation_mode,
            generation_backend=generation_backend,
            temporary=temporary,
            on_text_delta=on_text_delta,
            media_selection=media_selection,
            memory_search=memory_search,
        )
        return out if transcript is None else dataclasses.replace(out, transcript=transcript)

    async def _run_turn(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        session_id: uuid.UUID | None,
        message: str,
        mode: str,
        assistant_mode: str | None = None,
        attachments: list[AttachmentIn] | None = None,
        model: str | None = None,
        workspace_project_id: uuid.UUID | None = None,
        context: dict[str, Any] | None = None,
        edit_message_step_id: uuid.UUID | None = None,
        generation_mode: GenerationMode = "general",
        generation_backend: GenerationBackend = "legacy",
        temporary: bool = False,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        media_selection: dict[str, Any] | None = None,
        memory_search: bool | None = None,
    ) -> ChatRunOut:
        message_step_id = uuid.uuid4()  # CO-4b: billing key for this user message-step
        requested_backend: GenerationBackend = "v2" if generation_backend == "v2" else "legacy"
        use_generation_v2 = requested_backend == "v2"
        # ADR-064 §3 / ADR-082: ONE effective mode for prompt, axis-C, provider and price.
        # Legacy is `general` unless CHAT_LEGACY_WEB_SEARCH_ENABLED lifts it to `research`.
        # quiz.generate stays study_learn-only, so the axis-C gate still never fires on legacy.
        effective_generation_mode = _effective_generation_mode(
            generation_mode, use_generation_v2=use_generation_v2
        )
        # ADR-034 §3: resolve the session-fixed model. None (no field) → NULL (= instance default,
        # never substituted in the DB so the row stays "instance default" even if env default
        # changes). The schema guarantees a non-empty value here, so .strip() is safe.
        resolved_model = model.strip() if model is not None else None
        # ADR-034 §3: validate allowlist membership ONLY when a NEW session is being created (a
        # missing session_id, or an absent/expired one → get_or_create_session creates). On resume
        # the request `model` is IGNORED (the stored model is already valid) — so a bad model field
        # on a resume must NOT fail. Pre-determine «is this a create?» to gate validation; the
        # validation itself runs BEFORE the session row is created (no invalid model is written).
        will_create = await self._will_create_session(user_id, session_id)
        if (
            will_create
            and resolved_model is not None
            and resolved_model not in get_settings().allowed_models_union()
        ):
            raise ValidationFailedError(
                f"model '{resolved_model}' is not available on this instance"
            )
        # ADR-036 §3: workspaceProjectId is session-fixed (like mode/model). On CREATE validate the
        # workspace belongs to the user (foreign/missing → 404 workspace_not_found, isolation)
        # BEFORE the session row is written; on resume the request field is ignored (the binding is
        # read from the session). Empty/None → a chat without a workspace (backward-compatible).
        if (
            will_create
            and workspace_project_id is not None
            and not await self._deps.workspaces.owns_workspace(workspace_project_id, user_id)
        ):
            raise WorkspaceNotFoundError("workspace not found")
        # ADR-012: resolve assistant_mode for a NEW session — explicit request → preferences
        # default → 'chat'. Fixed on the session at creation; ignored when resuming a session
        # (assistant_mode is a session attribute). billing_mode (`mode`) is independent.
        resolved_assistant_mode = (
            assistant_mode
            if assistant_mode is not None
            else await self._deps.preferences.get_default_assistant_mode(user_id)
        )
        ctx = await self._deps.repo.get_or_create_session(
            user_id=user_id,
            project_id=project_id,
            mode=mode,
            session_id=session_id,
            assistant_mode=resolved_assistant_mode,
            # Auto-title from the first user message (chats/03); only used for a new session.
            title=derive_title(message),
            # ADR-034 §3: session-fixed model; written only at creation, ignored on resume.
            model=resolved_model,
            # ADR-036 §3: session-fixed workspace binding; written only at creation, ignored on
            # resume (the request field is validated above only when a new session is created).
            workspace_project_id=workspace_project_id if will_create else None,
            # Public chat backend contract: legacy `/v1/chat/*` or v2 `/v1/chat/v2/*`.
            generation_backend=requested_backend,
            # Temporary chat (v2): session-fixed; only written on create (resume ignores request).
            temporary=bool(temporary) if will_create else False,
        )
        sess = ctx.session
        await self._ensure_session_backend(
            sess,
            requested_backend=requested_backend,
            allow_upgrade_from_legacy=use_generation_v2,
        )
        # mode is fixed on the session; use the session's stored mode.
        effective_mode = Mode(sess.mode)

        # ADR-070: mediaChoices wizard continuation — no LLM, no chat debit.
        if media_selection is not None:
            if not use_generation_v2:
                raise ValidationFailedError("mediaSelection is only supported on /v1/chat/v2/*")
            if not get_settings().chat_media_tools_enabled:
                raise ValidationFailedError(
                    "media chat tools are disabled on this instance "
                    "(use /v1/media/* for generation)"
                )
            return await self._handle_media_selection(
                user_id=user_id,
                session_id=sess.id,
                message_step_id=message_step_id,
                media_selection=media_selection,
                message=message,
                generation_mode=effective_generation_mode,
            )

        # ADR-040 §2,§3: edit+regenerate. Truncate the session history from the edited turn (its
        # user-step and EVERYTHING after) BEFORE persisting the new user-step of this turn, in the
        # same request transaction (atomic; the request commits as one unit). Edit REQUIRES resume
        # of an existing OWNED session (ADR-040 §1,§5): a new session (sessionId was given but the
        # session is foreign/expired/missing → get_or_create created a fresh one, ctx.is_new=True)
        # means there is no turn to edit → 404, NO truncation, and the empty just-created session
        # row is rolled back with the request (the AppError propagates → db.session_scope rollback;
        # commit happens only on success). Truncation is scoped by `sess.id` — the resumed, owned
        # session — so a foreign chat can never be truncated. The new turn then proceeds normally:
        # the freshly generated message_step_id (above) yields a new debit (CO-7); on resume
        # Состояние провайдера здесь сбрасывается, поэтому ниже файлы проекта подмешиваются
        # заново: без этого правка навсегда лишала бы беседу файлов.
        if edit_message_step_id is not None:
            if ctx.is_new:
                raise MessageNotFoundError("message_not_found")
            deleted = await self._deps.repo.truncate_from_message_step(
                sess.id, edit_message_step_id
            )
            if deleted is None:
                raise MessageNotFoundError("message_not_found")
            await self._deps.repo.clear_provider_state(sess.id)
            schedule_delete_from_message_step(sess.id, edit_message_step_id)

        # ADR-036 §3/§6 + ADR-038 §3: workspace `instructions` live in the `system` param (NOT in
        # history) and MUST be injected on EVERY turn of a session with a workspace — decoupled
        # from `ctx.is_new` so that a chat MOVED into a workspace later (PATCH, ADR-038) also gets
        # the project instructions from its next message. Knowledge FILES stay turn-0-only (ADR-038
        # §3.2, variant a): they are heavy user-content, persisted as history content blocks on
        # turn 0 and replayed automatically; NOT re-injected retroactively for a moved chat
        # (Q-038-1).
        #   - turn 0 (new session): assemble (instructions + files) via context_for_session;
        #   - resume/next turn (not is_new): read ONLY instructions via instructions_for_session
        #     (light single-column) — files are NOT collected (context_for_session is not called).
        # For a non-workspace chat the system prompt is unchanged (base) → no double-injection and
        # the provider prompt cache stays intact.
        workspace_attachments: PreparedAttachments | None = None
        system_prompt = _system_prompt_for(sess.assistant_mode, effective_generation_mode)
        # Credits dual-provider (ADR-073): attachments/workspace follow the SESSION model.
        # BYOK keeps the instance default provider (same as before ADR-073; generation still
        # routes by the key in _generate_loop).
        session_provider = (
            _active_provider()
            if sess.mode == Mode.byok.value
            else get_settings().credits_provider_for_model(sess.model)
        )
        if sess.workspace_project_id is not None:
            # Файлы проекта подмешиваются, когда беседа будет собрана ИЗ НАШЕЙ ИСТОРИИ — а в ней
            # их нет: они уходят провайдеру блоками и НИКОГДА не сохраняются (см. ниже, сборка
            # first_turn). Пока провайдер держит состояние беседы у себя, файлы живут там и
            # повторять их незачем. Как только состояния нет, история восстанавливается из базы,
            # и без повторной вставки модель теряет файлы БЕЗВОЗВРАТНО.
            #
            # Прежнее условие `ctx.is_new` покрывало только первый ход и давало два отказа:
            #   * правка сообщения (ADR-040) сбрасывает состояние провайдера — воспроизведено
            #     2026-08-28 на lunexoro: до правки модель отвечала «ALPHA111» из файла, после —
            #     выдумывала ответ, потому что файла у неё больше не было;
            #   * провайдер без серверного состояния (Anthropic) не хранит ничего, поэтому там
            #     файлы пропадали уже со ВТОРОГО хода любой беседы.
            # Условие: «модель не увидит файлы, если их сейчас не подать». Это верно всегда,
            # когда беседа собирается ИЗ НАШЕЙ ИСТОРИИ — а файлов в ней нет.
            #
            # `not sess.provider_state` покрывает провайдера БЕЗ серверного состояния (Anthropic):
            # там история пересобирается каждый ход, и прежнее условие «только первый ход» теряло
            # файлы со ВТОРОГО хода любой беседы — то есть функция там не работала вовсе.
            #
            # Это ОТМЕНЯЕТ Q-038-1 (перенесённый в проект чат не получал файлы задним числом), и
            # отмена сознательная: решение принималось ради экономии, но с точки зрения человека
            # выглядело дефектом — он переносит чат в проект именно чтобы работать с его файлами.
            # Отличить перенесённый чат от потерявшего состояние по самому состоянию нельзя.
            #
            # Цена приемлема: у провайдера есть кеш промта, и повторная подача неизменного
            # префикса читается из него вдесятеро дешевле обычного ввода.
            needs_files = ctx.is_new or edit_message_step_id is not None or not sess.provider_state
            if needs_files:
                ws_context = await self._deps.workspaces.context_for_session(
                    sess.workspace_project_id, user_id, provider=session_provider
                )
                if ws_context is not None:
                    system_prompt = _system_prompt_with_workspace(
                        sess.assistant_mode, ws_context.instructions, effective_generation_mode
                    )
                    workspace_attachments = ws_context.attachments
            else:
                instructions = await self._deps.workspaces.instructions_for_session(
                    sess.workspace_project_id, user_id
                )
                system_prompt = _system_prompt_with_workspace(
                    sess.assistant_mode, instructions, effective_generation_mode
                )

        system_prompt = await self._system_prompt_with_last_media_job(sess.id, system_prompt)

        prefs = await self._deps.preferences.get(user_id)
        if self._deps.memory is not None:
            memory_block = await self._deps.memory.build_context_for_turn(
                user_id=user_id,
                message=message,
                memory_search=memory_search,
                memory_search_scope=prefs.memory_search_scope,
                workspace_project_id=sess.workspace_project_id,
                exclude_session_id=sess.id,
            )
            if memory_block:
                system_prompt = f"{system_prompt}\n\n{memory_block}"

        # ADR-020 / ADR-033 §3,§5: validate inline attachments (provider-aware) and split into
        # (a) the PreparedAttachments handed to the client ONCE on turn 0 — the client builds the
        # provider content blocks and injects them — and (b) light text placeholders persisted in
        # chat_steps.payload (provider-agnostic). Raw base64 is NEVER persisted (storage invariant).
        # Validation runs BEFORE persisting the user step so a bad attachment (incl. PDF-on-OpenAI)
        # is a clean 422 with no DB write. The shared validation runs before the provider branch.
        # ADR-037 §3,§4: build the per-message conversation-settings block from `context` and
        # PREPEND it to the turn-0 user text (block leads, then "\n\n", then the user message). When
        # no valid key survives validation → None → the text is the bare message (unchanged). The
        # block is injected into the USER content here — the single common turn-0 assembly point
        # BEFORE the provider client — never into `system` (prompt-cache invariant, ADR-037 §5) and
        # provider-agnostically (plain text in user content works on both Anthropic and OpenAI). It
        # is part of the persisted user-step payload below → correct replay; on continuation /
        # tool-result it is NOT re-injected (it already lives in the history of this turn).
        # ADR-039 §2,§3: compose the turn-0 user text (context block + message) and add the text
        # block ONLY when the text is non-empty. For an image-only / file-only turn the text is ""
        # and NO text block is created — a blank text block (text="") is never sent to the provider
        # (Anthropic/OpenAI may reject it; the decision lives here, the single turn-0 assembly
        # point, not in the clients). The validator (§1) guarantees the resulting content is
        # non-empty: empty text ⇒ there is ≥1 attachment ⇒ ≥1 placeholder. Text block (if any)
        # leads, then the attachment placeholders — order unchanged.
        context_block = _render_context_block(context)
        message_text = _compose_turn0_text(context_block, message)
        prepared: PreparedAttachments | None = None
        if attachments:
            prepared = prepare_attachments(attachments, get_settings(), session_provider)
        text_blocks: list[dict[str, Any]] = (
            [{"type": "text", "text": message_text}] if message_text else []
        )
        placeholders = prepared.placeholders if prepared is not None else []
        user_payload_content: list[dict[str, Any]] = [*text_blocks, *placeholders]

        # ADR-086 §3: ход модерируется тогда и только тогда, когда в запросе есть вложения. Ход без
        # вложений не порождает медиа и не оплачивает генерацию — модерация каждого текстового
        # сообщения добавила бы round-trip на КАЖДЫЙ ход ради контента, который клиент не
        # показывает как медиа. Точка вызова — после валидации вложений (кривой файл дешевле отбить
        # раньше) и ДО add_step: нарушение ⇒ 422, ни одного шага в БД, ни одного вызова LLM, кредит
        # не списан, только что созданная пустая сессия откатывается с транзакцией запроса.
        if attachments:
            await self._moderate_turn(message, attachments)

        # Persist fal https refs (TTL 1 day) for later useRecentImage — soft-fail if media off.
        attachment_refs: list[dict[str, Any]] = []
        if prepared is not None and prepared.images:
            media_svc = self._deps.global_tools._media  # noqa: SLF001
            attachment_refs = await upload_turn_attachment_refs(media_svc, prepared.images)

        # Ask-first: recent earlier photo, but NOT when this message already has a new image.
        has_turn_images = bool(prepared is not None and prepared.images)
        if not has_turn_images:
            system_prompt = await self._system_prompt_with_recent_photo(sess.id, system_prompt)

        # ADR-090 §6: если в сессии есть документы — сообщить модели их id и имена. Без этой строки
        # она не знает, что документы существуют, и не догадается вызвать document.list.
        # Содержимое НЕ подмешивается: оно большое и меняется, для чтения есть document.read.
        if self._deps.documents is not None:
            documents_line = await self._deps.documents.context_line(
                user_id=user_id, session_id=sess.id
            )
            if documents_line:
                system_prompt = f"{system_prompt}\n\n{documents_line}"

        # ADR-036 §6: merge the workspace knowledge-file blocks with the request's inline
        # attachment blocks (project context first). Only the request attachments leave a persisted
        # placeholder; workspace files are re-assembled from workspace_files, never persisted here.
        first_turn = _merge_attachments(prepared, workspace_attachments)

        # Persist the user message under this step (placeholders only — no base64, ADR-020 §3).
        user_payload: dict[str, Any] = {"content": user_payload_content}
        if use_generation_v2:
            user_payload["generationMode"] = generation_mode
        if attachment_refs:
            user_payload["attachmentRefs"] = attachment_refs
        await self._deps.repo.add_step(
            session_id=sess.id,
            message_step_id=message_step_id,
            role="user",
            payload=user_payload,
        )

        generation_credit_cost = _turn_credit_cost(
            effective_generation_mode, use_generation_v2=use_generation_v2
        )
        decision, state = await self._evaluate(
            user_id,
            effective_mode,
            sess.id,
            required_credits=generation_credit_cost,
        )
        if not decision_allow(decision):
            return self._blocked(sess.id, decision.block_reason)

        # mode=byok: resolve plaintext key in-memory + its provider (CO-6, ADR-044 §5).
        api_key, byok_provider = await self._resolve_api_key(user_id, effective_mode)

        return await self._generate_loop(
            user_id=user_id,
            session_id=sess.id,
            message_step_id=message_step_id,
            mode=effective_mode,
            billing=_billing_plan(
                effective_mode,
                state,
                credit_amount=generation_credit_cost,
                expose_credit_amount=use_generation_v2,
            ),
            api_key=api_key,
            byok_provider=byok_provider,
            system_prompt=system_prompt,
            # ADR-022 axis A: offer site.* only when the session has a project.
            has_project=sess.project_id is not None,
            first_turn_attachments=first_turn,
            # ADR-034 §4 / ADR-044 / ADR-073: session-fixed model (NULL → None). The effective
            # model is resolved inside _generate_loop against the right provider's allowlist
            # (stale-model fallback): credits → session model's provider, byok → key provider.
            model=sess.model or None,
            generation_mode=effective_generation_mode,
            generation_backend=requested_backend,
            on_text_delta=on_text_delta,
        )

    async def _moderate_turn(self, message: str, attachments: list[AttachmentIn]) -> None:
        """Пре-модерация хода с вложениями (ADR-086 §3). Нарушение → 422 до записи шага.

        В один вызов уходят: текст хода (сырой ``message``, ДО склейки с context-блоком ADR-037)
        вместе с содержимым текстовых вложений, и ВСЕ вложения класса ``image`` как data-URI.
        Отдельного лимита «сколько картинок проверяем» нет и не вводится: любой такой лимит оставил
        бы часть контента непроверенной, а число уже ограничено ``ATTACHMENT_MAX_COUNT``.
        ``document`` (PDF) в модерацию не уходит — omni-moderation его не принимает (Q-086-1).
        """
        if self._deps.moderation is None:
            return
        texts: list[str] = [message] if message else []
        image_urls: list[str] = []
        for att in attachments:
            if att.type == "image":
                image_urls.append(f"data:{att.mediaType};base64,{att.data}")
            elif att.type == "text":
                try:
                    texts.append(base64.b64decode(att.data, validate=True).decode("utf-8"))
                except (binascii.Error, ValueError, UnicodeDecodeError):
                    # prepare_attachments уже отбил бы такой файл; молча пропускаем, чтобы
                    # модерация не превратилась во второй валидатор с иным вердиктом.
                    continue
        verdict = await self._deps.moderation.check(
            surface=SURFACE_CHAT,
            stage=STAGE_INPUT,
            text="\n".join(texts),
            image_urls=image_urls,
        )
        if verdict.blocked:
            raise ContentPolicyViolationError(
                "сообщение отклонено правилами контента: измените текст или вложение"
            )

    async def _system_prompt_with_last_media_job(
        self, session_id: uuid.UUID, system_prompt: str
    ) -> str:
        """Append the latest chat media jobId so edits use image-to-image (ADR-070)."""
        if not get_settings().chat_media_tools_enabled:
            return system_prompt
        last_media = await self._deps.repo.last_media_job_ref(session_id)
        last_image = await self._deps.repo.last_image_job_ref(session_id)
        if last_media is None or not last_media.get("jobId"):
            return system_prompt
        job_id = last_media["jobId"]
        kind = last_media.get("kind", "image")
        hint = (
            f"{system_prompt}\n\nMost recent media job in this chat: "
            f"jobId={job_id} kind={kind}. "
            f"For edits/refinements of that media, pass sourceJobId={job_id}."
        )
        if last_image is not None and last_image.get("jobId"):
            img_id = last_image["jobId"]
            hint += (
                f" Most recent generated photo jobId={img_id}. When the user asks for a video "
                "based on that photo, call media.ask_params kind=video WITHOUT sourceJobId "
                "(the app asks «Использовать последнее фото?» via mediaChoices)."
            )
        return hint

    async def _system_prompt_with_recent_photo(
        self, session_id: uuid.UUID, system_prompt: str
    ) -> str:
        """Hint the model to ask before reusing a photo from recent user messages."""
        if not get_settings().chat_media_tools_enabled:
            return system_prompt
        payloads = await self._deps.repo.recent_user_payloads(
            session_id, limit=RECENT_USER_STEPS_SCAN
        )
        if not recent_image_available(payloads):
            return system_prompt
        alive = latest_alive_image_urls(payloads, max_urls=1)
        if alive:
            return (
                f"{system_prompt}\n\nA recent user message in this chat included a photo that is "
                "still available for generation (stored ~1 day). If the user asks to generate a "
                "photo or video and did not attach a new photo on this message, ask first whether "
                "they want to use that earlier photo. If they agree, call media.ask_params with "
                "useRecentImage true."
            )
        return (
            f"{system_prompt}\n\nA recent user message in this chat included a photo attachment. "
            "If the user asks to generate a photo or video and did not attach a new photo on this "
            "message, ask first whether they want to use that earlier photo. If they agree but "
            "useRecentImage fails, ask them to re-attach the photo."
        )

    async def _recent_image_urls_for_session(self, session_id: uuid.UUID) -> list[str]:
        payloads = await self._deps.repo.recent_user_payloads(
            session_id, limit=RECENT_USER_STEPS_SCAN
        )
        return latest_alive_image_urls(payloads, max_urls=1)

    async def _handle_media_selection(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        media_selection: dict[str, Any],
        message: str,
        generation_mode: str,
    ) -> ChatRunOut:
        """Continue or complete a mediaChoices wizard without calling the LLM (ADR-070).

        Intermediate taps patch the ``media.ask_params`` tool result in place — no extra
        user/assistant bubbles. Only the final submit writes one summary user step + assistant
        step with ``mediaJobs`` (cold-start recoverable).
        """
        from app.chat.media_choices import (
            build_wizard_state,
            effective_source_job_id,
            format_selection_summary,
            media_choices_response,
            submit_params_from_answers,
            validate_and_merge_answers,
        )

        _ = message  # optional client text; history uses a server-built summary on complete
        raw_sid = media_selection.get("selectionId")
        try:
            selection_id = raw_sid if isinstance(raw_sid, uuid.UUID) else uuid.UUID(str(raw_sid))
        except (TypeError, ValueError) as exc:
            raise ValidationFailedError("mediaSelection.selectionId must be a UUID") from exc
        incoming = media_selection.get("answers")
        if not isinstance(incoming, dict):
            raise ValidationFailedError("mediaSelection.answers must be an object")

        prior = await self._deps.repo.find_media_wizard_state(session_id, selection_id)
        if prior is None:
            raise ValidationFailedError("unknown mediaSelection.selectionId")

        kind = str(prior.get("kind") or "")
        prompt = str(prior.get("prompt") or "")
        source_job_id = prior.get("sourceJobId")
        source_job_id_str = str(source_job_id) if source_job_id else None
        raw_last = prior.get("lastImageJobId")
        last_image_job_id = str(raw_last) if raw_last else None
        raw_urls = prior.get("imageUrls")
        image_urls = (
            [str(u) for u in raw_urls if isinstance(u, str) and u]
            if isinstance(raw_urls, list)
            else []
        )
        raw_answers = prior.get("answers")
        existing_answers: dict[str, Any] = raw_answers if isinstance(raw_answers, dict) else {}

        try:
            merged = validate_and_merge_answers(
                kind=kind,
                source_job_id=source_job_id_str,
                image_urls=image_urls or None,
                last_image_job_id=last_image_job_id,
                existing={str(k): str(v) for k, v in existing_answers.items()},
                incoming=incoming,
            )
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc

        media_svc = self._deps.global_tools._media  # noqa: SLF001 — request-scoped service
        if media_svc is None:
            raise ValidationFailedError("media generation is not configured on this instance")

        def _credits(model: Any) -> int:
            return media_svc.credits_for(model)

        next_state = build_wizard_state(
            selection_id=str(selection_id),
            kind=kind,
            prompt=prompt,
            source_job_id=source_job_id_str,
            image_urls=image_urls or None,
            last_image_job_id=last_image_job_id,
            answers=merged,
            credits_for=_credits,
        )

        if next_state is not None:
            patched = await self._deps.repo.patch_media_ask_params_result(
                session_id,
                selection_id,
                answers=merged,
                step=str(next_state["step"]),
                questions=list(next_state["questions"]),
            )
            if patched is None:
                raise ValidationFailedError("unknown mediaSelection.selectionId")
            await self._session.commit()
            return ChatRunOut(
                status="assistant_message",
                session_id=session_id,
                assistant_message="Please choose the next option.",
                message_step_id=patched.message_step_id,
                step_id=patched.id,
                media_choices=media_choices_response(next_state),
            )

        # Wizard complete → one summary bubble + submit (media debit only; no chat debit).
        model_id = merged.get("model")
        if not model_id:
            raise ValidationFailedError("mediaSelection is missing model")
        params = submit_params_from_answers(merged)
        eff_source = effective_source_job_id(
            source_job_id=source_job_id_str,
            last_image_job_id=last_image_job_id,
            answers=merged,
        )
        source_uuid = uuid.UUID(eff_source) if eff_source else None
        try:
            view = await media_svc.submit(
                user_id=user_id,
                kind=kind,
                model_id=model_id,
                prompt=prompt,
                image_urls=image_urls,
                params=params,
                source_job_id=source_uuid,
            )
        except MediaGenerationNotConfiguredError as exc:
            raise ValidationFailedError("media generation is not configured") from exc
        except InsufficientCreditsError:
            raise
        except NotFoundError as exc:
            raise ValidationFailedError("sourceJobId not found") from exc
        except ValidationFailedError:
            raise
        except UpstreamError as exc:
            raise UpstreamError("media provider unavailable; try again later") from exc

        job = view.job
        job_ref = {
            "jobId": str(job.id),
            "kind": job.kind,
            "status": job.status,
            "model": job.model_id,
            "creditsCharged": job.credits_charged,
        }
        summary = format_selection_summary(
            prompt=prompt,
            kind=kind,
            answers=merged,
            credits_charged=job.credits_charged,
            source_job_id=eff_source,
        )
        await self._deps.repo.patch_media_ask_params_result(
            session_id,
            selection_id,
            answers=merged,
            step="done",
            questions=[],
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="user",
            payload={
                "content": [{"type": "text", "text": summary}],
                "generationMode": generation_mode,
                "mediaWizard": {
                    "selectionId": str(selection_id),
                    "kind": kind,
                    "prompt": prompt,
                    "sourceJobId": source_job_id_str,
                    "lastImageJobId": last_image_job_id,
                    "imageUrls": image_urls,
                    "answers": merged,
                    "step": "done",
                    "questions": [],
                    "jobId": str(job.id),
                },
            },
        )
        assistant_text = (
            f"Generation started ({job.credits_charged} cr.). "
            "I will let you know when it is ready."
        )
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={
                "content": [{"type": "text", "text": assistant_text}],
                "mediaJobs": [job_ref],
            },
        )
        await self._session.commit()
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=assistant_text,
            message_step_id=message_step_id,
            step_id=assistant_step.id,
            media_jobs=[job_ref],
        )

    async def run_stream(
        self,
        *,
        user_id: uuid.UUID,
        project_id: str | None,
        session_id: uuid.UUID | None,
        message: str,
        mode: str,
        assistant_mode: str | None = None,
        attachments: list[AttachmentIn] | None = None,
        model: str | None = None,
        workspace_project_id: uuid.UUID | None = None,
        context: dict[str, Any] | None = None,
        edit_message_step_id: uuid.UUID | None = None,
        generation_mode: GenerationMode = "general",
        generation_backend: GenerationBackend = "legacy",
        temporary: bool = False,
        media_selection: dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        """SSE text stream for one chat turn (ADR-069).

        Runs the turn on the caller's task (same AsyncSession), collects text deltas, then
        yields them followed by ``done``. Mid-stream failures after at least one delta yield
        ``error``; pre-stream failures propagate as exceptions for normal 4xx handling.

        Note: deltas are flushed after the provider round(s) complete inside ``run`` — the
        ASGI generator and DB session stay single-task (SQLAlchemy AsyncSession is not
        multi-task safe). Progressive flush during ``stream_message`` still happens at the
        LLM client boundary; the SSE frames are emitted as soon as ``run`` returns its
        collected deltas before ``done``.
        """
        deltas: list[str] = []

        async def _on_delta(text: str) -> None:
            deltas.append(text)

        try:
            out = await self.run(
                user_id=user_id,
                project_id=project_id,
                session_id=session_id,
                message=message,
                mode=mode,
                assistant_mode=assistant_mode,
                attachments=attachments,
                model=model,
                workspace_project_id=workspace_project_id,
                context=context,
                edit_message_step_id=edit_message_step_id,
                generation_mode=generation_mode,
                generation_backend=generation_backend,
                temporary=temporary,
                on_text_delta=_on_delta,
                media_selection=media_selection,
            )
        except BaseException as exc:
            if deltas:
                for chunk in deltas:
                    yield ChatStreamEvent.delta(chunk)
                code = getattr(exc, "code", None) or type(exc).__name__
                msg = str(exc) or type(exc).__name__
                yield ChatStreamEvent.error(str(code), msg)
                return
            raise

        for chunk in deltas:
            yield ChatStreamEvent.delta(chunk)
        yield ChatStreamEvent.done(out)

    async def tool_result(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        results: list[ToolResultIn],
        generation_backend: GenerationBackend = "legacy",
    ) -> ChatRunOut:
        """Apply a batch of tool results and continue only when the turn barrier closes (ADR-025).

        Each item is applied independently (per-item idempotency). The continuation to Anthropic is
        gated by the turn barrier: it runs ONLY when every client-side tool_call of the assistant
        turn (one message_step_id) is completed/errored — otherwise an orphan tool_use would make
        Anthropic reject the next messages.create (400 → 502). Until the barrier closes the response
        is status=tool_call with the remaining (not-yet-completed) client-side calls.
        """
        if not results:  # pragma: no cover - schema guarantees non-empty
            raise ValidationFailedError("results must be non-empty")

        # Resolve every referenced tool_call; enforce session ownership + single-turn invariant.
        sess = await self._deps.repo.get_session(session_id, user_id)
        if sess is None:
            raise NotFoundError("session not found")
        requested_backend: GenerationBackend = "v2" if generation_backend == "v2" else "legacy"
        use_generation_v2 = requested_backend == "v2"
        await self._ensure_session_backend(
            sess,
            requested_backend=requested_backend,
            allow_upgrade_from_legacy=False,
        )

        resolved: list[tuple[ToolResultIn, ToolCall]] = []
        message_step_id: uuid.UUID | None = None
        for item in results:
            tool_call = await self._deps.repo.get_tool_call(item.tool_call_id)
            if tool_call is None or tool_call.session_id != session_id:
                raise NotFoundError("tool call not found for session")
            if message_step_id is None:
                message_step_id = tool_call.message_step_id
            elif tool_call.message_step_id != message_step_id:
                # All batch items must belong to one turn (one message_step_id) — 02-api-contracts.
                raise ValidationFailedError("all results must belong to the same turn")
            resolved.append((item, tool_call))

        assert message_step_id is not None  # noqa: S101 - results is non-empty

        # ADR-064 §12 / ADR-082: restore the turn's generation mode ONCE, here — BEFORE any
        # leg can return — so every continuation uses the SAME effective mode. Legacy has no
        # persisted generationMode; the helper returns `research` only when this instance opted
        # into CHAT_LEGACY_WEB_SEARCH_ENABLED, else `general`.
        generation_mode = (
            await self._deps.repo.generation_mode_for_message_step(session_id, message_step_id)
            if use_generation_v2
            else _effective_generation_mode("general", use_generation_v2=False)
        )

        # Apply each result (per-item idempotency, ADR-005): already completed/errored → skip
        # the write (do NOT overwrite, do NOT re-audit). New ones transition pending → done.
        for item, tool_call in resolved:
            if tool_call.status in ("completed", "errored"):
                continue  # idempotent: result not overwritten
            await self._apply_tool_result(
                user_id=user_id,
                session_id=session_id,
                message_step_id=message_step_id,
                tool_call=tool_call,
                result=item.result,
                error=item.error,
            )

        # ADR-025 barrier: continuation only when ALL client-side tool_calls of this turn are
        # completed/errored. Server-side tools (project-scoped site.* AND global time.now,
        # ADR-026 §4) are executed on the backend and were completed in the run loop; the barrier
        # considers only client-side calls.
        turn_calls = await self._deps.repo.list_tool_calls_for_step(session_id, message_step_id)
        client_calls = [
            tc
            for tc in turn_calls
            if tc.tool_name not in SERVER_SIDE_TOOLS
            and tc.tool_name not in GLOBAL_SERVER_SIDE_TOOLS
        ]
        pending = [tc for tc in client_calls if tc.status not in ("completed", "errored")]
        if pending:
            # Barrier not closed → tell the client which results are still awaited. No Anthropic
            # call, no billing. messageStepId stable; stepId = the assistant turn step with the
            # tool_use blocks (ADR-025: same turn).
            await self._session.commit()
            remaining = [
                ToolCallOut(id=str(tc.id), name=tc.tool_name, args=dict(tc.args)) for tc in pending
            ]
            assistant_step_id = await self._deps.repo.assistant_tool_step_id(
                session_id, message_step_id
            )
            return await self._decorate_turn_out(
                ChatRunOut(
                    status="tool_call",
                    session_id=session_id,
                    tool_calls=remaining,
                    tool_call=remaining[0],
                    message_step_id=message_step_id,
                    step_id=assistant_step_id,
                ),
                message_step_id=message_step_id,
                quiz_accumulated=None,
                media_accumulated=None,
                generation_mode=generation_mode,
            )

        # Barrier closed. Idempotent replay: if a continuation step was already saved for this turn
        # (e.g. a repeated batch after the turn completed), return it without re-calling Anthropic.
        anchor_id = resolved[0][1].id
        saved = await self._deps.repo.next_step_after(session_id, message_step_id, anchor_id)
        if saved is not None and self._all_already_done_before(resolved):
            # ADR-064 §7: the replay is NOT a special rule — it is the ordinary turn-scoped fallback
            # (no accumulator in this call → read the turn's quiz step). server_tools stays empty
            # here by contrast (ADR-028): it is a per-call indicator, quiz is turn content.
            return await self._decorate_turn_out(
                self._render_saved_step(session_id, message_step_id, saved),
                message_step_id=message_step_id,
                quiz_accumulated=None,
                media_accumulated=None,
                generation_mode=generation_mode,
            )

        mode = Mode(sess.mode)
        generation_credit_cost = _turn_credit_cost(
            generation_mode, use_generation_v2=use_generation_v2
        )
        # Re-evaluate policy (access may have changed).
        decision, state = await self._evaluate(
            user_id,
            mode,
            session_id,
            required_credits=generation_credit_cost,
        )
        if not decision_allow(decision):
            return self._blocked(session_id, decision.block_reason)

        api_key, byok_provider = await self._resolve_api_key(user_id, mode)
        # ADR-036 §3: knowledge files are already replayed as content blocks in the history, but
        # `instructions` live in the `system` param (NOT in history) and are sent on EVERY LLM call.
        # So on each continuation re-inject the workspace instructions into system via the SAME
        # helper used on turn 0 (identical behavior). Read ONLY instructions (light single-column);
        # do NOT re-inject knowledge files. Empty/missing instructions or a deleted workspace → base
        # system prompt unchanged (graceful).
        # ADR-064: the continuation carries the SAME mode suffix as the original run leg (the mode
        # was restored from the user step above), so the model keeps the quiz instructions for the
        # rest of the turn.
        system_prompt = _system_prompt_for(sess.assistant_mode, generation_mode)
        if sess.workspace_project_id is not None:
            instructions = await self._deps.workspaces.instructions_for_session(
                sess.workspace_project_id, user_id
            )
            system_prompt = _system_prompt_with_workspace(
                sess.assistant_mode, instructions, generation_mode
            )
        system_prompt = await self._system_prompt_with_last_media_job(sess.id, system_prompt)
        system_prompt = await self._system_prompt_with_recent_photo(sess.id, system_prompt)
        return await self._generate_loop(
            user_id=user_id,
            session_id=session_id,
            message_step_id=message_step_id,
            mode=mode,
            billing=_billing_plan(
                mode,
                state,
                credit_amount=generation_credit_cost,
                expose_credit_amount=use_generation_v2,
            ),
            api_key=api_key,
            byok_provider=byok_provider,
            system_prompt=system_prompt,
            # ADR-022 axis A: project_id is session-fixed; gate site.* by the session's project.
            has_project=sess.project_id is not None,
            # ADR-034 §4 / ADR-044 / ADR-073: session-fixed model; effective model resolved in
            # _generate_loop against the right provider's allowlist (credits → session provider,
            # byok → key provider).
            model=sess.model or None,
            generation_mode=generation_mode,
            generation_backend=requested_backend,
        )

    @staticmethod
    def _all_already_done_before(resolved: list[tuple[ToolResultIn, ToolCall]]) -> bool:
        """True when every referenced tool_call was ALREADY completed/errored on entry (replay).

        A fully-replayed batch (all items previously applied) closes the barrier without any new
        transition → the saved continuation step is returned idempotently rather than re-calling
        Anthropic (ADR-025 idempotency: continuation runs once per barrier close).
        """
        return all(tc.status in ("completed", "errored") for _, tc in resolved)

    async def _apply_tool_result(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call: ToolCall,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> None:
        """Atomically transition one tool_call and persist its tool_result + audit (ADR-025)."""
        status = "errored" if error is not None else "completed"
        transitioned = await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call.id,
            status=status,
            result=result if result is not None else error,
        )
        if not transitioned:
            # Concurrent completion won the race → behave idempotently (no duplicate step/audit).
            return

        # Persist the tool_result as a tool step. (result size limit is enforced at the schema
        # layer; result content is opaque per-tool and forwarded to Claude as-is.)
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call.id),
                # ADR-008: tool_result.tool_use_id MUST equal the raw provider id of the matching
                # tool_use block, NOT the domain UUID. Stored here so _build_messages replays the
                # continuation history with a consistent id pair.
                "providerToolUseId": tool_call.provider_tool_use_id,
                "toolName": tool_call.tool_name,
                "result": result,
                "error": error,
            },
        )

        # Audit mutating tool completion (AC-7).
        if tool_call.tool_name in MUTATING_TOOLS:
            await self._deps.audit.record(
                AuditEvent(
                    user_id=user_id,
                    session_id=session_id,
                    event_type=EVENT_TOOL_MUTATION,
                    payload={
                        "toolCallId": str(tool_call.id),
                        "toolName": tool_call.tool_name,
                        "status": status,
                    },
                )
            )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call.id),
                    "toolName": tool_call.tool_name,
                    "status": status,
                },
            )
        )

    # ---- internals ----

    async def _evaluate(
        self,
        user_id: uuid.UUID,
        mode: Mode,
        session_id: uuid.UUID,
        *,
        required_credits: int = 1,
    ) -> tuple[Decision, PolicyState]:
        state = await load_policy_state(self._session, user_id)
        decision = evaluate(state, mode, required_credits=required_credits)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_POLICY_DECISION,
                payload={
                    "mode": mode.value,
                    "decision": "allow" if decision.allow else "blocked",
                    "blockReason": decision.block_reason.value if decision.block_reason else None,
                    "requiredCredits": max(1, required_credits),
                },
            )
        )
        log_event(
            logger,
            logging.INFO,
            "policy_decision",
            mode=mode.value,
            allow=decision.allow,
            blockReason=decision.block_reason.value if decision.block_reason else None,
            requiredCredits=max(1, required_credits),
        )
        return decision, state

    def _blocked(self, session_id: uuid.UUID, reason: BlockReason | None) -> ChatRunOut:
        resolved = reason or BlockReason.policy_denied
        blocked_requests_total.labels(reason=resolved.value).inc()
        return ChatRunOut(status="blocked", session_id=session_id, block_reason=resolved.value)

    async def _resolve_api_key(
        self, user_id: uuid.UUID, mode: Mode
    ) -> tuple[str | None, str | None]:
        """Resolve (plaintext api_key, byok_provider) for this turn (ADR-044 §5).

        - credits → ``(None, None)``: the service key of the active provider is used by the injected
          client; no provider routing.
        - byok → ``(plaintext_key, provider)``: the key is decrypted in-memory (never logged) and
          the provider is read from ``byok_keys.provider`` (fallback: detected from the plaintext
          for a legacy NULL row, ADR-044 §4). The provider routes generation to ``llm_client_for``
          in ``_generate_loop``. ``provider`` may be ``None`` only for a legacy key of unrecognized
          format → a defensive ``byok_invalid`` block downstream (unreachable for a valid key).
        """
        if mode is Mode.byok:
            byok_usage_share.set(1)
            resolved = await self._deps.byok.get_plaintext_key_with_provider(user_id)
            if resolved is None:
                # Policy should have blocked this; defensive.
                raise ValidationFailedError("byok key unavailable")
            return resolved
        byok_usage_share.set(0)
        return None, None  # service key used by the active provider's client

    async def _will_create_session(self, user_id: uuid.UUID, session_id: uuid.UUID | None) -> bool:
        """True when ``get_or_create_session`` would CREATE a new session (ADR-034 §3 model gate).

        Mirrors the repository's resume rule: a missing ``session_id``, or an absent / expired owned
        session, results in a create; an owned, non-expired session is a resume. Used only to gate
        the model-allowlist validation so the request ``model`` is ignored on resume (and validated
        before any new row is written on create). Read-only; the repository stays the single writer.
        """
        if session_id is None:
            return True
        existing = await self._deps.repo.get_session(session_id, user_id)
        if existing is None:
            return True
        return self._deps.repo.is_expired(existing)

    async def _ensure_session_backend(
        self,
        session: ChatSession,
        *,
        requested_backend: GenerationBackend,
        allow_upgrade_from_legacy: bool,
    ) -> None:
        """Enforce that a chat session is continued through the matching chat API generation path.

        `chat_sessions.generation_backend` is nullable because existing sessions predate v2; NULL is
        treated as `legacy`. A normal v2 `/run` may upgrade such a session because the caller
        explicitly opted into the new contract and the upgraded session loses nothing by having no
        provider-side state: v2 replays the full local history on EVERY turn while continuation is
        off (`_CONTINUATION_ENABLED`, TD-032), so an absent `previous_response_id` is the normal
        condition, not a degraded one. `/tool-result` does not upgrade: it must continue the same
        in-flight turn through the backend that created the tool call.
        """
        actual_backend: GenerationBackend = "v2" if session.generation_backend == "v2" else "legacy"
        if actual_backend == requested_backend:
            return
        if requested_backend == "legacy":
            raise ValidationFailedError("session belongs to chat v2; use /v1/chat/v2/run")
        if not allow_upgrade_from_legacy:
            raise ValidationFailedError("session belongs to legacy chat; use /v1/chat/tool-result")
        await self._deps.repo.set_generation_backend(session, "v2")
        await self._deps.repo.clear_provider_state(session.id)

    async def _resolve_turn_quiz(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        accumulated: dict[str, Any] | None,
        generation_mode: str,
    ) -> dict[str, Any] | None:
        """The quiz pool of the TURN, by the single turn-scoped predicate (ADR-064 §7).

        Two producers, in order:
        1. the accumulator of the CURRENT call (last-wins across rounds of this call);
        2. FALLBACK — the accumulator is empty AND the effective mode of the turn is
           ``study_learn`` → the last quiz tool-step of this ``message_step_id`` with a non-empty
           result. No such step → ``None``.

        The mode predicate is MANDATORY, not an optimization guard: it keeps the extra read strictly
        on quiz turns, so every other mode and the whole legacy path issue no additional query.

        One predicate covers ALL legs of the turn (run, each continuation, idempotent replay,
        blocked+max_tokens). That is the load-bearing part of the §7 guarantee: the
        ``assistantMessage`` suppression is keyed on a non-empty ``quiz``, so a leg that lost the
        pool would silently un-suppress the text and show the learner duplicated questions with the
        answers revealed — which is exactly the multi-call turn (`quiz.generate` + a client-side
        tool in one assistant step) that a per-call accumulator would break.
        """
        if accumulated is not None:
            return accumulated
        if generation_mode != "study_learn":
            return None
        return await self._deps.repo.last_tool_result_for_message_step(
            session_id, message_step_id, TOOL_QUIZ_GENERATE
        )

    async def _with_turn_quiz(
        self,
        out: ChatRunOut,
        *,
        message_step_id: uuid.UUID,
        accumulated: dict[str, Any] | None,
        generation_mode: str,
    ) -> ChatRunOut:
        """Attach the turn-scoped quiz pool to a terminal ``ChatRunOut`` (ADR-064 §7).

        Applied at EVERY leg that returns a turn's content, so the rule lives in one place instead
        of being re-derived per branch.

        A response with ``message_step_id is None`` carries no turn at all (policy-block before
        generation, or the credits_empty block that rolls the assistant step back), so there is
        nothing to attribute a pool to and no read is issued — ``quiz`` stays ``null``. The
        ``blocked``+``max_tokens`` leg is NOT this case: its turn and step ids are set, so it does
        get the turn's pool.
        """
        if out.message_step_id is None:
            return out
        quiz = await self._resolve_turn_quiz(
            session_id=out.session_id,
            message_step_id=message_step_id,
            accumulated=accumulated,
            generation_mode=generation_mode,
        )
        return replace(out, quiz=quiz)

    async def _resolve_turn_media_jobs(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        accumulated: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]] | None:
        """Media jobs of the TURN (ADR-068): call accumulator, else persisted media tool results."""
        if accumulated:
            return list(accumulated)
        recovered = await self._deps.repo.tool_results_for_message_step(
            session_id, message_step_id, _MEDIA_TOOL_NAMES
        )
        return recovered or None

    async def _with_turn_media_jobs(
        self,
        out: ChatRunOut,
        *,
        message_step_id: uuid.UUID,
        accumulated: list[dict[str, Any]] | None,
    ) -> ChatRunOut:
        """Attach turn-scoped mediaJobs to a terminal ChatRunOut (ADR-068)."""
        if out.message_step_id is None:
            return out
        media_jobs = await self._resolve_turn_media_jobs(
            session_id=out.session_id,
            message_step_id=message_step_id,
            accumulated=accumulated,
        )
        return replace(out, media_jobs=media_jobs)

    async def _resolve_turn_media_choices(
        self,
        *,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        accumulated: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """mediaChoices of the TURN (ADR-070): call accumulator, else last ask_params result."""
        from app.chat.media_choices import media_choices_response

        if accumulated is not None:
            return media_choices_response(accumulated)
        recovered = await self._deps.repo.last_tool_result_for_message_step(
            session_id, message_step_id, TOOL_MEDIA_ASK_PARAMS
        )
        if recovered is None or "questions" not in recovered:
            return None
        return media_choices_response(recovered)

    async def _with_turn_media_choices(
        self,
        out: ChatRunOut,
        *,
        message_step_id: uuid.UUID,
        accumulated: dict[str, Any] | None,
    ) -> ChatRunOut:
        """Attach turn-scoped mediaChoices (ADR-070). Prefer an already-set field on ``out``."""
        if out.media_choices is not None:
            return out
        if out.message_step_id is None:
            return out
        choices = await self._resolve_turn_media_choices(
            session_id=out.session_id,
            message_step_id=message_step_id,
            accumulated=accumulated,
        )
        return replace(out, media_choices=choices)

    async def _decorate_turn_out(
        self,
        out: ChatRunOut,
        *,
        message_step_id: uuid.UUID,
        quiz_accumulated: dict[str, Any] | None,
        media_accumulated: list[dict[str, Any]] | None,
        generation_mode: str,
        media_choices_accumulated: dict[str, Any] | None = None,
    ) -> ChatRunOut:
        """Attach turn-scoped quiz + mediaJobs + mediaChoices (ADR-064 / ADR-068 / ADR-070)."""
        decorated = await self._with_turn_quiz(
            out,
            message_step_id=message_step_id,
            accumulated=quiz_accumulated,
            generation_mode=generation_mode,
        )
        decorated = await self._with_turn_media_jobs(
            decorated,
            message_step_id=message_step_id,
            accumulated=media_accumulated,
        )
        return await self._with_turn_media_choices(
            decorated,
            message_step_id=message_step_id,
            accumulated=media_choices_accumulated,
        )

    async def _build_messages(self, session_id: uuid.UUID) -> list[NeutralMessage]:
        """Reconstruct the provider-NEUTRAL history from chat_steps (TD-002, ADR-033 §3).

        Returns neutral messages; the active client translates them to provider wire messages
        (Anthropic ``tool_result`` block / OpenAI ``role=tool``). user/assistant carry the wire
        content blocks of the active provider from ``payload``; a tool step carries the domain
        tool-result record (incl. the raw ``providerToolUseId`` — ADR-008/BUG-4 — used to align
        tool_use ↔ tool_result on replay, never a domain UUID).
        """
        steps = await self._deps.repo.list_steps(session_id)
        messages: list[NeutralMessage] = []
        for step in steps:
            payload = step.payload
            if step.role == "user":
                messages.append(NeutralMessage(role="user", content_blocks=payload["content"]))
            elif step.role == "assistant":
                messages.append(NeutralMessage(role="assistant", content_blocks=payload["content"]))
            elif step.role == "tool":
                messages.append(
                    NeutralMessage(
                        role="tool",
                        tool_call_id=payload.get("toolCallId"),
                        provider_tool_use_id=payload["providerToolUseId"],
                        tool_name=payload.get("toolName"),
                        result=payload.get("result"),
                        error=payload.get("error"),
                    )
                )
        return messages

    async def _credits_llm_with_failover(
        self,
        *,
        session_model: str | None,
        llm_kwargs: dict[str, Any],
        stored_provider_state: dict[str, Any] | None,
        use_generation_v2: bool,
        on_text_delta: Callable[[str], Awaitable[None]] | None,
        emit_text_deltas: bool,
    ) -> tuple[LLMResult, str]:
        """Try credits keys in ADR-074 order. Returns ``(result, provider_used)``.

        BYOK never enters here. Each ``/chat/run`` starts from the primary key again (no
        process-wide memory of a dead key). The session model stored in DB is not rewritten
        when a crossover candidate answers.
        """
        attempts = build_attempt_chain(session_model)
        active = get_settings().credits_provider_for_model(None)
        index = 0
        while True:
            attempt = attempts[index]
            llm = (
                self._deps.llm
                if attempt.provider == active
                else _credits_llm(provider=attempt.provider, use_generation_v2=use_generation_v2)
            )
            attempt_kwargs = {
                **llm_kwargs,
                "api_key": attempt.api_key,
                "model": (
                    attempt.model
                    if attempt.model is not None
                    else _model_for_provider(session_model, attempt.provider)
                ),
                "provider_state": _provider_state_for_attempt(attempt, stored_provider_state),
            }
            try:
                result = await _invoke_llm(
                    llm,
                    attempt_kwargs,
                    on_text_delta=on_text_delta,
                    emit_text_deltas=emit_text_deltas,
                )
                return result, attempt.provider
            except (AnthropicAuthError, OpenAIAuthError, UpstreamError) as exc:
                following = next_attempt_index(attempts, index, exc)
                if following is None:
                    if isinstance(exc, AnthropicAuthError | OpenAIAuthError):
                        raise UpstreamError("llm provider unavailable") from exc
                    raise
                nxt = attempts[following]
                log_event(
                    logger,
                    logging.WARNING,
                    "chat_provider_failover",
                    reason=("credentials" if is_credential_failure(exc) else "upstream_failure"),
                    from_provider=attempt.provider,
                    from_key_slot=attempt.key_index,
                    to_provider=nxt.provider,
                    to_key_slot=nxt.key_index,
                    requested_model=session_model,
                    next_model=nxt.model,
                    exception_class=type(exc).__name__,
                )
                index = following

    async def _generate_loop(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        mode: Mode,
        billing: _BillingPlan,
        api_key: str | None,
        system_prompt: str,
        has_project: bool,
        byok_provider: str | None = None,
        first_turn_attachments: PreparedAttachments | None = None,
        model: str | None = None,
        generation_mode: str = "general",
        generation_backend: GenerationBackend = "legacy",
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatRunOut:
        use_generation_v2 = generation_backend == "v2"
        # ADR-064 §3 / ADR-082: ONE effective mode for axis-C, provider and price. Same helper
        # as run()/tool_result — computing it twice with a different formula is how they drift.
        effective_generation_mode = _effective_generation_mode(
            generation_mode, use_generation_v2=use_generation_v2
        )
        # ADR-069: stream text deltas only for non-study turns (anti-spoiler for quiz).
        emit_text_deltas = on_text_delta is not None and effective_generation_mode != "study_learn"
        # ADR-064 §7 producer 1: pool of THIS call, last-wins, threaded through every round.
        quiz_accumulator = _QuizAccumulator()
        # ADR-068: media jobs of THIS call (append), threaded through every round.
        media_accumulator = _MediaJobsAccumulator()
        # ADR-070: mediaChoices wizard of THIS call, last-wins.
        media_choices_accumulator = _MediaChoicesAccumulator()
        # ADR-044 §5 / ADR-073 / ADR-074: select the generation client + the effective model.
        # - credits → first candidate is the session model's provider (ADR-073). Unset
        #   LLM_PROVIDERS → LLM_PROVIDER only (ADR-033). Spare keys / crossover happen inside
        #   the per-call loop (ADR-074); this assignment is the session provider, overwritten
        #   if a crossover candidate answers. Stale-model: id not in that provider's allowlist
        #   → None → client default (same guard as after an LLM_PROVIDER switch).
        # - byok → the client of the KEY's provider (llm_client_for), independent of LLM_PROVIDER;
        #   the session model is forwarded only if it is in the KEY provider's allowlist, else the
        #   BYOK default of that provider (a foreign model is never sent to the key's client).
        #   BYOK does not rotate keys (the user's key is the only candidate).
        llm: LLMClient | None = None
        effective_model: str | None = None
        if mode is Mode.byok:
            if byok_provider is None:
                # Defensive (ADR-044 §5.1): a valid/enabled key always has a detectable provider.
                # An unrecognized legacy key reached generation → block, do not call any provider.
                await self._session.commit()
                return self._blocked(session_id, BlockReason.byok_invalid)
            llm = (
                generation_llm_client_for(byok_provider)
                if _uses_generation_client(use_generation_v2)
                else llm_client_for(byok_provider)
            )
            effective_model = _model_for_provider(model, byok_provider)
            if effective_model is None:
                # §5.3: foreign/absent session model → the BYOK default of the key's provider.
                effective_model = get_settings().byok_default_model_for(byok_provider)
            provider = byok_provider
        else:
            # ADR-073: route credits by the session model (session-fixed; no mid-chat switch).
            # ADR-074 may still answer from the other provider on this call only.
            provider = get_settings().credits_provider_for_model(model)
        # ADR-011: server-side site.* tools are executed by the backend synchronously inside this
        # loop, WITHOUT a round-trip to iOS. We keep calling the LLM as long as the turn contains
        # ONLY server-side tools (their tool_results are produced here and fed straight back).
        # A turn with any client-side tool returns status=tool_call to iOS as before. A pure
        # assistant turn is the final step. The loop is bounded by MAX_SERVER_TOOL_ROUNDS (§2).
        max_rounds = get_settings().max_server_tool_rounds
        # ADR-028 Решение 2: accumulate the server-side tools executed across ALL rounds of THIS
        # call (one /chat/run or one /chat/tool-result continuation), in execution order. Threaded
        # into every terminal ChatRunOut of this loop so the client sees what ran, regardless of how
        # the turn ended (assistant_message / client tool_call / max_tokens).
        server_tools: list[ServerToolExecutionOut] = []
        # ADR-020 / ADR-033 §3: the PreparedAttachments are handed to the client on the FIRST
        # iteration ONLY; the client builds the provider content blocks and injects them into the
        # last user turn. Subsequent (tool-loop) iterations replay placeholders from chat_steps —
        # heavy base64 is never re-sent. The reference is consumed after the first call.
        turn0_attachments = first_turn_attachments
        # Same-turn image attachments stay available for media tools (upload → image-to-image).
        turn_images = list(first_turn_attachments.images) if first_turn_attachments else []
        recent_image_urls = await self._recent_image_urls_for_session(session_id)
        last_image_ref = await self._deps.repo.last_image_job_ref(session_id)
        last_image_job_id = (
            str(last_image_ref["jobId"])
            if last_image_ref is not None and last_image_ref.get("jobId")
            else None
        )
        for _ in range(max_rounds + 1):
            messages = await self._build_messages(session_id)
            sess = await self._session.get(ChatSession, session_id)
            provider_state = (
                dict(sess.provider_state)
                if sess is not None
                and isinstance(sess.provider_state, dict)
                and mode is not Mode.byok
                and use_generation_v2
                else None
            )
            # MAJOR-4: commit the persisted steps + audit BEFORE the network call so the pooled DB
            # connection is not held open for the whole LLM generation. Each subsequent
            # server-side round commits its own persisted tool_use/tool_result before re-calling.
            await self._session.commit()
            # Shared kwargs for create_message / stream_message (ADR-069).
            llm_kwargs: dict[str, Any] = {
                "system_prompt": system_prompt,
                "messages": messages,
                # ADR-022 axis A: in «чистый чат» (no project) site.* (SERVER_SIDE_TOOLS) are
                # NOT offered. Axis B (assistant_mode, Q-012-1) is not yet implemented; the
                # effective set = this project gate over current behavior. Neutral tool defs;
                # the client serializes them per provider (ADR-033 §4).
                # ADR-064 §3 axis C: mode-gated tools (quiz.generate) are offered only when the
                # EFFECTIVE mode of the turn allows them — the same value passed as
                # generation_mode below, never a second computation.
                "tools": neutral_tool_definitions(
                    include_server_side=has_project,
                    generation_mode=effective_generation_mode,
                    include_media_chat_tools=get_settings().chat_media_tools_enabled,
                    disabled_families=get_settings().disabled_tool_families(),
                    # ADR-094 ось D: инструменты кода предлагаются только при флаге инстанса И
                    # только в режиме `code`. Режим — не косметика: в обычном чате модель не
                    # должна даже рассматривать правку файлов на машине человека.
                    code_tools_enabled=(
                        get_settings().code_tools_enabled
                        and sess is not None
                        and sess.assistant_mode == "code"
                    ),
                ),
                "attachments": turn0_attachments,
                "generation_mode": effective_generation_mode,
            }
            try:
                result: LLMResult
                if mode is Mode.byok:
                    assert llm is not None
                    result = await _invoke_llm(
                        llm,
                        {
                            **llm_kwargs,
                            "api_key": api_key,
                            "model": effective_model,
                            "provider_state": None,
                        },
                        on_text_delta=on_text_delta,
                        emit_text_deltas=emit_text_deltas,
                    )
                else:
                    result, provider = await self._credits_llm_with_failover(
                        session_model=model,
                        llm_kwargs=llm_kwargs,
                        stored_provider_state=provider_state,
                        use_generation_v2=use_generation_v2,
                        on_text_delta=on_text_delta,
                        emit_text_deltas=emit_text_deltas,
                    )
            except (AnthropicAuthError, OpenAIAuthError):
                if mode is Mode.byok:
                    # ADR-016: a previously-valid BYOK key rejected with 401 on use → expired
                    # (revoked/expired), not freshly invalid. Both map to byok_invalid in policy.
                    await self._deps.byok.mark_expired(user_id)
                    await self._session.commit()
                    return self._blocked(session_id, BlockReason.byok_invalid)
                raise
            # Consume the attachment override after the first call (placeholders only afterwards).
            turn0_attachments = None

            usage = result.usage.to_dict()
            if use_generation_v2:
                usage["generationMode"] = generation_mode
            token_usage_total.labels(direction="input", model=result.usage.model).inc(
                result.usage.input_tokens
            )
            token_usage_total.labels(direction="output", model=result.usage.model).inc(
                result.usage.output_tokens
            )
            # ADR-079 §1: this dict is the step's usage — the ONE place per LLM call where the
            # model of the turn is known as it happens. If it carries no purchase price, the
            # whole turn's cost reads `None` in CRM, which looks exactly like "no traffic";
            # report it here so the gap surfaces without anyone opening CRM. Costing itself
            # stays on the read path.
            report_chat_step_pricing(usage)
            if use_generation_v2:
                await self._maybe_update_provider_state(
                    session_id=session_id,
                    mode=mode,
                    provider=provider,
                    model=result.usage.model,
                    result=result,
                )

            # ADR-025: dispatch by stop_reason, NOT by the mere presence of tool_use blocks. A
            # max_tokens-truncated turn may carry incomplete tool_use blocks in content — they are
            # not executable and must NOT be surfaced; only the canonical tool_use stop reason
            # enters the tool branch. ADR-033 §2: compare against canonical (provider-neutral)
            # values; the client already mapped its wire stop_reason to these constants.
            if result.stop_reason == STOP_REASON_MAX_TOKENS:
                api_key = None
                # ADR-028: blocked+max_tokens may carry NON-empty server_tools (server-side rounds
                # could have run before the final turn was truncated).
                # ADR-064 §7: it also carries the turn's quiz — including a pool produced on an
                # EARLIER leg of the turn — and the partial truncated text is then suppressed in
                # _to_response like any other status.
                return await self._decorate_turn_out(
                    await self._handle_max_tokens(
                        user_id=user_id,
                        session_id=session_id,
                        message_step_id=message_step_id,
                        result=result,
                        usage=usage,
                        server_tools=server_tools,
                    ),
                    message_step_id=message_step_id,
                    quiz_accumulated=quiz_accumulator.pool,
                    media_accumulated=media_accumulator.jobs or None,
                    generation_mode=effective_generation_mode,
                    media_choices_accumulated=media_choices_accumulator.state,
                )

            if result.stop_reason == STOP_REASON_TOOL_USE and result.tool_uses:
                outcome = await self._handle_tool_use(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    result=result,
                    usage=usage,
                    has_project=has_project,
                    server_tools=server_tools,
                    generation_mode=effective_generation_mode,
                    quiz_accumulator=quiz_accumulator,
                    media_accumulator=media_accumulator,
                    media_choices_accumulator=media_choices_accumulator,
                    turn_images=turn_images,
                    recent_image_urls=recent_image_urls,
                    last_image_job_id=last_image_job_id,
                )
                # Persist the tool_use step + tool_calls + tool_results + audit (no billing here).
                await self._session.commit()
                if outcome.client_out is not None:
                    # A client-side tool is pending → hand off to iOS (drop the plaintext key).
                    # server_tools carries any server-side tools executed in this same turn BEFORE
                    # the client-side hand-off (ADR-028).
                    api_key = None
                    # ADR-064 §7: this is the leg of the MAIN quiz scenario — the model called
                    # quiz.generate AND a client-side tool in one assistant step. The pool must ride
                    # along here, and (turn-scope) again on the tool-result leg that finishes it.
                    return await self._decorate_turn_out(
                        outcome.client_out,
                        message_step_id=message_step_id,
                        quiz_accumulated=quiz_accumulator.pool,
                        media_accumulated=media_accumulator.jobs or None,
                        generation_mode=effective_generation_mode,
                        media_choices_accumulated=media_choices_accumulator.state,
                    )
                # Pure server-side turn: results are persisted; continue the loop to Anthropic.
                continue

            # Final assistant_message — break out of the server-side loop and bill once.
            api_key = None
            return await self._decorate_turn_out(
                await self._finalize_assistant(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    billing=billing,
                    result=result,
                    usage=usage,
                    server_tools=server_tools,
                    media_jobs=media_accumulator.jobs or None,
                ),
                message_step_id=message_step_id,
                quiz_accumulated=quiz_accumulator.pool,
                media_accumulated=media_accumulator.jobs or None,
                generation_mode=effective_generation_mode,
                media_choices_accumulated=media_choices_accumulator.state,
            )

        # Exceeded MAX_SERVER_TOOL_ROUNDS consecutive server-side rounds (ADR-011 §2): controlled
        # failure + audit, never an infinite loop. No billing (no final assistant_message).
        api_key = None
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "error": "max_server_tool_rounds_exceeded",
                    "maxRounds": max_rounds,
                },
            )
        )
        await self._session.commit()
        raise UpstreamError("server-side tool loop exceeded maximum rounds")

    async def _maybe_update_provider_state(
        self,
        *,
        session_id: uuid.UUID,
        mode: Mode,
        provider: str,
        model: str,
        result: LLMResult,
    ) -> None:
        """Persist provider-side continuation handles after a successful model response.

        Nothing consumes these handles today: continuation is switched off
        (``_CONTINUATION_ENABLED`` in ``app.chat.openai_responses_client``, TD-032) and every v2
        turn replays the full local history. This write keeps the stored handle in step with the
        session so the switch has something valid to start from; the rules below are the ones that
        apply once it IS on.

        Only credit-mode OpenAI calls store Responses API ids. BYOK is intentionally excluded
        because a user can rotate the key between turns; a stored response id may belong to a
        different provider account. A max_tokens-truncated OpenAI turn clears the state rather than
        leaving a handle that names a partial remote response. Anthropic Messages API does not have
        the same ``previous_response_id`` contract in this integration, so Anthropic relies on
        local history replay plus prompt caching — which is, for now, exactly what OpenAI does too.
        """
        if mode is Mode.byok or provider != "openai":
            return
        if result.stop_reason == STOP_REASON_MAX_TOKENS:
            await self._deps.repo.clear_provider_state(session_id)
            return
        if not result.provider_response_id:
            return
        await self._deps.repo.set_provider_state(
            session_id,
            {
                "provider": "openai",
                "responseId": result.provider_response_id,
                "model": result.usage.model or model,
            },
        )

    async def _external_project_id(self, session_id: uuid.UUID) -> str:
        """external_project_id for site.* tools — from chat_sessions.project_id (session context).

        Never from model-supplied tool args (IDOR guard, website-builder/05-security.md).
        ADR-022 defensive-guard: called ONLY for sessions with a project (`project_id IS NOT NULL`);
        a NULL here is an upstream anomaly (site.* should not have been offered/executed).
        """
        sess = await self._session.get(ChatSession, session_id)
        if sess is None:  # pragma: no cover - session was just created/validated upstream
            raise NotFoundError("session not found")
        if sess.project_id is None:  # pragma: no cover - guarded by has_project before this call
            raise UpstreamError("site.* resolution attempted for a project-less session")
        return sess.project_id

    async def _finalize_assistant(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        billing: _BillingPlan,
        result: LLMResult,
        usage: dict[str, Any],
        server_tools: list[ServerToolExecutionOut],
        media_jobs: list[dict[str, Any]] | None = None,
    ) -> ChatRunOut:
        # Final assistant_message. The assistant-step + billing (debit or trial flip) + audit are
        # committed together as one short transaction (atomicity per MAJOR-4 / CRITICAL-1).
        # ADR-023: capture the persisted assistant step's id → ChatResponse.stepId. It is the same
        # ChatStep.id that GET /v1/chats/{id} renders as ChatStepSchema.id for this step (sync
        # invariant).
        # ADR-068/070: persist mediaJobs on the assistant payload so GET /v1/chats/{id} can show
        # generation anchors without scanning tool steps (iOS cold start).
        if billing.debit_credits and billing.expose_credit_amount:
            usage = {**usage, "creditsCharged": billing.credit_amount}
        assistant_payload: dict[str, Any] = {"content": result.content_blocks}
        if media_jobs:
            assistant_payload["mediaJobs"] = list(media_jobs)
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload=assistant_payload,
            usage=usage,
        )
        sess = await self._session.get(ChatSession, session_id)
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "role": "assistant",
                    "model": usage.get("model"),
                    "usage": usage,
                },
            )
        )

        # CO-7 / ADR-002 / ADR-005: bill exactly once on the final assistant_message.
        # - active subscription + credits → consume the generation-mode cost;
        # - trial (subscription=none, trial_used=false) → free, flip users.trial_used;
        # - byok / already-trial-used → free, no write.
        credits_spent = 0
        if billing.debit_credits:
            try:
                credits_spent = await self._debit(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    usage=usage,
                    generation_mode=str(usage.get("generationMode") or "general"),
                    amount=billing.credit_amount,
                )
            except InsufficientCreditsError:
                # Balance dropped below the mode cost after policy allow → business block.
                # Roll back the assistant-step+audit so the unbillable step is not persisted.
                await self._session.rollback()
                return self._blocked(session_id, BlockReason.credits_empty)
        elif billing.mark_trial:
            # CRITICAL-1: consume the single lifetime trial atomically (idempotent).
            await self._deps.repo.mark_trial_used(user_id)

        if sess is not None:
            await self._deps.repo.touch_session(sess)

        await self._session.commit()
        schedule_index_turn(session_id, message_step_id)
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=result.text,
            usage=usage,
            message_step_id=message_step_id,
            step_id=assistant_step.id,
            # ADR-028: server-side tools executed in this /chat/run before the final assistant turn.
            server_tools=list(server_tools),
            media_jobs=list(media_jobs) if media_jobs else None,
            credits_spent=credits_spent,
        )

    async def _handle_max_tokens(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        result: LLMResult,
        usage: dict[str, Any],
        server_tools: list[ServerToolExecutionOut],
    ) -> ChatRunOut:
        """Handle a max_tokens-truncated turn (ADR-025 A2): blocked(max_tokens), NO debit.

        The turn was truncated by the output-token limit (stop_reason="max_tokens"). Its tool_use
        blocks (if any) are INCOMPLETE and must NOT be surfaced — toolCall(s) are omitted. The
        truncated assistant step IS persisted (history/diagnostics), but its incomplete tool_use
        blocks are excluded from continuation replay (re-entry by this turn is not supported). The
        response is status=blocked, blockReason=max_tokens with usage + message_step_id + step_id
        (unlike policy-blocked where they are null), assistantMessage = partial text if any. No
        credit is debited, no trial flip — the user does not pay for a truncated generation.
        """
        # Persist the truncated assistant step (for history/diagnostics). Its content is replayed
        # via _build_messages only as the assistant turn; since no tool_result will ever be sent
        # for its incomplete tool_use blocks, re-entry by this turn is not initiated (no pending
        # client tool_calls are created here — we do NOT call _handle_tool_use).
        truncated_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_CHAT_STEP,
                payload={
                    "sessionId": str(session_id),
                    "role": "assistant",
                    "blockReason": BlockReason.max_tokens.value,
                    "model": usage.get("model"),
                    "usage": usage,
                },
            )
        )
        await self._session.commit()
        blocked_requests_total.labels(reason=BlockReason.max_tokens.value).inc()
        return ChatRunOut(
            status="blocked",
            session_id=session_id,
            # Partial text of the truncated turn (if Claude produced any) — clients may show
            # "ответ оборван". None when there was no text block.
            assistant_message=result.text or None,
            block_reason=BlockReason.max_tokens.value,
            usage=usage,
            message_step_id=message_step_id,
            step_id=truncated_step.id,
            # ADR-028: server-side rounds may have run before the final turn hit max_tokens →
            # surface them (this blocked row may be NON-empty, unlike policy-block).
            server_tools=list(server_tools),
        )

    async def _handle_tool_use(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        result: LLMResult,
        usage: dict[str, Any],
        has_project: bool,
        server_tools: list[ServerToolExecutionOut],
        generation_mode: str = "general",
        quiz_accumulator: _QuizAccumulator | None = None,
        media_accumulator: _MediaJobsAccumulator | None = None,
        media_choices_accumulator: _MediaChoicesAccumulator | None = None,
        turn_images: list[ImageAttachmentRef] | None = None,
        recent_image_urls: list[str] | None = None,
        last_image_job_id: str | None = None,
    ) -> _TurnOutcome:
        """Process a tool_use turn (ADR-008/011): persist tool_calls, branch server/client-side.

        For every tool_use block a tool_call row is persisted with its own domain id (uuid4) and
        raw provider_tool_use_id (toolu_..., never derived from the anthropic id — BUG-4). Then:
        - server-side (site.*): executed on the backend NOW; tool_call goes straight to status
          completed with the backend result; a tool step records the tool_result (replayed to
          Anthropic on continuation, ADR-011 §4). No round-trip to iOS.
        - client-side (files.*/...): left pending; ALL of them are returned as status=tool_call to
          iOS in toolCalls[] (ADR-025 parallel tool use); tool_call (singular, deprecated) =
          toolCalls[0]. The Anthropic tool-loop requires a tool_result for EVERY tool_use of the
          turn — surfacing only the first would orphan the rest → Anthropic 400 → 502.
        If the turn contains any client-side tool, client_out is set (hand off to iOS). If the turn
        is purely server-side, client_out is None and the orchestrator continues the loop.

        Two SOFT refusals apply to mode-gated tools (ADR-064 §5/§6), both keeping the turn alive,
        and their ORDER IS NORMATIVE — the mode check runs FIRST, before args validation:
        - the tool was NOT offered in this mode (upstream anomaly) → tool-result
          `tool_not_available`, nothing is executed;
        - its args fail validation AND it is in ARGS_DEGRADE_TOOLS → tool-result `invalid_quiz`,
          the model fixes the pool within the same turn.
        On the intersection `tool_not_available` wins: validating the args of a tool that was never
        offered is pointless, and an `invalid_quiz` message would send the model off to repair a
        pool for a tool it still cannot use, burning server-side rounds up to
        MAX_SERVER_TOOL_ROUNDS. Both are deliberately UNLIKE the neighbouring guards: a `site.*`
        call without a project stays a HARD failure (UpstreamError) because executing it would
        resolve someone's project — a data isolation boundary — while quiz.generate has no side
        effects at all; and for every tool outside ARGS_DEGRADE_TOOLS bad args remain a 422 on the
        whole turn.
        """
        # Persist the assistant tool_use step (no debit on tool-rounds). ADR-023: this is the
        # step-of-record for a status=tool_call response — ChatResponse.stepId = its ChatStep.id
        # (the history step whose payload carries the tool_use block). NOT toolCall.id.
        assistant_step = await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="assistant",
            payload={"content": result.content_blocks},
            usage=usage,
        )

        # ADR-022 §2/§4 defensive-guard: _external_project_id() (which resolves the project for
        # site.* execution) is resolved ONLY when the session has a project. Without a project,
        # site.* were not offered to Claude, so this path is unreachable in normal operation; if
        # Claude returns a site.* tool_use anyway (upstream anomaly), we must NOT execute it and
        # must NOT resolve a project — see the per-block guard below.
        external_project_id = await self._external_project_id(session_id) if has_project else None
        # ADR-025: collect ALL client-side tool calls of this turn (in block order) → toolCalls[].
        client_outs: list[ToolCallOut] = []
        for block in result.tool_uses:
            tool_name = str(block["name"])
            provider_tool_use_id = str(block["id"])  # raw anthropic "toolu_...", opaque

            # ADR-022 defensive-guard: a server-side site.* tool_use with no project must never be
            # executed (the tool was not offered; this is an upstream anomaly, treated like an
            # unknown tool name — ADR-008). Fail before validating args / resolving any project.
            if tool_name in SERVER_SIDE_TOOLS and not has_project:
                raise UpstreamError("server-side site.* tool requested for a project-less session")

            raw_args = dict(block["input"])

            # ADR-064 §6 defensive-guard, FIRST — before args validation (normative order, see the
            # docstring). A mode-gated tool called outside its modes is not executed at all.
            if tool_name in TOOL_GENERATION_MODES and not offered_in_generation_mode(
                tool_name, generation_mode
            ):
                await self._record_refused_tool_call(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_name=tool_name,
                    raw_args=raw_args,
                    provider_tool_use_id=provider_tool_use_id,
                    execution=ToolExecution.error(
                        "tool_not_available",
                        f"tool {tool_name} is not available in this generation mode",
                    ),
                    server_tools=server_tools,
                )
                continue

            # ADR-081: whole family disabled on this instance (other instances keep the tools).
            if not offered_tool_family(
                tool_name,
                disabled_families=get_settings().disabled_tool_families(),
            ):
                await self._record_refused_tool_call(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_name=tool_name,
                    raw_args=raw_args,
                    provider_tool_use_id=provider_tool_use_id,
                    execution=ToolExecution.error(
                        "tool_not_available",
                        f"tool {tool_name} is not available on this instance",
                    ),
                    server_tools=server_tools,
                )
                continue

            # ADR-072: media chat tools disabled on this instance (REST /v1/media/* may still work).
            if tool_name in MEDIA_CHAT_TOOLS and not offered_media_chat_tool(
                tool_name,
                include_media_chat_tools=get_settings().chat_media_tools_enabled,
            ):
                await self._record_refused_tool_call(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_name=tool_name,
                    raw_args=raw_args,
                    provider_tool_use_id=provider_tool_use_id,
                    execution=ToolExecution.error(
                        "tool_not_available",
                        f"tool {tool_name} is not available on this instance",
                    ),
                    server_tools=server_tools,
                )
                continue

            try:
                validated_args = validate_tool_args(tool_name, raw_args)
            except ValueError as exc:
                if tool_name not in ARGS_DEGRADE_TOOLS:
                    # Unchanged behaviour for every other tool: their args come from fixed schemas,
                    # so malformed args ARE an anomaly → 422 on the whole turn. This branch and the
                    # degrade branch below sit side by side and behave OPPOSITELY on purpose; do not
                    # transfer one to the other in either direction (ADR-064 §5).
                    raise ValidationFailedError(str(exc)) from exc
                # ADR-064 §5: no provider in this integration guarantees the quiz constraints
                # (types, counts, lengths, correctIndex < len(options)), so a violation is an
                # EXPECTED outcome, not an anomaly — failing the turn would throw away the
                # explanation the model already generated. Degrade to a tool-result error and let
                # the loop continue: the model sees it in the SAME turn and regenerates the pool.
                # The message is content-free (field path + error kind) — never str(exc), which
                # pydantic renders with the offending quiz text.
                if tool_name == TOOL_QUIZ_GENERATE:
                    degrade_code = QUIZ_INVALID_ERROR_CODE
                    degrade_msg = f"{content_free_args_error(exc)}; {QUIZ_CONSTRAINTS_HINT}"
                elif tool_name == TOOL_FILES_PATCH:
                    # ADR-094: подсказка о формате — часть отказа, иначе модель видит «неверные
                    # аргументы» и не знает, чем они неверны. Сообщение content-free: str(exc)
                    # у pydantic цитирует САМ diff, то есть код с машины пользователя.
                    degrade_code = PATCH_INVALID_ERROR_CODE
                    degrade_msg = f"{content_free_args_error(exc)}; {PATCH_FORMAT_HINT}"
                elif tool_name in _DOCUMENT_TOOL_NAMES:
                    # Свой код, а не media-шный: модель по нему понимает, ЧТО переспросить.
                    degrade_code = DOCUMENT_INVALID_ERROR_CODE
                    degrade_msg = content_free_args_error(exc)
                else:
                    degrade_code = MEDIA_INVALID_ERROR_CODE
                    degrade_msg = content_free_args_error(exc)
                await self._record_refused_tool_call(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_name=tool_name,
                    raw_args=raw_args,
                    provider_tool_use_id=provider_tool_use_id,
                    execution=ToolExecution.error(degrade_code, degrade_msg),
                    server_tools=server_tools,
                )
                continue

            tool_call_id = uuid.uuid4()  # domain id: fresh UUID, independent of anthropic id
            await self._deps.repo.create_tool_call(
                session_id=session_id,
                message_step_id=message_step_id,
                tool_name=tool_name,
                args=validated_args,
                tool_call_id=tool_call_id,
                provider_tool_use_id=provider_tool_use_id,
            )
            await self._deps.audit.record(
                AuditEvent(
                    user_id=user_id,
                    session_id=session_id,
                    event_type=EVENT_TOOL_CALL_INITIATED,
                    payload={"toolCallId": str(tool_call_id), "toolName": tool_name},
                )
            )

            if tool_name in GLOBAL_SERVER_SIDE_TOOLS:
                # ADR-026 §4: global server-side (time.now, quiz.generate) is routed BEFORE the
                # project-scoped branch — executed immediately WITHOUT external_project_id and
                # WITHOUT the has_project guard. «Нет проекта» is the normal mode here, not an
                # anomaly.
                execution = await self._execute_global_server_side_tool(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=validated_args,
                    provider_tool_use_id=provider_tool_use_id,
                    server_tools=server_tools,
                    turn_images=turn_images,
                    recent_image_urls=recent_image_urls,
                    last_image_job_id=last_image_job_id,
                )
                if (
                    tool_name == TOOL_QUIZ_GENERATE
                    and quiz_accumulator is not None
                    and not execution.is_error
                    and isinstance(execution.result, dict)
                ):
                    # ADR-064 §7 producer 1: last-wins. A later valid pool in the same call
                    # REPLACES an earlier one — pools are never merged.
                    quiz_accumulator.pool = execution.result
                if (
                    tool_name in _MEDIA_TOOL_NAMES
                    and media_accumulator is not None
                    and not execution.is_error
                    and isinstance(execution.result, dict)
                ):
                    # ADR-068: append — several media jobs in one turn are all surfaced.
                    media_accumulator.jobs.append(execution.result)
                if (
                    tool_name == TOOL_MEDIA_ASK_PARAMS
                    and media_choices_accumulator is not None
                    and not execution.is_error
                    and isinstance(execution.result, dict)
                ):
                    # ADR-070: last-wins wizard state for ChatResponse.mediaChoices.
                    media_choices_accumulator.state = execution.result
            elif tool_name in SERVER_SIDE_TOOLS:
                # Invariant (ADR-022): reaching here implies has_project is True (the project-less
                # site.* anomaly raised above), so external_project_id is a resolved string. The
                # assert applies ONLY to project-scoped site.* (ADR-026 §4).
                assert external_project_id is not None  # noqa: S101 - ADR-022 guard invariant
                await self._execute_server_side_tool(
                    user_id=user_id,
                    session_id=session_id,
                    message_step_id=message_step_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=validated_args,
                    provider_tool_use_id=provider_tool_use_id,
                    external_project_id=external_project_id,
                    server_tools=server_tools,
                )
            else:
                # Client-side: leave pending; surface in toolCalls[] (ADR-025).
                client_outs.append(
                    ToolCallOut(id=str(tool_call_id), name=tool_name, args=validated_args)
                )

        if client_outs:
            return _TurnOutcome(
                client_out=ChatRunOut(
                    status="tool_call",
                    session_id=session_id,
                    # ADR-024 §3 / Q-024-1 (variant A): carry the accompanying text of THIS same
                    # assistant step (the one whose tool_use blocks are returned). result.text is
                    # the concatenation of this turn's text blocks; empty → None (no text).
                    assistant_message=result.text or None,
                    # ADR-025: ALL client-side calls; tool_call (deprecated) = toolCalls[0].
                    tool_calls=client_outs,
                    tool_call=client_outs[0],
                    usage=usage,
                    message_step_id=message_step_id,
                    step_id=assistant_step.id,
                    # ADR-028: any server-side tools executed in this turn BEFORE the client-side
                    # hand-off are surfaced (snapshot — copy, not the live accumulator).
                    server_tools=list(server_tools),
                )
            )
        # Purely server-side turn → continue the loop (no hand-off to iOS).
        return _TurnOutcome(client_out=None)

    async def _execute_server_side_tool(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        provider_tool_use_id: str,
        external_project_id: str,
        server_tools: list[ServerToolExecutionOut],
    ) -> None:
        """Execute a site.* tool on the backend and persist its tool_result (ADR-011 §1, §4).

        The tool_call is moved to status=completed immediately (no client tool_result is awaited).
        The tool step stores the providerToolUseId so _build_messages replays the continuation with
        a consistent id pair (ADR-008). MUTATING audit (site.write_file/site.delete → tool_mutation)
        is recorded inside the handler, in this same transaction (audit/03-architecture).
        ADR-028: append a COMPACT (status + summary, NO raw result/path/URL/token) entry to
        server_tools for the /chat/run response.
        """
        execution = await self._deps.site_tools.execute(
            tool_name=tool_name,
            args=args,
            user_id=user_id,
            external_project_id=external_project_id,
            session_id=session_id,
        )
        payload = execution.to_tool_result_payload()
        status = "errored" if execution.is_error else "completed"
        # ADR-028 Решение 2: record the server-side execution (domain name, status, summary).
        # _server_tool_summary deliberately ignores the raw payload — only "ok" / short error code.
        server_tools.append(
            ServerToolExecutionOut(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                summary=_server_tool_summary(execution),
            )
        )
        await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            result=payload,
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                "providerToolUseId": provider_tool_use_id,
                "toolName": tool_name,
                "result": payload.get("result"),
                "error": payload.get("error"),
            },
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call_id),
                    "toolName": tool_name,
                    "status": status,
                },
            )
        )

    async def _execute_global_server_side_tool(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        tool_name: str,
        args: dict[str, Any],
        provider_tool_use_id: str,
        server_tools: list[ServerToolExecutionOut],
        turn_images: list[ImageAttachmentRef] | None = None,
        recent_image_urls: list[str] | None = None,
        last_image_job_id: str | None = None,
    ) -> ToolExecution:
        """Execute a global server-side tool (time.now, quiz.generate) on the backend (ADR-026 §4).

        Mirrors _execute_server_side_tool but is PROJECT-INDEPENDENT: no external_project_id is
        resolved or passed (these tools are global). The tool_call is moved to status
        completed/errored immediately (no client tool_result is awaited); the tool step stores
        providerToolUseId so _build_messages replays the continuation with a consistent id pair
        (ADR-008). Neither tool is in MUTATING_TOOLS → no tool_mutation audit; only the standard
        tool_call_completed audit is recorded. Billing is unchanged (a server-side round adds no
        debit, ADR-006). ADR-028: a COMPACT (status + summary, NO raw result) entry is appended to
        server_tools — for quiz.generate that means "ok" / a short error code, never quiz content.

        Returns the ToolExecution so the caller can lift a successful quiz pool into the turn's
        accumulator (ADR-064 §7) without re-reading the persisted step.
        """
        execution = await self._deps.global_tools.execute(
            tool_name=tool_name,
            args=args,
            user_id=user_id,
            # ADR-090: document.* адресуют документы в пределах ТЕКУЩЕЙ сессии — без session_id
            # инструмент не смог бы отличить свой документ от чужого.
            session_id=session_id,
            turn_images=turn_images,
            recent_image_urls=recent_image_urls,
            last_image_job_id=last_image_job_id,
        )
        await self._persist_tool_execution(
            user_id=user_id,
            session_id=session_id,
            message_step_id=message_step_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            provider_tool_use_id=provider_tool_use_id,
            execution=execution,
            server_tools=server_tools,
        )
        return execution

    async def _record_refused_tool_call(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_name: str,
        raw_args: dict[str, Any],
        provider_tool_use_id: str,
        execution: ToolExecution,
        server_tools: list[ServerToolExecutionOut],
    ) -> None:
        """Refuse a tool_use SOFTLY: persist it as an errored tool-call, keep the turn alive.

        Used by the two mode-gated soft refusals of ADR-064 (`tool_not_available` §6 and
        `invalid_quiz` §5). The tool is NOT executed; the model receives the machine-readable error
        as an ordinary tool-result on the next round of the same turn and can correct itself.

        The tool_call row stores the model's RAW input (it never passed validation, so there is no
        validated form) — it is the record of what was attempted. Everything downstream is identical
        to a normally-executed server-side tool, so history replay, the turn barrier (server-side
        calls are excluded from it) and serverTools[] all behave as usual.
        """
        tool_call_id = uuid.uuid4()  # domain id: fresh UUID, independent of the provider id
        await self._deps.repo.create_tool_call(
            session_id=session_id,
            message_step_id=message_step_id,
            tool_name=tool_name,
            args=raw_args,
            tool_call_id=tool_call_id,
            provider_tool_use_id=provider_tool_use_id,
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_CALL_INITIATED,
                payload={"toolCallId": str(tool_call_id), "toolName": tool_name},
            )
        )
        await self._persist_tool_execution(
            user_id=user_id,
            session_id=session_id,
            message_step_id=message_step_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            provider_tool_use_id=provider_tool_use_id,
            execution=execution,
            server_tools=server_tools,
        )

    async def _persist_tool_execution(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        tool_call_id: uuid.UUID,
        tool_name: str,
        provider_tool_use_id: str,
        execution: ToolExecution,
        server_tools: list[ServerToolExecutionOut],
    ) -> None:
        """Persist the outcome of a backend-side tool: tool_call → tool step → audit → serverTools.

        Shared by the global-tool execution path and the soft-refusal path so both produce exactly
        the same shape of record (one place to keep the ADR-008 id pairing and the ADR-028 compact
        summary correct).
        """
        payload = execution.to_tool_result_payload()
        status = "errored" if execution.is_error else "completed"
        # ADR-065 §3: per-tool outcome counter for quiz.generate. THIS is the single point every
        # outcome of the tool passes through — normal execution AND both soft refusals
        # (tool_not_available, invalid_quiz) — so the counter cannot miss a path. Labels are a
        # bounded enum (the tool's own error codes); no quiz content ever reaches a label.
        if tool_name == TOOL_QUIZ_GENERATE:
            quiz_generate_total.labels(
                result=(execution.error_code or "errored") if execution.is_error else "ok"
            ).inc()
        # ADR-028 Решение 2: compact indicator only — _server_tool_summary deliberately ignores the
        # raw payload ("ok" / a short error code), so no quiz text or path can reach the response.
        server_tools.append(
            ServerToolExecutionOut(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                summary=_server_tool_summary(execution),
            )
        )
        await self._deps.repo.complete_tool_call(
            tool_call_id=tool_call_id,
            status=status,
            result=payload,
        )
        await self._deps.repo.add_step(
            session_id=session_id,
            message_step_id=message_step_id,
            role="tool",
            payload={
                "toolCallId": str(tool_call_id),
                "providerToolUseId": provider_tool_use_id,
                "toolName": tool_name,
                "result": payload.get("result"),
                "error": payload.get("error"),
            },
        )
        await self._deps.audit.record(
            AuditEvent(
                user_id=user_id,
                session_id=session_id,
                event_type=EVENT_TOOL_CALL_COMPLETED,
                payload={
                    "toolCallId": str(tool_call_id),
                    "toolName": tool_name,
                    "status": status,
                },
            )
        )

    async def _debit(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        usage: dict[str, Any],
        generation_mode: str,
        amount: int,
    ) -> int:
        # amount is generation-mode dependent, still idempotent by messageStepId.
        # InsufficientCreditsError propagates to the caller, which maps it to a credits_empty block.
        result = await self._deps.wallet.consume(
            user_id=user_id,
            amount=amount,
            idempotency_key=str(message_step_id),
            meta={
                "usage": usage,
                "model": usage.get("model"),
                "generationMode": generation_mode,
                "creditsCharged": amount,
            },
            session_id=session_id,
        )
        return 0 if result.idempotent_replay else amount

    def _render_saved_step(
        self,
        session_id: uuid.UUID,
        message_step_id: uuid.UUID,
        step: ChatStep | None,
    ) -> ChatRunOut:
        if step is None:
            # Nothing generated yet for this step (e.g. concurrent in-flight) → treat as not found.
            raise NotFoundError("no completed step for tool result")
        text = ""
        for block in step.payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        # ADR-023: idempotent replay returns the same sync ids as the original response — the turn
        # (message_step_id, stable across re-entry) and the saved step's own id.
        return ChatRunOut(
            status="assistant_message",
            session_id=session_id,
            assistant_message=text,
            usage=step.usage,
            message_step_id=message_step_id,
            step_id=step.id,
        )


def decision_allow(decision: Decision) -> bool:
    return decision.allow
