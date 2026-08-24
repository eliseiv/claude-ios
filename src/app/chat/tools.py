"""Tool schemas (CO-1): client-side iOS tools + server-side site.* tools, Pydantic v2.

Two classes (ADR-011):
- client-side (files.*/calendar.*/reminders.*): backend only INITIATES the tool-call; the iOS
  client executes it and posts a tool_result.
- server-side (site.*): backend EXECUTES the handler itself, in the same tool-loop, without a
  round-trip to iOS (SERVER_SIDE_TOOLS).

Mutating tools (files.write, files.mkdir, calendar.create_events, reminders.create,
site.write_file, site.delete) require an audit record. Args/result are strictly validated
(extra='forbid'); `path` rejects `..`-traversal.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Tool names (fixed list — validated at the API boundary).
TOOL_FILES_READ = "files.read"
TOOL_FILES_WRITE = "files.write"
TOOL_FILES_LIST = "files.list"
TOOL_FILES_MKDIR = "files.mkdir"
TOOL_CALENDAR_READ = "calendar.read"
TOOL_CALENDAR_CREATE = "calendar.create_events"
TOOL_REMINDERS_READ = "reminders.read"
TOOL_REMINDERS_CREATE = "reminders.create"

# Server-side tools (site.*, ADR-011): executed by the backend, not the iOS client.
TOOL_SITE_WRITE_FILE = "site.write_file"
TOOL_SITE_PREVIEW = "site.preview"
TOOL_SITE_LIST = "site.list"
TOOL_SITE_READ = "site.read"
TOOL_SITE_DELETE = "site.delete"

# Global server-side tool (time.now, ADR-026): executed by the backend, like site.*, but
# project-INDEPENDENT — offered to Claude ALWAYS (including «чистый чат» with no project) and
# routed before the project-scoped branch (no external_project_id, no has_project guard).
TOOL_TIME_NOW = "time.now"

# Global server-side document tools (ADR-090): текстовые документы чата, которые модель создаёт,
# читает и правит, а клиент скачивает. Project-independent, как time.now. НЕ путать с files.* —
# те исполняет УСТРОЙСТВО пользователя, эти живут на бэкенде и переживают ход.
TOOL_DOCUMENT_CREATE = "document.create"
TOOL_DOCUMENT_LIST = "document.list"
TOOL_DOCUMENT_READ = "document.read"
TOOL_DOCUMENT_UPDATE = "document.update"

# Global server-side tool, MODE-GATED (quiz.generate, ADR-064): executed by the backend like
# time.now and equally project-independent, but — unlike time.now — offered to the model ONLY when
# the effective generation mode of the turn is `study_learn` (axis C, TOOL_GENERATION_MODES below).
# «Global» means «needs no project», NOT «offered always»: the rule of one tool in this registry
# must not be carried over to its neighbour.
TOOL_QUIZ_GENERATE = "quiz.generate"

# Global server-side media tools (ADR-068): submit an image/video job via MediaGenerationService
# (same path as POST /v1/media/images|videos). Project-independent and NOT mode-gated — offered in
# every generation mode, including «чистый чат». Tool = submit only (returns jobId/queued); the
# model MUST NOT wait for fal completion inside the tool-loop (minutes; ADR-060).
TOOL_MEDIA_GENERATE_IMAGE = "media.generate_image"
TOOL_MEDIA_GENERATE_VIDEO = "media.generate_video"
# ADR-070: present catalog-backed quiz-like choices (model/resolution/…) before submit.
TOOL_MEDIA_ASK_PARAMS = "media.ask_params"

# Project-scoped server-side tools (site.*, ADR-011/022): executed by the backend in the
# tool-loop; offered to Claude ONLY when the session has a project (project_id IS NOT NULL).
SERVER_SIDE_TOOLS = frozenset(
    {
        TOOL_SITE_WRITE_FILE,
        TOOL_SITE_PREVIEW,
        TOOL_SITE_LIST,
        TOOL_SITE_READ,
        TOOL_SITE_DELETE,
    }
)

# Global (project-independent) server-side tools (ADR-026 §2). DISJOINT from SERVER_SIDE_TOOLS:
# the two registries are mutually exclusive (invariant GLOBAL_SERVER_SIDE_TOOLS ∩ SERVER_SIDE_TOOLS
# = ∅). Combined server-side = SERVER_SIDE_TOOLS ∪ GLOBAL_SERVER_SIDE_TOOLS; everything else in
# ALL_TOOL_NAMES is client-side.
GLOBAL_SERVER_SIDE_TOOLS = frozenset(
    {
        TOOL_TIME_NOW,
        TOOL_QUIZ_GENERATE,
        TOOL_MEDIA_GENERATE_IMAGE,
        TOOL_MEDIA_GENERATE_VIDEO,
        TOOL_MEDIA_ASK_PARAMS,
        TOOL_DOCUMENT_CREATE,
        TOOL_DOCUMENT_LIST,
        TOOL_DOCUMENT_READ,
        TOOL_DOCUMENT_UPDATE,
    }
)

# Families that CHAT_DISABLED_TOOL_FAMILIES may hide per instance (ADR-081). Empty env = none
# hidden — every other instance keeps the full set. `media` stays on CHAT_MEDIA_TOOLS_ENABLED.
DISABLEABLE_TOOL_FAMILIES: frozenset[str] = frozenset({"files", "calendar", "reminders", "site"})

# Chat media tools (ADR-068 / ADR-070). Gated per-instance by CHAT_MEDIA_TOOLS_ENABLED (ADR-072);
# orthogonal to FAL_API_KEY (which gates /v1/media/*).
MEDIA_CHAT_TOOLS = frozenset(
    {
        TOOL_MEDIA_GENERATE_IMAGE,
        TOOL_MEDIA_GENERATE_VIDEO,
        TOOL_MEDIA_ASK_PARAMS,
    }
)

# Axis C — generation-mode gate (ADR-064 §3). A tool listed here is offered to the model IF AND
# ONLY IF the EFFECTIVE generation mode of the turn is in its set. A tool ABSENT from this registry
# is not mode-gated at all (all 14 others behave exactly as before). The gate is evaluated against
# the same single value that goes to the provider and to billing
# (`_effective_generation_mode`: v2 = request/restored mode; legacy = `general`, or `research`
# when CHAT_LEGACY_WEB_SEARCH_ENABLED — ADR-082), never against the request field. quiz.generate
# is study_learn-only, so the legacy path still never offers a mode-gated tool. Axes A (project)
# / B (assistant_mode) / C compose with logical AND.
TOOL_GENERATION_MODES: dict[str, frozenset[str]] = {
    TOOL_QUIZ_GENERATE: frozenset({"study_learn"}),
}

# Tools whose ARGUMENT-VALIDATION FAILURE degrades instead of failing the turn (ADR-064 §5).
# For a tool in this registry a failed `validate_tool_args` becomes a tool-result error and the
# tool-loop CONTINUES (the model fixes itself within the same turn); for every OTHER tool the
# behaviour is unchanged — ValidationFailedError → 422 on the whole turn.
# The two neighbouring branches of the same `except` behave OPPOSITELY ON PURPOSE: quiz constraints
# (cross-field `correctIndex < len(options)`, counts, lengths) are guaranteed by NO provider in this
# integration — strict tool-args mode is off for both — so a violation is an EXPECTED scenario, not
# an anomaly. Other tools' args come from fixed schemas, where a malformed args IS an anomaly.
# Do not transfer the behaviour of either branch to the other.
# Media args can be wrong enums / mutually exclusive refs from the model; degrade like quiz so the
# turn survives and the model can ask clarifying questions instead of 422-ing the whole chat turn.
ARGS_DEGRADE_TOOLS = frozenset(
    {
        TOOL_QUIZ_GENERATE,
        TOOL_MEDIA_GENERATE_IMAGE,
        TOOL_MEDIA_GENERATE_VIDEO,
        TOOL_MEDIA_ASK_PARAMS,
        # ADR-090: аргументы document.* тоже порождает модель, и ни один провайдер их не
        # гарантирует. Прод 2026-08-24 дал два падения подряд на одном и том же вызове:
        # пропущенный `mediaType`, затем ключ `mediatype` в нижнем регистре — оба роняли ВЕСЬ ход
        # в 422, и пользователь не получал ответа. Причина попадания сюда та же, что у quiz/media:
        # кривые аргументы — ОЖИДАЕМЫЙ исход, а не аномалия схемы.
        TOOL_DOCUMENT_CREATE,
        TOOL_DOCUMENT_LIST,
        TOOL_DOCUMENT_READ,
        TOOL_DOCUMENT_UPDATE,
    }
)

ALL_TOOL_NAMES = frozenset(
    {
        TOOL_FILES_READ,
        TOOL_FILES_WRITE,
        TOOL_FILES_LIST,
        TOOL_FILES_MKDIR,
        TOOL_CALENDAR_READ,
        TOOL_CALENDAR_CREATE,
        TOOL_REMINDERS_READ,
        TOOL_REMINDERS_CREATE,
        *SERVER_SIDE_TOOLS,
        *GLOBAL_SERVER_SIDE_TOOLS,
    }
)

# BUG-3: Anthropic Messages API requires tool.name to match ^[a-zA-Z0-9_-]{1,128}$ — a dot is
# rejected with 400 (→ backend 502). The public iOS contract (TZ §5) uses dotted domain names and
# must NOT change. We therefore keep a static, bidirectional name map (13 fixed pairs, incl.
# server-side site.*) that is the single source of truth for name correspondence. It is applied
# ONLY at the Anthropic transport
# boundary: forward (domain→anthropic) when building tools[].name for messages.create, reverse
# (anthropic→domain) when parsing a tool_use block from Claude. Everywhere else — DB
# (tool_calls.tool_name), audit, API responses (toolCall.name), arg/result typing — stays domain.
_DOMAIN_TO_ANTHROPIC: dict[str, str] = {
    TOOL_FILES_READ: "files_read",
    TOOL_FILES_WRITE: "files_write",
    TOOL_FILES_LIST: "files_list",
    TOOL_FILES_MKDIR: "files_mkdir",
    TOOL_CALENDAR_READ: "calendar_read",
    TOOL_CALENDAR_CREATE: "calendar_create_events",
    TOOL_REMINDERS_READ: "reminders_read",
    TOOL_REMINDERS_CREATE: "reminders_create",
    # Server-side site.* (ADR-011 §3): same dot→underscore mapping as client-side tools.
    TOOL_SITE_WRITE_FILE: "site_write_file",
    TOOL_SITE_PREVIEW: "site_preview",
    TOOL_SITE_LIST: "site_list",
    TOOL_SITE_READ: "site_read",
    TOOL_SITE_DELETE: "site_delete",
    # Global server-side time.now (ADR-026 §2): same dot→underscore mapping.
    TOOL_TIME_NOW: "time_now",
    # Global server-side, mode-gated quiz.generate (ADR-064 §2): same dot→underscore mapping.
    TOOL_QUIZ_GENERATE: "quiz_generate",
    # Global server-side media tools (ADR-068 / ADR-070): same dot→underscore mapping.
    TOOL_MEDIA_GENERATE_IMAGE: "media_generate_image",
    TOOL_MEDIA_GENERATE_VIDEO: "media_generate_video",
    TOOL_MEDIA_ASK_PARAMS: "media_ask_params",
    # ADR-090: тот же dot→underscore маппинг.
    TOOL_DOCUMENT_CREATE: "document_create",
    TOOL_DOCUMENT_LIST: "document_list",
    TOOL_DOCUMENT_READ: "document_read",
    TOOL_DOCUMENT_UPDATE: "document_update",
}
_ANTHROPIC_TO_DOMAIN: dict[str, str] = {a: d for d, a in _DOMAIN_TO_ANTHROPIC.items()}


class UnknownToolNameError(Exception):
    """Claude returned a tool_use.name that is not in the static map (upstream anomaly).

    Treated as an upstream processing error, never forwarded to iOS as a valid tool name.
    """


def to_anthropic_tool_name(domain_name: str) -> str:
    """Forward map domain-name (dotted) → anthropic-name (underscore). Static table only."""
    anthropic_name = _DOMAIN_TO_ANTHROPIC.get(domain_name)
    if anthropic_name is None:
        raise UnknownToolNameError(f"unknown domain tool name: {domain_name}")
    return anthropic_name


def to_domain_tool_name(anthropic_name: str) -> str:
    """Reverse map anthropic-name (underscore) → domain-name (dotted). Static table only.

    Raises UnknownToolNameError if Claude returns a name absent from the map (upstream anomaly).
    """
    domain_name = _ANTHROPIC_TO_DOMAIN.get(anthropic_name)
    if domain_name is None:
        raise UnknownToolNameError(f"unknown anthropic tool name: {anthropic_name}")
    return domain_name


# Mutating tools require audit (AC-7; ADR-011 §4 adds site.write_file / site.delete).
MUTATING_TOOLS = frozenset(
    {
        TOOL_FILES_WRITE,
        TOOL_FILES_MKDIR,
        TOOL_CALENDAR_CREATE,
        TOOL_REMINDERS_CREATE,
        TOOL_SITE_WRITE_FILE,
        TOOL_SITE_DELETE,
        # ADR-090: создание и замена содержимого меняют состояние на сервере — тот же признак,
        # что у site.write_file. document.list/read только читают.
        TOOL_DOCUMENT_CREATE,
        TOOL_DOCUMENT_UPDATE,
    }
)


def _validate_safe_path(value: str) -> str:
    parts = value.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("path must not contain '..' traversal")
    return value


SafePath = Annotated[str, Field(min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PathModel(_StrictModel):
    path: SafePath

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_safe_path(value)


# --- files ---
class FilesReadArgs(_PathModel):
    pass


class FilesWriteArgs(_PathModel):
    content: str
    encoding: Literal["utf8", "base64"]
    overwrite: bool


class FilesListArgs(_PathModel):
    recursive: bool


class FilesMkdirArgs(_PathModel):
    createIntermediates: bool


# --- calendar ---
class CalendarReadArgs(_StrictModel):
    start: str
    end: str
    calendarId: str | None = None


class CalendarEventInput(_StrictModel):
    title: str
    start: str
    end: str
    location: str | None = None
    notes: str | None = None
    calendarId: str | None = None


class CalendarCreateArgs(_StrictModel):
    events: list[CalendarEventInput]


# --- reminders ---
class RemindersReadArgs(_StrictModel):
    listId: str | None = None
    includeCompleted: bool


class ReminderInput(_StrictModel):
    title: str
    due: str | None = None
    notes: str | None = None
    listId: str | None = None


class RemindersCreateArgs(_StrictModel):
    reminders: list[ReminderInput]


# --- server-side site.* (ADR-011) ---
# IMPORTANT (IDOR guard, website-builder/05-security.md): args carry ONLY file data. The owning
# userId and external_project_id come from the session context on the backend, NEVER from these
# args — so the model cannot target another user's project.
class SiteWriteFileArgs(_PathModel):
    content: str
    contentType: str
    encoding: Literal["utf8", "base64"]


class SitePreviewArgs(_StrictModel):
    entry: str | None = None


class SiteListArgs(_StrictModel):
    pass


class SiteReadArgs(_PathModel):
    pass


class SiteDeleteArgs(_PathModel):
    pass


# --- global server-side time.now (ADR-026) ---
# Q-026-1: length cap for the optional tz arg (≤ 64 — longer than any valid IANA name). Enforced
# in the handler (GlobalToolHandlers) so an over-limit tz becomes a tool-result error
# `invalid_timezone` (the turn survives, ADR-026 §6) rather than a 422 of the turn. It is therefore
# NOT a pydantic max_length constraint here (that would 422 the turn instead).
TIME_NOW_TZ_MAX_LENGTH = 64


class DocumentCreateArgs(_StrictModel):
    """Args for document.create (ADR-090 §3).

    Все поля НЕОБЯЗАТЕЛЬНЫ, дефолты подставляет обработчик. Причина та же, что у `tz` в
    `time.now`: пропущенный или кривой аргумент инструмента обязан выродиться в tool-result
    ошибку, а не уронить ход с `422`. Модель, не приславшая `mediaType`, роняла весь ответ —
    воспроизведено на проде 2026-08-24.
    """

    filename: str | None = None
    mediaType: str | None = None
    content: str | None = None


class DocumentListArgs(_StrictModel):
    """Args for document.list — их нет; строгая модель запрещает лишние ключи."""


class DocumentReadArgs(_StrictModel):
    documentId: str | None = None


class DocumentUpdateArgs(_StrictModel):
    """Args for document.update: содержимое заменяется ЦЕЛИКОМ, патча нет (ADR-090 §3).

    Необязательность — по той же причине, что и в create: пропуск аргумента даёт tool-result
    ошибку (обработчик проверит `documentId`), а не `422` на весь ход.
    """

    documentId: str | None = None
    content: str | None = None


class TimeNowArgs(_StrictModel):
    """Args for time.now (ADR-026 §6): optional IANA timezone name (e.g. Europe/Moscow).

    `extra='forbid'` (any other key → args validation error, like other tools). `tz` length and
    IANA validity are checked in GlobalToolHandlers, not here — an invalid/over-long tz must degrade
    to a tool-result error `invalid_timezone`, not fail the turn with 422 (Q-026-1, ADR-026 §6).
    """

    tz: str | None = None


# --- global server-side, mode-gated quiz.generate (ADR-064 §4) ---
# Normative pool constraints. They live in the schema handed to the provider as a HINT
# (minItems/maxItems/maxLength stay in the JSON Schema — strict tool-args mode is off for both
# providers, so neither rejects them), but the AUTHORITATIVE check is this server-side model:
# no provider guarantees types or cross-field invariants here.
QUIZ_MIN_QUESTIONS = 3
QUIZ_MAX_QUESTIONS = 10
QUIZ_MIN_OPTIONS = 2
QUIZ_MAX_OPTIONS = 10
QUIZ_QUESTION_MAX_LENGTH = 1000
QUIZ_OPTION_MAX_LENGTH = 400
QUIZ_EXPLANATION_MAX_LENGTH = 2000

# Machine-readable tool-result error code for a pool that violates any constraint above (ADR-064
# §5). All-or-nothing: ONE code for the whole pool — partial acceptance (dropping the bad question)
# is forbidden, it would silently shrink the pool and deprive the model of feedback.
QUIZ_INVALID_ERROR_CODE = "invalid_quiz"
# Content-FREE constraint reminder appended to the degrade message so the model can fix the pool
# without the message ever carrying quiz text (ADR-064 §5).
QUIZ_CONSTRAINTS_HINT = (
    f"expected {QUIZ_MIN_QUESTIONS}-{QUIZ_MAX_QUESTIONS} questions, "
    f"{QUIZ_MIN_OPTIONS}-{QUIZ_MAX_OPTIONS} options, 0-based correctIndex < len(options)"
)


# ADR-065 §4: the per-option length limit is expressed ON THE ITEM TYPE, not in a custom validator,
# so it lands in the JSON Schema as `options.items.maxLength` and reaches the model as a hint — like
# every neighbouring bound. While it lived only in a validator, the model learned about a too-long
# option ONLY from a degrade round: one extra upstream call on a turn that costs 2 credits.
# Annotated keeps BOTH properties in one declaration: the schema keyword AND the authoritative
# server-side check (the server check is not weakened, it is the same constraint).
QuizOption = Annotated[str, Field(min_length=1, max_length=QUIZ_OPTION_MAX_LENGTH)]


# ADR-065 §5 — the SINGLE declaration of the quiz question structure. The wire model of the response
# field `ChatResponse.quiz` reuses this very class (see `app.schemas.chat`) instead of declaring its
# own copy, so the validated pool and the serialized pool cannot drift: a change to a field name,
# type, requiredness or bound is impossible to make in only one of them.
#
# The class DOCSTRING is user-facing on purpose: unlike the tool `inputSchema` — where model
# metainformation is cut at the boundary (`_INTERNAL_SCHEMA_KEYS`) — this model is also an OpenAPI
# component, and there the docstring IS the published `description`. Engineering rationale therefore
# lives in these comments, never in the docstring (no ADR/TD/Q references there).
class QuizQuestion(_StrictModel):
    """Один вопрос квиза: формулировка, варианты ответа, индекс правильного и пояснение."""

    question: str = Field(min_length=1, max_length=QUIZ_QUESTION_MAX_LENGTH)
    options: list[QuizOption] = Field(min_length=QUIZ_MIN_OPTIONS, max_length=QUIZ_MAX_OPTIONS)
    correctIndex: int
    explanation: str = Field(min_length=1, max_length=QUIZ_EXPLANATION_MAX_LENGTH)

    @field_validator("correctIndex", mode="before")
    @classmethod
    def _reject_bool_index(cls, value: Any) -> Any:
        # In Python `bool` IS a subclass of `int` and pydantic's lax mode coerces True→1, so a
        # `correctIndex: true` would silently pass as index 1 (ADR-064 §4 requires rejecting it).
        # Checked BEFORE coercion — the declared `int` type alone does not catch this.
        if isinstance(value, bool):
            raise ValueError("correctIndex must be an integer, not a boolean")
        return value

    @model_validator(mode="after")
    def _index_in_range(self) -> QuizQuestion:
        # The ONLY constraint that JSON Schema cannot express (it relates two fields), which is why
        # it stays a validator while every numeric bound above is a schema keyword (ADR-065 §4).
        if not 0 <= self.correctIndex < len(self.options):
            raise ValueError("correctIndex out of range")
        return self


# The WHOLE pool travels in ONE `quiz.generate` call (ADR-064 §4): one call, not N — every
# server-side tool call spends a round of the tool-loop, and N calls would give a non-deterministic
# number of questions, extra latency and duplicate questions. "Execution" of the tool = validation
# + echo of this object. ADR-065 §5: this is ALSO the wire model of `ChatResponse.quiz`
# (re-exported by `app.schemas.chat`). Docstring stays user-facing — see QuizQuestion above.
class Quiz(_StrictModel):
    """Пул вопросов квиза, который клиент рендерит карточками."""

    questions: list[QuizQuestion] = Field(
        min_length=QUIZ_MIN_QUESTIONS, max_length=QUIZ_MAX_QUESTIONS
    )


# --- global server-side media.generate_* (ADR-068) ---
# Bounds mirror POST /v1/media/images|videos (schemas/media.py). Authoritative catalog checks
# (allowed aspectRatio/resolution/duration per model) happen in MediaGenerationService.submit;
# these schemas only enforce structural shape so the model gets a useful JSON Schema hint.
_MEDIA_PROMPT_MAX = 5000
_MEDIA_NEGATIVE_PROMPT_MAX = 2000
_MEDIA_URL_MAX = 2048
_MEDIA_MAX_IMAGE_URLS = 14
_MEDIA_SEED_MAX = 2**31 - 1


def _validate_media_https_urls(values: list[str]) -> list[str]:
    for value in values:
        if not value.startswith("https://"):
            raise ValueError("image URLs must start with https://")
        if len(value) > _MEDIA_URL_MAX:
            raise ValueError(f"image URL must be at most {_MEDIA_URL_MAX} characters")
    return values


class MediaGenerateImageArgs(_StrictModel):
    """Args for media.generate_image — same intent as POST /v1/media/images."""

    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=_MEDIA_PROMPT_MAX)
    imageUrls: list[str] | None = Field(default=None, max_length=_MEDIA_MAX_IMAGE_URLS)
    sourceJobId: str | None = None
    useRecentImage: bool | None = None
    aspectRatio: str | None = None
    resolution: str | None = None
    numImages: int | None = Field(default=None, ge=1, le=4)
    outputFormat: Literal["jpeg", "png", "webp"] | None = None
    seed: int | None = Field(default=None, ge=0, le=_MEDIA_SEED_MAX)

    @field_validator("imageUrls")
    @classmethod
    def _check_urls(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_media_https_urls(value)

    @model_validator(mode="after")
    def _exclusive_refs(self) -> MediaGenerateImageArgs:
        if self.sourceJobId is not None and self.imageUrls:
            raise ValueError("sourceJobId and imageUrls are mutually exclusive")
        if self.sourceJobId is not None and self.useRecentImage:
            raise ValueError("sourceJobId and useRecentImage are mutually exclusive")
        if self.imageUrls and self.useRecentImage:
            raise ValueError("imageUrls and useRecentImage are mutually exclusive")
        return self


class MediaGenerateVideoArgs(_StrictModel):
    """Args for media.generate_video — same intent as POST /v1/media/videos."""

    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1, max_length=_MEDIA_PROMPT_MAX)
    imageUrl: str | None = Field(default=None, max_length=_MEDIA_URL_MAX)
    sourceJobId: str | None = None
    useRecentImage: bool | None = None
    negativePrompt: str | None = Field(default=None, max_length=_MEDIA_NEGATIVE_PROMPT_MAX)
    aspectRatio: str | None = None
    resolution: str | None = None
    duration: str | None = None
    generateAudio: bool | None = None
    cfgScale: float | None = Field(default=None, ge=0, le=1)
    seed: int | None = Field(default=None, ge=0, le=_MEDIA_SEED_MAX)

    @field_validator("imageUrl")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_media_https_urls([value])[0]

    @model_validator(mode="after")
    def _exclusive_refs(self) -> MediaGenerateVideoArgs:
        if self.sourceJobId is not None and self.imageUrl is not None:
            raise ValueError("sourceJobId and imageUrl are mutually exclusive")
        if self.sourceJobId is not None and self.useRecentImage:
            raise ValueError("sourceJobId and useRecentImage are mutually exclusive")
        if self.imageUrl is not None and self.useRecentImage:
            raise ValueError("imageUrl and useRecentImage are mutually exclusive")
        return self


class MediaAskParamsArgs(_StrictModel):
    """Args for media.ask_params — start a catalog-backed choices wizard (ADR-070)."""

    kind: Literal["image", "video"]
    prompt: str = Field(min_length=1, max_length=_MEDIA_PROMPT_MAX)
    sourceJobId: str | None = None
    useRecentImage: bool | None = None

    @model_validator(mode="after")
    def _exclusive_refs(self) -> MediaAskParamsArgs:
        if self.sourceJobId is not None and self.useRecentImage:
            raise ValueError("sourceJobId and useRecentImage are mutually exclusive")
        return self


_ARGS_BY_TOOL: dict[str, type[_StrictModel]] = {
    TOOL_FILES_READ: FilesReadArgs,
    TOOL_FILES_WRITE: FilesWriteArgs,
    TOOL_FILES_LIST: FilesListArgs,
    TOOL_FILES_MKDIR: FilesMkdirArgs,
    TOOL_CALENDAR_READ: CalendarReadArgs,
    TOOL_CALENDAR_CREATE: CalendarCreateArgs,
    TOOL_REMINDERS_READ: RemindersReadArgs,
    TOOL_REMINDERS_CREATE: RemindersCreateArgs,
    TOOL_SITE_WRITE_FILE: SiteWriteFileArgs,
    TOOL_SITE_PREVIEW: SitePreviewArgs,
    TOOL_SITE_LIST: SiteListArgs,
    TOOL_SITE_READ: SiteReadArgs,
    TOOL_SITE_DELETE: SiteDeleteArgs,
    TOOL_TIME_NOW: TimeNowArgs,
    TOOL_QUIZ_GENERATE: Quiz,
    TOOL_MEDIA_GENERATE_IMAGE: MediaGenerateImageArgs,
    TOOL_MEDIA_GENERATE_VIDEO: MediaGenerateVideoArgs,
    TOOL_MEDIA_ASK_PARAMS: MediaAskParamsArgs,
    TOOL_DOCUMENT_CREATE: DocumentCreateArgs,
    TOOL_DOCUMENT_LIST: DocumentListArgs,
    TOOL_DOCUMENT_READ: DocumentReadArgs,
    TOOL_DOCUMENT_UPDATE: DocumentUpdateArgs,
}


# Human-readable tool descriptions — single source of truth for both the Anthropic tool
# definitions and the GET /v1/tools catalog (ADR-019).
TOOL_DESCRIPTIONS: dict[str, str] = {
    TOOL_FILES_READ: "Read a file from the user's device.",
    TOOL_FILES_WRITE: "Write a file on the user's device.",
    TOOL_FILES_LIST: "List files/directories on the user's device.",
    TOOL_FILES_MKDIR: "Create a directory on the user's device.",
    TOOL_CALENDAR_READ: (
        "Read calendar events within a time range. 'start' and 'end' are ISO8601 datetime "
        "strings in local time without timezone offset, e.g. '2026-06-11T09:00:00'. For a "
        "whole day use start at 00:00:00 and end at the next day 00:00:00 (end-exclusive). "
        "Use the time.now tool if you do not know the current date."
    ),
    TOOL_CALENDAR_CREATE: (
        "Create calendar events. Each event's 'start' and 'end' are ISO8601 datetime strings "
        "in local time without timezone offset, e.g. '2026-06-11T09:00:00'."
    ),
    TOOL_REMINDERS_READ: "Read reminders.",
    TOOL_REMINDERS_CREATE: "Create reminders.",
    TOOL_SITE_WRITE_FILE: (
        "Write or overwrite a file in the website project. Path is relative to the project "
        "root. Use encoding 'utf8' for text (HTML/CSS/JS) and 'base64' for binary assets "
        "(images/fonts) — but ONLY with REAL base64 bytes. NEVER write an image or other "
        "binary file with placeholder, fake, or invented content (e.g. content like "
        "'base64 placeholder for dish1.jpg'): it is rejected as invalid base64 and the page "
        "is left with broken images. If you do NOT have real image bytes, do NOT create image "
        "files and do NOT reference external image URLs — instead make the page self-contained "
        "by rendering visuals with CSS gradients/backgrounds, inline SVG, or emoji. The project "
        "is the current chat session's project (no project id needed)."
    ),
    TOOL_SITE_PREVIEW: (
        "Get a temporary signed preview URL for the current website project. Optional 'entry' "
        "selects the start file (default index.html). The returned `url` is an ABSOLUTE URL that "
        "opens directly in a browser (signed token, no authentication). Use it exactly as "
        "returned — do NOT change, shorten, or add a host/domain to it."
    ),
    TOOL_SITE_LIST: "List the files of the current website project.",
    TOOL_SITE_READ: "Read a file from the current website project by relative path.",
    TOOL_SITE_DELETE: "Delete a file from the current website project by relative path.",
    TOOL_DOCUMENT_CREATE: (
        "Create a text document stored on the server for THIS chat, which the user can then open "
        "and download. Use it whenever the user asks for a file, a report, a table or any content "
        "meant to be kept rather than just read in the reply. 'mediaType' is one of "
        "'text/markdown', 'text/plain', 'text/csv', 'application/json'. Put the FULL content in "
        "'content'."
    ),
    TOOL_DOCUMENT_LIST: "List the documents stored in this chat (id, filename, size, version).",
    TOOL_DOCUMENT_READ: (
        "Read the full content of a document of this chat by its id. Always read before updating "
        "so your replacement is based on the current text."
    ),
    TOOL_DOCUMENT_UPDATE: (
        "Replace the ENTIRE content of a document of this chat. There is no partial patch: send "
        "the complete new text in 'content'. Read the document first unless you wrote it in this "
        "same turn."
    ),
    TOOL_TIME_NOW: (
        "Get the current date and time. Always returns UTC (ISO8601, unix timestamp, weekday). "
        "Pass an optional IANA timezone 'tz' (e.g. 'Europe/Moscow') to also get the local time. "
        "Call this whenever the request depends on the current date, time, or day of the week — "
        "do not guess."
    ),
    # ADR-064 §7 (soft level): the description itself instructs the model to keep the questions
    # OUT of the free text — the hard, deterministic guarantee is the assistantMessage suppression
    # in the single response-mapping point.
    TOOL_QUIZ_GENERATE: (
        "Generate an interactive quiz for the learner: a pool of multiple-choice questions about "
        f"the topic you are explaining. Send the WHOLE pool in ONE call: {QUIZ_MIN_QUESTIONS} to "
        f"{QUIZ_MAX_QUESTIONS} questions, each with {QUIZ_MIN_OPTIONS} to {QUIZ_MAX_OPTIONS} "
        "answer options, a 0-based 'correctIndex' pointing at the correct option, and a short "
        "'explanation' shown to the learner after they answer. Ask the questions ONLY through this "
        "tool: never repeat the question wording in your reply text and never reveal the correct "
        "options or explanations there. Keep any accompanying text short."
    ),
    TOOL_MEDIA_ASK_PARAMS: (
        "Present tappable choices so the user picks image/video parameters (model, resolution, "
        "duration, …) before generation. Call this when the user wants a photo or video and has "
        "not already chosen those parameters. Pass kind ('image'|'video') and the prompt to use "
        "(never repeat that prompt in your visible reply). "
        "If the user attached a photo on THIS message, the server uses it as the image-to-image "
        "reference automatically — do not invent URLs. "
        "If the user agreed to reuse a photo from a recent earlier message, pass useRecentImage "
        "true (server resolves a stored URL with a 1-day TTL). "
        "For edits/refinements of a prior generation, pass sourceJobId (that job's jobId) so "
        "the provider runs image-to-image / image-to-video instead of a new text-to-* render. "
        "Exception: when starting a video after a previously GENERATED photo in this chat, omit "
        "sourceJobId and useRecentImage — the app shows a Yes/No mediaChoices card "
        "«Использовать последнее фото?» before model/duration. Do not ask that "
        "Yes/No in plain text. "
        "Do NOT invent model ids or resolutions — the app shows catalog options. After this tool, "
        "wait for the user to tap; do not call media.generate_* in the same turn."
    ),
    TOOL_MEDIA_GENERATE_IMAGE: (
        "Submit an image generation job when model and quality are ALREADY known (e.g. the user "
        "stated them). Prefer media.ask_params when parameters are unclear. Returns immediately "
        "with jobId and status 'queued' — do NOT wait for the image; the app polls "
        "GET /v1/media/jobs/{jobId}. 'model' is a catalog id such as 'nano-banana-2'. "
        "For edits of a previous image, pass sourceJobId; optional imageUrls (https) or "
        "useRecentImage true (agreed reuse of a recent chat photo) are alternatives. "
        "Never repeat the generation prompt in your visible reply. Costs media credits in "
        "addition to the chat turn."
    ),
    TOOL_MEDIA_GENERATE_VIDEO: (
        "Submit a video generation job when model, duration, and quality are ALREADY known. "
        "Prefer media.ask_params when parameters are unclear. Returns immediately with jobId and "
        "status 'queued' — do NOT wait for the video; the app polls GET /v1/media/jobs/{jobId}. "
        "'model' is a catalog id such as 'veo-3.1'. For image-to-video from a prior job, pass "
        "sourceJobId; optional imageUrl or useRecentImage true are alternatives. Never repeat "
        "the generation prompt in your visible reply. Costs media credits in addition to the "
        "chat turn."
    ),
}


def validate_tool_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate Claude-produced tool args against the strict schema. Raises ValueError."""
    model = _ARGS_BY_TOOL.get(tool_name)
    if model is None:
        raise ValueError(f"unknown tool: {tool_name}")
    return model.model_validate(args).model_dump()


