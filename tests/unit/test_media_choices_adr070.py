"""Unit: mediaChoices wizard steps from catalog (ADR-070)."""

from __future__ import annotations

import pytest

from app.chat.media_choices import (
    STEP_MODEL,
    STEP_RESOLUTION,
    STEP_USE_LAST_IMAGE,
    build_wizard_state,
    effective_source_job_id,
    format_selection_summary,
    next_step_id,
    validate_and_merge_answers,
)
from app.media_generation.catalog import find_model

_LAST_IMAGE = "11111111-1111-1111-1111-111111111111"


def test_first_step_is_model() -> None:
    assert next_step_id({}, kind="image", source_job_id=None) == STEP_MODEL


def test_video_with_last_image_asks_use_last_first() -> None:
    assert (
        next_step_id(
            {},
            kind="video",
            source_job_id=None,
            last_image_job_id=_LAST_IMAGE,
        )
        == STEP_USE_LAST_IMAGE
    )


def test_use_last_image_skipped_when_source_job_set() -> None:
    assert (
        next_step_id(
            {},
            kind="video",
            source_job_id=_LAST_IMAGE,
            last_image_job_id=_LAST_IMAGE,
        )
        == STEP_MODEL
    )


def test_build_use_last_image_yes_no_options() -> None:
    state = build_wizard_state(
        selection_id="sel-1",
        kind="video",
        prompt="owl blinks",
        source_job_id=None,
        last_image_job_id=_LAST_IMAGE,
        answers={},
        credits_for=lambda m: m.default_credits,
    )
    assert state is not None
    assert state["step"] == STEP_USE_LAST_IMAGE
    assert state["lastImageJobId"] == _LAST_IMAGE
    q = state["questions"][0]
    assert q["question"] == "Использовать последнее фото?"
    by_value = {o["value"]: o["label"] for o in q["options"]}
    assert by_value == {"true": "Да", "false": "Нет"}


def test_yes_sets_effective_source_job() -> None:
    merged = validate_and_merge_answers(
        kind="video",
        source_job_id=None,
        last_image_job_id=_LAST_IMAGE,
        existing={},
        incoming={"useLastImage": "true"},
    )
    assert merged == {"useLastImage": "true"}
    assert (
        effective_source_job_id(
            source_job_id=None,
            last_image_job_id=_LAST_IMAGE,
            answers=merged,
        )
        == _LAST_IMAGE
    )
    assert (
        next_step_id(
            merged,
            kind="video",
            source_job_id=None,
            last_image_job_id=_LAST_IMAGE,
        )
        == STEP_MODEL
    )


def test_no_keeps_text_to_video() -> None:
    merged = validate_and_merge_answers(
        kind="video",
        source_job_id=None,
        last_image_job_id=_LAST_IMAGE,
        existing={},
        incoming={"useLastImage": False},
    )
    assert merged == {"useLastImage": "false"}
    assert (
        effective_source_job_id(
            source_job_id=None,
            last_image_job_id=_LAST_IMAGE,
            answers=merged,
        )
        is None
    )


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


def test_resolution_labels_include_tier_prices() -> None:
    state = build_wizard_state(
        selection_id="sel-1",
        kind="image",
        prompt="a cat",
        source_job_id=None,
        answers={"model": "nano-banana-2"},
        credits_for=lambda m: m.default_credits,
    )
    assert state is not None
    assert state["step"] == STEP_RESOLUTION
    q = state["questions"][0]
    by_value = {o["value"]: o for o in q["options"]}
    model = find_model("nano-banana-2")
    assert model is not None
    assert "1K" in by_value and "cr." in by_value["1K"]["label"]
    assert "4K" in by_value and "cr." in by_value["4K"]["label"]
    assert isinstance(by_value["1K"]["credits"], int)
    assert isinstance(by_value["4K"]["credits"], int)
    assert by_value["4K"]["credits"] > by_value["1K"]["credits"]
    # Question title carries prices so UI that ignores labels still shows cost.
    assert "1K:" in q["question"] and "cr." in q["question"]


def test_media_choices_wire_omits_fal_prompt() -> None:
    from app.chat.media_choices import media_choices_response

    state = build_wizard_state(
        selection_id="sel-1",
        kind="image",
        prompt="secret fal prompt text",
        source_job_id=None,
        answers={},
        credits_for=lambda m: m.default_credits,
    )
    assert state is not None
    wire = media_choices_response(state)
    assert "prompt" not in wire
    assert wire["step"] == STEP_MODEL


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


def test_format_selection_summary_is_one_line() -> None:
    text = format_selection_summary(
        prompt="a fluffy cat",
        kind="image",
        answers={"model": "nano-banana-2", "resolution": "2K", "aspectRatio": "1:1"},
        credits_charged=6,
        source_job_id=None,
    )
    assert text.startswith("Media: image")
    assert "fluffy cat" not in text  # fal prompt must not appear in history
    assert "2K" in text and "6 cr." in text
    assert "edit" not in text


def test_format_selection_summary_marks_from_photo() -> None:
    text = format_selection_summary(
        prompt="add a hat",
        kind="image",
        answers={"model": "nano-banana-2", "resolution": "1K"},
        credits_charged=4,
        source_job_id=_LAST_IMAGE,
    )
    assert "add a hat" not in text
    assert "from photo" in text


def test_format_selection_summary_use_last_image_yes() -> None:
    text = format_selection_summary(
        prompt="blink",
        kind="video",
        answers={"useLastImage": "true", "model": "kling-video"},
        credits_charged=10,
        source_job_id=None,
    )
    assert "from photo" in text
