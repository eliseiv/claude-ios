"""Unit: the fal model catalog and its request→fal input projection (ADR-060 §2).

The catalog is a contract in two directions: iOS relies on the public model ids being stable, and
fal relies on receiving exactly the input keys its endpoint accepts. Both are pinned here.
"""

from __future__ import annotations

import pytest

from app.media_generation.catalog import (
    ALL_MODEL_IDS,
    KIND_IMAGE,
    KIND_VIDEO,
    all_models,
    build_fal_input,
    duration_seconds,
    fal_field_name,
    find_model,
    models_of_kind,
    price_multiplier,
)

# The five models the product ships with. Ids are a public contract (iOS sends them in `model`).
_EXPECTED_IDS = (
    "nano-banana-pro",
    "nano-banana-2",
    "kling-video",
    "kling-video-v3",
    "veo-3.1",
)

# Public model id -> (text-to-X endpoint, image-input endpoint).
_EXPECTED_ENDPOINTS = {
    "nano-banana-pro": ("fal-ai/nano-banana-pro", "fal-ai/nano-banana-pro/edit"),
    "nano-banana-2": ("fal-ai/nano-banana-2", "fal-ai/nano-banana-2/edit"),
    "kling-video": (
        "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
    ),
    "kling-video-v3": (
        "fal-ai/kling-video/v3/pro/text-to-video",
        "fal-ai/kling-video/v3/pro/image-to-video",
    ),
    "veo-3.1": ("fal-ai/veo3.1", "fal-ai/veo3.1/image-to-video"),
}


def test_catalog_exposes_exactly_the_five_shipped_models() -> None:
    assert tuple(m.id for m in all_models()) == _EXPECTED_IDS
    assert set(ALL_MODEL_IDS) == set(_EXPECTED_IDS)


def test_kinds_split_two_image_and_three_video_models() -> None:
    assert tuple(m.id for m in models_of_kind(KIND_IMAGE)) == ("nano-banana-pro", "nano-banana-2")
    assert tuple(m.id for m in models_of_kind(KIND_VIDEO)) == (
        "kling-video",
        "kling-video-v3",
        "veo-3.1",
    )


@pytest.mark.parametrize(("model_id", "endpoints"), _EXPECTED_ENDPOINTS.items())
def test_endpoint_ids_match_the_fal_model_ids(model_id: str, endpoints: tuple[str, str]) -> None:
    model = find_model(model_id)
    assert model is not None
    assert model.text_variant.endpoint == endpoints[0]
    assert model.image_variant is not None
    assert model.image_variant.endpoint == endpoints[1]


def test_unknown_model_id_is_not_resolved() -> None:
    assert find_model("stable-diffusion") is None


def test_reference_image_field_name_differs_per_model_family() -> None:
    # The whole reason the field name lives in the catalog: it is NOT uniform upstream.
    image_pro = find_model("nano-banana-pro")
    kling_25 = find_model("kling-video")
    kling_v3 = find_model("kling-video-v3")
    veo = find_model("veo-3.1")
    assert image_pro is not None and kling_25 is not None
    assert kling_v3 is not None and veo is not None

    assert (image_pro.image_field, image_pro.image_field_is_list) == ("image_urls", True)
    assert (kling_25.image_field, kling_25.image_field_is_list) == ("image_url", False)
    # v3 image-to-video names the start frame start_image_url, not image_url.
    assert (kling_v3.image_field, kling_v3.image_field_is_list) == ("start_image_url", False)
    assert (veo.image_field, veo.image_field_is_list) == ("image_url", False)


def test_field_names_are_translated_to_fal_snake_case() -> None:
    assert fal_field_name("negativePrompt") == "negative_prompt"
    assert fal_field_name("aspectRatio") == "aspect_ratio"
    assert fal_field_name("numImages") == "num_images"
    assert fal_field_name("generateAudio") == "generate_audio"
    assert fal_field_name("prompt") == "prompt"


def test_build_input_drops_none_values_so_upstream_defaults_apply() -> None:
    model = find_model("nano-banana-2")
    assert model is not None
    payload = build_fal_input(
        model=model,
        variant=model.text_variant,
        values={"prompt": "a cat", "aspectRatio": None, "resolution": "2K"},
        image_urls=[],
    )
    assert payload == {"prompt": "a cat", "resolution": "2K"}


def test_build_input_drops_fields_the_endpoint_does_not_accept() -> None:
    # Veo takes no cfg_scale or num_images; forwarding them would be an upstream 422.
    veo = find_model("veo-3.1")
    assert veo is not None
    payload = build_fal_input(
        model=veo,
        variant=veo.text_variant,
        values={"prompt": "a city", "cfgScale": 0.7, "numImages": 2, "generateAudio": True},
        image_urls=[],
    )
    assert payload == {"prompt": "a city", "generate_audio": True}


def test_kling_takes_cfg_scale_and_v3_also_audio() -> None:
    kling_25 = find_model("kling-video")
    kling_v3 = find_model("kling-video-v3")
    assert kling_25 is not None and kling_v3 is not None

    values = {"prompt": "a boat", "cfgScale": 0.8, "generateAudio": True}
    # 2.5 Turbo Pro has cfg_scale but no audio; v3 has both.
    assert build_fal_input(
        model=kling_25, variant=kling_25.text_variant, values=values, image_urls=[]
    ) == {"prompt": "a boat", "cfg_scale": 0.8}
    assert build_fal_input(
        model=kling_v3, variant=kling_v3.text_variant, values=values, image_urls=[]
    ) == {"prompt": "a boat", "cfg_scale": 0.8, "generate_audio": True}
    assert kling_v3.supports_audio and not kling_25.supports_audio


