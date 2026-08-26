"""Prometheus metrics (01-architecture.md#наблюдаемость)."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

chat_run_latency_seconds = Histogram(
    "chat_run_latency_seconds",
    "Latency of chat orchestration (policy + orchestrator + db), excluding Anthropic.",
)
blocked_requests_total = Counter(
    "blocked_requests_total",
    "Count of business-blocked requests by reason.",
    ["reason"],
)
wallet_debit_total = Counter(
    "wallet_debit_total",
    "Count of wallet debit attempts by result.",
    ["result"],
)
tool_call_roundtrip_latency_seconds = Histogram(
    "tool_call_roundtrip_latency_seconds",
    "Latency from tool_call initiation to tool_result handling.",
)
byok_usage_share = Gauge(
    "byok_usage_share",
    "Share of chat requests using BYOK mode.",
)
token_usage_total = Counter(
    "token_usage_total",
    "Total tokens by direction and model.",
    ["direction", "model"],
)
# Admin (ADM-7): grant outcomes by result (success | conflict | not_found).
admin_grant_total = Counter(
    "admin_grant_total",
    "Count of admin credit-grant attempts by result.",
    ["result"],
)
# Token purchase (ADR-015): consumable purchase outcomes by result
# (granted | replay | unknown_product | invalid_transaction | forbidden).
token_purchase_total = Counter(
    "token_purchase_total",
    "Count of consumable token-purchase attempts by result.",
    ["result"],
)
# Website builder (WB-8).
site_file_write_total = Counter(
    "site_file_write_total",
    "Count of site.write_file tool executions by result.",
    ["result"],
)
preview_request_total = Counter(
    "preview_request_total",
    "Count of preview endpoint requests by result (ok | forbidden | not_found).",
    ["result"],
)
# quiz.generate outcomes (ADR-065 §3): bounded-enum label only, never quiz content.
# Required rather than nice-to-have: quiz.generate is the first tool whose contract EXPECTS
# failures and DESIGNS a retry, so a systematically malformed model burns up to
# MAX_SERVER_TOOL_ROUNDS upstream calls per turn, ends the turn with an error and debits NO credit
# — the operator pays and nothing else signals it (blocked_requests_total does not move: it is not
# a policy block; llm_upstream_errors_total does not move: upstream answers 200). Without this
# counter a degrading model is indistinguishable from silence.
quiz_generate_total = Counter(
    "quiz_generate_total",
    "Count of quiz.generate tool executions by result (ok | invalid_quiz | tool_not_available).",
    ["result"],
)
# Anthropic upstream errors (TD-014): bounded enum labels only (no user-content).
# status_code is the numeric HTTP status or "none" for timeout/connection errors;
# error_type is the Anthropic error.type (or "unknown" when the body has none).
# KEPT for existing dashboards/tests; the generalized provider-labeled metric below is the
# ADR-033 §10 unified series (both are incremented on the Anthropic path).
anthropic_upstream_errors_total = Counter(
    "anthropic_upstream_errors_total",
    "Count of Anthropic upstream errors by status_code and error_type.",
    ["status_code", "error_type"],
)
# Generalized LLM upstream errors (ADR-033 §10): provider-labeled unified series for both
# Anthropic and OpenAI. provider ∈ {anthropic, openai}; status_code is the numeric HTTP status or
# "none" for timeout/connection errors; error_type is the provider error.type / exception class
# (or "unknown"). Bounded enum labels only (no user-content).
llm_upstream_errors_total = Counter(
    "llm_upstream_errors_total",
    "Count of LLM upstream errors by provider, status_code and error_type.",
    ["provider", "status_code", "error_type"],
)
# Unpriceable chat step (ADR-079 §1, rule `None ≠ 0`): producer — `report_chat_step_pricing`
# (`app.pricing.provider_prices`), called from the chat WRITE path once per LLM call, next to
# `token_usage_total`; consumer — GET /metrics.
#
# The write path is the point of the placement, not an implementation detail: it is where a STEP
# happens, which is what the series counts. The CRM read path prices the same stored step on every
# render (and twice per card — row plus revenue roll-up), and reports nothing at all while no
# operator has CRM open — a fault signal that only fires when someone is already looking is not one.
#
# Required rather than nice-to-have: an unpriceable step makes the WHOLE turn's cost `None`, and
# the operator sees that as an empty «Себестоимость» cell — indistinguishable from "this instance
# has no chat traffic". Nothing else moves: the call succeeded, no credit was refused, no upstream
# error was raised. Without this series a model drifting out of the price table (a provider
# renaming its snapshot, an allowlist naming a model the table never heard of) is silent.
#
# `model` is the name as stored in `chat_steps.usage.model` — a provider model id, the same
# bounded set `token_usage_total` already labels by; "none" when the step carries no model name.
# `reason` ∈ {unknown_model | no_model | no_token_counts}.
chat_unpriced_steps_total = Counter(
    "chat_unpriced_steps_total",
    "Count of chat usage steps that have no purchase price, by model and reason (ADR-079).",
    ["model", "reason"],
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


# ADR-086 §10: producer — места вызова модерации (ChatOrchestrator.run,
# MediaGenerationService.submit / _advance / upload_reference_image); consumer — GET /metrics.
moderation_decisions_total = Counter(
    "moderation_decisions_total",
    "Moderation verdicts by surface/stage/decision (ADR-086)",
    ["surface", "stage", "decision"],
)
moderation_errors_total = Counter(
    "moderation_errors_total",
    "Moderation provider failures by reason (ADR-086)",
    ["reason"],
)
