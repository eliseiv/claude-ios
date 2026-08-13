"""Credits-path provider-key failover (ADR-074), ported from 232 ADR-047.

Two questions live here: which candidates to try, and when to leave a candidate. Rotation
is only for an ACCOUNT failure (money or access). A malformed request repeats on every key
and every provider; walking four candidates for it would quadruple the failure latency.

Credential reasons (owner product rule):

| Reason | OpenAI | Anthropic |
|---|---|---|
| key invalid / revoked | 401 | 401 |
| organization blocked | 403 | 403 |
| funds exhausted | 429 + ``insufficient_quota`` | **400** + credit-balance text |

A plain ``429 rate limit`` is NOT a reason to rotate: the spare key of the same org hits the
same limit. OpenAI sends both quota and rate-limit as 429; the distinction is ``error.type`` /
``error.code``, read before any text heuristic (rate-limit bodies often contain the word
``billing``).

Anthropic reports a dead balance as ``400 invalid_request_error`` — the same code as a
malformed request. Rotating on every 400 would retry a bad form four times. For 400 only the
message text decides.

Asymmetry of directions (same as 232):

- key → key: credential failure only
- Anthropic → OpenAI: any upstream failure, skipping remaining Anthropic keys
- OpenAI → Anthropic: credential failure only

BYOK does not use this module. Empty backup / empty crossover-model env → previous behaviour.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from app.chat.anthropic_client import AnthropicAuthError
from app.chat.openai_client import OpenAIAuthError
from app.config import Settings, get_settings
from app.errors import AppError, UpstreamError

_OPENAI: Final = "openai"
_ANTHROPIC: Final = "anthropic"

_CREDENTIAL_STATUS: Final = frozenset({401, 403})

_QUOTA_MARKERS: Final = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "billing_hard_limit_reached",
        "account_deactivated",
    }
)

#: Machine codes of a plain rate limit. Checked BEFORE text markers: OpenAI rate-limit bodies
#: often link to a billing page, and the text heuristic would treat a burst as a dead key.
_RATE_LIMIT_MARKERS: Final = frozenset(
    {"rate_limit_exceeded", "rate_limit_error", "overloaded_error"}
)

_QUOTA_TEXT_MARKERS: Final = tuple(
    marker.lower()
    for marker in (
        "credit balance is too low",
        "credit balance",
        "no credits remaining",
        "insufficient_quota",
        "insufficient credits",
        "quota",
        "billing",
    )
)

_AUTH_ERRORS: Final = (OpenAIAuthError, AnthropicAuthError)


@dataclass(frozen=True)
class Attempt:
    """One candidate: which provider, which key slot, which key, optional crossover model."""

    provider: str
    key_index: int
    api_key: str | None
    #: ``None`` — keep the session model (stale-model guard still applies). String — substitute
    #: when crossing to the other provider (the requested id does not exist there).
    model: str | None


def openai_chat_fallback_anthropic_model(settings: Settings | None = None) -> str | None:
    """Anthropic model id for an OpenAI→Anthropic crossover; ``None`` → no cross-provider hop."""
    target = (settings or get_settings()).openai_chat_fallback_anthropic_model.strip()
    return target or None


def anthropic_chat_fallback_openai_model(settings: Settings | None = None) -> str | None:
    """OpenAI model id for an Anthropic→OpenAI crossover; ``None`` → no cross-provider hop."""
    target = (settings or get_settings()).anthropic_chat_fallback_openai_model.strip()
    return target or None


def build_attempt_chain(
    session_model: str | None,
    *,
    settings: Settings | None = None,
) -> tuple[Attempt, ...]:
    """Candidates in try order: every key of the primary provider, then the spare provider.

    Primary is ``credits_provider_for_model(session_model)`` (ADR-073): GPT → OpenAI, Claude →
    Anthropic, ``None`` → ``LLM_PROVIDER``. The spare provider is added ONLY when a crossover
    model env is set: without it the other upstream has nothing to serve, and inventing a model
    name would be making it up.

    An empty key chain yields one candidate with ``api_key=None`` so the client uses its
    configured key and a missing key becomes an honest upstream 401 — not our error.
    """
    cfg = settings or get_settings()
    primary = cfg.credits_provider_for_model(session_model)
    if primary == _ANTHROPIC:
        secondary = _OPENAI
        primary_keys = cfg.anthropic_api_key_chain()
        secondary_keys = cfg.openai_api_key_chain()
        crossover = anthropic_chat_fallback_openai_model(cfg)
    else:
        secondary = _ANTHROPIC
        primary_keys = cfg.openai_api_key_chain()
        secondary_keys = cfg.anthropic_api_key_chain()
        crossover = openai_chat_fallback_anthropic_model(cfg)

    attempts = [Attempt(primary, i, key, None) for i, key in enumerate(primary_keys)]
    if not attempts:
        attempts = [Attempt(primary, 0, None, None)]
    if crossover is not None:
        attempts += [
            Attempt(secondary, i, key, crossover) for i, key in enumerate(secondary_keys)
        ] or [Attempt(secondary, 0, None, crossover)]
    return tuple(attempts)


def next_attempt_index(attempts: tuple[Attempt, ...], index: int, exc: BaseException) -> int | None:
    """Index of the next candidate, or ``None`` if the error should reach the client.

    A credential failure advances to the NEIGHBOUR — first the spare key of the same provider,
    and only then the other provider. An Anthropic upstream failure skips remaining Anthropic
    keys straight to OpenAI: 5xx / timeout belong to the upstream, and a second account of the
    same org would hit the same outage.
    """
    if is_credential_failure(exc):
        return index + 1 if index + 1 < len(attempts) else None
    if attempts[index].provider == _ANTHROPIC and _is_anthropic_upstream_failure(exc):
        for following in range(index + 1, len(attempts)):
            if attempts[following].provider != _ANTHROPIC:
                return following
    return None


def is_credential_failure(exc: BaseException) -> bool:
    """``True`` ⇔ the failure is the KEY's state (money or access), not the request itself.

    Network failures and timeouts do not count: the upstream did not answer, so nothing is
    known about the key. Rotating on them would treat a network blip as an org ban.
    """
    status, body = _status_and_body(exc)
    if status is None:
        return False
    if status in _CREDENTIAL_STATUS:
        return True
    if status not in (400, 429):
        return False

    error = _error_object(body)
    machine_codes = {
        value.strip().lower()
        for field in ("type", "code")
        for value in (error.get(field),)
        if isinstance(value, str)
    }
    if machine_codes & _QUOTA_MARKERS:
        return True
    if machine_codes & _RATE_LIMIT_MARKERS:
        return False

    message = error.get("message")
    haystack = message.lower() if isinstance(message, str) else body.lower()
    return any(marker in haystack for marker in _QUOTA_TEXT_MARKERS)


def _is_anthropic_upstream_failure(exc: BaseException) -> bool:
    """Any Anthropic-side transport/status error that is not a credential failure.

    ``AnthropicAuthError`` is already a credential failure (tried the spare Anthropic key first).
    ``UpstreamError`` wraps timeout / connection / 5xx / other status from the Anthropic client.
    """
    if isinstance(exc, AnthropicAuthError):
        return False
    for item in _walk(exc):
        if isinstance(item, UpstreamError):
            return True
        if type(item).__name__ in {"APITimeoutError", "APIConnectionError", "APIStatusError"}:
            return True
    return False


def _walk(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


def _status_and_body(exc: BaseException) -> tuple[int | None, str]:
    saw_auth = False
    auth_text = ""
    for item in _walk(exc):
        if isinstance(item, _AUTH_ERRORS):
            saw_auth = True
            auth_text = str(item)
        status, body = _status_from_attrs(item)
        if status is not None:
            return status, body
    if saw_auth:
        return 401, auth_text
    return None, str(exc)


def _status_from_attrs(item: BaseException) -> tuple[int | None, str]:
    # AppError.status_code is OUR HTTP mapping (502 for UpstreamError), not the provider's.
    if isinstance(item, AppError):
        return None, str(item)
    status = getattr(item, "status_code", None)
    if not isinstance(status, int):
        return None, str(item)
    return status, _body_of(item)


def _body_of(item: BaseException) -> str:
    raw = getattr(item, "body", None)
    if isinstance(raw, dict):
        return json.dumps(raw)
    if isinstance(raw, str) and raw:
        return raw
    response = getattr(item, "response", None)
    text = getattr(response, "text", None) if response is not None else None
    if isinstance(text, str) and text:
        return text
    return str(item)


def _error_object(body: str) -> dict[str, object]:
    """``{"error": {...}}`` from an upstream body; ``{}`` if missing or unparseable."""
    try:
        start = body.index("{")
        end = body.rindex("}") + 1
        payload = json.loads(body[start:end])
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    return error if isinstance(error, dict) else {}
