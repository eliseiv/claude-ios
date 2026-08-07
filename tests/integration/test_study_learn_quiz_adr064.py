"""Integration: Study & Learn end-to-end path, degrade and guards (ADR-064).

Real PostgreSQL container; the provider is faked at the client boundary (conftest
``FakeAnthropicClient``), so no network and placeholder API keys are enough. Every assertion here
runs through the WORKING path — an HTTP call to ``/v1/chat/v2/run`` (or ``/v1/chat/run`` for the
legacy contrast) — because 09-testing.md §Study & Learn refuses component-only coverage: a test that
builds a pool itself proves the validator, not the chain producer → consumer.

Covered rows of 09-testing.md §Study & Learn:
- Integration — сквозной путь: pool reaches the client, the tool is really offered to the provider,
  assistantMessage suppression (+ its reverse), history untouched, mode price debited.
- Integration — degrade и guard: invalid pool degrades (turn survives), the neighbouring branch
  still 422s, MAX_SERVER_TOOL_ROUNDS bounds the model's persistence, out-of-mode call is softly
  refused, mode check wins over args validation, and the site.* guard stays HARD.

Turn-scope / continuation / replay / legacy / capabilities live in
tests/integration/test_study_learn_turn_scope_adr064.py.
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

_WIRE_NAME = "quiz_generate"
_DOMAIN_NAME = "quiz.generate"

# A distinctive price: different from general (1) AND from research/reasoning (3), so a debit that
# used the wrong mode's price is visible instead of accidentally matching.
_STUDY_LEARN_PRICE = 4


@pytest.fixture
def study_learn_price() -> Iterator[int]:
    """Force CHAT_CREDIT_COST_STUDY_LEARN to a value no other mode uses (restored after)."""
    settings = get_settings()
    original = settings.chat_credit_cost_study_learn
    settings.chat_credit_cost_study_learn = _STUDY_LEARN_PRICE
    yield _STUDY_LEARN_PRICE
    settings.chat_credit_cost_study_learn = original


def _pool(count: int = 4, *, tag: str = "A") -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": f"[{tag}] Question {i}?",
                "options": [f"[{tag}] option {i}.0", f"[{tag}] option {i}.1"],
                "correctIndex": i % 2,
                "explanation": f"[{tag}] because {i}.",
            }
            for i in range(count)
        ]
    }


def _quiz_result(
    pool: dict[str, Any],
    *,
    provider_id: str = "toolu_quiz01",
    text_block: str = "",
) -> Any:
    """A provider turn calling quiz.generate, shaped exactly like production parsing.

    ``content_blocks`` (stored verbatim in chat_steps.payload) carry the UNDERSCORE wire name +
    the raw ``toolu_...`` id — the invariant of the fake in 09-testing.md; ``tool_uses`` (which
    drives the orchestrator) carries the DOMAIN dotted name, as the real client reverse-maps it.
    """
    from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

    usage = AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model="claude-sonnet-4-5",
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    blocks: list[dict[str, Any]] = []
    if text_block:
        blocks.append({"type": "text", "text": text_block})
    blocks.append({"type": "tool_use", "id": provider_id, "name": _WIRE_NAME, "input": pool})
    return AnthropicResult(
        stop_reason="tool_use",
        content_blocks=blocks,
        usage=usage,
        text=text_block,
        tool_uses=[{"id": provider_id, "name": _DOMAIN_NAME, "input": pool}],
    )


async def _run_study_learn(
    client: AsyncClient,
    uid: uuid.UUID,
    *,
    message: str = "teach me fractions",
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


def _wire_tool_names(fake: FakeAnthropicClient, index: int = -1) -> set[str]:
    return {t["name"] for t in fake.calls[index]["tools"]}


def _server_tools(body: dict[str, Any], tool_name: str = _DOMAIN_NAME) -> list[dict[str, Any]]:
    return [st for st in body.get("serverTools") or [] if st["toolName"] == tool_name]


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
            or 0
        )


# ================================ сквозной путь ==============================================
@pytest.mark.asyncio
async def test_quiz_pool_reaches_the_client(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff: fails if `quiz` is not filled on the working path (producer → consumer chain)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    pool = _pool(4)
    fake_anthropic.responses = [
        _quiz_result(pool, text_block="Fractions are parts of a whole."),
        fake_anthropic.text_result("Try the cards above."),
    ]

    r = await _run_study_learn(client, uid)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] is not None
    assert len(body["quiz"]["questions"]) == 4
    # Byte-for-byte the pool the model produced (validation + echo, no reshaping).
    assert body["quiz"] == pool
    # The server-side execution is surfaced compactly, with no quiz content in the summary.
    entries = _server_tools(body)
    assert len(entries) == 1
    assert entries[0]["status"] == "completed"
    assert entries[0]["summary"] == "ok"
    # quiz.generate is a server-side tool: it is never handed to iOS as a client tool call.
    assert body.get("toolCalls") in (None, [])
    assert body.get("toolCall") is None


@pytest.mark.asyncio
async def test_quiz_tool_offered_to_provider_only_in_study_learn_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff (axis C on the working path): the same session switches mode between turns."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [fake_anthropic.text_result("ok study")]

    r1 = await _run_study_learn(client, uid)
    assert r1.status_code == 200, r1.text
    session_id = r1.json()["sessionId"]
    assert _WIRE_NAME in _wire_tool_names(fake_anthropic)

    fake_anthropic.responses = [fake_anthropic.text_result("ok general")]
    r2 = await _run_study_learn(
        client, uid, message="now just chat", session_id=session_id, generation_mode="general"
    )
    assert r2.status_code == 200, r2.text
    assert _WIRE_NAME not in _wire_tool_names(fake_anthropic)
    # Sanity: the rest of the tool-set is untouched by the mode switch (axis C moved one tool).
    assert _wire_tool_names(fake_anthropic, 0) - {_WIRE_NAME} == _wire_tool_names(fake_anthropic, 1)


@pytest.mark.asyncio
async def test_system_suffix_reaches_provider_and_is_byte_stable(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff (wiring): the mode suffix must be in the system_prompt the CLIENT was called with."""
    from app.chat.orchestrator import _STUDY_LEARN_INSTRUCTION

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [fake_anthropic.text_result("first")]
    r1 = await _run_study_learn(client, uid)
    session_id = r1.json()["sessionId"]
    first_system = fake_anthropic.calls[-1]["system_prompt"]
    assert _STUDY_LEARN_INSTRUCTION in first_system

    fake_anthropic.responses = [fake_anthropic.text_result("second")]
    await _run_study_learn(client, uid, message="more", session_id=session_id)
    # Static suffix → byte-identical system across turns of the mode (prompt-cache stability).
    assert fake_anthropic.calls[-1]["system_prompt"] == first_system

    fake_anthropic.responses = [fake_anthropic.text_result("plain")]
    await _run_study_learn(
        client, uid, message="plain turn", session_id=session_id, generation_mode="general"
    )
    assert _STUDY_LEARN_INSTRUCTION not in fake_anthropic.calls[-1]["system_prompt"]


