"""Build quiz-like mediaChoices wizard steps from the fal catalog (ADR-070).

Options are ALWAYS taken from ``catalog.models_of_kind`` / variant allowlists — never from the
LLM. One question per response so cascading enums (resolution depends on model) stay correct.

Priced options (resolution / duration / audio) show the estimated credit cost in the label so the
user sees that 1K/2K/4K are not the same price.

When starting a **video** wizard and the chat has a prior generated image, the first card asks
``useLastImage`` (Да/Нет) — same UI as duration/resolution — before model/params.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from app.media_generation.catalog import FalModel, FalVariant, find_model, models_of_kind, run_price

# Wizard step ids (= question.id and answers keys).
STEP_USE_LAST_IMAGE = "useLastImage"
STEP_MODEL = "model"
STEP_RESOLUTION = "resolution"
STEP_DURATION = "duration"
STEP_GENERATE_AUDIO = "generateAudio"
STEP_ASPECT_RATIO = "aspectRatio"

_STEP_QUESTIONS: dict[str, str] = {
    STEP_USE_LAST_IMAGE: "Использовать последнее фото?",
    STEP_MODEL: "Choose a model",
    STEP_RESOLUTION: "Choose a resolution",
    STEP_DURATION: "Choose a duration",
    STEP_GENERATE_AUDIO: "Generate audio?",
    STEP_ASPECT_RATIO: "Choose an aspect ratio",
}


def _option(value: str, label: str, credits: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"value": value, "label": label}
    if credits is not None:
        out["credits"] = credits
    return out


def _question(
    step: str, options: list[dict[str, Any]], *, question: str | None = None
) -> dict[str, Any]:
    return {
        "id": step,
        "question": question or _STEP_QUESTIONS[step],
        "options": options,
    }


def _priced_question_title(step: str, options: list[dict[str, Any]]) -> str:
    """Question text that always surfaces tier prices (even if the client renders only values)."""
    bits: list[str] = []
    for opt in options:
        cr = opt.get("credits")
        if isinstance(cr, int):
            bits.append(f"{opt['value']}: {cr} cr.")
    if not bits:
        return _STEP_QUESTIONS[step]
    return f"{_STEP_QUESTIONS[step]} ({', '.join(bits)})"


def offers_use_last_image(
    *,
    kind: str,
    source_job_id: str | None,
    image_urls: list[str] | None,
    last_image_job_id: str | None,
) -> bool:
    """True when the wizard should open with the «Использовать последнее фото?» Да/Нет card."""
    if kind != "video":
        return False
    if source_job_id or image_urls:
        return False
    return bool(last_image_job_id)


def effective_source_job_id(
    *,
    source_job_id: str | None,
    last_image_job_id: str | None,
    answers: Mapping[str, str],
) -> str | None:
    """Resolve sourceJobId for variant selection / submit after useLastImage answers."""
    if source_job_id:
        return source_job_id
    if answers.get(STEP_USE_LAST_IMAGE) == "true" and last_image_job_id:
        return last_image_job_id
    return None


def _with_image(source_job_id: str | None, image_urls: list[str] | None = None) -> bool:
    """True when the run is image-to-* (prior job and/or uploaded reference URLs)."""
    return bool(source_job_id) or bool(image_urls)


def _variant_for(
    model: FalModel,
    *,
    source_job_id: str | None,
    image_urls: list[str] | None = None,
) -> FalVariant | None:
    return model.variant_for(with_image=_with_image(source_job_id, image_urls))


def estimate_run_credits(
    model: FalModel,
    answers: Mapping[str, str],
    *,
    base_credits: int,
    overrides: Mapping[str, str] | None = None,
) -> int:
    """Credits for a hypothetical submit with answers (+ overrides for option labels)."""
    merged = {**dict(answers), **dict(overrides or {})}
    audio: bool | None = None
    if STEP_GENERATE_AUDIO in merged:
        audio = merged[STEP_GENERATE_AUDIO] == "true"
    return run_price(
        model=model,
        base_credits=base_credits,
        resolution=merged.get(STEP_RESOLUTION),
        duration=merged.get(STEP_DURATION),
        generate_audio=audio,
    )


def next_step_id(
    answers: Mapping[str, str],
    *,
    kind: str,
    source_job_id: str | None,
    image_urls: list[str] | None = None,
    last_image_job_id: str | None = None,
) -> str | None:
    """Return the next unanswered step id, or None when the wizard is complete."""
    if (
        offers_use_last_image(
            kind=kind,
            source_job_id=source_job_id,
            image_urls=image_urls,
            last_image_job_id=last_image_job_id,
        )
        and STEP_USE_LAST_IMAGE not in answers
    ):
        return STEP_USE_LAST_IMAGE

    eff_source = effective_source_job_id(
        source_job_id=source_job_id,
        last_image_job_id=last_image_job_id,
        answers=answers,
    )

    if STEP_MODEL not in answers:
        return STEP_MODEL
    model = find_model(answers[STEP_MODEL])
    if model is None or model.kind != kind:
        return STEP_MODEL
    variant = _variant_for(model, source_job_id=eff_source, image_urls=image_urls)
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
    image_urls: list[str] | None = None,
    last_image_job_id: str | None = None,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Build (step, questions[]) for the next wizard step, or None if ready to submit."""
    step = next_step_id(
        answers,
        kind=kind,
        source_job_id=source_job_id,
        image_urls=image_urls,
        last_image_job_id=last_image_job_id,
    )
    if step is None:
        return None

    if step == STEP_USE_LAST_IMAGE:
        options = [
            _option("true", "Да"),
            _option("false", "Нет"),
        ]
        return step, [_question(STEP_USE_LAST_IMAGE, options)]

    eff_source = effective_source_job_id(
        source_job_id=source_job_id,
        last_image_job_id=last_image_job_id,
        answers=answers,
    )

    if step == STEP_MODEL:
        options = []
        for m in models_of_kind(kind):
            cr = credits_for(m)
            options.append(_option(m.id, f"{m.title} · from {cr} cr.", credits=cr))
        if not options:
            raise ValueError(f"no media models configured for kind={kind}")
        return step, [_question(STEP_MODEL, options)]

    model = find_model(answers[STEP_MODEL])
    if model is None or model.kind != kind:
        raise ValueError("selected model is not available")
    variant = _variant_for(model, source_job_id=eff_source, image_urls=image_urls)
    if variant is None:
        raise ValueError("selected model does not support this reference mode")
    base = credits_for(model)

    if step == STEP_RESOLUTION:
        options = []
        for v in variant.resolutions:
            cr = estimate_run_credits(
                model, answers, base_credits=base, overrides={STEP_RESOLUTION: v}
            )
            options.append(_option(v, f"{v} · {cr} cr.", credits=cr))
        return step, [
            _question(
                STEP_RESOLUTION, options, question=_priced_question_title(STEP_RESOLUTION, options)
            )
        ]
    if step == STEP_DURATION:
        options = []
        for v in variant.durations:
            cr = estimate_run_credits(
                model, answers, base_credits=base, overrides={STEP_DURATION: v}
            )
            options.append(_option(v, f"{v} · {cr} cr.", credits=cr))
        return step, [
            _question(
                STEP_DURATION, options, question=_priced_question_title(STEP_DURATION, options)
            )
        ]
    if step == STEP_GENERATE_AUDIO:
        yes_cr = estimate_run_credits(
            model, answers, base_credits=base, overrides={STEP_GENERATE_AUDIO: "true"}
        )
        no_cr = estimate_run_credits(
            model, answers, base_credits=base, overrides={STEP_GENERATE_AUDIO: "false"}
        )
        options = [
            _option("true", f"Yes · {yes_cr} cr.", credits=yes_cr),
            _option("false", f"No · {no_cr} cr.", credits=no_cr),
        ]
        return step, [
            _question(
                STEP_GENERATE_AUDIO,
                options,
                question=_priced_question_title(STEP_GENERATE_AUDIO, options),
            )
        ]
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
    image_urls: list[str] | None = None,
    last_image_job_id: str | None = None,
) -> set[str]:
    """Catalog allowlist for ``step`` given answers already accepted before it."""
    if (
        next_step_id(
            answers_before,
            kind=kind,
            source_job_id=source_job_id,
            image_urls=image_urls,
            last_image_job_id=last_image_job_id,
        )
        != step
    ):
        return set()
    built = build_step_questions(
        kind=kind,
        answers=answers_before,
        source_job_id=source_job_id,
        image_urls=image_urls,
        last_image_job_id=last_image_job_id,
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
    image_urls: list[str] | None = None,
    last_image_job_id: str | None = None,
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
        step = next_step_id(
            built,
            kind=kind,
            source_job_id=source_job_id,
            image_urls=image_urls,
            last_image_job_id=last_image_job_id,
        )
        if step is None or step not in candidate:
            break
        allowed = allowed_values_for_step(
            step,
            kind=kind,
            answers_before=built,
            source_job_id=source_job_id,
            image_urls=image_urls,
            last_image_job_id=last_image_job_id,
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
    image_urls: list[str] | None = None,
    last_image_job_id: str | None = None,
) -> dict[str, Any] | None:
    """Persisted wizard state for the next question, or None when ready to submit."""
    urls = [u for u in (image_urls or []) if isinstance(u, str) and u]
    last_id = (
        last_image_job_id if isinstance(last_image_job_id, str) and last_image_job_id else None
    )
    built = build_step_questions(
        kind=kind,
        answers=answers,
        source_job_id=source_job_id,
        image_urls=urls or None,
        last_image_job_id=last_id,
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
        "lastImageJobId": last_id,
        "imageUrls": urls,
        "answers": dict(answers),
        "step": step,
        "questions": questions,
    }


def media_choices_response(state: Mapping[str, Any]) -> dict[str, Any]:
    """Wire shape for ``ChatResponse.mediaChoices`` (no internal answers / fal prompt)."""
    return {
        "selectionId": state["selectionId"],
        "kind": state["kind"],
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


def format_selection_summary(
    *,
    prompt: str,
    kind: str,
    answers: Mapping[str, str],
    credits_charged: int,
    source_job_id: str | None,
) -> str:
    """Single history bubble for the completed wizard (replaces N intermediate taps).

    The fal ``prompt`` is intentionally omitted — it is an internal generation input, not UI copy.
    """
    _ = prompt  # kept in signature for call-site stability; not shown to the user
    parts: list[str] = [kind]
    model = find_model(answers.get(STEP_MODEL, ""))
    if model is not None:
        parts.append(model.title)
    elif STEP_MODEL in answers:
        parts.append(answers[STEP_MODEL])
    for key in (STEP_RESOLUTION, STEP_DURATION, STEP_ASPECT_RATIO):
        if key in answers:
            parts.append(answers[key])
    if STEP_GENERATE_AUDIO in answers:
        parts.append("audio" if answers[STEP_GENERATE_AUDIO] == "true" else "silent")
    if answers.get(STEP_USE_LAST_IMAGE) == "true" or source_job_id:
        parts.append("from photo")
    parts.append(f"{credits_charged} cr.")
    return "Media: " + " · ".join(parts)
