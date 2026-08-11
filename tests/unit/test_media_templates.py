"""Unit: media template schema + cover URL / validation helpers (ADR-066)."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.errors import ValidationFailedError
from app.media_generation.templates_repository import MediaTemplatesRepository
from app.media_generation.templates_service import MediaTemplatesService
from app.schemas.media_templates import MediaTemplateCreateRequest

# 1×1 PNG
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_create_request_rejects_bad_id() -> None:
    with pytest.raises(ValidationError):
        MediaTemplateCreateRequest.model_validate(
            {
                "id": "Bad-Id",
                "kind": "image",
                "title": "T",
                "prompt": "p",
                "model": "nano-banana-2",
                "cover": {"mediaType": "image/png", "data": _PNG_B64},
            }
        )


def test_create_request_rejects_video_params_on_image() -> None:
    with pytest.raises(ValidationError):
        MediaTemplateCreateRequest.model_validate(
            {
                "id": "ok_id",
                "kind": "image",
                "title": "T",
                "prompt": "p",
                "model": "nano-banana-2",
                "parameters": {"duration": "5"},
                "cover": {"mediaType": "image/png", "data": _PNG_B64},
            }
        )


def test_create_request_rejects_video_with_two_inputs() -> None:
    with pytest.raises(ValidationError):
        MediaTemplateCreateRequest.model_validate(
            {
                "id": "ok_id",
                "kind": "video",
                "title": "T",
                "prompt": "p",
                "model": "kling-video",
                "requiredInputImages": 2,
                "cover": {"mediaType": "image/png", "data": _PNG_B64},
            }
        )


def test_cover_url_absolute_when_domain_set() -> None:
    settings = Settings(SERVICE_DOMAIN="ravelumi.shop")
    svc = MediaTemplatesService(repo=MediaTemplatesRepository(None), settings=settings)  # type: ignore[arg-type]
    assert (
        svc.cover_url_for("profile_picture")
        == "https://ravelumi.shop/v1/media/templates/profile_picture/cover"
    )


def test_cover_url_relative_when_domain_empty() -> None:
    settings = Settings(SERVICE_DOMAIN="")
    svc = MediaTemplatesService(repo=MediaTemplatesRepository(None), settings=settings)  # type: ignore[arg-type]
    assert svc.cover_url_for("x") == "/v1/media/templates/x/cover"


def test_validate_model_rejects_wrong_kind() -> None:
    settings = Settings()
    svc = MediaTemplatesService(repo=MediaTemplatesRepository(None), settings=settings)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailedError):
        svc._validate_model_and_params(
            kind="image",
            model_id="kling-video",
            required_input_images=0,
            parameters={},
        )


def test_validate_model_rejects_bad_aspect() -> None:
    settings = Settings()
    svc = MediaTemplatesService(repo=MediaTemplatesRepository(None), settings=settings)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailedError):
        svc._validate_model_and_params(
            kind="image",
            model_id="nano-banana-2",
            required_input_images=0,
            parameters={"aspectRatio": "not-a-ratio"},
        )


def test_png_placeholder_is_valid_cover_bytes() -> None:
    raw = base64.b64decode(_PNG_B64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
