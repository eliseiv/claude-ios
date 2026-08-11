"""Integration: turn-scoped `quiz`, continuation, replay, legacy and capabilities (ADR-064 §7/§10).

Real PostgreSQL container; the provider is faked at the client boundary. Everything runs through
the working HTTP path (`/v1/chat/v2/run`, `/v1/chat/v2/tool-result`, `/v1/chat/run`,
`GET /v1/chat/v2/capabilities`).

Covers 09-testing.md §Study & Learn → «Integration — continuation, реплей, legacy, capabilities»
plus the `blocked`+`max_tokens` row of the previous subsection (its assertion is turn-scoped by
normative requirement: a pool produced on the `run` leg must still be in `quiz` when the turn is
truncated on the `tool-result` leg — the per-call assertion is explicitly FORBIDDEN there).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_QUIZ_WIRE = "quiz_generate"
_QUIZ_DOMAIN = "quiz.generate"
_STUDY_LEARN_PRICE = 4


@pytest.fixture
def study_learn_price() -> Iterator[int]:
    settings = get_settings()
    original = settings.chat_credit_cost_study_learn
    settings.chat_credit_cost_study_learn = _STUDY_LEARN_PRICE
    yield _STUDY_LEARN_PRICE
    settings.chat_credit_cost_study_learn = original


def _pool(count: int = 3, *, tag: str = "A") -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": f"[{tag}] Question {i}?",
                "options": [f"[{tag}] a{i}", f"[{tag}] b{i}"],
                "correctIndex": i % 2,
                "explanation": f"[{tag}] why {i}.",
            }
            for i in range(count)
        ]
    }


def _usage() -> Any:
    from app.chat.anthropic_client import AnthropicUsage

    return AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def _tool_use_result(
    calls: list[tuple[str, str, dict[str, Any], str]], *, text_block: str = ""
) -> Any:
    """One assistant turn with N tool_use blocks: (wire_name, domain_name, args, provider_id).

    Production shape: ``content_blocks`` (persisted verbatim) carry the UNDERSCORE wire names and
    the raw ``toolu_...`` ids; ``tool_uses`` (which drives the orchestrator) carry DOMAIN names.
    """
    from app.chat.anthropic_client import AnthropicResult

    blocks: list[dict[str, Any]] = []
    if text_block:
        blocks.append({"type": "text", "text": text_block})
    tool_uses: list[dict[str, Any]] = []
    for wire, domain, args, provider_id in calls:
        blocks.append({"type": "tool_use", "id": provider_id, "name": wire, "input": args})
        tool_uses.append({"id": provider_id, "name": domain, "input": args})
    return AnthropicResult(
        stop_reason="tool_use",
        content_blocks=blocks,
        usage=_usage(),
        text=text_block,
        tool_uses=tool_uses,
    )


def _quiz_call(
    pool: dict[str, Any], provider_id: str = "toolu_quiz01"
) -> tuple[str, str, Any, str]:
    return (_QUIZ_WIRE, _QUIZ_DOMAIN, pool, provider_id)


def _files_read_call(
    path: str = "notes.txt", provider_id: str = "toolu_files01"
) -> tuple[str, str, Any, str]:
    return ("files_read", "files.read", {"path": path}, provider_id)


async def _run(
    client: AsyncClient,
    uid: uuid.UUID,
    *,
    message: str = "teach me",
    session_id: str | None = None,
    generation_mode: str = "study_learn",
) -> Any:
    body: dict[str, Any] = {
        "userId": str(uid),
        "message": message,
        "mode": "credits",
        "generationMode": generation_mode,
    }
    if session_id is not None:
        body["sessionId"] = session_id
    return await client.post("/v1/chat/v2/run", json=body, headers=auth_headers(uid))


async def _tool_result(
    client: AsyncClient, uid: uuid.UUID, session_id: str, tool_call_id: str
) -> Any:
    return await client.post(
        "/v1/chat/v2/tool-result",
        json={
            "userId": str(uid),
            "sessionId": session_id,
            "toolCallId": tool_call_id,
            "result": {"content": "notes"},
        },
        headers=auth_headers(uid),
    )


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
            or 0
        )


async def _debit_count(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(
                text("SELECT count(*) FROM ledger_transactions WHERE user_id=:u AND type='debit'"),
                {"u": str(uid)},
            )
            or 0
        )


# ================================ continuation ==============================================
@pytest.mark.asyncio
async def test_continuation_inherits_the_mode_tool_set_and_price(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    study_learn_price: int,
) -> None:
    """diff: fails if `study_learn` is missing from generation_mode_for_message_step's whitelist.

    Such a turn degrades SILENTLY to `general`: the continuation round would neither be offered
    quiz.generate (so no pool at all) nor be charged the study_learn price.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    pool = _pool(3, tag="CONT")
    fake_anthropic.responses = [_tool_use_result([_files_read_call()])]

    run = (await _run(client, uid)).json()
    assert run["status"] == "tool_call"
    assert run["quiz"] is None  # no pool produced yet on this leg

    fake_anthropic.responses = [
        _tool_use_result([_quiz_call(pool)]),
        fake_anthropic.text_result("continued"),
    ]
    cont = (await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])).json()

    # (а) the continuation round was offered the mode-gated tool …
    assert _QUIZ_WIRE in {t["name"] for t in fake_anthropic.calls[-1]["tools"]}
    # (б) … was charged the study_learn price …
    assert await _balance(db_sessionmaker, uid) == 20 - study_learn_price
    assert cont["usage"]["generationMode"] == "study_learn"
    # (в) … and the pool produced on this leg reached the client.
    assert cont["quiz"] == pool
    assert cont["assistantMessage"] is None


