"""Unit: mediaChoices wizard steps from catalog (ADR-070)."""

from __future__ import annotations

import pytest

from app.chat.media_choices import (
    STEP_MODEL,
    STEP_RESOLUTION,
    build_wizard_state,
    next_step_id,
    validate_and_merge_answers,
)


def test_first_step_is_model() -> None:
    assert next_step_id({}, kind="image", source_job_id=None) == STEP_MODEL


def test_build_model_step_options_from_catalog() -> None:
    state = build_wizard_state(
        selection_id="sel-1",
        kind="image",
        prompt="a cat",
        source_job_id=None,
        answers={},
        credits_for=lambda m: m.default_credits,
    )
    assert state is not None
    assert state["step"] == STEP_MODEL
    values = {o["value"] for o in state["questions"][0]["options"]}
    assert "nano-banana-2" in values
    assert "nano-banana-pro" in values


def test_after_model_asks_resolution() -> None:
    assert (
        next_step_id({"model": "nano-banana-2"}, kind="image", source_job_id=None)
        == STEP_RESOLUTION
    )


def test_validate_rejects_hallucinated_model() -> None:
    with pytest.raises(ValueError, match="invalid value"):
        validate_and_merge_answers(
            kind="image",
            source_job_id=None,
            existing={},
            incoming={"model": "not-a-real-model"},
        )


def test_validate_accepts_catalog_resolution() -> None:
    merged = validate_and_merge_answers(
        kind="image",
        source_job_id=None,
        existing={"model": "nano-banana-2"},
        incoming={"model": "nano-banana-2", "resolution": "2K"},
    )
    assert merged == {"model": "nano-banana-2", "resolution": "2K"}
    assert next_step_id(merged, kind="image", source_job_id=None) == "aspectRatio"