def test_build_input_drops_aspect_ratio_for_kling_image_to_video() -> None:
    # In image-to-video the aspect ratio comes from the start frame, so it is not a valid input.
    model = find_model("kling-video")
    assert model is not None
    assert model.image_variant is not None
    payload = build_fal_input(
        model=model,
        variant=model.image_variant,
        values={"prompt": "pan right", "aspectRatio": "16:9", "duration": "5"},
        image_urls=["https://cdn.example.com/frame.png"],
    )
    assert payload == {
        "prompt": "pan right",
        "duration": "5",
        "image_url": "https://cdn.example.com/frame.png",
    }


def test_build_input_passes_a_list_for_multi_image_models() -> None:
    model = find_model("nano-banana-pro")
    assert model is not None
    assert model.image_variant is not None
    urls = ["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"]
    payload = build_fal_input(
        model=model,
        variant=model.image_variant,
        values={"prompt": "blend these"},
        image_urls=urls,
    )
    assert payload == {"prompt": "blend these", "image_urls": urls}


def test_every_variant_accepts_a_prompt_and_has_a_positive_price() -> None:
    for model in all_models():
        assert "prompt" in model.text_variant.fields, model.id
        assert model.default_credits > 0, model.id
        if model.image_variant is not None:
            assert "prompt" in model.image_variant.fields, model.id
            assert model.image_field is not None, model.id
            assert model.max_input_images >= 1, model.id


# ------------------------- accepted values live on the variant -------------------------


def test_veo_allows_aspect_ratio_auto_only_with_a_reference_image() -> None:
    """The reason the enums hang off the variant: upstream really does differ per mode."""
    veo = find_model("veo-3.1")
    assert veo is not None
    assert veo.image_variant is not None
    assert veo.text_variant.aspect_ratios == ("16:9", "9:16")
    assert veo.image_variant.aspect_ratios == ("auto", "16:9", "9:16")


def test_kling_image_to_video_reports_no_aspect_ratio_at_all() -> None:
    for model_id in ("kling-video", "kling-video-v3"):
        model = find_model(model_id)
        assert model is not None
        assert model.image_variant is not None
        assert model.text_variant.aspect_ratios == ("16:9", "9:16", "1:1"), model_id
        # Empty set == "no such control in this mode" for the client.
        assert model.image_variant.aspect_ratios == (), model_id


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("nano-banana-pro", ("1K", "2K", "4K")),
        ("nano-banana-2", ("0.5K", "1K", "2K", "4K")),
        ("veo-3.1", ("720p", "1080p", "4k")),
        ("kling-video", ()),
        ("kling-video-v3", ()),
    ],
)
def test_resolutions_match_the_published_fal_enums(
    model_id: str, expected: tuple[str, ...]
) -> None:
    model = find_model(model_id)
    assert model is not None
    assert model.text_variant.resolutions == expected


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("kling-video", ("5", "10")),
        ("kling-video-v3", tuple(str(n) for n in range(3, 16))),
        ("veo-3.1", ("4s", "6s", "8s")),
        ("nano-banana-pro", ()),
    ],
)
def test_durations_match_the_published_fal_enums(model_id: str, expected: tuple[str, ...]) -> None:
    model = find_model(model_id)
    assert model is not None
    assert model.text_variant.durations == expected


def test_modes_are_named_per_kind_and_ordered_text_first() -> None:
    image = find_model("nano-banana-2")
    video = find_model("veo-3.1")
    assert image is not None and video is not None
    assert [mode for mode, _ in image.variants()] == ["textToImage", "imageToImage"]
    assert [mode for mode, _ in video.variants()] == ["textToVideo", "imageToVideo"]


# ------------------------- price scaling -------------------------


def test_duration_seconds_reads_both_upstream_spellings() -> None:
    assert duration_seconds("5") == 5
    assert duration_seconds("8s") == 8
    assert duration_seconds("auto") is None


def test_image_price_scales_with_the_number_of_images() -> None:
    model = find_model("nano-banana-2")
    assert model is not None
    assert price_multiplier(model=model, num_images=None, duration=None) == 1
    assert price_multiplier(model=model, num_images=1, duration=None) == 1
    assert price_multiplier(model=model, num_images=4, duration=None) == 4


def test_video_price_scales_with_the_requested_length() -> None:
    kling = find_model("kling-video-v3")  # base 5s
    veo = find_model("veo-3.1")  # base 8s
    assert kling is not None and veo is not None
    assert price_multiplier(model=kling, num_images=None, duration="5") == 1
    assert price_multiplier(model=kling, num_images=None, duration="10") == 2
    assert price_multiplier(model=kling, num_images=None, duration="15") == 3
    # Rounded up, and a shorter-than-base clip is never free.
    assert price_multiplier(model=kling, num_images=None, duration="7") == 2
    assert price_multiplier(model=veo, num_images=None, duration="4s") == 1
    assert price_multiplier(model=veo, num_images=None, duration="8s") == 1


def test_unparseable_or_absent_duration_falls_back_to_the_base_price() -> None:
    veo = find_model("veo-3.1")
    assert veo is not None
    assert price_multiplier(model=veo, num_images=None, duration=None) == 1
    assert price_multiplier(model=veo, num_images=None, duration="whatever") == 1
