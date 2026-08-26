"""What a request costs US — provider purchase prices, in USD (ADR-079).

The CRM column «Себестоимость» answers one question for the operator: how much did this
request cost the business. Credits answer a different one (what the user was charged), and
the two are calibrated ×2 apart for media (ADR-061 §1), so one can never be read as the
other.

Two rules hold everywhere in this module:

* **A price we do not have is ``None``, never ``0.0``.** A model missing from the table is
  an unpriced model, and ``0.0`` would report "this request was free" as a measurement.
  Per-instance model allowlists (``OPENAI_MODELS`` / ``ANTHROPIC_MODELS``) can name models
  this table has never heard of, so the gap is normal and must stay visible.
* **A number we cannot derive exactly is marked estimated**, not silently rounded into a
  fact. ``ProviderCost.estimated`` travels to the CRM, which renders such values with «≈».

Chat is exact: ``chat_steps.usage`` stores the token counts fal-style billing needs, and
every LLM call of a tool-loop turn is summed (the provider billed each one).

Media splits by age. New jobs store the exact cost at submit time
(``media_jobs.provider_cost_usd``), where the price-affecting knobs — resolution, duration,
audio, image count — are known. Historic rows predate that column, and the knobs were never
persisted, so their cost is recovered from ``credits_charged``: exact where credits and
dollars scale together (images with a known asset count, Kling 2.5), an upper bound where
fal bills per second inside a whole credit pack (Kling v3, Veo).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.media_generation.catalog import KIND_IMAGE, FalModel, image_unit_credits
from app.observability.logging import get_logger, log_event
from app.observability.metrics import chat_unpriced_steps_total

_logger = get_logger("app.pricing.provider_prices")

# --- chat ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatTokenPrices:
    """List prices of one model, USD per 1M tokens, plus the per-search tool fee.

    ``cache_read_in_input`` records a real difference between the two providers' usage
    reports, not a preference: OpenAI's ``prompt_tokens`` INCLUDES the cached prefix
    (``prompt_tokens_details.cached_tokens`` is a subset of it), while Anthropic reports
    ``input_tokens`` WITHOUT ``cache_read_input_tokens``. Ignoring that would bill the
    cached prefix twice for OpenAI — at the full input rate and again at the cache rate.
    """

    input_usd: float
    output_usd: float
    cache_read_usd: float
    cache_write_usd: float
    cache_read_in_input: bool
    web_search_usd_per_request: float = 0.01


# Verified against the providers' own pricing pages on 2026-08-14 (openai.com/api/pricing,
# platform.claude.com/docs/en/about-claude/pricing). Prices are NOT re-checked automatically —
# same operator procedure as the fal table (ADR-061 §Consequences, Q-061-1). Web search is
# $10 per 1000 requests at both providers, i.e. $0.01 per request.
#
# Cache columns: OpenAI does not bill cache writes at all (auto-cache, no write counter — see
# `LLMUsage`), so `cache_write_usd` is 0 for its rows; Anthropic bills 5-minute writes at
# 1.25× input, which is what its rows carry.
_OPENAI_CACHE_WRITE_FREE = 0.0

CHAT_TOKEN_PRICES: Mapping[str, ChatTokenPrices] = MappingProxyType(
    {
        # OpenAI
        "gpt-4o": ChatTokenPrices(2.50, 10.00, 1.25, _OPENAI_CACHE_WRITE_FREE, True),
        "gpt-4.1": ChatTokenPrices(2.00, 8.00, 0.50, _OPENAI_CACHE_WRITE_FREE, True),
        "gpt-5": ChatTokenPrices(1.25, 10.00, 0.125, _OPENAI_CACHE_WRITE_FREE, True),
        "gpt-5-mini": ChatTokenPrices(0.25, 2.00, 0.025, _OPENAI_CACHE_WRITE_FREE, True),
        "gpt-5.1": ChatTokenPrices(1.25, 10.00, 0.125, _OPENAI_CACHE_WRITE_FREE, True),
        # Anthropic
        "claude-fable-5": ChatTokenPrices(10.00, 50.00, 1.00, 12.50, False),
        "claude-opus-5": ChatTokenPrices(5.00, 25.00, 0.50, 6.25, False),
        "claude-opus-4-7": ChatTokenPrices(5.00, 25.00, 0.50, 6.25, False),
        "claude-opus-4-6": ChatTokenPrices(5.00, 25.00, 0.50, 6.25, False),
        "claude-sonnet-5": ChatTokenPrices(2.00, 10.00, 0.20, 2.50, False),
        "claude-sonnet-4-6": ChatTokenPrices(3.00, 15.00, 0.30, 3.75, False),
        "claude-sonnet-4-5": ChatTokenPrices(3.00, 15.00, 0.30, 3.75, False),
        "claude-haiku-4-5-20251001": ChatTokenPrices(1.00, 5.00, 0.10, 1.25, False),
    }
)

_PER_MILLION = 1_000_000.0


_TOKEN_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")


def _tokens(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def _has_token_counts(usage: Mapping[str, Any]) -> bool:
    """Does this step actually carry counts, or only a model name?

    A step written before token usage was persisted has nothing to multiply the price by, and
    treating its missing counts as zeros would publish "$0.00" — a measurement — for a call
    that certainly cost something.
    """
    return any(
        not isinstance(usage.get(key), bool) and isinstance(usage.get(key), int | float)
        for key in _TOKEN_KEYS
    )


PROVIDER_OPENAI = "OpenAI"
PROVIDER_ANTHROPIC = "Anthropic"
PROVIDER_FAL = "Fal"
# A paid call whose vendor cannot be named: token counters are there, the model name is not.
# NOT a vendor — a place that keeps such traffic visible instead of dropping it (ADR-092 §6).
# Naming a real vendor here would invent a bill and spoil an already correct cell; a consumer
# that does not recognise the key folds it into "other" and does not lose it.
PROVIDER_UNKNOWN = "Unknown"


def provider_of_chat_model(model: str) -> str:
    """Which vendor's bill this model lands on. Only two chat providers exist here (ADR-033)."""
    return PROVIDER_ANTHROPIC if model.strip().lower().startswith("claude") else PROVIDER_OPENAI