# Bounds on a degrade message. All three are hard caps, not semantic limits — the message is
# persisted in a chat step and replayed to the model on the next round, so its size must not depend
# on what the model sent. Capping the ITEM COUNT alone is not enough: a `loc` segment can be an
# arbitrary key name invented by the model (`extra_forbidden` reports the offending key), so each
# part is capped too and the joined result is capped again. Same defensive pattern as the hard cap
# on serverTools[].summary.
_ARGS_ERROR_MAX_ITEMS = 5
_ARGS_ERROR_MAX_PART_CHARS = 120
_ARGS_ERROR_MAX_CHARS = 400


def content_free_args_error(exc: Exception) -> str:
    """Build a CONTENT-FREE description of an args-validation failure (ADR-064 §5).

    Only the field PATH (``loc``) and the error KIND are used — never ``exc``'s own string form,
    which pydantic renders WITH the offending input values. For ``quiz.generate`` that difference is
    the whole point: the quiz text must not leak into the tool-result echo that is persisted, logged
    and replayed to the model. ``value_error`` entries carry OUR OWN validator message (a fixed
    string written in this module), so they are content-free by construction.

    The result is also LENGTH-BOUNDED (see ``_ARGS_ERROR_MAX_*``): a ``loc`` segment can be a key
    name the model invented, so neither an individual part nor the whole message may grow with the
    input.
    """
    if not isinstance(exc, ValidationError):
        # Non-pydantic ValueError (e.g. «unknown tool: …») — already content-free by construction.
        return str(exc)[:_ARGS_ERROR_MAX_CHARS]
    parts: list[str] = []
    for err in exc.errors()[:_ARGS_ERROR_MAX_ITEMS]:
        location = ".".join(str(part) for part in err.get("loc", ()))
        kind = str(err.get("type", "invalid"))
        if kind == "value_error":
            detail = str(err.get("msg", "")).removeprefix("Value error, ").strip() or kind
        else:
            detail = kind
        part = f"{location}: {detail}" if location else detail
        parts.append(part[:_ARGS_ERROR_MAX_PART_CHARS])
    return ("; ".join(parts) or "invalid arguments")[:_ARGS_ERROR_MAX_CHARS]


