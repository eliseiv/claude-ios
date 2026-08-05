"""Unit: MEDIA_MODEL_CREDITS parsing, server-side pricing and result normalization (ADR-060).

Pricing is the anti-tamper surface: the request has no price field at all, and a malformed or
hostile override table must degrade to the catalog defaults rather than to a free run.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.media_generation.catalog import KIND_IMAGE, KIND_VIDEO, find_model
from app.media_generation.service import MediaGenerationService, _normalize_result
from app.schemas.media import ImageGenerationRequest, VideoGenerationRequest


def _settings(raw: str) -> Settings:
    return Settings(MEDIA_MODEL_CREDITS=raw)  # type: ignore[call-arg]


def _service(settings: Settings) -> MediaGenerationService:
    # Pricing needs no collaborators; the None-typed ones are never touched by credits_for.
    return MediaGenerationService(
        repo=None,  # type: ignore[arg-type]
        fal=None,  # type: ignore[arg-type]
        wallet=None,  # type: ignore[arg-type]
        settings=settings,
    )


# --------------------------- MEDIA_MODEL_CREDITS parsing ---------------------------


def test_valid_override_mapping_is_parsed() -> None:
    parsed = _settings('{"veo-3.1":400,"nano-banana-2":3}').media_model_credits()
    assert parsed == {"veo-3.1": 400, "nano-banana-2": 3}


def test_unset_override_yields_empty_mapping() -> None:
    assert Settings().media_model_credits() == {}


@pytest.mark.parametrize(
    "raw",
    [
        "{not valid json",
        "[1, 2, 3]",  # not an object
        '"veo-3.1"',  # not an object
    ],
)
def test_malformed_override_degrades_to_no_overrides(raw: str) -> None:
    assert _settings(raw).media_model_credits() == {}


@pytest.mark.parametrize(
    "raw",
    [
        '{"veo-3.1":0}',  # zero would make the run free
        '{"veo-3.1":-10}',
        '{"veo-3.1":true}',  # bool is an int subclass — must not become 1
        '{"veo-3.1":"400"}',
        '{"veo-3.1":12.5}',
    ],
)
def test_non_positive_int_overrides_are_dropped(raw: str) -> None:
    assert _settings(raw).media_model_credits() == {}


# --------------------------- server-side pricing ---------------------------


def test_price_falls_back_to_the_catalog_default() -> None:
    service = _service(Settings())
    model = find_model("veo-3.1")
    assert model is not None
    assert service.credits_for(model) == model.default_credits


def test_price_uses_the_operator_override_when_present() -> None:
    service = _service(_settings('{"veo-3.1":400}'))
    model = find_model("veo-3.1")
    assert model is not None
    assert service.credits_for(model) == 400


def test_image_override_scales_resolution_tiers_from_the_1k_cell() -> None:
    # Override sets the 1K cell; 4K stays at 2× that cell (catalog 8/4).
    service = _service(_settings('{"nano-banana-2":10}'))
    model = find_model("nano-banana-2")
    assert model is not None
    assert service.price_of(model=model, resolution="1K") == 10
    assert service.price_of(model=model, resolution="4K") == 20


def test_a_dropped_override_cannot_make_a_run_free() -> None:
    service = _service(_settings('{"veo-3.1":0}'))
    model = find_model("veo-3.1")
    assert model is not None
    assert service.credits_for(model) == model.default_credits


def test_request_schemas_have_no_price_field() -> None:
    # Anti-tamper (cf. BR-TP-1): the client cannot state what a generation costs.
    for schema in (ImageGenerationRequest, VideoGenerationRequest):
        assert "credits" not in schema.model_fields
        assert "creditsCharged" not in schema.model_fields


# --------------------------- result normalization ---------------------------


def test_image_output_is_normalized_to_assets() -> None:
    body: dict[str, Any] = {
        "images": [
            {"url": "https://cdn/a.png", "content_type": "image/png", "file_name": "a.png"},
            {"url": "https://cdn/b.png", "content_type": "image/png", "file_name": "b.png"},
        ],
        "description": "two cats",
    }
    assert _normalize_result(body, kind=KIND_IMAGE) == {
        "assets": [
            {"url": "https://cdn/a.png", "contentType": "image/png", "fileName": "a.png"},
            {"url": "https://cdn/b.png", "contentType": "image/png", "fileName": "b.png"},
        ],
        "description": "two cats",
    }


def test_video_output_is_normalized_to_assets() -> None:
    body: dict[str, Any] = {"video": {"url": "https://cdn/out.mp4"}, "seed": 7}
    assert _normalize_result(body, kind=KIND_VIDEO) == {
        "assets": [{"url": "https://cdn/out.mp4", "contentType": None, "fileName": None}],
        "seed": 7,
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"images": []},
        {"images": [{"content_type": "image/png"}]},  # no url
        {"images": ["https://cdn/a.png"]},  # not an object
        {"video": {"url": ""}},
    ],
)
def test_output_without_a_usable_url_yields_no_assets(body: dict[str, Any]) -> None:
    for kind in (KIND_IMAGE, KIND_VIDEO):
        assert _normalize_result(body, kind=kind)["assets"] == []
