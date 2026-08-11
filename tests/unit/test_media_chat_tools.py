"""Unit tests for media.generate_* chat tools (ADR-068)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.chat.global_tools import (
    MEDIA_INSUFFICIENT_CREDITS_ERROR_CODE,
    MEDIA_INVALID_ERROR_CODE,
    MEDIA_NOT_CONFIGURED_ERROR_CODE,
    GlobalToolHandlers,
)
from app.chat.orchestrator import (
    _MEDIA_GENERATE_INSTRUCTION,
    _SYSTEM_PROMPT_CHAT,
    _SYSTEM_PROMPT_CODE,
)
from app.chat.tools import (
    ALL_TOOL_NAMES,
    ARGS_DEGRADE_TOOLS,
    GLOBAL_SERVER_SIDE_TOOLS,
    MUTATING_TOOLS,
    TOOL_GENERATION_MODES,
    TOOL_MEDIA_GENERATE_IMAGE,
    TOOL_MEDIA_GENERATE_VIDEO,
    anthropic_tool_definitions,
    offered_in_generation_mode,
    to_anthropic_tool_name,
    to_domain_tool_name,
    validate_tool_args,
)
from app.errors import InsufficientCreditsError, ValidationFailedError
from app.media_generation.service import MediaJobView
from tests.tool_registry import TOOLS_OFFERED_IN_EVERY_MODE, TOOLS_OFFERED_WITHOUT_PROJECT


@dataclass
class _FakeJob:
    id: uuid.UUID
    kind: str
    status: str
    model_id: str
    credits_charged: int


class _FakeMedia:
    def __init__(self) -> None:
        self.submit = AsyncMock()


def test_media_tools_registered_as_global_non_mutating_not_mode_gated() -> None:
    assert TOOL_MEDIA_GENERATE_IMAGE in GLOBAL_SERVER_SIDE_TOOLS
    assert TOOL_MEDIA_GENERATE_VIDEO in GLOBAL_SERVER_SIDE_TOOLS
    assert TOOL_MEDIA_GENERATE_IMAGE in ALL_TOOL_NAMES
    assert TOOL_MEDIA_GENERATE_VIDEO in ALL_TOOL_NAMES
    assert TOOL_MEDIA_GENERATE_IMAGE not in MUTATING_TOOLS
    assert TOOL_MEDIA_GENERATE_VIDEO not in MUTATING_TOOLS
    assert TOOL_MEDIA_GENERATE_IMAGE not in TOOL_GENERATION_MODES
    assert TOOL_MEDIA_GENERATE_VIDEO not in TOOL_GENERATION_MODES
    assert TOOL_MEDIA_GENERATE_IMAGE in ARGS_DEGRADE_TOOLS
    assert TOOL_MEDIA_GENERATE_VIDEO in ARGS_DEGRADE_TOOLS


def test_media_tool_name_maps() -> None:
    assert to_anthropic_tool_name(TOOL_MEDIA_GENERATE_IMAGE) == "media_generate_image"
    assert to_domain_tool_name("media_generate_image") == TOOL_MEDIA_GENERATE_IMAGE
    assert to_anthropic_tool_name(TOOL_MEDIA_GENERATE_VIDEO) == "media_generate_video"
    assert to_domain_tool_name("media_generate_video") == TOOL_MEDIA_GENERATE_VIDEO


def test_media_tools_offered_in_every_mode_with_and_without_project() -> None:
    for mode in ("general", "research", "reasoning", "study_learn"):
        assert offered_in_generation_mode(TOOL_MEDIA_GENERATE_IMAGE, mode)
        assert offered_in_generation_mode(TOOL_MEDIA_GENERATE_VIDEO, mode)
    defs_no_project = anthropic_tool_definitions(include_server_side=False)
    without = {to_domain_tool_name(d["name"]) for d in defs_no_project}
    with_project = {
        to_domain_tool_name(d["name"]) for d in anthropic_tool_definitions(include_server_side=True)
    }
    assert TOOL_MEDIA_GENERATE_IMAGE in without
    assert TOOL_MEDIA_GENERATE_VIDEO in without
    assert without == set(TOOLS_OFFERED_WITHOUT_PROJECT)
    assert with_project == set(TOOLS_OFFERED_IN_EVERY_MODE)


def test_system_prompts_include_media_instruction() -> None:
    assert _MEDIA_GENERATE_INSTRUCTION in _SYSTEM_PROMPT_CHAT
    assert _MEDIA_GENERATE_INSTRUCTION in _SYSTEM_PROMPT_CODE


def test_validate_image_args_rejects_exclusive_refs() -> None:
    with pytest.raises(ValueError):
        validate_tool_args(
            TOOL_MEDIA_GENERATE_IMAGE,
            {
                "model": "nano-banana-2",
                "prompt": "cat",
                "imageUrls": ["https://example.com/a.jpg"],
                "sourceJobId": str(uuid.uuid4()),
            },
        )


@pytest.mark.asyncio
async def test_media_generate_image_ok() -> None:
    job_id = uuid.uuid4()
    user_id = uuid.uuid4()
    media = _FakeMedia()
    media.submit.return_value = MediaJobView(
        job=_FakeJob(
            id=job_id,
            kind="image",
            status="queued",
            model_id="nano-banana-2",
            credits_charged=4,
        ),
        assets=[],
    )
    handlers = GlobalToolHandlers(media=media)  # type: ignore[arg-type]
    execution = await handlers.execute(
        tool_name=TOOL_MEDIA_GENERATE_IMAGE,
        args={"model": "nano-banana-2", "prompt": "a cat", "resolution": "1K"},
        user_id=user_id,
    )
    assert execution.is_error is False
    assert execution.result == {
        "jobId": str(job_id),
        "kind": "image",
        "status": "queued",
        "model": "nano-banana-2",
        "creditsCharged": 4,
    }
    media.submit.assert_awaited_once()
    kwargs = media.submit.await_args.kwargs
    assert kwargs["user_id"] == user_id
    assert kwargs["kind"] == "image"
    assert kwargs["model_id"] == "nano-banana-2"
    assert kwargs["prompt"] == "a cat"


@pytest.mark.asyncio
async def test_media_generate_without_service_is_not_configured() -> None:
    execution = await GlobalToolHandlers().execute(
        tool_name=TOOL_MEDIA_GENERATE_VIDEO,
        args={"model": "veo-3.1", "prompt": "cats"},
        user_id=uuid.uuid4(),
    )
    assert execution.is_error is True
    assert execution.error_code == MEDIA_NOT_CONFIGURED_ERROR_CODE


@pytest.mark.asyncio
async def test_media_generate_maps_insufficient_credits() -> None:
    media = _FakeMedia()
    media.submit.side_effect = InsufficientCreditsError("insufficient_credits")
    execution = await GlobalToolHandlers(media=media).execute(  # type: ignore[arg-type]
        tool_name=TOOL_MEDIA_GENERATE_IMAGE,
        args={"model": "nano-banana-2", "prompt": "x"},
        user_id=uuid.uuid4(),
    )
    assert execution.is_error is True
    assert execution.error_code == MEDIA_INSUFFICIENT_CREDITS_ERROR_CODE


@pytest.mark.asyncio
async def test_media_generate_maps_validation_error() -> None:
    media = _FakeMedia()
    media.submit.side_effect = ValidationFailedError("unknown model: nope")
    execution = await GlobalToolHandlers(media=media).execute(  # type: ignore[arg-type]
        tool_name=TOOL_MEDIA_GENERATE_IMAGE,
        args={"model": "nope", "prompt": "x"},
        user_id=uuid.uuid4(),
    )
    assert execution.is_error is True
    assert execution.error_code == MEDIA_INVALID_ERROR_CODE
    assert "unknown model" in (execution.error_message or "")


@pytest.mark.asyncio
async def test_media_generate_video_passes_image_url() -> None:
    job_id = uuid.uuid4()
    media = _FakeMedia()
    media.submit.return_value = MediaJobView(
        job=_FakeJob(
            id=job_id,
            kind="video",
            status="queued",
            model_id="veo-3.1",
            credits_charged=40,
        ),
        assets=[],
    )
    handlers = GlobalToolHandlers(media=media)  # type: ignore[arg-type]
    execution = await handlers.execute(
        tool_name=TOOL_MEDIA_GENERATE_VIDEO,
        args={
            "model": "veo-3.1",
            "prompt": "pan",
            "imageUrl": "https://example.com/a.jpg",
            "duration": "8s",
        },
        user_id=uuid.uuid4(),
    )
    assert execution.is_error is False
    kwargs: dict[str, Any] = media.submit.await_args.kwargs
    assert kwargs["kind"] == "video"
    assert kwargs["image_urls"] == ["https://example.com/a.jpg"]
    assert kwargs["params"]["duration"] == "8s"
