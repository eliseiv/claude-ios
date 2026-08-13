"""Provider-key failover: classifier, attempt chain, rotation reasons (ADR-074).

Mirrors 232 ``test_provider_key_failover``: the successful generation looks the same
regardless of which key served it, so these tests assert order and the reason to move.
"""

from __future__ import annotations

import json

import pytest

from app.chat.anthropic_client import AnthropicAuthError
from app.chat.key_failover import (
    Attempt,
    build_attempt_chain,
    is_credential_failure,
    next_attempt_index,
)
from app.chat.openai_client import OpenAIAuthError
from app.config import Settings
from app.errors import UpstreamError, ValidationFailedError

OPENAI_PRIMARY = "sk-proj-primary-do-not-reuse"
OPENAI_BACKUP = "sk-proj-backup-do-not-reuse"
ANTHROPIC_PRIMARY = "sk-ant-primary-do-not-reuse"
ANTHROPIC_BACKUP = "sk-ant-backup-do-not-reuse"

OPENAI_NO_CREDITS = json.dumps(
    {
        "error": {
            "message": "You have no credits remaining. Add credits to continue using the API.",
            "type": "insufficient_quota",
            "code": "credit_balance_exhausted",
        }
    }
)
OPENAI_RATE_LIMIT = json.dumps(
    {
        "error": {
            "message": "Rate limit reached. Visit https://platform.openai.com/account/billing.",
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }
    }
)
OPENAI_BAD_REQUEST = json.dumps(
    {"error": {"message": "Invalid input: expected a string", "type": "invalid_request_error"}}
)
ANTHROPIC_NO_CREDITS = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Your credit balance is too low to access the Anthropic API.",
        },
    }
)


class _Status(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(body)
        self.status_code = status_code
        self.body = body


def _wrapped_upstream(status: int, body: str) -> UpstreamError:
    exc = UpstreamError("openai upstream error")
    exc.__cause__ = _Status(status, body)
    return exc


def _both_keys(**extra: object) -> Settings:
    payload: dict[str, object] = {
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": OPENAI_PRIMARY,
        "OPENAI_API_KEY_BACKUP": OPENAI_BACKUP,
        "ANTHROPIC_API_KEY": ANTHROPIC_PRIMARY,
        "ANTHROPIC_API_KEY_BACKUP": ANTHROPIC_BACKUP,
        "OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL": "claude-sonnet-4-5",
        "ANTHROPIC_CHAT_FALLBACK_OPENAI_MODEL": "gpt-4o",
        "OPENAI_MODEL": "gpt-4o",
        "ANTHROPIC_MODEL": "claude-sonnet-4-5",
        "OPENAI_MODELS": json.dumps({"gpt-4o": "GPT-4o"}),
        "ANTHROPIC_MODELS": json.dumps({"claude-sonnet-4-5": "Sonnet"}),
        "LLM_PROVIDERS": "",
    }
    payload.update(extra)
    return Settings(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "body", "expected", "why"),
    [
        (401, '{"error":{"type":"authentication_error"}}', True, "ключ отозван"),
        (403, '{"error":{"type":"permission_error"}}', True, "организация заблокирована"),
        (429, OPENAI_NO_CREDITS, True, "кончились средства"),
        (400, ANTHROPIC_NO_CREDITS, True, "Anthropic сообщает деньги кодом 400"),
        (429, OPENAI_RATE_LIMIT, False, "лимит частоты — не повод менять ключ"),
        (400, OPENAI_BAD_REQUEST, False, "ошибка формы повторится на любом ключе"),
        (500, "upstream is down", False, "сбой апстрима ничего не говорит о ключе"),
        (404, '{"error":{"type":"not_found_error"}}', False, "объекта нет — ключ ни при чём"),
        (429, "not a json at all", False, "неразбираемое тело не опознаётся как отказ ключа"),
        (429, '{"error":"boom"}', False, "`error` строкой, а не объектом — разбор не падает"),
        (429, '{"detail":"nope"}', False, "тело без объекта `error` вовсе"),
    ],
)
def test_only_money_and_access_count_as_a_credential_failure(
    status: int, body: str, expected: bool, why: str
) -> None:
    assert is_credential_failure(_wrapped_upstream(status, body)) is expected, why


def test_auth_error_classes_count_as_credential_failures() -> None:
    assert is_credential_failure(OpenAIAuthError("unauthorized")) is True
    assert is_credential_failure(AnthropicAuthError("unauthorized")) is True


def test_a_network_failure_is_not_a_credential_failure() -> None:
    assert is_credential_failure(ConnectionError("no route to host")) is False
    assert is_credential_failure(TimeoutError("timed out")) is False
    assert is_credential_failure(UpstreamError("openai upstream error")) is False


