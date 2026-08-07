"""Unit: the tool catalog / provider definitions leak no internal identifiers (normative).

09-testing.md §«Unit — каталог инструментов и утечка внутренних идентификаторов» +
02-api-contracts.md §inputSchema («вырезана модельная метаинформация» / инвариант «внутренние
идентификаторы не покидают процесс»).

Two opposite regressions are covered as a PAIR — cutting too little and cutting too much:
- the leak detector scans every artifact that leaves the process for internal identifiers;
- `test_only_the_model_metainformation_is_cut` asserts the useful half survived (per-field
  titles/descriptions, constraint keywords, the inlined nested object).

Enumeration is over MODES × SERIALIZERS, never over one default set. A mode-gated tool
(`quiz.generate`, ADR-064 axis C) is INVISIBLE to a scan that only looks at `general` — and a
mode-gated tool with a fresh docstring is exactly the kind of tool that reintroduces the leak.
The scan therefore asserts its own coverage (see `test_leak_scan_covers_every_mode_and_serializer`)
so that «zero matches» can never come from having scanned nothing.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app.chat.tools import (
    _ARGS_BY_TOOL,
    ALL_TOOL_NAMES,
    SERVER_SIDE_TOOLS,
    TOOL_QUIZ_GENERATE,
    anthropic_tool_definitions,
    neutral_tool_definitions,
    openai_tool_definitions,
    to_anthropic_tool_name,
    tool_catalog,
    tool_input_schema,
)
from tests.tool_registry import TOOLS_OFFERED_IN_EVERY_MODE

# Classes of internal development identifiers, verbatim from 09-testing.md §Детектор утечки.
_INTERNAL_IDENTIFIER_RE = re.compile(
    r"ADR-\d+|TD-\d+|Q-\d+-\d+|BUG-\d+|MAX_SERVER_TOOL_ROUNDS"
    r"|GlobalToolHandlers|SiteToolHandlers|_ARGS_BY_TOOL|[A-Za-z]+Args"
)

_MODES = ("general", "research", "reasoning", "study_learn")
_PROJECT_STATES = (True, False)


def _surfaces() -> list[tuple[str, str]]:
    """Every artifact that LEAVES the process, as (label, text) pairs.

    Two exit surfaces per 02-api-contracts §inputSchema: the public `GET /v1/tools` body (the
    catalog) and the tool definitions shipped to a provider on every round of a turn. The latter is
    enumerated over the full matrix modes × axis-A × serializers, because each serializer builds its
    own wire payload and each mode offers its own tool-set.
    """
    surfaces: list[tuple[str, str]] = []
    for entry in tool_catalog():
        name = entry["name"]
        surfaces.append((f"catalog[{name}].description", str(entry["description"])))
        surfaces.append((f"catalog[{name}].inputSchema", json.dumps(entry["inputSchema"])))

    for mode in _MODES:
        for has_project in _PROJECT_STATES:
            scope = f"mode={mode},project={has_project}"
            for definition in neutral_tool_definitions(
                include_server_side=has_project, generation_mode=mode
            ):
                label = f"neutral[{scope},{definition['name']}]"
                surfaces.append((f"{label}.description", str(definition["description"])))
                surfaces.append((f"{label}.input_schema", json.dumps(definition["input_schema"])))
            for definition in anthropic_tool_definitions(
                include_server_side=has_project, generation_mode=mode
            ):
                label = f"anthropic[{scope},{definition['name']}]"
                surfaces.append((f"{label}.description", str(definition["description"])))
                surfaces.append((f"{label}.input_schema", json.dumps(definition["input_schema"])))
            for definition in openai_tool_definitions(
                include_server_side=has_project, generation_mode=mode
            ):
                function = definition["function"]
                label = f"openai[{scope},{function['name']}]"
                surfaces.append((f"{label}.description", str(function["description"])))
                surfaces.append((f"{label}.parameters", json.dumps(function["parameters"])))
    return surfaces


def test_no_internal_identifier_leaves_the_process() -> None:
    """diff: fails the moment the model metainformation strip is removed.

    Args-model docstrings deliberately carry ADR references and internal class names (they are
    internal documentation); without the strip they ride out in `inputSchema` — to the iOS client
    AND into the paid prompt payload of every provider round.
    """
    leaks = [
        (label, sorted(set(_INTERNAL_IDENTIFIER_RE.findall(text))))
        for label, text in _surfaces()
        if _INTERNAL_IDENTIFIER_RE.search(text)
    ]
    assert leaks == [], f"internal identifiers leaked on {len(leaks)} surface(s): {leaks[:5]}"


def test_leak_scan_covers_every_mode_and_serializer() -> None:
    """Anti-vacuity: a scan of nothing also reports «zero matches».

    Pins WHAT was scanned, not just the verdict: the surface count is derived from the registry,
    and the mode-gated tool must be present in its own mode on every serializer (backend's remark —
    a scan of the default set alone cannot see the tool this very release added).
    """
    labels = [label for label, _ in _surfaces()]

    offered_with_project = {
        mode: len(TOOLS_OFFERED_IN_EVERY_MODE) + (1 if mode == "study_learn" else 0)
        for mode in _MODES
    }
    expected = 2 * len(ALL_TOOL_NAMES)  # catalog: description + inputSchema per tool
    for mode in _MODES:
        with_project = offered_with_project[mode]
        without_project = with_project - len(SERVER_SIDE_TOOLS)
        # 3 serializers × 2 text fields per definition.
        expected += 3 * 2 * (with_project + without_project)
    assert len(labels) == expected

    # The mode-gated tool is scanned in its mode on all three serializers …
    for serializer, wire_name in (
        ("neutral", TOOL_QUIZ_GENERATE),
        ("anthropic", to_anthropic_tool_name(TOOL_QUIZ_GENERATE)),
        ("openai", to_anthropic_tool_name(TOOL_QUIZ_GENERATE)),
    ):
        assert any(
            label.startswith(f"{serializer}[mode=study_learn,") and wire_name in label
            for label in labels
        ), (serializer, wire_name)
    # … and is absent from the other modes (its schema would otherwise be scanned twice over and
    # the axis-C gate would be silently broken).
    for mode in ("general", "research", "reasoning"):
        assert not any(
            label.startswith(f"neutral[mode={mode},") and TOOL_QUIZ_GENERATE in label
            for label in labels
        ), mode


def test_root_model_metainformation_is_cut_from_every_catalog_entry() -> None:
    # Root `title` == the Python class name, root `description` == the class docstring: both are
    # model metainformation, neither is part of the tool contract.
    for entry in tool_catalog():
        schema = entry["inputSchema"]
        assert "title" not in schema, entry["name"]
        assert "description" not in schema, entry["name"]
        # The catalog entry's own human-facing description (TOOL_DESCRIPTIONS) stays, of course.
        assert entry["description"]
        assert schema.get("type") == "object", entry["name"]


def test_only_the_model_metainformation_is_cut() -> None:
    """The opposite regression: «cut too much» must fail here.

    Per-field metadata and the constraint keywords are part of the contract handed to the model and
    MUST survive the strip (02-api-contracts §inputSchema, «Сохраняются: …»).
    """
    for name in _ARGS_BY_TOOL:
        schema = tool_input_schema(name)
        properties = schema.get("properties", {})
        for field_name, field_schema in properties.items():
            where = f"{name}.{field_name}"
            # Per-field title survives (pydantic derives it from the FIELD name, not the class).
            assert field_schema.get("title"), where
            # Where a field declares its own description via Field(description=...), it must not be
            # emptied by the strip. (No args model declares one today; the check is generic so the
            # first one added is covered automatically.)
            if "description" in field_schema:
                assert field_schema["description"].strip(), where

    # Concrete, non-vacuous survivors on the tool whose schema is rebuilt the most (inlining).
    quiz = tool_input_schema(TOOL_QUIZ_GENERATE)
    questions = quiz["properties"]["questions"]
    assert (questions["minItems"], questions["maxItems"]) == (3, 10)
    assert questions["title"] == "Questions"
    question = questions["items"]
    assert question["type"] == "object"
    assert set(question["required"]) == {"question", "options", "correctIndex", "explanation"}
    assert question["properties"]["question"]["maxLength"] == 1000
    assert question["properties"]["options"]["minItems"] == 2
    assert question["additionalProperties"] is False


def test_inlined_nested_definition_carries_no_model_metainformation() -> None:
    # The nested model is inlined INTO the schema, so its own class name/docstring would slip past
    # a root-only strip — 02-api-contracts requires the same two keys cut on inlined definitions.
    quiz = tool_input_schema(TOOL_QUIZ_GENERATE)
    question = quiz["properties"]["questions"]["items"]
    assert "title" not in question
    assert "description" not in question
    assert "$defs" not in quiz
    assert "$ref" not in json.dumps(quiz)


@pytest.mark.parametrize("name", sorted(_ARGS_BY_TOOL))
def test_schema_is_json_serializable_and_object_typed(name: str) -> None:
    schema: dict[str, Any] = tool_input_schema(name)
    assert json.loads(json.dumps(schema)) == schema
    assert schema["type"] == "object"