@pytest.mark.asyncio
async def test_quiz_from_run_leg_survives_into_the_tool_result_leg(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff, MAIN guarantee of §7: fails on a PER-CALL accumulator.

    With per-call semantics the second leg would return quiz=null → the suppression would not fire
    → the user would get the duplicated questions with the answers revealed, which is exactly what
    the rule exists to prevent.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    pool = _pool(4, tag="RUNLEG")
    # ONE assistant step: quiz.generate (server-side) + files.read (client-side).
    fake_anthropic.responses = [
        _tool_use_result(
            [_quiz_call(pool), _files_read_call()],
            text_block="Here are some questions; answer 2 is correct.",
        )
    ]

    run = (await _run(client, uid)).json()
    assert run["status"] == "tool_call"
    assert run["quiz"] == pool
    assert run["assistantMessage"] is None  # suppression fires on the tool_call status too

    fake_anthropic.responses = [fake_anthropic.text_result("Well done, the answer was option 2.")]
    cont = (await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])).json()

    assert cont["status"] == "assistant_message"
    assert cont["quiz"] == pool  # the pool of the TURN, not of this call
    assert cont["assistantMessage"] is None
    assert cont["messageStepId"] == run["messageStepId"]
    # Contrast (per-call indicator): serverTools of the second leg do NOT replay the quiz round.
    assert all(st["toolName"] != _QUIZ_DOMAIN for st in cont["serverTools"])


@pytest.mark.asyncio
async def test_turn_scope_does_not_leak_into_the_next_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [
        _tool_use_result([_quiz_call(_pool(3))]),
        fake_anthropic.text_result("first turn"),
    ]
    first = (await _run(client, uid)).json()
    assert first["quiz"] is not None

    fake_anthropic.responses = [fake_anthropic.text_result("second turn")]
    second = (
        await _run(
            client,
            uid,
            message="now plain chat",
            session_id=first["sessionId"],
            generation_mode="general",
        )
    ).json()

    assert second["messageStepId"] != first["messageStepId"]
    assert second["quiz"] is None
    assert second["assistantMessage"] == "second turn"


