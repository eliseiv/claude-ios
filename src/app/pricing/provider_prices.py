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

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.media_generation.catalog import KIND_IMAGE, FalModel, image_unit_credits

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


def provider_of_chat_model(model: str) -> str:
    """Which vendor's bill this model lands on. Only two chat providers exist here (ADR-033)."""
    return PROVIDER_ANTHROPIC if model.strip().lower().startswith("claude") else PROVIDER_OPENAI


def chat_cost_usd_by_provider(usages: Sequence[Mapping[str, Any]]) -> dict[str, float] | None:
    """Cost of one chat TURN, split by vendor.

    A tool-loop turn calls the provider several times (each ``assistant`` step is one call)
    and is billed for each, so summing the steps is the cost — not an approximation of it.
    The split exists because a turn CAN change model mid-loop, and the money then belongs to
    two different bills.

    Returns ``None`` when the turn holds no usage at all, or when ANY of its calls is
    unpriceable — an unknown model, or a step with no token counts to price. A partial sum
    would understate the cost while looking like a full one.
    """
    per_provider: dict[str, float] = {}
    for usage in usages:
        model = usage.get("model")
        prices = CHAT_TOKEN_PRICES.get(model) if isinstance(model, str) else None
        if prices is None or not isinstance(model, str) or not _has_token_counts(usage):
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
