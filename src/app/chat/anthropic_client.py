"""Anthropic Claude client wrapper (CO-2).

Real integration with the Anthropic Python SDK. Supports prompt caching (cache_control
ephemeral on system prompt and tool definitions), tool-loop generation, usage parsing
(including cache read/write tokens), and a per-call api_key override for BYOK (the user's
key is passed in-memory only and never logged — 05-security.md, ADR-003).

Model and key come from config / per-call override; never hardcoded (02-tech-stack.md).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, cast

import anthropic

from app.chat.tools import UnknownToolNameError, to_domain_tool_name
from app.config import get_settings
from app.errors import UpstreamError, ValidationFailedError
from app.observability.logging import get_logger, log_event
from app.observability.metrics import anthropic_upstream_errors_total

_logger = get_logger("app.chat.anthropic")


@dataclass(frozen=True)
class AnthropicUsage:
    input_tokens: int
    output_tokens: int
    model: str
    cache_read_tokens: int
    cache_write_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "model": self.model,
            "cacheReadTokens": self.cache_read_tokens,
            "cacheWriteTokens": self.cache_write_tokens,
        }


@dataclass(frozen=True)
class AnthropicResult:
    stop_reason: str | None
    content_blocks: list[dict[str, Any]]
    usage: AnthropicUsage
    text: str = ""
    tool_uses: list[dict[str, Any]] = field(default_factory=list)


class AnthropicAuthError(Exception):
    """Raised when Anthropic rejects the (BYOK) key as unauthorized → key_status=invalid."""


class KeyValidation(str, enum.Enum):
    """Outcome of a BYOK key validation call (ADR-016).

    valid   — Anthropic accepted the key.
    invalid — Anthropic rejected the key with 401 Unauthorized.
    offline — validation could not be performed (timeout/connection/non-401 status), NOT a 401.
    """

    valid = "valid"
    invalid = "invalid"
    offline = "offline"


def _extract_error_body(exc: Exception) -> tuple[str | None, str | None]:
    """Extract Anthropic error.type / error.message from the SDK exception body (TD-014).

    The Anthropic wire error body is `{"type": "error", "error": {"type": ..., "message": ...}}`.
    `APIError.body` is the decoded JSON (or raw/None when undecodable). This is the *provider's*
    error body — never user-content — so it is safe to log (03-architecture.md §Логирование
    upstream-ошибок Anthropic). Returns (error_type, error_message); either may be None when the
    body is absent or not in the expected shape (then the field is omitted from the log).
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    error_type = error.get("type")
    error_message = error.get("message")
    return (
        error_type if isinstance(error_type, str) else None,
        error_message if isinstance(error_message, str) else None,
    )


def _log_upstream_error(exc: Exception, *, model: str) -> None:
    """Log the `anthropic_upstream_error` event BEFORE mapping to UpstreamError (TD-014).

    Logs only the provider error body (status_code / error.type / error.message), the anthropic
    request id, the model and the exception class — never the api-key, BYOK key or user-content
    (03-architecture.md §Логирование upstream-ошибок Anthropic, 05-security.md §Логирование).
    Correlation fields (requestId/sessionId) are attached automatically by the log formatter.
    Levels per the TD-014 matrix: WARNING for 4xx (incl. 429), ERROR for 5xx and for
    timeout/connection errors (which carry no HTTP status). The same metric is incremented with
    bounded-enum labels (01-architecture.md §Наблюдаемость).
    """
    status_code: int | None = getattr(exc, "status_code", None)
    error_type, error_message = _extract_error_body(exc)
    anthropic_request_id: str | None = getattr(exc, "request_id", None)

    if status_code is not None and 400 <= status_code < 500:
        level = logging.WARNING
    else:
        # 5xx, or timeout/connection errors with no HTTP status.
        level = logging.ERROR

    fields: dict[str, Any] = {
        "event": "anthropic_upstream_error",
        "model": model,
        "exceptionClass": type(exc).__name__,
    }
    if status_code is not None:
        fields["status_code"] = status_code
    if error_type is not None:
        fields["errorType"] = error_type
    if error_message is not None:
        fields["errorMessage"] = error_message
    if anthropic_request_id is not None:
        fields["anthropicRequestId"] = anthropic_request_id

    log_event(_logger, level, "anthropic_upstream_error", **fields)
    anthropic_upstream_errors_total.labels(
        status_code=str(status_code) if status_code is not None else "none",
        error_type=error_type or "unknown",
    ).inc()