@pytest.mark.asyncio
async def test_assistant_message_suppressed_when_quiz_present(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff: fails on an implementation without the suppression in _to_response."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [
        _quiz_result(_pool(3)),
        fake_anthropic.text_result("Q1 answer is option 2 — spoiler text the user must not see."),
    ]

    body = (await _run_study_learn(client, uid)).json()

    assert body["quiz"] is not None
    assert body["assistantMessage"] is None


@pytest.mark.asyncio
async def test_study_learn_turn_without_quiz_keeps_its_text(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """The reverse side: the rule is keyed on the PRESENCE of a quiz, not on the mode."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [fake_anthropic.text_result("Here is the explanation.")]

    body = (await _run_study_learn(client, uid)).json()

    assert body["quiz"] is None
    assert body["assistantMessage"] == "Here is the explanation."


@pytest.mark.asyncio
async def test_history_of_a_quiz_turn_carries_the_pool_without_the_spoiler_text(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff, ADR-065 §2 (REVOKES ADR-064 §7): fails without the read-time strip.

    Motivating path: the OS unloads the app mid-quiz → cold start → history load. Without the strip
    the client restores the cards TOGETHER with the model's text that repeats the questions and
    reveals the answers — the live-response guarantee (ADR-064 §7) never covered this channel.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    pool = _pool(3)
    spoiler = "SPOILER-Q1-the-correct-option-is-2"
    fake_anthropic.responses = [
        _quiz_result(pool, text_block=spoiler),
        fake_anthropic.text_result("Final words with the answers again."),
    ]
    body = (await _run_study_learn(client, uid)).json()
    session_id = body["sessionId"]

    raw = await client.get(f"/v1/chats/{session_id}", headers=auth_headers(uid))
    history = raw.json()
    steps = history["steps"]

    # No text block survives on the assistant steps of the quiz turn …
    assistant_text_blocks = [
        block
        for step in steps
        if step["role"] == "assistant" and step["messageStepId"] == body["messageStepId"]
        for block in step["payload"].get("content", [])
        if block.get("type") == "text"
    ]
    assert assistant_text_blocks == []
    assert spoiler not in raw.text  # not through any other field either
    # … while the pool stays fully available, so the cards survive a cold start.
    tool_steps = [st for st in steps if st["role"] == "tool"]
    assert len(tool_steps) == 1
    assert tool_steps[0]["payload"]["toolName"] == _DOMAIN_NAME
    assert tool_steps[0]["payload"]["result"] == pool
    # tool_use blocks of the same assistant step are NOT touched (only text blocks are cut).
    tool_use_blocks = [
        block
        for step in steps
        if step["role"] == "assistant"
        for block in step["payload"].get("content", [])
        if block.get("type") == "tool_use"
    ]
    assert tool_use_blocks, "the tool_use block must survive the text strip"

    # Storage is NOT mutated — the strip happens on a copy at serialization time (like ADR-042),
    # and the provider replay keeps the full text.
    async with db_sessionmaker() as s:
        stored = (
            (
                await s.execute(
                    text(
                        "SELECT payload::text FROM chat_steps "
                        "WHERE session_id=:sid AND role='assistant' ORDER BY seq"
                    ),
                    {"sid": session_id},
                )
            )
            .scalars()
            .all()
        )
    assert any(spoiler in row for row in stored)


@pytest.mark.asyncio
async def test_history_strip_does_not_touch_non_quiz_turns(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """The trigger is a NEIGHBOURING step, so an ordinary turn keeps its text verbatim."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [
        _quiz_result(_pool(3), text_block="quiz turn text"),
        fake_anthropic.text_result("quiz turn final"),
    ]
    first = (await _run_study_learn(client, uid)).json()

    fake_anthropic.responses = [fake_anthropic.text_result("PLAIN-TURN-TEXT-KEPT")]
    second = (
        await _run_study_learn(
            client, uid, message="plain", session_id=first["sessionId"], generation_mode="general"
        )
    ).json()

    history = (
        await client.get(f"/v1/chats/{first['sessionId']}", headers=auth_headers(uid))
    ).json()
    plain_texts = [
        block.get("text")
        for step in history["steps"]
        if step["role"] == "assistant" and step["messageStepId"] == second["messageStepId"]
        for block in step["payload"].get("content", [])
        if block.get("type") == "text"
    ]
    assert plain_texts == ["PLAIN-TURN-TEXT-KEPT"]  # byte-for-byte


