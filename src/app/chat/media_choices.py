"""Build quiz-like mediaChoices wizard steps from the fal catalog (ADR-070).

Options are ALWAYS taken from ``catalog.models_of_kind`` / variant allowlists — never from the
LLM. One question per response so cascading enums (resolution depends on model) stay correct.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.media_generation.catalog import FalModel, FalVariant, find_model, models_of_kind

# Wizard step ids (= question.id and answers keys).
STEP_MODEL = "model"
STEP_RESOLUTION = "resolution"
STEP_DURATION = "duration"
STEP_GENERATE_AUDIO = "generateAudio"
STEP_ASPECT_RATIO = "aspectRatio"

_STEP_QUESTIONS: dict[str, str] = {
    STEP_MODEL: "Choose a model",
    STEP_RESOLUTION: "Choose a resolution",
    STEP_DURATION: "Choose a duration",
    STEP_GENERATE_AUDIO: "Generate audio?",
    STEP_ASPECT_RATIO: "Choose an aspect ratio",
}


def _option(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _question(step: str, options: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": step,
        "question": _STEP_QUESTIONS[step],
        "options": options,
    }


def _with_image(source_job_id: str | None) -> bool:
    return bool(source_job_id)


def _variant_for(model: FalModel, *, source_job_id: str | None) -> FalVariant | None:
    return model.variant_for(with_image=_with_image(source_job_id))


def next_step_id(answers: Mapping[str, str], *, kind: str, source_job_id: str | None) -> str | None:
    """Return the next unanswered step id, or None when the wizard is complete."""
    if STEP_MODEL not in answers:
        return STEP_MODEL
    model = find_model(answers[STEP_MODEL])
    if model is None or model.kind != kind:
        return STEP_MODEL
    variant = _variant_for(model, source_job_id=source_job_id)
    if variant is None:
        return STEP_MODEL
    if variant.resolutions and STEP_RESOLUTION not in answers:
        return STEP_RESOLUTION
    if variant.durations and STEP_DURATION not in answers:
        return STEP_DURATION
    if (
        model.supports_audio
        and "generateAudio" in variant.fields
        and STEP_GENERATE_AUDIO not in answers
    ):
        return STEP_GENERATE_AUDIO
    if variant.aspect_ratios and STEP_ASPECT_RATIO not in answers:
        return STEP_ASPECT_RATIO
    return None


def build_step_questions(
    *,
    kind: str,
    answers: Mapping[str, str],
    source_job_id: str | None,
    credits_for: Callable[[FalModel], int],
) -> tuple[str, list[dict[str, Any]]] | None:
    """Build (step, questions[]) for the next wizard step, or None if ready to submit."""
    step = next_step_id(answers, kind=kind, source_job_id=source_job_id)
    if step is None:
        return None

    if step == STEP_MODEL:
        options = [
            _option(m.id, f"{m.title} · from {credits_for(m)} cr.") for m in models_of_kind(kind)
        ]
        if not options:
            raise ValueError(f"no media models configured for kind={kind}")
        return step, [_question(STEP_MODEL, options)]

    model = find_model(answers[STEP_MODEL])
    if model is None or model.kind != kind:
        raise ValueError("selected model is not available")
    variant = _variant_for(model, source_job_id=source_job_id)
    if variant is None:
        raise ValueError("selected model does not support this reference mode")

    if step == STEP_RESOLUTION:
        options = [_option(v, v) for v in variant.resolutions]
        return step, [_question(STEP_RESOLUTION, options)]
    if step == STEP_DURATION:
        options = [_option(v, v) for v in variant.durations]
        return step, [_question(STEP_DURATION, options)]
    if step == STEP_GENERATE_AUDIO:
        options = [_option("true", "Yes"), _option("false", "No")]
        return step, [_question(STEP_GENERATE_AUDIO, options)]
    if step == STEP_ASPECT_RATIO:
        options = [_option(v, v) for v in variant.aspect_ratios]
        return step, [_question(STEP_ASPECT_RATIO, options)]
    raise ValueError(f"unknown wizard step: {step}")


def allowed_values_for_step(
    step: str,
    *,
    kind: str,
    answers_before: Mapping[str, str],
    source_job_id: str | None,
) -> set[str]:
    """Catalog allowlist for ``step`` given answers already accepted before it."""
    if next_step_id(answers_before, kind=kind, source_job_id=source_job_id) != step:
        return set()
    built = build_step_questions(
        kind=kind,
        answers=answers_before,
        source_job_id=source_job_id,
        credits_for=lambda m: m.default_credits,
    )
    if built is None or built[0] != step:
        return set()
    return {opt["value"] for opt in built[1][0]["options"]}


def _normalize_answer_value(raw_value: Any) -> str:
    if isinstance(raw_value, bool):
        return "true" if raw_value else "false"
    if isinstance(raw_value, str | int):
        return str(raw_value)
    raise ValueError("answers values must be strings")


def validate_and_merge_answers(
    *,
    kind: str,
    source_job_id: str | None,
    existing: Mapping[str, str],
    incoming: Mapping[str, Any],
) -> dict[str, str]:
    """Merge ``incoming`` into ``existing`` along the wizard order; catalog-check every value."""
    candidate: dict[str, str] = dict(existing)
    for raw_key, raw_value in incoming.items():
        if not isinstance(raw_key, str):
            raise ValueError("answers keys must be strings")
        if raw_key not in _STEP_QUESTIONS:
            raise ValueError(f"unknown mediaSelection answer key: {raw_key}")
        candidate[raw_key] = _normalize_answer_value(raw_value)

    built: dict[str, str] = {}
    while True:
        step = next_step_id(built, kind=kind, source_job_id=source_job_id)
        if step is None or step not in candidate:
            break
        allowed = allowed_values_for_step(
            step, kind=kind, answers_before=built, source_job_id=source_job_id
        )
        value = candidate[step]
        if value not in allowed:
            raise ValueError(f"invalid value for {step}")
        built[step] = value
    extra = set(candidate) - set(built)
    if extra:
        raise ValueError(f"unexpected mediaSelection answers: {sorted(extra)}")
    return built


def build_wizard_state(
    *,
    selection_id: str,
    kind: str,
    prompt: str,
    source_job_id: str | None,
    answers: Mapping[str, str],
    credits_for: Callable[[FalModel], int],
) -> dict[str, Any] | None:
    """Persisted wizard state for the next question, or None when ready to submit."""
    built = build_step_questions(
        kind=kind,
        answers=answers,
        source_job_id=source_job_id,
        credits_for=credits_for,
    )
    if built is None:
        return None
    step, questions = built
    return {
        "selectionId": selection_id,
        "kind": kind,
        "prompt": prompt,
        "sourceJobId": source_job_id,
        "answers": dict(answers),
        "step": step,
        "questions": questions,
    }


def media_choices_response(state: Mapping[str, Any]) -> dict[str, Any]:
    """Wire shape for ``ChatResponse.mediaChoices`` (no internal answers)."""
    return {
        "selectionId": state["selectionId"],
        "kind": state["kind"],
        "prompt": state["prompt"],
        "step": state["step"],
        "questions": state["questions"],
    }


def submit_params_from_answers(answers: Mapping[str, str]) -> dict[str, Any]:
    """Map wizard answers to MediaGenerationService.submit ``params``."""
    params: dict[str, Any] = {}
    if STEP_RESOLUTION in answers:
        params["resolution"] = answers[STEP_RESOLUTION]
    if STEP_DURATION in answers:
        params["duration"] = answers[STEP_DURATION]
    if STEP_ASPECT_RATIO in answers:
        params["aspectRatio"] = answers[STEP_ASPECT_RATIO]
    if STEP_GENERATE_AUDIO in answers:
        params["generateAudio"] = answers[STEP_GENERATE_AUDIO] == "true"
    return params
