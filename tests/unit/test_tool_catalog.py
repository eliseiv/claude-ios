"""Unit tests for the GET /v1/tools catalog payload (ADR-019, chat-orchestrator/02).

``tool_catalog()`` is the single source of truth backing the endpoint; these tests assert the
catalog contract (dotted domain names, correct mutating/execution flags, inputSchema, and full
coverage of the tool registry) without an app/DB round-trip. The HTTP wiring (JWT-protection,
response shape) is exercised in tests/integration/test_tools_endpoint.py.

The number of tools is asserted **against the registry, never as a literal** (09-testing.md
§Study & Learn / 06-testing-strategy.md): a literal count would duplicate the registry on the test
surface and make the next tool addition fail on the number instead of on the substance.

The catalog is NOT filtered by axis A/B/C (02-api-contracts §GET /v1/tools): it is the full
technical registry, so ``quiz.generate`` is present here even though the model is offered it only
in ``study_learn``.
"""

from __future__ import annotations

from app.chat.tools import (
    _ARGS_BY_TOOL,
    ALL_TOOL_NAMES,
    GLOBAL_SERVER_SIDE_TOOLS,
    MUTATING_TOOLS,
    SERVER_SIDE_TOOLS,
    tool_catalog,
)

# Composition per ADR-011 / ADR-026 / ADR-064 / chat-orchestrator/02 §Полный список: client-side
# iOS tools + server-side site.* + global server-side (time.now, quiz.generate). The COUNT is
# never spelled out here — it is asserted against the registry (see the first test).
_EXPECTED_NAMES = {
    "files.read",
    "files.write",
    "files.list",
    "files.mkdir",
    "calendar.read",
    "calendar.create_events",
    "reminders.read",
    "reminders.create",
    "site.write_file",
    "site.preview",
    "site.list",
    "site.read",
    "site.delete",
    "time.now",
    "quiz.generate",
    "media.generate_image",
    "media.generate_video",
    "media.ask_params",
    # ADR-090: документы чата — серверные, project-independent.
    "document.create",
    "document.list",
    "document.read",
    "document.update",
}


def test_catalog_covers_the_whole_tool_registry() -> None:
    catalog = tool_catalog()
    # Composition — the load-bearing assertion. `_EXPECTED_NAMES` mirrors 02-api-contracts
    # §Полный список, and `ALL_TOOL_NAMES` is a registry declared INDEPENDENTLY of the
    # `_ARGS_BY_TOOL` map the catalog is generated from, so a tool added to one and forgotten in
    # the other fails right here.
    assert {t["name"] for t in catalog} == _EXPECTED_NAMES == set(ALL_TOOL_NAMES)
    # Count — against the REGISTRY, never a literal (09-testing.md §Study & Learn → Каталог). The
    # list length (not the set) is compared, so a DUPLICATED entry — which set equality above
    # silently collapses — fails too.
    assert len(catalog) == len(_ARGS_BY_TOOL) == len(ALL_TOOL_NAMES)


def test_every_tool_name_is_dotted_domain_not_underscore() -> None:
    # The iOS-facing contract uses dotted domain names (files.read, site.write_file); the
    # underscore wire names are an Anthropic-transport detail and must NOT leak here (BUG-3).
    for tool in tool_catalog():
        assert "." in tool["name"], tool["name"]
        assert "_" not in tool["name"].split(".")[0]  # the domain segment has no underscore


def test_mutating_flag_matches_mutating_tools() -> None:
    expected_mutating = {
        "files.write",
        "files.mkdir",
        "calendar.create_events",
        "reminders.create",
        "site.write_file",
        "site.delete",
        # ADR-090: create/update пишут на сервере; list/read — нет.
        "document.create",
        "document.update",
    }
    assert expected_mutating == set(MUTATING_TOOLS)
    by_name = {t["name"]: t for t in tool_catalog()}
    for name, tool in by_name.items():
        assert isinstance(tool["mutating"], bool)
        assert tool["mutating"] is (name in expected_mutating), name


def test_execution_is_server_for_site_and_global_and_client_otherwise() -> None:
    # ADR-026 §2: execution == "server" for project-scoped site.* AND global server-side time.now;
    # everything else is client-side.
    by_name = {t["name"]: t for t in tool_catalog()}
    for name, tool in by_name.items():
        expected = (
            "server" if name in SERVER_SIDE_TOOLS or name in GLOBAL_SERVER_SIDE_TOOLS else "client"
        )
        assert tool["execution"] == expected, (name, tool["execution"])
    # Cross-check: site.*, time.now and quiz.generate are the server-side set.
    assert by_name["time.now"]["execution"] == "server"
    assert by_name["time.now"]["mutating"] is False
    # ADR-064: quiz.generate is server-side (backend validates + echoes) and non-mutating.
    assert by_name["quiz.generate"]["execution"] == "server"
    assert by_name["quiz.generate"]["mutating"] is False
    for name in SERVER_SIDE_TOOLS:
        assert by_name[name]["execution"] == "server"


def test_every_tool_has_input_schema_and_description() -> None:
    for tool in tool_catalog():
        assert isinstance(tool["inputSchema"], dict)
        assert tool["inputSchema"], f"{tool['name']} has empty inputSchema"
        # JSON Schema object shape (Pydantic emits type=object with properties for arg models).
        assert tool["inputSchema"].get("type") == "object"
        assert isinstance(tool["description"], str) and tool["description"]