class AnthropicClient:
    """Thin async wrapper around anthropic.AsyncAnthropic.

    The service key is read from config; BYOK callers pass api_key per call. TLS verification
    is enabled by default by the SDK (httpx). No secrets are logged.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._default_model = settings.anthropic_model
        self._max_tokens = settings.anthropic_max_tokens
        self._service_key = settings.anthropic_api_key
        # One client per process; per-call key overrides via with_options(api_key=...).
        # timeout / max_retries are config-driven (no upstream call hangs the request pool).
        self._client = anthropic.AsyncAnthropic(
            api_key=self._service_key or "placeholder",
            timeout=settings.anthropic_timeout_seconds,
            max_retries=settings.anthropic_max_retries,
        )

    def _build_system(self, system_prompt: str) -> list[dict[str, Any]]:
        # cache_control on the (stable) system prompt for prompt caching.
        return [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _parse_usage(message: anthropic.types.Message, model: str) -> AnthropicUsage:
        usage = message.usage
        return AnthropicUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=model,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    async def create_message(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        api_key: str | None = None,
    ) -> AnthropicResult:
        """Call messages.create with prompt caching and tools.

        api_key: optional per-call override (BYOK). When None, uses the service key.
        Returns parsed stop_reason, content blocks, usage, text and tool_uses.
        """
        model = self._default_model
        client = self._client
        if api_key is not None:
            client = client.with_options(api_key=api_key)

        # cache_control on the last tool definition caches the whole tool list + system.
        cached_tools = [dict(t) for t in tools]
        if cached_tools:
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

        try:
            message = await client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=cast(Any, self._build_system(system_prompt)),
                tools=cast(Any, cached_tools),
                messages=cast(Any, messages),
            )
        except anthropic.AuthenticationError as exc:
            # BYOK/service key rejected → mapped to byok_invalid (block) or 401 upstream.
            # TD-014: log the upstream error (401 → WARNING) BEFORE mapping; key is never logged.
            _log_upstream_error(exc, model=model)
            raise AnthropicAuthError(str(exc)) from exc
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ) as exc:
            # Timeout / connection / 5xx (or other status) from Anthropic → 502 upstream_error.
            # TD-014: log the structured upstream-error event BEFORE mapping to UpstreamError;
            # only the provider error body is logged, never the api_key or user-content.
            _log_upstream_error(exc, model=model)
            raise UpstreamError("anthropic upstream error") from exc

        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        for block in message.content:
            block_dict = block.model_dump()
            content_blocks.append(block_dict)
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                # BUG-3 reverse map: Claude returns the anthropic-name (underscore). Translate to
                # the domain name (dotted) BEFORE it reaches tool_calls / toolCall.name / audit.
                # `content_blocks` keeps the raw anthropic-name on purpose: the assistant turn is
                # replayed verbatim to Anthropic on continuation and must match the wire protocol.
                try:
                    domain_name = to_domain_tool_name(block.name)
                except UnknownToolNameError as exc:
                    # Upstream anomaly: an unmapped tool name must never surface to iOS as a valid
                    # tool. Fail processing explicitly rather than forwarding garbage.
                    raise ValidationFailedError(str(exc)) from exc
                tool_uses.append({"id": block.id, "name": domain_name, "input": block.input})

        return AnthropicResult(
            stop_reason=message.stop_reason,
            content_blocks=content_blocks,
            usage=self._parse_usage(message, model),
            text="".join(text_parts),
            tool_uses=tool_uses,
        )

    async def validate_key(self, api_key: str) -> KeyValidation:
        """Lightweight Anthropic call to validate a BYOK key (ADR-003 step 6, ADR-016).

        Returns:
        - KeyValidation.valid   — Anthropic accepted the key;
        - KeyValidation.invalid — 401 Unauthorized;
        - KeyValidation.offline — timeout/connection/non-401 status (cannot determine validity).
        Never logs the key.
        """
        client = self._client.with_options(api_key=api_key)
        try:
            await client.messages.create(
                model=self._default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
        except anthropic.AuthenticationError:
            return KeyValidation.invalid
        except (
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        ):
            # Network/transient/non-401 status: we could not validate → offline (ADR-016).
            return KeyValidation.offline
        return KeyValidation.valid


_anthropic_singleton: AnthropicClient | None = None


def get_anthropic_client() -> AnthropicClient:
    global _anthropic_singleton
    if _anthropic_singleton is None:
        _anthropic_singleton = AnthropicClient()
    return _anthropic_singleton