def test_the_chain_puts_both_own_keys_before_the_other_provider() -> None:
    chain = build_attempt_chain("gpt-4o", settings=_both_keys())
    assert [(a.provider, a.key_index) for a in chain] == [
        ("openai", 0),
        ("openai", 1),
        ("anthropic", 0),
        ("anthropic", 1),
    ]
    assert [a.api_key for a in chain[:2]] == [OPENAI_PRIMARY, OPENAI_BACKUP]
    assert [a.model for a in chain] == [None, None, "claude-sonnet-4-5", "claude-sonnet-4-5"]


def test_the_chain_is_mirrored_for_a_claude_model() -> None:
    settings = _both_keys(LLM_PROVIDER="anthropic", LLM_PROVIDERS="openai")
    chain = build_attempt_chain("claude-sonnet-4-5", settings=settings)
    assert [(a.provider, a.key_index) for a in chain] == [
        ("anthropic", 0),
        ("anthropic", 1),
        ("openai", 0),
        ("openai", 1),
    ]
    assert [a.model for a in chain[2:]] == ["gpt-4o", "gpt-4o"]


def test_without_a_crossover_model_the_chain_stays_inside_one_provider() -> None:
    settings = _both_keys(OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL="")
    chain = build_attempt_chain("gpt-4o", settings=settings)
    assert [a.provider for a in chain] == ["openai", "openai"]


def test_a_duplicated_backup_key_does_not_add_a_candidate() -> None:
    settings = _both_keys(
        OPENAI_API_KEY_BACKUP=OPENAI_PRIMARY, OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL=""
    )
    chain = build_attempt_chain("gpt-4o", settings=settings)
    assert len(chain) == 1


def test_empty_key_chain_still_yields_one_candidate() -> None:
    settings = Settings(
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="",
        OPENAI_API_KEY_BACKUP="",
        OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL="",
        LLM_PROVIDERS="",
    )
    chain = build_attempt_chain(None, settings=settings)
    assert len(chain) == 1
    assert chain[0].api_key is None
    assert chain[0].provider == "openai"


def test_backup_alias_open_ai_back_up_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_PRIMARY)
    monkeypatch.delenv("OPENAI_API_KEY_BACKUP", raising=False)
    monkeypatch.setenv("OPEN_AI_BACK_UP_API_KEY", OPENAI_BACKUP)
    monkeypatch.setenv("OPENAI_CHAT_FALLBACK_ANTHROPIC_MODEL", "")
    monkeypatch.setenv("LLM_PROVIDERS", "")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    settings = Settings()
    assert settings.openai_api_key_chain() == (OPENAI_PRIMARY, OPENAI_BACKUP)


def test_backup_alias_anthropic_fallback_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_PRIMARY)
    monkeypatch.delenv("ANTHROPIC_API_KEY_BACKUP", raising=False)
    monkeypatch.setenv("ANTHROPIC_FALLBACK_API_KEY", ANTHROPIC_BACKUP)
    monkeypatch.setenv("ANTHROPIC_CHAT_FALLBACK_OPENAI_MODEL", "")
    monkeypatch.setenv("LLM_PROVIDERS", "")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    settings = Settings()
    assert settings.anthropic_api_key_chain() == (ANTHROPIC_PRIMARY, ANTHROPIC_BACKUP)


def test_llm_provider_stays_the_catalog_default_when_failover_keys_are_set() -> None:
    """Failover keys do not turn on dual-credits (ADR-073 opt-in stays LLM_PROVIDERS)."""
    settings = _both_keys()
    assert settings.credits_providers() == ("openai",)
    assert settings.credits_provider_for_model(None) == "openai"


def _chain() -> tuple[Attempt, ...]:
    return build_attempt_chain("gpt-4o", settings=_both_keys())


def test_credential_failure_rotates_to_the_next_key() -> None:
    chain = _chain()
    assert next_attempt_index(chain, 0, OpenAIAuthError("unauthorized")) == 1
    assert next_attempt_index(chain, 1, _wrapped_upstream(429, OPENAI_NO_CREDITS)) == 2


def test_a_plain_rate_limit_does_not_rotate() -> None:
    chain = _chain()
    assert next_attempt_index(chain, 0, _wrapped_upstream(429, OPENAI_RATE_LIMIT)) is None


def test_an_openai_request_error_does_not_cross_over() -> None:
    chain = _chain()
    assert next_attempt_index(chain, 0, _wrapped_upstream(400, OPENAI_BAD_REQUEST)) is None


def test_an_anthropic_upstream_failure_skips_straight_to_openai() -> None:
    settings = _both_keys(LLM_PROVIDER="anthropic", LLM_PROVIDERS="openai")
    chain = build_attempt_chain("claude-sonnet-4-5", settings=settings)
    assert [a.provider for a in chain] == ["anthropic", "anthropic", "openai", "openai"]
    following = next_attempt_index(chain, 0, UpstreamError("anthropic upstream error"))
    assert following == 2
    assert chain[following].provider == "openai"


def test_validation_error_does_not_rotate() -> None:
    chain = _chain()
    assert next_attempt_index(chain, 0, ValidationFailedError("bad attachment")) is None