# A dated snapshot suffix, in both shapes the two providers use: `-2025-11-13` (OpenAI) and
# `-20251001` (Anthropic). Nothing else counts as a snapshot — see `resolve_chat_price_model`.
_SNAPSHOT_SUFFIX = re.compile(r"^-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def resolve_chat_price_model(model: str) -> str | None:
    """Which row of :data:`CHAT_TOKEN_PRICES` prices this model name, if any.

    The table is keyed by the ALIAS a caller asks for (``gpt-5.1``), while a provider may answer,
    and history may therefore hold, a DATED SNAPSHOT of that alias (``gpt-5.1-2025-11-13``). The
    provider bills the snapshot at its alias's list price, so mapping one onto the other reads the
    price we have — it does not invent one. This also prices history written before the clients
    agreed to store the requested alias, without a migration: the cost is computed at read time.

    Longest alias wins: ``gpt-5-mini-2025-08-07`` belongs to ``gpt-5-mini``, not to ``gpt-5``.

    ONLY a date suffix resolves. Any other suffix marks a DIFFERENT model with its own price
    (``gpt-5-pro``, ``…-chat-latest``), and charging it the base model's rate would publish a
    number we do not have as a measurement — precisely what the ``None`` rule of this module
    forbids. Such a model stays unpriced and becomes visible through
    ``chat_unpriced_steps_total``, which is how it gets a price: an operator adds the row.
    """
    if model in CHAT_TOKEN_PRICES:
        return model
    best: str | None = None
    for alias in CHAT_TOKEN_PRICES:
        if not model.startswith(alias) or not _SNAPSHOT_SUFFIX.match(model[len(alias) :]):
            continue
        if best is None or len(alias) > len(best):
            best = alias
    return best


def _prices_of(model: str) -> ChatTokenPrices | None:
    """The price row that bills this model name, or ``None`` when we have none for it."""
    priced = resolve_chat_price_model(model)
    return None if priced is None else CHAT_TOKEN_PRICES.get(priced)


_REASON_UNKNOWN_MODEL = "unknown_model"
_REASON_NO_MODEL = "no_model"
_REASON_NO_TOKEN_COUNTS = "no_token_counts"

# One log line per (model, reason) per process: the counter carries the RATE, the log carries the
# name once. Bounded, so a stream of distinct junk names cannot grow the set without limit.
#
# PAST the cap the log goes SILENT — it does not fall back to logging every occurrence. The cap
# exists precisely because names may arrive unboundedly; a cap that stops REMEMBERING but keeps
# EMITTING would turn its own worst case into a WARNING flood. One `chat_step_unpriced_log_capped`
# event marks the boundary, and from there the counter is the only reporter — which is its job:
# it carries the rate, per model and reason, without a line per occurrence.
_LOGGED_UNPRICED: set[tuple[str, str]] = set()
_LOGGED_UNPRICED_CAP = 256
_LOGGED_UNPRICED_CAP_ANNOUNCED = False


def _report_unpriced_step(model: str | None, reason: str) -> None:
    """Make an unpriceable step audible (ADR-079 §1).

    An unpriceable step nulls the cost of its whole turn, and the operator sees only an empty
    «Себестоимость» cell — which looks exactly like "no traffic". Nothing else signals it: the
    call succeeded and was paid for. Silence here is what let a model-name drift run unnoticed.
    """
    global _LOGGED_UNPRICED_CAP_ANNOUNCED
    label = model if model else "none"
    chat_unpriced_steps_total.labels(model=label, reason=reason).inc()
    key = (label, reason)
    if key in _LOGGED_UNPRICED:
        return
    if len(_LOGGED_UNPRICED) >= _LOGGED_UNPRICED_CAP:
        if _LOGGED_UNPRICED_CAP_ANNOUNCED:
            return
        _LOGGED_UNPRICED_CAP_ANNOUNCED = True
        log_event(
            _logger,
            logging.WARNING,
            "chat_step_unpriced_log_capped",
            distinct_names=_LOGGED_UNPRICED_CAP,
        )
        return
    _LOGGED_UNPRICED.add(key)
    log_event(
        _logger,
        logging.WARNING,
        "chat_step_unpriced",
        model=label,
        reason=reason,
    )


def _unpriced_reason(usage: Mapping[str, Any]) -> str | None:
    """Why this ONE step cannot be priced, or ``None`` when it can."""
    model = usage.get("model")
    if not isinstance(model, str) or not model:
        return _REASON_NO_MODEL
    if resolve_chat_price_model(model) is None:
        return _REASON_UNKNOWN_MODEL
    if not _has_token_counts(usage):
        return _REASON_NO_TOKEN_COUNTS
    return None


def report_chat_step_pricing(usage: Mapping[str, Any]) -> None:
    """Report one just-generated chat step to ``chat_unpriced_steps_total`` if it has no price.

    Called from the WRITE path — once per LLM call, where the step is created — and nowhere else.
    That is what makes the series count STEPS, as its HELP says. Costing itself runs on the CRM
    read path over the whole stored history: reporting from there would count RENDERS, inflating
    one step into several (a single card render prices the same step twice, once for the revenue
    roll-up and once for the row) and — worse — would report NOTHING at all until an operator
    happens to open CRM, which is exactly the blind spot this series exists to remove.

    Silent for a priceable step: the series is a fault signal, not a traffic counter.
    """
    reason = _unpriced_reason(usage)
    if reason is None:
        return
    model = usage.get("model")
    _report_unpriced_step(model if isinstance(model, str) and model else None, reason)


def chat_cost_usd_by_provider(usages: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
    """Cost of one chat TURN, split by vendor.

    A tool-loop turn calls the provider several times (each ``assistant`` step is one call)
    and is billed for each, so summing the steps is the cost — not an approximation of it.
    The split exists because a turn CAN change model mid-loop, and the money then belongs to
    two different bills.

    Returns ``None`` when the turn holds no usage at all, or when ANY of its calls is
    unpriceable — an unknown model, or a step with no token counts to price. A partial sum
    would understate the cost while looking like a full one.

    Pure and silent: this is a READ path, run once per rendered row and again for the revenue
    roll-up, so anything reported here would count renders rather than steps. The gap is made
    audible where the step is created — :func:`report_chat_step_pricing`.
    """
    per_provider: dict[str, float] = {}
    for usage in usages:
        model = usage.get("model")
        if not isinstance(model, str) or not model:
            return None
        priced_model = resolve_chat_price_model(model)
        prices = CHAT_TOKEN_PRICES.get(priced_model) if priced_model is not None else None
        if prices is None or not _has_token_counts(usage):
            return None
        cache_read = _tokens(usage, "cacheReadTokens")
        input_tokens = _tokens(usage, "inputTokens")
        billed_input = (
            max(0, input_tokens - cache_read) if prices.cache_read_in_input else input_tokens
        )
        cost = (
            billed_input * prices.input_usd
            + _tokens(usage, "outputTokens") * prices.output_usd
            + cache_read * prices.cache_read_usd
            + _tokens(usage, "cacheWriteTokens") * prices.cache_write_usd
        ) / _PER_MILLION
        cost += _tokens(usage, "webSearchRequests") * prices.web_search_usd_per_request
        provider = provider_of_chat_model(model)
        per_provider[provider] = per_provider.get(provider, 0.0) + cost
    return per_provider or None


def chat_cost_usd(usages: Sequence[Mapping[str, Any]]) -> float | None:
    """Cost of one chat TURN — the sum of :func:`chat_cost_usd_by_provider`."""
    per_provider = chat_cost_usd_by_provider(usages)
    return None if per_provider is None else sum(per_provider.values())


@dataclass(frozen=True, slots=True)
class ChatUsageTotals:
    """Token counters of MANY calls of ONE model, already summed.

    The period report the CRM reads (`GET /v1/admin/costs/daily`) cannot price call by call: a
    92-day window holds hundreds of thousands of steps, and shipping their ``usage`` blobs to
    Python would cost more than the answer is worth. It sums the counters per (day, model) in
    SQL and prices the sums HERE — so the price table stays the one home of the numbers instead
    of being re-expressed as a SQL CASE, where the two copies would drift apart silently.

    Summing before pricing is exact, not an approximation: every term of the per-call formula is
    linear in its own counter. The one term that is NOT — the OpenAI subtraction of the cached
    prefix, which floors at zero — is therefore summed per call upstream and arrives ready as
    ``input_excl_cache_read_tokens``. Subtracting the two totals here instead would differ from
    the per-call answer for any call that reported more cached tokens than input tokens.

    Only calls that actually carry counters may be summed into these fields; a call whose usage
    has no counters at all has nothing to price (``_has_token_counts``), and folding its implicit
    zeros in would report "this call was free" as a measurement.
    """

    input_tokens: int
    input_excl_cache_read_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    web_search_requests: int


def chat_cost_usd_of_totals(model: str, totals: ChatUsageTotals) -> float | None:
    """USD of the calls behind ``totals``; ``None`` when this model has no price on file.

    Same formula, same table and same cache convention as :func:`chat_cost_usd_by_provider` —
    only the granularity differs (a period of one model instead of one turn).
    """
    prices = _prices_of(model)
    if prices is None:
        return None
    billed_input = (
        totals.input_excl_cache_read_tokens if prices.cache_read_in_input else totals.input_tokens
    )
    cost = (
        billed_input * prices.input_usd
        + totals.output_tokens * prices.output_usd
        + totals.cache_read_tokens * prices.cache_read_usd
        + totals.cache_write_tokens * prices.cache_write_usd
    ) / _PER_MILLION
    return cost + totals.web_search_requests * prices.web_search_usd_per_request


def chat_billed_tokens(model: str, totals: ChatUsageTotals) -> int | None:
    """How many tokens the provider billed for those calls — the cached prefix counted ONCE.

    The two providers report the prefix differently (``cache_read_in_input``), so a single
    ``input + output + cache`` sum would count OpenAI's cached tokens twice — the same defect the
    price columns exist to prevent, in the counter the CRM shows as «Токенов».

    ``None`` for a model with no price row: without its row we do not know WHICH convention its
    usage follows, and guessing would publish a count we cannot stand behind.
    """
    prices = _prices_of(model)
    if prices is None:
        return None
    if prices.cache_read_in_input:
        return totals.input_tokens + totals.output_tokens
    return (
        totals.input_tokens
        + totals.cache_read_tokens
        + totals.cache_write_tokens
        + totals.output_tokens
    )


# --- media --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderCost:
    """A cost with its precision. ``usd is None`` means not measurable, never "free"."""

    usd: float | None
    estimated: bool = False


# fal purchase prices, ADR-061 §«Закупочные цены fal» (fal.ai/models/…, 2026-08-05). This is
# the SAME table the credit prices were calibrated against, so the two cannot drift apart
# silently: a change here without a change there breaks the ×2 coverage invariant of ADR-061.
_IMAGE_USD_PER_IMAGE: Mapping[str, Mapping[str, float]] = MappingProxyType(
    {
        "nano-banana-pro": MappingProxyType({"1K": 0.15, "2K": 0.15, "4K": 0.30}),
        "nano-banana-2": MappingProxyType({"0.5K": 0.06, "1K": 0.08, "2K": 0.12, "4K": 0.16}),
    }
)

# Veo bills per second, and both 4K and audio change the RATE, not the duration.
_VEO_USD_PER_SECOND: Mapping[tuple[str, bool], float] = MappingProxyType(
    {
        ("720p", False): 0.20,
        ("720p", True): 0.40,
        ("1080p", False): 0.20,
        ("1080p", True): 0.40,
        ("4k", False): 0.40,
        ("4k", True): 0.60,
    }
)

_KLING_25_FIRST_PACK_USD = 0.35
_KLING_25_PACK_SECONDS = 5
_KLING_25_EXTRA_SECOND_USD = 0.07
_KLING_V3_USD_PER_SECOND_SILENT = 0.112
_KLING_V3_USD_PER_SECOND_AUDIO = 0.168

# One full base pack of each video model at its cheapest settings — the unit historic rows are
# recovered with. Kling 2.5 offers only whole packs (5 s / 10 s), so credits→USD is exact for
# it; Kling v3 (1 s granularity) and Veo (4 s packs but 4/6/8 s durations) can bill fal LESS
# than the pack we charged for, which is why their recovered value is an upper bound.
_VIDEO_BASE_PACK_USD: Mapping[str, float] = MappingProxyType(
    {
        "kling-video": _KLING_25_FIRST_PACK_USD,
        "kling-video-v3": _KLING_V3_USD_PER_SECOND_SILENT * 5,
        "veo-3.1": 0.20 * 4,
    }
)
_VIDEO_CREDITS_ARE_EXACT = frozenset({"kling-video"})


def _seconds(duration: str | None) -> int | None:
    """``"8s"`` / ``"10"`` → seconds. The catalog stores both spellings (ADR-061 §3 table)."""
    if duration is None:
        return None
    text = duration.strip().lower().removesuffix("s")
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _video_usd_of_run(
    *, model_id: str, seconds: int, resolution: str | None, generate_audio: bool
) -> float | None:
    if model_id == "kling-video":
        extra = max(0, seconds - _KLING_25_PACK_SECONDS)
        return _KLING_25_FIRST_PACK_USD + extra * _KLING_25_EXTRA_SECOND_USD
    if model_id == "kling-video-v3":
        rate = _KLING_V3_USD_PER_SECOND_AUDIO if generate_audio else _KLING_V3_USD_PER_SECOND_SILENT
        return seconds * rate
    if model_id == "veo-3.1":
        veo_rate = _VEO_USD_PER_SECOND.get(((resolution or "720p").lower(), generate_audio))
        return None if veo_rate is None else seconds * veo_rate
    return None


def media_cost_usd_of_run(
    *,
    model: FalModel,
    num_images: int | None = None,
    duration: str | None = None,
    resolution: str | None = None,
    generate_audio: bool | None = None,
) -> float | None:
    """Exact fal cost of a run whose price-affecting values are known (submit path).

    Takes the values ALREADY resolved by ``resolve_values`` — the same mapping that priced the
    run in credits and went upstream (ADR-061 §3). Anything else would price a different run
    than the one fal was asked for, which is the defect that ADR closed.
    """
    if model.kind == KIND_IMAGE:
        table = _IMAGE_USD_PER_IMAGE.get(model.id)
        if table is None:
            return None
        per_image = table.get(resolution or "") or table.get("1K")
        if per_image is None:
            return None
        return per_image * max(1, num_images or 1)
    seconds = _seconds(duration)
    if seconds is None:
        return None
    return _video_usd_of_run(
        model_id=model.id,
        seconds=seconds,
        resolution=resolution,
        generate_audio=bool(generate_audio),
    )


def _image_cost_from_credits(
    *, model: FalModel, base_credits: int, credits_charged: int, asset_count: int | None
) -> ProviderCost:
    table = _IMAGE_USD_PER_IMAGE.get(model.id)
    if table is None or credits_charged <= 0:
        return ProviderCost(None)
    tiers = {
        resolution: (image_unit_credits(model, resolution, base_credits=base_credits), usd)
        for resolution, usd in table.items()
    }
    ratios = {round(usd / unit, 9) for unit, usd in tiers.values() if unit > 0}
    if len(ratios) == 1:
        # Credits and dollars scale together across every tier (nano-banana-2), so the split
        # into resolution × image count cannot change the answer — no asset count needed.
        return ProviderCost(credits_charged * ratios.pop())
    if asset_count and credits_charged % asset_count == 0:
        unit_credits = credits_charged // asset_count
        for unit, usd in tiers.values():
            if unit == unit_credits:
                return ProviderCost(usd * asset_count)
    # Failed runs keep no assets, and the resolution was never persisted: the tiers that could
    # produce these credits disagree on price, so the honest answer is the ceiling of them.
    return ProviderCost(credits_charged * max(ratios), estimated=True)


def media_cost_usd_from_credits(
    *, model: FalModel, base_credits: int, credits_charged: int, asset_count: int | None
) -> ProviderCost:
    """Recover the fal cost of a historic job from what we charged for it.

    Works because credit prices were derived FROM the fal table (ADR-061 §2): the multipliers
    that scale credits — resolution tier, image count, duration packs, audio — are the same
    ones that scale the bill. Where fal's own unit is finer than a credit pack (per-second
    video), the recovered value is the pack ceiling and is returned as an estimate.
    """
    if base_credits <= 0 or credits_charged <= 0:
        return ProviderCost(None)
    if model.kind == KIND_IMAGE:
        return _image_cost_from_credits(
            model=model,
            base_credits=base_credits,
            credits_charged=credits_charged,
            asset_count=asset_count,
        )
    pack_usd = _VIDEO_BASE_PACK_USD.get(model.id)
    if pack_usd is None:
        return ProviderCost(None)
    usd = credits_charged * (pack_usd / base_credits)
    return ProviderCost(usd, estimated=model.id not in _VIDEO_CREDITS_ARE_EXACT)


def round_usd(value: float | None) -> float | None:
    """Round to the micro-dollar the API and DB column carry (``numeric(12, 6)``)."""
    if value is None:
        return None
    return math.floor(value * 1_000_000 + 0.5) / 1_000_000
