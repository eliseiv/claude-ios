"""Unit coverage for Study & Learn registries, schema, axis C and pricing (ADR-064).

Scope = the pure half of [09-testing.md §Study & Learn](../../docs/modules/chat-orchestrator/
09-testing.md): tool catalog / self-contained schema / axis-C gate + sweep / registry invariants /
pool validation boundaries / content-free degrade message / mode price + the positivity validator.

The section's acceptance rule is explicit: a test that constructs a pool itself and checks only the
validator does NOT count as coverage of the FEATURE. Everything here is deliberately the COMPONENT
half — the working-path (HTTP) chains live in
tests/integration/test_study_learn_quiz_adr064.py and
tests/integration/test_study_learn_turn_scope_adr064.py.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.chat.global_tools import GlobalToolHandlers
from app.chat.repository import ChatRepository
from app.chat.tools import (
    _ARGS_ERROR_MAX_CHARS,
    _ARGS_ERROR_MAX_ITEMS,
    _ARGS_ERROR_MAX_PART_CHARS,
    ALL_TOOL_NAMES,
    ARGS_DEGRADE_TOOLS,
    GLOBAL_SERVER_SIDE_TOOLS,
    QUIZ_CONSTRAINTS_HINT,
    QUIZ_INVALID_ERROR_CODE,
    SERVER_SIDE_TOOLS,
    TOOL_GENERATION_MODES,
    TOOL_MEDIA_GENERATE_IMAGE,
    TOOL_MEDIA_GENERATE_VIDEO,
    TOOL_QUIZ_GENERATE,
    Quiz,
    QuizQuestion,
    content_free_args_error,
    neutral_tool_definitions,
    offered_in_generation_mode,
    tool_catalog,
    tool_input_schema,
    validate_tool_args,
)
from app.config import Settings
from tests.tool_registry import (
    ALL_REGISTERED_TOOL_NAMES,
    TOOLS_OFFERED_IN_EVERY_MODE,
    TOOLS_OFFERED_WITHOUT_PROJECT,
)

_ALL_MODES = ("general", "research", "reasoning", "study_learn")


def _question(
    *,
    text: str = "What is 2+2?",
    options: list[str] | None = None,
    correct_index: Any = 0,
    explanation: str = "Because arithmetic.",
) -> dict[str, Any]:
    return {
        "question": text,
        "options": options if options is not None else ["3", "4"],
        "correctIndex": correct_index,
        "explanation": explanation,
    }


def _pool(count: int = 3, **question_overrides: Any) -> dict[str, Any]:
    return {"questions": [_question(**question_overrides) for _ in range(count)]}


# ============================== catalog =====================================================
def test_catalog_contains_quiz_generate_as_non_mutating_server_tool() -> None:
    catalog = tool_catalog()
    # 09-testing.md §Study & Learn → Каталог: the entry count is compared with the REGISTRY, not
    # with a literal (the list length also rules out a duplicated entry).
    assert len(catalog) == len(ALL_REGISTERED_TOOL_NAMES)
    assert {t["name"] for t in catalog} == set(ALL_REGISTERED_TOOL_NAMES)
    entry = next(t for t in catalog if t["name"] == TOOL_QUIZ_GENERATE)
    assert entry["execution"] == "server"
    assert entry["mutating"] is False
    assert entry["description"]


def test_catalog_order_is_deterministic() -> None:
    # tool_catalog() iterates the _ARGS_BY_TOOL declaration order — the same list on every call.
    assert [t["name"] for t in tool_catalog()] == [t["name"] for t in tool_catalog()]


# ============================== self-contained schema (diff) ================================
def _walk(node: Any) -> list[Any]:
    found = [node]
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def test_quiz_schema_is_self_contained_no_refs() -> None:
    # ADR-064 §4 (diff): input_schema is shipped to TWO providers, so the nested question model is
    # INLINED — a naive model_json_schema() would emit {"$ref": "#/$defs/QuizQuestion"} and fail
    # this test.
    schema = tool_input_schema(TOOL_QUIZ_GENERATE)
    assert "$defs" not in schema
    for node in _walk(schema):
        if isinstance(node, dict):
            assert "$ref" not in node, node
    # The inlined question object really is there (not merely stripped away).
    question = schema["properties"]["questions"]["items"]
    assert question["type"] == "object"
    assert set(question["required"]) == {"question", "options", "correctIndex", "explanation"}


def test_quiz_schema_keeps_constraint_keywords_as_model_hints() -> None:
    schema = tool_input_schema(TOOL_QUIZ_GENERATE)
    questions = schema["properties"]["questions"]
    assert (questions["minItems"], questions["maxItems"]) == (3, 10)
    question = questions["items"]
    assert question["properties"]["question"]["maxLength"] == 1000
    assert question["properties"]["explanation"]["maxLength"] == 2000
    options = question["properties"]["options"]
    assert (options["minItems"], options["maxItems"]) == (2, 10)
    # ADR-065 §4 (diff): the per-OPTION length cap must live in the schema shipped to the provider,
    # not only in a custom validator — otherwise the model learns about a violation from a degrade
    # round, i.e. one extra upstream call inside a turn that costs 2 credits.
    assert options["items"]["maxLength"] == 400


def test_quiz_question_is_declared_once_for_the_tool_and_the_wire() -> None:
    # ADR-065 §5: the tool-args model and the response model of `quiz` must not drift apart. They
    # share ONE declaration today, so this is trivially green — and it stays the only guard if
    # someone ever forks the declaration (the drift would otherwise surface as a live-turn 500).
    from app.schemas.chat import QuizQuestionSchema, QuizSchema

    assert QuizQuestionSchema.model_json_schema() == QuizQuestion.model_json_schema()
    assert QuizSchema.model_json_schema() == Quiz.model_json_schema()


def test_other_tools_keep_their_published_schema_shape() -> None:
    # The inlining is scoped to _SELF_CONTAINED_SCHEMA_TOOLS: calendar.create_events keeps its
    # $defs-based shape (widening the set would be a contract change, not a drive-by).
    assert "$defs" in tool_input_schema("calendar.create_events")


# ============================== axis C: gate + sweep ========================================
def _offered(mode: str, *, include_server_side: bool = True) -> set[str]:
    return {
        d["name"]
        for d in neutral_tool_definitions(
            include_server_side=include_server_side, generation_mode=mode
        )
    }


def test_quiz_generate_offered_only_in_study_learn() -> None:
    # ADR-064 §3 (diff): fails if the tool is added to the offered set unconditionally.
    assert TOOL_QUIZ_GENERATE in _offered("study_learn")
    for mode in ("general", "research", "reasoning"):
        assert TOOL_QUIZ_GENERATE not in _offered(mode), mode
    # No generation_mode argument at all → the `general` default → still not offered.
    assert TOOL_QUIZ_GENERATE not in {d["name"] for d in neutral_tool_definitions()}


def test_axis_c_predicate_matches_the_registry() -> None:
    assert TOOL_GENERATION_MODES[TOOL_QUIZ_GENERATE] == frozenset({"study_learn"})
    assert offered_in_generation_mode(TOOL_QUIZ_GENERATE, "study_learn") is True
    for mode in ("general", "research", "reasoning", "nonsense"):
        assert offered_in_generation_mode(TOOL_QUIZ_GENERATE, mode) is False
    # A tool ABSENT from the registry is not mode-gated at all.
    for mode in _ALL_MODES:
        assert offered_in_generation_mode("files.read", mode) is True


@pytest.mark.parametrize("include_server_side", [True, False])
def test_axis_c_does_not_touch_any_other_tool_of_the_registry(include_server_side: bool) -> None:
    # ADR-064 §3 sweep: with axes A/B equal, the offered set of every OTHER tool of the registry
    # (`_ARGS_BY_TOOL` minus the mode-gated ones — the number is taken from the registry, never
    # spelled out here) is identical in all four modes: axis C moved exactly one tool.
    expected = TOOLS_OFFERED_IN_EVERY_MODE if include_server_side else TOOLS_OFFERED_WITHOUT_PROJECT
    for mode in _ALL_MODES:
        names = _offered(mode, include_server_side=include_server_side) - {TOOL_QUIZ_GENERATE}
        assert names == set(expected), mode
    # 06-testing-strategy.md: the neighbouring global tool `time.now` is offered in EVERY mode —
    # «global» does not imply «mode-gated», the rule of one registry entry is not shared.
    assert "time.now" in expected


# ============================== registry invariants =========================================
def test_registries_are_disjoint_and_within_the_tool_namespace() -> None:
    assert SERVER_SIDE_TOOLS.isdisjoint(GLOBAL_SERVER_SIDE_TOOLS)
    assert TOOL_QUIZ_GENERATE in GLOBAL_SERVER_SIDE_TOOLS
    assert set(TOOL_GENERATION_MODES) <= set(ALL_TOOL_NAMES)
    assert set(ARGS_DEGRADE_TOOLS) <= set(ALL_TOOL_NAMES)
    # Degrade is an allowlist: quiz (ADR-064) + media.generate_* (ADR-068). Do not widen casually.
    assert set(ARGS_DEGRADE_TOOLS) == {
        TOOL_QUIZ_GENERATE,
        TOOL_MEDIA_GENERATE_IMAGE,
        TOOL_MEDIA_GENERATE_VIDEO,
    }


# ============================== pool validation boundaries ==================================
@pytest.mark.parametrize("count", [3, 10])
def test_valid_pool_sizes_are_accepted(count: int) -> None:
    out = validate_tool_args(TOOL_QUIZ_GENERATE, _pool(count))
    assert len(out["questions"]) == count


@pytest.mark.parametrize("count", [0, 2, 11])
def test_invalid_pool_sizes_are_rejected(count: int) -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, _pool(count))


@pytest.mark.parametrize("option_count", [1, 11])
def test_invalid_option_counts_are_rejected(option_count: int) -> None:
    options = [f"opt{i}" for i in range(option_count)]
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, _pool(3, options=options, correct_index=0))


@pytest.mark.parametrize("bad_index", [2, -1])
def test_correct_index_out_of_range_is_rejected(bad_index: int) -> None:
    # options has length 2 → valid indexes are 0 and 1 only.
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, _pool(3, correct_index=bad_index))


def test_correct_index_true_is_rejected_bool_is_not_an_int() -> None:
    # Python bool IS a subclass of int and pydantic's lax mode would coerce True → 1: without the
    # explicit before-validator the pool would silently pass with «option 1 is correct».
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, _pool(3, correct_index=True))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "q" * 1001),
        ("explanation", "e" * 2001),
    ],
)
def test_over_length_fields_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, _pool(3, **{field: value}))


def test_over_length_option_is_rejected() -> None:
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, _pool(3, options=["ok", "o" * 401]))


def test_extra_key_and_missing_field_are_rejected() -> None:
    extra = _pool(3)
    extra["questions"][0]["hint"] = "nope"
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, extra)
    missing = _pool(3)
    del missing["questions"][1]["explanation"]
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, missing)
    wrapper_extra = {**_pool(3), "topic": "math"}
    with pytest.raises(ValidationError):
        validate_tool_args(TOOL_QUIZ_GENERATE, wrapper_extra)


def test_all_or_nothing_one_bad_question_invalidates_the_whole_pool() -> None:
    # ADR-064 §5: partial acceptance does not exist — there is no «pool minus the bad question».
    pool = _pool(5)
    pool["questions"][2]["correctIndex"] = 7
    execution = GlobalToolHandlers()._quiz_generate(pool)
    assert execution.is_error is True
    assert execution.error_code == QUIZ_INVALID_ERROR_CODE
    assert execution.result is None


def test_valid_pool_is_echoed_verbatim_by_the_handler() -> None:
    pool = _pool(3)
    execution = GlobalToolHandlers()._quiz_generate(pool)
    assert execution.is_error is False
    assert execution.result == pool


# ============================== content-free degrade message ================================
def test_degrade_message_carries_field_path_but_no_quiz_content() -> None:
    secret_question = "SENTINEL-QUESTION-TEXT"
    secret_option = "SENTINEL-OPTION-TEXT"
    secret_explanation = "SENTINEL-EXPLANATION-TEXT"
    pool = _pool(
        3,
        text=secret_question,
        options=[secret_option, "other"],
        correct_index=5,
        explanation=secret_explanation,
    )
    with pytest.raises(ValidationError) as exc_info:
        validate_tool_args(TOOL_QUIZ_GENERATE, pool)
    message = f"{content_free_args_error(exc_info.value)}; {QUIZ_CONSTRAINTS_HINT}"

    assert "questions.0" in message  # field path present
    assert "correctIndex" in message  # error kind present
    assert QUIZ_CONSTRAINTS_HINT in message  # machine-fixable hint present
    for secret in (secret_question, secret_option, secret_explanation):
        assert secret not in message, message


def test_degrade_message_length_does_not_grow_with_the_input() -> None:
    """The message is persisted in a chat step and replayed to the model — its size must be OURS.

    Content-freedom alone does not bound it: on ``extra_forbidden`` pydantic puts the OFFENDING KEY
    into ``loc``, and that key is chosen by the model, so an unbounded ``loc`` segment is a
    model-controlled payload in our own tool-result. Both hard caps are exercised: the per-part cap
    (one huge key) and the joined cap (several huge keys, each already capped).
    """
    huge_key = "K" * 5000
    pool = _pool(3)
    pool["questions"][0][huge_key] = 1
    with pytest.raises(ValidationError) as exc_info:
        validate_tool_args(TOOL_QUIZ_GENERATE, pool)
    message = content_free_args_error(exc_info.value)

    assert len(message) <= _ARGS_ERROR_MAX_CHARS
    assert all(len(part) <= _ARGS_ERROR_MAX_PART_CHARS for part in message.split("; "))
    assert huge_key not in message  # the model-invented key is truncated, never echoed whole

    # Several long parts at once: each is capped, and the JOINED result is capped again.
    many = _pool(6)
    for index, question in enumerate(many["questions"]):
        question["K" * 900 + str(index)] = 1
    with pytest.raises(ValidationError) as many_info:
        validate_tool_args(TOOL_QUIZ_GENERATE, many)
    joined = content_free_args_error(many_info.value)
    assert len(many_info.value.errors()) > _ARGS_ERROR_MAX_ITEMS  # the item cap is exercised too
    assert len(joined) <= _ARGS_ERROR_MAX_CHARS
    assert len(joined.split("; ")) <= _ARGS_ERROR_MAX_ITEMS


def test_non_pydantic_error_message_is_bounded_too() -> None:
    # The other branch of the same helper (a plain ValueError, e.g. «unknown tool: …») returns the
    # exception's own text — which must be capped as well, not passed through at any length.
    assert len(content_free_args_error(ValueError("x" * 5000))) <= _ARGS_ERROR_MAX_CHARS


def test_handler_degrade_message_is_content_free_too() -> None:
    secret = "SENTINEL-IN-HANDLER"
    execution = GlobalToolHandlers()._quiz_generate(_pool(3, text=secret, correct_index=9))
    assert execution.error_code == QUIZ_INVALID_ERROR_CODE
    assert secret not in (execution.error_message or "")


# ============================== pricing =====================================================
def test_study_learn_price_comes_from_its_env_and_defaults_to_two() -> None:
    assert Settings().chat_generation_credit_cost("study_learn") == 2
    assert Settings(CHAT_CREDIT_COST_STUDY_LEARN=6).chat_generation_credit_cost("study_learn") == 6
    # Unknown mode still falls back to the general price (unchanged behaviour).
    settings = Settings(CHAT_CREDIT_COST_GENERAL=4, CHAT_CREDIT_COST_STUDY_LEARN=6)
    assert settings.chat_generation_credit_cost("unknown_mode") == 4


@pytest.mark.parametrize(
    ("env_name", "mode"),
    [
        ("CHAT_CREDIT_COST_GENERAL", "general"),
        ("CHAT_CREDIT_COST_RESEARCH", "research"),
        ("CHAT_CREDIT_COST_REASONING", "reasoning"),
        ("CHAT_CREDIT_COST_STUDY_LEARN", "study_learn"),
    ],
)
@pytest.mark.parametrize("bad_value", [0, -5])
def test_every_mode_price_is_clamped_to_one_by_the_shared_validator(
    env_name: str, mode: str, bad_value: int
) -> None:
    # ADR-064 §9 (diff): EVERY CHAT_CREDIT_COST_* field must sit in the SAME positivity validator.
    # Parametrized over all four so the next price added outside the validator fails here — a price
    # left out fails silently in production (no start-up error, no block: the mode becomes free).
    settings = Settings(**{env_name: bad_value})
    assert settings.chat_generation_credit_cost(mode) == 1


# ============================== mode whitelist (continuation boundary) ======================
class _StubSession:
    """Minimal AsyncSession stand-in: `scalar` returns the stored user-step mode value."""

    def __init__(self, value: Any) -> None:
        self.value = value

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        return self.value


@pytest.mark.parametrize("mode", _ALL_MODES)
async def test_generation_mode_for_message_step_accepts_all_four_modes(mode: str) -> None:
    # ADR-064 §12: a mode missing from this whitelist degrades SILENTLY to `general` — the
    # continuation of a quiz turn would lose BOTH its price and quiz.generate from the tool-set.
    repo = ChatRepository(_StubSession(mode))  # type: ignore[arg-type]
    assert await repo.generation_mode_for_message_step(uuid.uuid4(), uuid.uuid4()) == mode


@pytest.mark.parametrize("stored", ["deep_research", "", None, 7])
async def test_generation_mode_for_message_step_degrades_unknown_values(stored: Any) -> None:
    repo = ChatRepository(_StubSession(stored))  # type: ignore[arg-type]
    assert await repo.generation_mode_for_message_step(uuid.uuid4(), uuid.uuid4()) == "general"