def offered_media_chat_tool(tool_name: str, *, include_media_chat_tools: bool) -> bool:
    """ADR-072: media chat tools are offered only when the instance enables them."""
    if tool_name not in MEDIA_CHAT_TOOLS:
        return True
    return include_media_chat_tools


def tool_family(tool_name: str) -> str:
    """Prefix before the first dot (`files.read` → `files`). A name without a dot is itself."""
    return tool_name.split(".", 1)[0]


def parse_disabled_tool_families(raw: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """Parse ``CHAT_DISABLED_TOOL_FAMILIES`` (comma-separated). Pure — no I/O.

    Returns ``(accepted, unknown)``. Unknown tokens are not applied (caller may warn).
    Empty / blank → ``(frozenset(), ())``.
    """
    accepted: set[str] = set()
    unknown: list[str] = []
    for part in raw.split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token in DISABLEABLE_TOOL_FAMILIES:
            accepted.add(token)
        else:
            unknown.append(token)
    return frozenset(accepted), tuple(unknown)


def offered_tool_family(tool_name: str, *, disabled_families: frozenset[str]) -> bool:
    """ADR-081: hide one family (`files`/`calendar`/`reminders`/`site`) on this instance."""
    return tool_family(tool_name) not in disabled_families


def offered_in_generation_mode(tool_name: str, generation_mode: str) -> bool:
    """Axis C predicate (ADR-064 §3): may ``tool_name`` be offered in ``generation_mode``?

    A tool ABSENT from ``TOOL_GENERATION_MODES`` is not mode-gated and is always allowed by this
    axis (the other 14 tools keep their previous behaviour). ``generation_mode`` MUST be the
    EFFECTIVE mode of the turn — the same value that goes to the provider and to billing.
    """
    modes = TOOL_GENERATION_MODES.get(tool_name)
    return modes is None or generation_mode in modes


# Tools whose args JSON Schema MUST be self-contained — no ``$ref``/``$defs`` (ADR-064 §4).
# Reason: ``input_schema``/``parameters`` are shipped to TWO different providers and the contract
# must not rely on either supporting ``$ref``. Scoped deliberately: the pre-existing schemas of
# calendar.*/reminders.* are part of an already-published catalog shape (they keep their ``$defs``);
# widening this set is a contract change for architect, not a drive-by here.
_SELF_CONTAINED_SCHEMA_TOOLS = frozenset({TOOL_QUIZ_GENERATE})

# MODEL metainformation that pydantic derives from the class itself rather than from the tool
# contract: root ``title`` (= the Python class name) and root ``description`` (= the class
# docstring). Both are cut out of every args schema — normative requirement, 02-api-contracts.md
# §inputSchema («вырезана модельная метаинформация»).
#
# Why: no artifact that LEAVES THE PROCESS may carry internal development identifiers (ADR-NNN /
# TD-NNN / Q-NNN-N / BUG-N references, internal class names, internal constant and registry names),
# and a tool's args schema leaves through TWO such surfaces — the public GET /v1/tools body and the
# ``input_schema``/``parameters`` shipped to the provider on every round of the turn (where it is
# also paid-for prompt payload).
#
# Why HERE and not by policing docstrings: the requirement is addressed to the SCHEMA GENERATOR —
# one point — precisely because a rule like «never write an ADR reference in a docstring» is not
# checkable and breaks with the very next tool added. Model docstrings stay normal internal
# documentation; the human-facing text of a tool is TOOL_DESCRIPTIONS.
#
# NOT stripped (normative, must survive): PER-FIELD ``title``/``description`` coming from
# ``Field(...)``, plus ``type``/``properties``/``items``/``required``/``enum``/
# ``additionalProperties`` and the constraint keywords.
_INTERNAL_SCHEMA_KEYS = ("title", "description")


def _inline_schema_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Recursively replace ``{"$ref": "#/$defs/X"}`` nodes with a copy of the definition ``X``.

    The definition's own ``title``/``description`` are dropped on the way in: they are the nested
    model's class name and docstring, and inlining would otherwise smuggle them past the root-level
    strip — which is why 02-api-contracts.md §inputSchema requires the same two keys to be cut
    «у инлайненных определений вложенных моделей», not only at the root. Sibling keys next to a
    ``$ref`` are preserved and win over the definition's own keys. Only local ``#/$defs/``
    references are resolved; anything else is left untouched (there are none in these models — this
    is defensive, not a feature).
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = defs.get(ref.removeprefix("#/$defs/"))
            if isinstance(target, dict):
                inlined = {
                    k: v for k, v in copy.deepcopy(target).items() if k not in _INTERNAL_SCHEMA_KEYS
                }
                siblings = {k: v for k, v in node.items() if k != "$ref"}
                return _inline_schema_refs({**inlined, **siblings}, defs)
        return {key: _inline_schema_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_schema_refs(item, defs) for item in node]
    return node


def tool_input_schema(tool_name: str) -> dict[str, Any]:
    """JSON Schema of a tool's args, with the model's metainformation cut out (NORMATIVE format).

    Built from ``model_json_schema()`` MINUS the model metainformation — root ``title`` and root
    ``description`` (see ``_INTERNAL_SCHEMA_KEYS``), and for self-contained schemas the same two
    keys on the inlined nested definitions. The format is deliberately NOT «the raw output of
    ``model_json_schema()``»: equality with the raw output would violate the invariant that internal
    identifiers never leave the process (02-api-contracts.md §inputSchema).

    For tools in ``_SELF_CONTAINED_SCHEMA_TOOLS`` the nested models are INLINED and ``$defs`` is
    dropped, so the schema handed to a provider carries no ``$ref`` (ADR-064 §4). Per-field
    ``title``/``description`` and the constraint keywords
    (``minItems``/``maxItems``/``maxLength``) are KEPT — they are a hint for the model; the
    authoritative check stays server-side.
    """
    schema = _ARGS_BY_TOOL[tool_name].model_json_schema()
    for internal_key in _INTERNAL_SCHEMA_KEYS:
        schema.pop(internal_key, None)
    if tool_name in _SELF_CONTAINED_SCHEMA_TOOLS:
        defs = schema.pop("$defs", None) or {}
        inlined = _inline_schema_refs(schema, defs)
        assert isinstance(inlined, dict)  # noqa: S101 - schema root is always an object
        return inlined
    return schema


def tool_catalog(*, disabled_families: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Machine-readable catalog of backend tools for GET /v1/tools (ADR-019, ADR-081).

    Single source of truth: iterates ``_ARGS_BY_TOOL`` (deterministic order). Each entry carries
    the dotted domain ``name`` (NOT the anthropic-underscore transport name), description,
    ``mutating`` (name in MUTATING_TOOLS), ``execution`` ("server" for SERVER_SIDE_TOOLS ∪
    GLOBAL_SERVER_SIDE_TOOLS else "client", ADR-026 §2) and ``inputSchema`` (the args JSON Schema).
    ``disabled_families`` (ADR-081) drops whole families on one instance; default empty = full
    registry (axes A/B/C still do not filter this catalog).
    """
    catalog: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        if not offered_tool_family(name, disabled_families=disabled_families):
            continue
        catalog.append(
            {
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "mutating": name in MUTATING_TOOLS,
                "execution": (
                    "server"
                    if name in SERVER_SIDE_TOOLS or name in GLOBAL_SERVER_SIDE_TOOLS
                    else "client"
                ),
                "inputSchema": tool_input_schema(name),
            }
        )
    return catalog


def _offered_to_model(
    name: str,
    *,
    include_server_side: bool,
    generation_mode: str,
    include_media_chat_tools: bool,
    disabled_families: frozenset[str],
) -> bool:
    if not include_server_side and name in SERVER_SIDE_TOOLS:
        return False
    if not offered_in_generation_mode(name, generation_mode):
        return False
    if not offered_media_chat_tool(name, include_media_chat_tools=include_media_chat_tools):
        return False
    return offered_tool_family(name, disabled_families=disabled_families)


def anthropic_tool_definitions(
    *,
    include_server_side: bool = True,
    generation_mode: str = "general",
    include_media_chat_tools: bool = True,
    disabled_families: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Tool definitions for the Anthropic messages API (input_schema per tool).

    ADR-022 (axis A — project presence): when ``include_server_side`` is False, PROJECT-SCOPED
    server-side ``site.*`` tools (``SERVER_SIDE_TOOLS``) are EXCLUDED from the offered set — Claude
    never sees them and cannot call them. The orchestrator passes ``include_server_side=False`` for
    «чистый чат» sessions (``chat_sessions.project_id IS NULL``) and ``True`` when a project is
    present.

    ADR-026 §3: the ``include_server_side`` flag gates ONLY project-scoped ``SERVER_SIDE_TOOLS``
    (``site.*``). GLOBAL server-side tools (``GLOBAL_SERVER_SIDE_TOOLS`` — ``time.now``) are NEVER
    excluded by this flag — they are offered to Claude ALWAYS, with or without a project, in both
    assistant_modes (utility tool, axis B does not filter it).

    Note (Q-012-1 — Open): the orthogonal assistant_mode filter (axis B) is NOT yet implemented in
    code. Until it is, the effective offer-set = this project_id gate over the current behavior
    (all client-side tools always offered; site.* gated only by project presence; time.now always
    offered). When axis B lands, it composes by logical AND with this flag (time.now stays exempt).

    ADR-064 §3 (axis C — generation mode): a tool listed in ``TOOL_GENERATION_MODES`` is offered
    only when ``generation_mode`` (the EFFECTIVE mode of the turn) is in its set. The default
    ``general`` therefore excludes ``quiz.generate`` — including on the legacy path, which forces
    ``general``. Axes A and C compose by logical AND.
    """
    definitions: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        if not _offered_to_model(
            name,
            include_server_side=include_server_side,
            generation_mode=generation_mode,
            include_media_chat_tools=include_media_chat_tools,
            disabled_families=disabled_families,
        ):
            continue
        definitions.append(
            {
                # BUG-3 forward map: Anthropic requires underscore names; iOS-facing names stay
                # dotted. `name` here is the domain name; emit the anthropic-name transport-side.
                "name": to_anthropic_tool_name(name),
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": tool_input_schema(name),
            }
        )
    return definitions


def neutral_tool_definitions(
    *,
    include_server_side: bool = True,
    generation_mode: str = "general",
    include_media_chat_tools: bool = True,
    disabled_families: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Provider-neutral tool definitions (ADR-033 §4): ``{name(domain dotted), description,
    input_schema}``.

    Single source of truth handed to ``LLMClient.create_message``; the client serializes them to
    its provider wire format (Anthropic underscore names / OpenAI function-tool wrapper).
    The ``include_server_side`` gate is identical to ``anthropic_tool_definitions`` (ADR-022 axis A:
    drop project-scoped ``site.*`` when there is no project; ``GLOBAL_SERVER_SIDE_TOOLS`` like
    ``time.now`` are never gated — ADR-026 §3), and so is the ``generation_mode`` gate (ADR-064 §3
    axis C: ``quiz.generate`` only in ``study_learn``; the ``general`` default excludes it).
    ADR-072: ``include_media_chat_tools=False`` drops ``MEDIA_CHAT_TOOLS``.
    ADR-081: ``disabled_families`` drops ``files`` / ``calendar`` / ``reminders`` / ``site``.
    """
    definitions: list[dict[str, Any]] = []
    for name in _ARGS_BY_TOOL:
        if not _offered_to_model(
            name,
            include_server_side=include_server_side,
            generation_mode=generation_mode,
            include_media_chat_tools=include_media_chat_tools,
            disabled_families=disabled_families,
        ):
            continue
        definitions.append(
            {
                # Domain (dotted) name — the client maps it to the provider transport name.
                "name": name,
                "description": TOOL_DESCRIPTIONS[name],
                "input_schema": tool_input_schema(name),
            }
        )
    return definitions


def openai_tool_function(neutral_def: dict[str, Any]) -> dict[str, Any]:
    """Serialize ONE neutral tool definition to the OpenAI function-tool wire shape (ADR-033 §4).

    Single source of truth for the OpenAI wire wrapping — used both by ``openai_tool_definitions``
    (the SSOT generator) and by ``OpenAIClient._serialize_tools`` on the live path, so the shape is
    defined in exactly one place.

    Input: a neutral def ``{name(domain dotted), description, input_schema}``. A def already in the
    OpenAI shape (has ``function``) is passed through unchanged (back-compat for any caller that
    pre-serialized). Output:
    ``{type:"function", function:{name(underscore), description, parameters(=input_schema)}}``.
    OpenAI function names match the SAME ``^[a-zA-Z0-9_-]{1,64}$`` constraint as Anthropic — dots
    are forbidden for both providers — so the underscore map (``to_anthropic_tool_name``) is reused;
    the name is provider-neutral by value (dot↔underscore).
    """
    if "function" in neutral_def:  # already OpenAI-shaped — pass through
        return neutral_def
    name = str(neutral_def.get("name", ""))
    # Same underscore transport name as Anthropic (dots forbidden on both).
    fn_name = to_anthropic_tool_name(name) if "." in name else name
    return {
        "type": "function",
        "function": {
            "name": fn_name,
            "description": neutral_def["description"],
            "parameters": neutral_def["input_schema"],
        },
    }


def openai_tool_definitions(
    *,
    include_server_side: bool = True,
    generation_mode: str = "general",
    include_media_chat_tools: bool = True,
    disabled_families: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Tool definitions for the OpenAI Chat Completions API (ADR-033 §4).

    SSOT for the OpenAI offered tool-set: builds neutral defs (``neutral_tool_definitions``) and
    serializes each via ``openai_tool_function`` (the one OpenAI-wire wrapper). The
    ``include_server_side`` gate is identical to ``anthropic_tool_definitions`` (ADR-022 §A;
    ``GLOBAL_SERVER_SIDE_TOOLS`` never gated — ADR-026 §3), and so is the ``generation_mode``
    axis-C gate (ADR-064 §3). ADR-072: ``include_media_chat_tools`` mirrors neutral defs.
    """
    return [
        openai_tool_function(d)
        for d in neutral_tool_definitions(
            include_server_side=include_server_side,
            generation_mode=generation_mode,
            include_media_chat_tools=include_media_chat_tools,
            disabled_families=disabled_families,
        )
    ]