@pytest.mark.asyncio
async def test_history_keeps_text_when_the_quiz_round_only_errored(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """No VALID pool in the turn → it is not a quiz turn → nothing to spoil, nothing to strip."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    bad = _pool(3, tag="BAD")
    bad["questions"][0]["correctIndex"] = 99
    fake_anthropic.responses = [
        _quiz_result(bad, provider_id="toolu_baderr", text_block="TEXT-AFTER-FAILED-QUIZ"),
        fake_anthropic.text_result("recovered without a quiz"),
    ]
    body = (await _run_study_learn(client, uid)).json()
    assert body["quiz"] is None  # the turn produced no valid pool

    history = (await client.get(f"/v1/chats/{body['sessionId']}", headers=auth_headers(uid))).json()
    texts = [
        block.get("text")
        for step in history["steps"]
        if step["role"] == "assistant"
        for block in step["payload"].get("content", [])
        if block.get("type") == "text"
    ]
    assert "TEXT-AFTER-FAILED-QUIZ" in texts


@pytest.mark.asyncio
async def test_steps_view_does_not_leak_the_stripped_text_as_summary(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """The second read channel must not reintroduce the spoiler (ADR-065 §2)."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    spoiler = "SPOILER-IN-SUMMARY-answer-is-B"
    fake_anthropic.responses = [
        _quiz_result(_pool(3), text_block=spoiler),
        fake_anthropic.text_result("done"),
    ]
    body = (await _run_study_learn(client, uid)).json()

    steps_view = await client.get(f"/v1/chats/{body['sessionId']}/steps", headers=auth_headers(uid))

    assert steps_view.status_code == 200, steps_view.text
    assert spoiler not in steps_view.text


@pytest.mark.asyncio
async def test_study_learn_debits_its_own_price_once(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    study_learn_price: int,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [
        _quiz_result(_pool(3)),
        fake_anthropic.text_result("done"),
    ]

    body = (await _run_study_learn(client, uid)).json()

    assert body["usage"]["generationMode"] == "study_learn"
    assert await _balance(db_sessionmaker, uid) == 20 - study_learn_price
    # Exactly one debit for the turn, keyed by messageStepId (the server-side round adds none).
    async with db_sessionmaker() as s:
        debits = (
            await s.execute(
                text(
                    "SELECT amount, idempotency_key FROM ledger_transactions "
                    "WHERE user_id=:u AND type='debit'"
                ),
                {"u": str(uid)},
            )
        ).all()
    assert len(debits) == 1
    amount, key = debits[0]
    assert int(amount) == study_learn_price
    assert key == body["messageStepId"]


# ================================ degrade и guard ============================================
@pytest.mark.asyncio
async def test_invalid_pool_degrades_and_the_turn_survives(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff: fails (422) on an implementation where validate_tool_args kills the turn."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    bad = _pool(3, tag="BAD")
    bad["questions"][1]["correctIndex"] = len(bad["questions"][1]["options"])  # == out of range
    good = _pool(4, tag="GOOD")
    fake_anthropic.responses = [
        _quiz_result(bad, provider_id="toolu_bad01"),
        _quiz_result(good, provider_id="toolu_good1"),
        fake_anthropic.text_result("here you go"),
    ]

    r = await _run_study_learn(client, uid)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] == good  # the regenerated pool, not the rejected one
    entries = _server_tools(body)
    assert [(e["status"], e["summary"]) for e in entries] == [
        ("errored", "invalid_quiz"),
        ("completed", "ok"),
    ]
    # The model saw a content-free error as an ordinary tool result in the SAME turn.
    async with db_sessionmaker() as s:
        error_payload = await s.scalar(
            text(
                "SELECT payload->'error' FROM chat_steps WHERE session_id=:sid AND role='tool' "
                "AND payload->>'toolName'=:tn AND payload->'error' IS NOT NULL ORDER BY seq LIMIT 1"
            ),
            {"sid": body["sessionId"], "tn": _DOMAIN_NAME},
        )
    assert error_payload["code"] == "invalid_quiz"
    assert "[BAD]" not in error_payload["message"]  # no quiz content leaked into the echo


@pytest.mark.asyncio
async def test_bad_args_of_a_neighbouring_tool_still_422(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Contrast (must live next to the degrade test): the two `except` branches stay opposite."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    # files.write without `path` — args from a fixed schema, so malformed args ARE an anomaly.
    fake_anthropic.responses = [
        fake_anthropic.tool_result("files.write", {"content": "x", "encoding": "utf8"})
    ]

    r = await _run_study_learn(client, uid)

    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_persistent_invalid_pool_is_bounded_by_max_server_tool_rounds(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    settings = get_settings()
    original = settings.max_server_tool_rounds
    settings.max_server_tool_rounds = 2
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=20)
        bad = _pool(2)  # below the 3-question minimum — invalid on every attempt
        fake_anthropic.responses = [
            _quiz_result(bad, provider_id=f"toolu_loop{i}") for i in range(6)
        ]

        r = await _run_study_learn(client, uid)

        assert r.status_code == 502, r.text
        async with db_sessionmaker() as s:
            debits = int(
                await s.scalar(
                    text(
                        "SELECT count(*) FROM ledger_transactions "
                        "WHERE user_id=:u AND type='debit'"
                    ),
                    {"u": str(uid)},
                )
                or 0
            )
            audits = int(
                await s.scalar(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE user_id=:u AND payload->>'error'='max_server_tool_rounds_exceeded'"
                    ),
                    {"u": str(uid)},
                )
                or 0
            )
        assert debits == 0  # no final assistant_message → no billing
        assert audits == 1  # the general guard fired; there is no quiz-specific soft landing
    finally:
        settings.max_server_tool_rounds = original


@pytest.mark.asyncio
async def test_quiz_call_outside_its_mode_is_softly_refused(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff (ADR-064 §6): the turn survives; the tool is NOT executed and `quiz` stays null."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [
        _quiz_result(_pool(3), provider_id="toolu_offmode"),
        fake_anthropic.text_result("carrying on"),
    ]

    r = await _run_study_learn(client, uid, generation_mode="general")

    assert r.status_code == 200, r.text  # NOT 502 — the site.* guard's behaviour is not shared
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["quiz"] is None
    entries = _server_tools(body)
    assert len(entries) == 1
    assert entries[0]["status"] == "errored"
    assert entries[0]["summary"] == "tool_not_available"
    # Nothing was executed: the tool step carries the refusal, never a result.
    async with db_sessionmaker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT payload->'result', payload->'error'->>'code' FROM chat_steps "
                    "WHERE session_id=:sid AND role='tool' ORDER BY seq LIMIT 1"
                ),
                {"sid": body["sessionId"]},
            )
        ).one()
    assert row[0] is None
    assert row[1] == "tool_not_available"


@pytest.mark.asyncio
async def test_mode_check_wins_over_args_validation(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """diff (ADR-064 §6, normative order): fails if args are validated before the mode check."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    invalid = _pool(1)  # out of mode AND invalid args at the same time
    fake_anthropic.responses = [
        _quiz_result(invalid, provider_id="toolu_both"),
        fake_anthropic.text_result("still fine"),
    ]

    body = (await _run_study_learn(client, uid, generation_mode="general")).json()

    entries = _server_tools(body)
    assert len(entries) == 1  # exactly one record — no invalid_quiz round happened at all
    assert entries[0]["summary"] == "tool_not_available"
    assert all(e["summary"] != "invalid_quiz" for e in body["serverTools"])


@pytest.mark.asyncio
async def test_site_tool_without_project_still_fails_hard(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Contrast: the two guards differ ON PURPOSE — site.* would resolve someone's project."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "site.write_file",
            {"path": "a.html", "content": "x", "contentType": "text/html", "encoding": "utf8"},
        )
    ]

    r = await _run_study_learn(client, uid)  # project-less session

    assert r.status_code == 502, r.text
