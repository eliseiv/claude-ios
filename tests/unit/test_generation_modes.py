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
from app.instance_config.snapshot import EMPTY_SNAPSHOT
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


def test_temporary_defaults_to_false_on_v2() -> None:
    req = ChatV2RunRequest.model_validate(_run_payload())
    assert req.temporary is False


def test_temporary_accepts_true_on_v2() -> None:
    req = ChatV2RunRequest.model_validate(_run_payload(temporary=True))
    assert req.temporary is True


def test_legacy_chat_run_request_accepts_5115_compat_fields() -> None:
    req = ChatRunRequest.model_validate(
        _run_payload(
            dialogMode="smart",
            temporary=True,
            actionPrompt="Explain simpler",
            history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        )
    )
    assert req.dialogMode == "smart"
    assert req.temporary is True
    assert req.actionPrompt == "Explain simpler"
    assert req.history is not None and len(req.history) == 2


def test_v2_run_request_accepts_5115_compat_fields() -> None:
    req = ChatV2RunRequest.model_validate(
        _run_payload(dialogMode="search", actionPrompt="x", history=[], temporary=False)
    )
    assert req.dialogMode == "search"
    assert req.actionPrompt == "x"
    assert req.history == []


def test_action_prompt_alone_is_valid_turn() -> None:
    req = ChatRunRequest.model_validate(
        {"userId": str(_UID), "mode": "credits", "message": "", "actionPrompt": "Summarize"}
    )
    assert req.effective_user_text() == "Summarize"


def test_turn_price_is_a_function_of_the_model_not_of_the_generation_mode() -> None:
    """ADR-099 §5: решение владельца №1 сняло надбавку за режим.

    Мост цены остался ЕДИНСТВЕННЫМ (ADR-064 §9), сменился только его аргумент: раньше цену давал
    режим, теперь — модель. Кейс diff-стойкий с двух сторон: он падает и если резолвер вернётся к
    `CHAT_CREDIT_COST_RESEARCH`/`_REASONING` (значения выбраны заведомо разными), и если он
    перестанет читать `CHAT_CREDIT_COST_GENERAL` как дефолт строки тарифа.
    """
    from app.instance_config import chat_turn_credit_cost

    settings = Settings(
        CHAT_CREDIT_COST_GENERAL=2,
        CHAT_CREDIT_COST_RESEARCH=5,
        CHAT_CREDIT_COST_REASONING=7,
    )

    price = chat_turn_credit_cost(None, settings=settings, snapshot=EMPTY_SNAPSHOT)
    assert price == 2
    for model_id in settings.allowed_models_union():
        assert chat_turn_credit_cost(model_id, settings=settings, snapshot=EMPTY_SNAPSHOT) == 2


def test_generation_mode_credit_costs_fallback_to_one_when_misconfigured() -> None:
    """Валидатор положительности остаётся: из `CHAT_CREDIT_COST_*` берётся дефолт строки тарифа.

    Ноль не даёт ни ошибки старта, ни блокировки — гейт баланса проходит, дебит списывает ноль,
    и ход тихо становится бесплатным (ADR-099 §5.1).
    """
    from app.instance_config import chat_turn_credit_cost

    settings = Settings(
        CHAT_CREDIT_COST_GENERAL=0,
        CHAT_CREDIT_COST_RESEARCH=-10,
        CHAT_CREDIT_COST_REASONING=0,
    )

    assert settings.chat_credit_cost_general == 1
    assert settings.chat_credit_cost_research == 1
    assert settings.chat_credit_cost_reasoning == 1
    assert chat_turn_credit_cost(None, settings=settings, snapshot=EMPTY_SNAPSHOT) == 1


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
