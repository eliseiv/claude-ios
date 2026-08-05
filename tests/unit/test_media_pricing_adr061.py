"""Unit: the calibrated prices and the "what we bill is what we send" invariant (ADR-061).

Two things are pinned here, and they are the two things ADR-061 exists to fix.

1. **Every offered run covers its own cost at least twice**, measured against the *cheapest* credit
   a user can buy. The dollar figures below are fal's published per-model prices as of 2026-08-05;
   they are written out so a future price change fails a test instead of quietly eating the margin.
2. **A parameter the client omitted is filled in by us, not by fal.** Before ADR-061 an omitted
   field was dropped so fal's own default applied — and fal defaults audio to on and Veo's duration
   to 8s, so the run we charged for and the run we asked for were different runs.
"""

from __future__ import annotations

import math

import pytest

from app.media_generation.catalog import (
    KIND_IMAGE,
    all_models,
    build_fal_input,
    find_model,
    resolve_values,
    run_price,
)

# The cheapest credit a user can buy: the largest TOKEN_PRODUCTS pack ($99.99 / 2000 credits).
# Margin has to hold at this price, not at the average — a bulk buyer pays exactly this.
_CREDIT_USD = 0.05
_MIN_COVERAGE = 2.0


def _price(model_id: str, **kwargs: object) -> int:
    model = find_model(model_id)
    assert model is not None
    return run_price(model=model, base_credits=model.default_credits, **kwargs)  # type: ignore[arg-type]


# --------------------------------- coverage of fal's bill ---------------------------------

# (model id, run parameters, what fal charges us for that exact run in USD).
_COVERAGE_CASES = [
    # Images: $0.06/$0.08/$0.12/$0.16 per image by resolution tier.
    ("nano-banana-2", {"resolution": "0.5K"}, 0.06),
    ("nano-banana-2", {"resolution": "1K"}, 0.08),
    ("nano-banana-2", {"resolution": "2K"}, 0.12),
    ("nano-banana-2", {"resolution": "4K"}, 0.16),
    ("nano-banana-2", {"resolution": "4K", "num_images": 4}, 0.16 * 4),
    # nano-banana-pro: $0.15 at 1K and 2K, $0.30 at 4K.
    ("nano-banana-pro", {"resolution": "1K"}, 0.15),
    ("nano-banana-pro", {"resolution": "2K"}, 0.15),
    ("nano-banana-pro", {"resolution": "4K"}, 0.30),
    # Kling 2.5 Turbo Pro: $0.35 for 5 s, +$0.07 per extra second.
    ("kling-video", {"duration": "5"}, 0.35),
    ("kling-video", {"duration": "10"}, 0.70),
    # Kling V3 Pro: $0.112/s without audio, $0.168/s with.
    ("kling-video-v3", {"duration": "5"}, 5 * 0.112),
    ("kling-video-v3", {"duration": "15"}, 15 * 0.112),
    ("kling-video-v3", {"duration": "5", "generate_audio": True}, 5 * 0.168),
    ("kling-video-v3", {"duration": "15", "generate_audio": True}, 15 * 0.168),
    # Veo 3.1: $0.20/s at 720p and 1080p, $0.40/s with audio; 4k is $0.40 and $0.60.
    ("veo-3.1", {"duration": "4s", "resolution": "720p"}, 4 * 0.20),
    ("veo-3.1", {"duration": "8s", "resolution": "1080p"}, 8 * 0.20),
    ("veo-3.1", {"duration": "8s", "resolution": "1080p", "generate_audio": True}, 8 * 0.40),
    ("veo-3.1", {"duration": "4s", "resolution": "4k"}, 4 * 0.40),
    ("veo-3.1", {"duration": "8s", "resolution": "4k", "generate_audio": True}, 8 * 0.60),
]


@pytest.mark.parametrize(("model_id", "params", "fal_usd"), _COVERAGE_CASES)
def test_every_offered_run_covers_fals_bill_at_least_twice(
    model_id: str, params: dict[str, object], fal_usd: float
) -> None:
    revenue = _price(model_id, **params) * _CREDIT_USD
    assert (
        revenue >= fal_usd * _MIN_COVERAGE
    ), f"{model_id} {params}: ${revenue:.2f} against ${fal_usd:.2f} upstream"


def test_calibrated_base_prices() -> None:
    """The numbers ADR-061 §2 committed to. A silent edit of the catalog fails here."""
    expected = {
        "nano-banana-pro": 8,
        "nano-banana-2": 4,
        "kling-video": 14,
        "kling-video-v3": 23,
        "veo-3.1": 32,
    }
    assert {model.id: model.default_credits for model in all_models()} == expected


def test_kling_v3_audio_is_billed_and_rounded_up() -> None:
    """x1.5 is not a whole multiplier; the price must round UP, never down."""
    kling = find_model("kling-video-v3")
    assert kling is not None
    assert kling.audio_multiplier == 1.5
    # 23 x 1 pack x 1.5 = 34.5 -> 35, not 34.
    assert _price("kling-video-v3", duration="5", generate_audio=True) == 35
    assert _price("kling-video-v3", duration="15", generate_audio=True) == math.ceil(23 * 3 * 1.5)
    # Without audio the price stays exactly integral.
    assert _price("kling-video-v3", duration="15") == 69


