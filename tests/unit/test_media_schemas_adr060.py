"""Unit: /v1/media/* request schema validation (ADR-060, media-generation/02-api-contracts.md).

Reference images arrive as URLs that fal fetches server-side, so the scheme allowlist is a security
boundary, not a formatting nicety: anything but https must be refused before it is forwarded.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.media import ImageGenerationRequest, VideoGenerationRequest


def test_image_request_accepts_a_minimal_body() -> None:
    req = ImageGenerationRequest(model="nano-banana-2", prompt="a cat")
    assert req.imageUrls is None
    assert req.aspectRatio is None


def test_video_request_accepts_a_minimal_body() -> None:
    req = VideoGenerationRequest(model="veo-3.1", prompt="a city at dusk")
    assert req.imageUrl is None
    assert req.generateAudio is None


@pytest.mark.parametrize("schema", [ImageGenerationRequest, VideoGenerationRequest])
def test_unknown_fields_are_rejected(schema: type) -> None:
    with pytest.raises(ValidationError):
        schema(model="nano-banana-2", prompt="a cat", falEndpoint="fal-ai/anything")


@pytest.mark.parametrize("schema", [ImageGenerationRequest, VideoGenerationRequest])
def test_empty_prompt_is_rejected(schema: type) -> None:
    with pytest.raises(ValidationError):
        schema(model="nano-banana-2", prompt="")


@pytest.mark.parametrize("schema", [ImageGenerationRequest, VideoGenerationRequest])
def test_empty_model_is_rejected(schema: type) -> None:
    with pytest.raises(ValidationError):
        schema(model="", prompt="a cat")


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.com/a.png",
        "file:///etc/passwd",
        "data:image/png;base64,iVBORw0KGgo=",
        "//cdn.example.com/a.png",
        "https:/cdn.example.com/a.png",
    ],
)
def test_non_https_reference_images_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="https"):
        ImageGenerationRequest(model="nano-banana-pro", prompt="edit", imageUrls=[url])
    with pytest.raises(ValidationError, match="https"):
        VideoGenerationRequest(model="veo-3.1", prompt="animate", imageUrl=url)


def test_https_reference_images_are_accepted() -> None:
    urls = ["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"]
    req = ImageGenerationRequest(model="nano-banana-pro", prompt="blend", imageUrls=urls)
    assert req.imageUrls == urls


def test_more_than_fourteen_reference_images_are_rejected() -> None:
    urls = [f"https://cdn.example.com/{i}.png" for i in range(15)]
    with pytest.raises(ValidationError):
        ImageGenerationRequest(model="nano-banana-pro", prompt="blend", imageUrls=urls)


def test_num_images_is_bounded_to_one_through_four() -> None:
    assert ImageGenerationRequest(model="nano-banana-2", prompt="a cat", numImages=4).numImages == 4
    for bad in (0, 5):
        with pytest.raises(ValidationError):
            ImageGenerationRequest(model="nano-banana-2", prompt="a cat", numImages=bad)


def test_output_format_is_restricted_to_supported_containers() -> None:
    with pytest.raises(ValidationError):
        ImageGenerationRequest(model="nano-banana-2", prompt="a cat", outputFormat="tiff")


def test_over_long_prompt_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ImageGenerationRequest(model="nano-banana-2", prompt="x" * 5001)
