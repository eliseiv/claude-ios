"""Registry-derived expectations for tool-set assertions — no literal tool counts in tests.

Normative rule ([06-testing-strategy.md §time.now](../docs/06-testing-strategy.md),
[chat-orchestrator/09-testing.md §Study & Learn](../docs/modules/chat-orchestrator/09-testing.md)):
the number of tools is asserted **against the registry, not against a literal**. A literal count
duplicates the registry on the test surface: the next tool added to `tools.py` breaks N unrelated
tests on the number rather than on the substance, and fixing the numbers starts the cycle again.

These constants are built from registry DECLARATIONS (`ALL_TOOL_NAMES`, `TOOL_GENERATION_MODES`,
`SERVER_SIDE_TOOLS`) and deliberately NOT from the gating LOGIC (`offered_in_generation_mode`,
`neutral_tool_definitions`). That keeps the assertions honest: comparing the offered set with a
set computed by the very function under test would be a tautology, whereas comparing it with the
declared registries still fails when the gate drops or adds a tool it must not touch.

Where a count is asserted, compare the length of the emitted LIST with the length of one of these
sets: set equality alone cannot see a DUPLICATED definition (the set collapses it), so the pair
«set equality + list length» is what actually pins the offer down.
"""

from __future__ import annotations

from app.chat.tools import ALL_TOOL_NAMES, SERVER_SIDE_TOOLS, TOOL_GENERATION_MODES

# Tools gated by axis C (generation mode) — today only `quiz.generate` (ADR-064 §3).
MODE_GATED_TOOL_NAMES: frozenset[str] = frozenset(TOOL_GENERATION_MODES)

# Offered in EVERY generation mode when the session has a project: the whole registry minus the
# mode-gated tools (axis C is the only axis that removes a tool from the full set).
# NB: this holds while every mode-gated tool excludes `general`. A future tool gated to a set that
# INCLUDES a mode used here (e.g. {"general", "study_learn"}) makes this derivation wrong, and the
# tests using it will fail — deliberately: the expectation must then be reworked consciously,
# not silently widened.
TOOLS_OFFERED_IN_EVERY_MODE: frozenset[str] = frozenset(ALL_TOOL_NAMES) - MODE_GATED_TOOL_NAMES

# The same set under axis A «no project» (ADR-022): project-scoped `site.*` drop out; global
# server-side tools (`time.now`) stay — «global» means «needs no project» (ADR-026 §3).
TOOLS_OFFERED_WITHOUT_PROJECT: frozenset[str] = TOOLS_OFFERED_IN_EVERY_MODE - SERVER_SIDE_TOOLS

# The full registry — what a `study_learn` turn with a project is offered, and what the
# unfiltered `GET /v1/tools` catalog returns (the catalog is not filtered by any axis).
ALL_REGISTERED_TOOL_NAMES: frozenset[str] = frozenset(ALL_TOOL_NAMES)
