"""Unit coverage for turn-scoped chat generation modes.

``generationMode`` is an API-level single-select for one user turn. It is separate from
``mode=credits|byok`` billing mode and from session-fixed ``assistantMode``. These tests keep the
schema/config/policy contract small and explicit before the integration tests exercise persistence
and wallet debits.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.policy.engine import (
    BlockReason,
    ByokState,
    Mode,
    PolicyState,
    SubscriptionStatus,
    evaluate,
)
from app.schemas.chat import ChatRunRequest, ChatV2RunRequest

_UID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _run_payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"userId": str(_UID), "message": "hi", "mode": "credits"}
    base.update(overrides)
    return base


def test_generation_mode_defaults_to_general() -> None:
    req = ChatV2RunRequest.model_validate(_run_payload())
    assert req.generationMode == "general"


@pytest.mark.parametrize("mode", ["general", "research", "reasoning"])
def test_generation_mode_accepts_supported_values(mode: str) -> None:
    req = ChatV2RunRequest.model_validate(_run_payload(generationMode=mode))
    assert req.generationMode == mode


def test_generation_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        ChatV2RunRequest.model_validate(_run_payload(generationMode="deep_research"))


def test_legacy_chat_run_request_rejects_generation_mode() -> None:
    with pytest.raises(ValidationError):
        ChatRunRequest.model_validate(_run_payload(generationMode="research"))


def test_generation_mode_credit_costs_are_configurable_positive_values() -> None:
    settings = Settings(
        CHAT_CREDIT_COST_GENERAL=2,
        CHAT_CREDIT_COST_RESEARCH=5,
        CHAT_CREDIT_COST_REASONING=7,
    )

    assert settings.chat_generation_credit_cost("general") == 2
    assert settings.chat_generation_credit_cost("research") == 5
    assert settings.chat_generation_credit_cost("reasoning") == 7
    assert settings.chat_generation_credit_cost("unknown") == 2


def test_generation_mode_credit_costs_fallback_to_one_when_misconfigured() -> None:
    settings = Settings(
        CHAT_CREDIT_COST_GENERAL=0,
        CHAT_CREDIT_COST_RESEARCH=-10,
        CHAT_CREDIT_COST_REASONING=0,
    )

    assert settings.chat_generation_credit_cost("general") == 1
    assert settings.chat_generation_credit_cost("research") == 1
    assert settings.chat_generation_credit_cost("reasoning") == 1


def test_policy_blocks_active_credits_when_balance_below_required_cost() -> None:
    state = PolicyState(
        subscription_status=SubscriptionStatus.active,
        trial_used=True,
        credits_balance=2,
        byok_enabled=False,
        byok_status=ByokState.missing,
    )

    decision = evaluate(state, Mode.credits, required_credits=3)

    assert decision.allow is False
    assert decision.block_reason is BlockReason.credits_empty


def test_policy_allows_active_credits_when_balance_covers_required_cost() -> None:
    state = PolicyState(
        subscription_status=SubscriptionStatus.active,
        trial_used=True,
        credits_balance=3,
        byok_enabled=False,
        byok_status=ByokState.missing,
    )

    assert evaluate(state, Mode.credits, required_credits=3).allow is True