def test_audio_never_makes_a_run_cheaper() -> None:
    for model in all_models():
        if model.kind == KIND_IMAGE or model.audio_multiplier is None:
            continue
        for duration in model.text_variant.durations:
            silent = run_price(model=model, base_credits=model.default_credits, duration=duration)
            loud = run_price(
                model=model,
                base_credits=model.default_credits,
                duration=duration,
                generate_audio=True,
            )
            assert loud >= silent, (model.id, duration)


# --------------------------------- priced defaults ---------------------------------


def test_every_default_is_a_field_the_variant_forwards() -> None:
    """A default for a key this endpoint does not accept would be silently dropped downstream —
    priced but never sent, which is the exact failure ADR-061 removes."""
    for model in all_models():
        for _mode, variant in model.variants():
            for key in variant.defaults:
                assert key in variant.fields, (model.id, variant.endpoint, key)


def test_every_enum_default_is_an_accepted_value() -> None:
    """Defaults bypass request validation, so an out-of-set default would reach fal unchecked and
    come back as a paid-for 422."""
    for model in all_models():
        for _mode, variant in model.variants():
            for key in ("resolution", "duration"):
                default = variant.defaults.get(key)
                if default is None:
                    continue
                assert default in variant.allowed(key), (model.id, variant.endpoint, key)


def test_price_affecting_fields_all_have_defaults() -> None:
    """If a field can move the price it must not be left to the provider."""
    priced = {"resolution", "duration", "generateAudio", "numImages"}
    for model in all_models():
        for _mode, variant in model.variants():
            missing = (priced & variant.fields) - set(variant.defaults)
            assert not missing, (model.id, variant.endpoint, missing)


def test_non_price_fields_have_no_defaults() -> None:
    """Aspect ratio, seed and friends stay the provider's business."""
    free = {"aspectRatio", "outputFormat", "negativePrompt", "cfgScale", "seed", "prompt"}
    for model in all_models():
        for _mode, variant in model.variants():
            assert not (free & set(variant.defaults)), (model.id, variant.endpoint)


@pytest.mark.parametrize(
    ("model_id", "with_image"),
    [(model.id, with_image) for model in all_models() for with_image in (False, True)],
)
def test_an_omitted_field_costs_the_same_as_the_default_sent_explicitly(
    model_id: str, with_image: bool
) -> None:
    """The whole point of ADR-061 §3: omitting a knob must not change what the run costs."""
    model = find_model(model_id)
    assert model is not None
    variant = model.variant_for(with_image=with_image)
    if variant is None:
        pytest.skip("model has no reference-image variant")

    omitted = resolve_values(variant=variant, values={"prompt": "x"})
    explicit = resolve_values(variant=variant, values={"prompt": "x", **variant.defaults})
    assert omitted == explicit

    def price(values: dict[str, object]) -> int:
        return run_price(
            model=model,
            base_credits=model.default_credits,
            num_images=values.get("numImages"),  # type: ignore[arg-type]
            duration=values.get("duration"),  # type: ignore[arg-type]
            resolution=values.get("resolution"),  # type: ignore[arg-type]
            generate_audio=values.get("generateAudio"),  # type: ignore[arg-type]
        )

    assert price(omitted) == price(explicit)


def test_defaults_reach_the_fal_payload() -> None:
    """Veo used to inherit fal's 8s + audio-on defaults while being billed for a silent 4s pack."""
    veo = find_model("veo-3.1")
    assert veo is not None
    values = resolve_values(variant=veo.text_variant, values={"prompt": "a city"})
    payload = build_fal_input(model=veo, variant=veo.text_variant, values=values, image_urls=[])
    assert payload == {
        "prompt": "a city",
        "duration": "8s",
        "resolution": "720p",
        "generate_audio": False,
    }


def test_a_client_value_always_beats_the_default() -> None:
    veo = find_model("veo-3.1")
    assert veo is not None
    values = resolve_values(
        variant=veo.text_variant,
        values={"prompt": "a city", "duration": "4s", "generateAudio": True},
    )
    assert values["duration"] == "4s"
    assert values["generateAudio"] is True
    # An explicit False is a value, not an absence: it must not be replaced by the default.
    silent = resolve_values(
        variant=veo.text_variant, values={"prompt": "a city", "generateAudio": False}
    )
    assert silent["generateAudio"] is False


def test_defaults_are_not_applied_to_fields_the_mode_lacks() -> None:
    """Kling 2.5 image-to-video has no aspect ratio and no audio; nothing may be invented for it."""
    kling = find_model("kling-video")
    assert kling is not None
    assert kling.image_variant is not None
    values = resolve_values(variant=kling.image_variant, values={"prompt": "pan"})
    assert values == {"prompt": "pan", "duration": "5"}