# ================================ idempotent replay =========================================
@pytest.mark.asyncio
async def test_idempotent_replay_returns_the_turn_quiz_but_not_server_tools(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    study_learn_price: int,
) -> None:
    """diff: fails where a replay returns quiz=null (a network retry would show the spoiler)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    pool = _pool(3, tag="REPLAY")
    fake_anthropic.responses = [_tool_use_result([_files_read_call()])]
    run = (await _run(client, uid)).json()
    fake_anthropic.responses = [
        _tool_use_result([_quiz_call(pool)]),
        fake_anthropic.text_result("Spoiler text that must stay hidden."),
    ]
    first = (await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])).json()
    assert first["quiz"] == pool
    calls_after_first = len(fake_anthropic.calls)

    replay = (await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])).json()

    assert replay["quiz"] == pool  # turn content is reconstructed …
    assert replay["assistantMessage"] is None  # … so the suppression fires here as well
    assert replay["serverTools"] == []  # … while the per-call indicator is NOT reconstructed
    assert replay["messageStepId"] == first["messageStepId"]
    assert len(fake_anthropic.calls) == calls_after_first  # the provider was not called again
    assert await _debit_count(db_sessionmaker, uid) == 1
    assert await _balance(db_sessionmaker, uid) == 20 - study_learn_price


@pytest.mark.asyncio
async def test_replay_of_a_turn_without_quiz_returns_null(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [_tool_use_result([_files_read_call()])]
    run = (await _run(client, uid)).json()
    fake_anthropic.responses = [fake_anthropic.text_result("plain answer")]
    await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])

    replay = (await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])).json()

    assert replay["quiz"] is None
    assert replay["assistantMessage"] == "plain answer"


# ================================ blocked legs ==============================================
@pytest.mark.asyncio
async def test_max_tokens_on_the_continuation_keeps_the_run_leg_pool(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Turn-scoped by requirement: the truncation happens on a LEG that produced no pool itself."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    pool = _pool(3, tag="TRUNC")
    fake_anthropic.responses = [_tool_use_result([_quiz_call(pool), _files_read_call()])]
    run = (await _run(client, uid)).json()
    assert run["quiz"] == pool

    fake_anthropic.responses = [
        fake_anthropic.max_tokens_result(text="Partial spoiler: the answer is ")
    ]
    cut = (await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])).json()

    assert cut["status"] == "blocked"
    assert cut["blockReason"] == "max_tokens"
    assert cut["quiz"] == pool  # the pool of the TURN, produced on the previous leg
    assert cut["assistantMessage"] is None  # the truncated partial text is suppressed too
    # Other ADR-025 rules unchanged: ids and usage present, no credit taken.
    assert cut["messageStepId"] is not None
    assert cut["stepId"] is not None
    assert cut["usage"] is not None
    assert await _debit_count(db_sessionmaker, uid) == 0


@pytest.mark.asyncio
async def test_policy_blocked_turn_has_null_quiz(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    study_learn_price: int,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=1, trial_used=True)
    fake_anthropic.responses = [fake_anthropic.text_result("never reached")]

    body = (await _run(client, uid)).json()

    assert body["status"] == "blocked"
    assert body["blockReason"] == "credits_empty"  # balance 1 < study_learn price
    assert body["quiz"] is None
    assert body["messageStepId"] is None


# ================================ fallback cost =============================================
@pytest.mark.asyncio
async def test_no_quiz_step_lookup_outside_study_learn_turns(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mode predicate keeps the extra read strictly on quiz turns.

    Asserted as «the quiz-step SELECT is not issued», not as a raw query count for the turn: the
    turn's generation mode is read on every v2 continuation by design, so counting all statements
    would fail for an unrelated reason.
    """
    from app.chat.repository import ChatRepository

    lookups = {"n": 0}
    original = ChatRepository.last_tool_result_for_message_step

    async def _spy(
        self: ChatRepository, session_id: uuid.UUID, message_step_id: uuid.UUID, tool_name: str
    ) -> dict[str, Any] | None:
        # Count only quiz recovery (ADR-064). media.ask_params may also use this helper (ADR-070).
        if tool_name == _QUIZ_DOMAIN:
            lookups["n"] += 1
        return await original(self, session_id, message_step_id, tool_name)

    monkeypatch.setattr(ChatRepository, "last_tool_result_for_message_step", _spy)

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    # v2 turn in `general`, including a client-side tool round (run + continuation legs).
    fake_anthropic.responses = [_tool_use_result([_files_read_call()])]
    run = (await _run(client, uid, generation_mode="general")).json()
    fake_anthropic.responses = [fake_anthropic.text_result("plain")]
    await _tool_result(client, uid, run["sessionId"], run["toolCall"]["id"])
    assert lookups["n"] == 0

    # Legacy turn (forced `general`, no v2 mode at all).
    fake_anthropic.responses = [fake_anthropic.text_result("legacy")]
    legacy = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert legacy.status_code == 200
    assert lookups["n"] == 0

    # Control: a study_learn turn whose call produced no pool DOES consult the turn's steps.
    fake_anthropic.responses = [fake_anthropic.text_result("study")]
    assert (await _run(client, uid)).status_code == 200
    assert lookups["n"] == 1


