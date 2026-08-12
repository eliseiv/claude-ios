"""Unit: chat image attachments bridge into media image-to-image (ADR-062 + ADR-070)."""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from app.chat.attachments import ImageAttachmentRef, prepare_attachments
from app.chat.global_tools import GlobalToolHandlers
from app.chat.media_choices import build_wizard_state
from app.chat.tools import TOOL_MEDIA_ASK_PARAMS, TOOL_MEDIA_GENERATE_IMAGE
from app.config import get_settings
from app.media_generation.service import MediaJobView, UploadedFile
from app.schemas.chat import AttachmentIn


def _tiny_png_b64() -> str:
    # 1x1 PNG
    raw = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
        b"\x00\x00IEND\xaeB`\x82"
    )
    return base64.b64encode(raw).decode("ascii")


@dataclass
class _FakeJob:
    id: uuid.UUID
    kind: str
    status: str
    model_id: str
    credits_charged: int


def test_prepare_attachments_keeps_image_refs() -> None:
    prepared = prepare_attachments(
        [
            AttachmentIn(
                type="image",
                mediaType="image/png",
                filename="selfie.png",
                data=_tiny_png_b64(),
            )
        ],
        get_settings(),
    )
    assert len(prepared.images) == 1
    assert prepared.images[0].filename == "selfie.png"
    assert prepared.images[0].media_type == "image/png"


def test_wizard_stores_image_urls_in_state() -> None:
    state = build_wizard_state(
        selection_id="s1",
        kind="image",
        prompt="on a chaise",
        source_job_id=None,
        image_urls=["https://fal.media/files/example.png"],
        answers={},
        credits_for=lambda m: m.default_credits,
    )
    assert state is not None
    assert state["imageUrls"] == ["https://fal.media/files/example.png"]


@pytest.mark.asyncio
async def test_ask_params_uploads_turn_images() -> None:
    media = AsyncMock()
    media.credits_for = lambda m: m.default_credits
    media.upload_reference_image = AsyncMock(
        return_value=UploadedFile(
            url="https://fal.media/files/uploaded.png",
            media_type="image/png",
            size=10,
            expires_at=None,
        )
    )
    handlers = GlobalToolHandlers(media=media)
    turn_images = [
        ImageAttachmentRef(media_type="image/png", filename="me.png", data=_tiny_png_b64())
    ]
    out = await handlers.execute(
        tool_name=TOOL_MEDIA_ASK_PARAMS,
        args={"kind": "image", "prompt": "lying on a chaise lounge"},
        turn_images=turn_images,
    )
    assert out.is_error is False
    assert out.result is not None
    assert out.result["imageUrls"] == ["https://fal.media/files/uploaded.png"]
    media.upload_reference_image.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_image_uploads_turn_images_when_no_urls() -> None:
    media = AsyncMock()
    media.credits_for = lambda m: m.default_credits
    media.upload_reference_image = AsyncMock(
        return_value=UploadedFile(
            url="https://fal.media/files/uploaded.png",
            media_type="image/png",
            size=10,
            expires_at=None,
        )
    )
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    media.submit = AsyncMock(
        return_value=MediaJobView(
            job=_FakeJob(
                id=job_id,
                kind="image",
                status="queued",
                model_id="nano-banana-2",
                credits_charged=4,
            ),
            assets=[],
        )
    )
    handlers = GlobalToolHandlers(media=media)
    turn_images = [
        ImageAttachmentRef(media_type="image/png", filename="me.png", data=_tiny_png_b64())
    ]
    out = await handlers.execute(
        tool_name=TOOL_MEDIA_GENERATE_IMAGE,
        args={"model": "nano-banana-2", "prompt": "on a chaise", "resolution": "1K"},
        user_id=user_id,
        turn_images=turn_images,
    )
    assert out.is_error is False
    submit_kwargs = media.submit.await_args.kwargs
    assert submit_kwargs["image_urls"] == ["https://fal.media/files/uploaded.png"]
    assert submit_kwargs["source_job_id"] is None


@pytest.mark.asyncio
async def test_ask_params_use_recent_image() -> None:
    from app.chat.attachment_refs import MEDIA_NO_RECENT_IMAGE_ERROR_CODE

    media = AsyncMock()
    media.credits_for = lambda m: m.default_credits
    handlers = GlobalToolHandlers(media=media)
    out = await handlers.execute(
        tool_name=TOOL_MEDIA_ASK_PARAMS,
        args={"kind": "image", "prompt": "on a beach", "useRecentImage": True},
        recent_image_urls=["https://fal.media/files/recent.png"],
    )
    assert out.is_error is False
    assert out.result is not None
    assert out.result["imageUrls"] == ["https://fal.media/files/recent.png"]

    missing = await handlers.execute(
        tool_name=TOOL_MEDIA_ASK_PARAMS,
        args={"kind": "image", "prompt": "on a beach", "useRecentImage": True},
        recent_image_urls=[],
    )
    assert missing.is_error is True
    assert missing.error_code == MEDIA_NO_RECENT_IMAGE_ERROR_CODE