# ================================ legacy contract ===========================================
@pytest.mark.asyncio
async def test_legacy_run_is_untouched_by_the_mode_gated_tool(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff: fails if axis C is computed from the REQUEST field instead of the effective mode."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [fake_anthropic.text_result("legacy answer")]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )

    assert r.status_code == 200, r.text
    body = r.json()
    # (а) the tool was never offered to the provider on the legacy path …
    assert _QUIZ_WIRE not in {t["name"] for t in fake_anthropic.calls[-1]["tools"]}
    # (б) … the additive field is present but always null …
    assert body["quiz"] is None
    assert body["assistantMessage"] == "legacy answer"
    # (в) … and the fixed legacy price is charged.
    assert await _balance(db_sessionmaker, uid) == 19


@pytest.mark.asyncio
async def test_legacy_request_still_rejects_generation_mode(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    r = await client.post(
        "/v1/chat/run",
        json={
            "userId": str(uid),
            "message": "hi",
            "mode": "credits",
            "generationMode": "study_learn",
        },
        headers=auth_headers(uid),
    )

    assert r.status_code == 422, r.text


# ================================ capabilities ==============================================
# ADR-065 §1 REVISES ADR-064 §10: the four-element list is no longer the default. The advertised
# set is an env allowlist, fail-closed by default — an instance whose app cannot draw the quiz must
# not offer the mode, or the user pays 2 credits for an empty screen.


@pytest.fixture
def advertised_modes(request: pytest.FixtureRequest) -> Iterator[str]:
    """Override CHAT_ADVERTISED_GENERATION_MODES on the cached Settings singleton (restored)."""
    settings = get_settings()
    original = settings.chat_advertised_generation_modes_raw
    settings.chat_advertised_generation_modes_raw = request.param
    yield request.param
    settings.chat_advertised_generation_modes_raw = original


async def _capability_modes(client: AsyncClient, uid: uuid.UUID) -> list[dict[str, Any]]:
    r = await client.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))
    assert r.status_code == 200, r.text
    return list(r.json()["generationModes"])


@pytest.mark.asyncio
async def test_capabilities_default_is_fail_closed_without_study_learn(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """diff (ADR-065 §1.2): fails on the previous literal four-element list.

    The check is ABSENCE of the element, not `available: false` — the clients this protects are
    already-released binaries that may ignore that field entirely (§1.1/§1.8).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    modes = await _capability_modes(client, uid)

    assert [m["mode"] for m in modes] == ["general", "research", "reasoning"]
    assert all(m["mode"] != "study_learn" for m in modes)
    assert all(m["available"] is True for m in modes)


@pytest.mark.parametrize(
    ("advertised_modes", "expected"),
    [
        # Explicit full list → four elements, study_learn last.
        (
            "general,research,reasoning,study_learn",
            ["general", "research", "reasoning", "study_learn"],
        ),
        # §1.3: `general` is added even when the operator omits it.
        ("study_learn", ["general", "study_learn"]),
        # §1.5: canonical order, never the order typed in env.
        ("study_learn,general", ["general", "study_learn"]),
        # §1.4: unknown values are ignored — WARNING, not a startup crash.
        ("general,nope,research", ["general", "research"]),
        # Blank / entirely invalid → the fail-closed default set.
        ("   ", ["general", "research", "reasoning"]),
        ("nope,alsonope", ["general", "research", "reasoning"]),
    ],
    indirect=["advertised_modes"],
)
@pytest.mark.asyncio
async def test_capabilities_advertisement_allowlist(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    advertised_modes: str,
    expected: list[str],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    modes = await _capability_modes(client, uid)

    assert [m["mode"] for m in modes] == expected
    assert "general" in expected  # §1.3 invariant, restated where it is easy to break
    assert all(m["available"] is True for m in modes)  # §1.8: `available` is never the gate


@pytest.mark.parametrize(
    "advertised_modes", ["general,research,reasoning,study_learn"], indirect=True
)
@pytest.mark.asyncio
async def test_capabilities_lists_study_learn_last_with_the_env_price(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    study_learn_price: int,
    advertised_modes: str,
) -> None:
    """diff: fails if creditCost is hardcoded instead of coming from the single pricing bridge.

    Requires the advertising env (ADR-065 §1): without it the element is absent altogether.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    modes = await _capability_modes(client, uid)

    assert [m["mode"] for m in modes] == ["general", "research", "reasoning", "study_learn"]
    study = modes[-1]
    assert study["creditCost"] == study_learn_price  # follows the env the debit uses
    assert study["available"] is True
    assert all(m["available"] is True for m in modes)


@pytest.mark.asyncio
async def test_unadvertised_mode_still_runs_advertisement_is_not_a_behaviour_gate(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    study_learn_price: int,
) -> None:
    """diff, KEY separation (ADR-065 §1.6): fails if the allowlist is applied to request validation.

    The env is left at its DEFAULT here — `study_learn` is NOT advertised — and the very same turn
    must still work end to end: an app that knows the mode name keeps working on every instance.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    assert "study_learn" not in get_settings().advertised_generation_modes()  # precondition
    pool = _pool(3, tag="UNADVERTISED")
    fake_anthropic.responses = [
        _tool_use_result([_quiz_call(pool)]),
        fake_anthropic.text_result("done"),
    ]

    r = await _run(client, uid)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] == pool  # the quiz is produced, not refused
    assert body["usage"]["generationMode"] == "study_learn"
    assert (
        await _balance(db_sessionmaker, uid) == 20 - study_learn_price
    )  # its own price is charged


@pytest.mark.asyncio
async def test_capabilities_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/v1/chat/v2/capabilities")).status_code == 401
