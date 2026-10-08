"""Fixed vendor routing of a media run through the proxy service (ADR-108 §2, §2.1).

Public model ids, variants, field allowlists, defaults and credit prices stay in ``catalog.py``
untouched. Every run gets an ordered route list: the vendor route when it can be built — Nano
Banana → ``sosana``, video → ``kie`` — then ``fal``. ``fal`` is always last and the only route that
executes the run with every catalog field; a field the vendor has no counterpart for is dropped
on the vendor route (§2.1) and takes effect only when the run falls back to ``fal``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.media_generation.catalog import (
    _IMAGE_ASPECT_RATIOS,
    KIND_IMAGE,
    KIND_VIDEO,
    FalModel,
    FalVariant,
    duration_seconds,
)

SERVICE_FAL = "fal"
SERVICE_KIE = "kie"
SERVICE_SOSANA = "sosana"

FAL_QUEUE_HOST = "https://queue.fal.run"
KIE_CREATE_TASK = "https://api.kie.ai/api/v1/jobs/createTask"
SOSANA_CREATE_IMAGE = "https://api.sosana.art/api/image/create-async"

# §2: the sosana route exists only for these resolutions and the base aspect-ratio set (the
# `0.5K` tier and the panoramic 4:1/1:4/8:1/1:8 of nano-banana-2 stay on fal).
_SOSANA_RESOLUTIONS = frozenset({"1K", "2K", "4K"})
_SOSANA_ASPECT_RATIOS = frozenset(_IMAGE_ASPECT_RATIOS)
# kling/v2-5-turbo-image-to-video-pro caps negative_prompt at 500 characters (text-to-video: 2500).
_KIE_KLING_25_IMAGE_NEGATIVE_PROMPT_MAX = 500


@dataclass(frozen=True)
class VendorRoute:
    """One concrete proxy call for a public model run."""

    service: str
    endpoint: str
    payload: dict[str, Any]
    catalog_endpoint: str


def fal_queue_url(endpoint: str) -> str:
    """The fal queue URL the proxy calls for a fal route: ``https://queue.fal.run/<endpoint>``."""
    return f"{FAL_QUEUE_HOST}/{endpoint.lstrip('/')}"


def fal_route(*, endpoint: str, payload: dict[str, Any]) -> VendorRoute:
    """The fal route — always present; its payload is EXACTLY the direct fal payload."""
    return VendorRoute(
        service=SERVICE_FAL,
        endpoint=fal_queue_url(endpoint),
        payload=dict(payload),
        catalog_endpoint=endpoint,
    )


def clip_video_prompt(text: str, *, limit: int) -> str:
    """Shorten to ``limit`` on a sentence end in the second half, otherwise on the last space."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    best = -1
    for index, char in enumerate(cut):
        if char in ".!?" and index >= limit // 2:
            best = index
    if best >= 0:
        return cut[: best + 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        return cut[:space].rstrip()
    return cut.rstrip()


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    if value is not None:
        target[key] = value


def _prompt(fal_payload: Mapping[str, Any]) -> str:
    return _as_str(fal_payload.get("prompt")) or ""


def _seconds(fal_payload: Mapping[str, Any]) -> int | None:
    raw = fal_payload.get("duration")
    if isinstance(raw, str):
        return duration_seconds(raw)
    return _as_int(raw)


def _duration_label(fal_payload: Mapping[str, Any]) -> str | None:
    seconds = _seconds(fal_payload)
    return None if seconds is None else str(seconds)


def _sosana_route(
    *,
    model: FalModel,
    variant: FalVariant,
    values: Mapping[str, Any],
    fal_payload: dict[str, Any],
) -> VendorRoute | None:
    """§2.1: ``seed``/``outputFormat`` are dropped; no ``aspectRatio`` → the vendor's ``auto``."""
    num_images = values.get("numImages")
    if isinstance(num_images, bool) or num_images != 1:
        return None
    resolution = values.get("resolution")
    if not isinstance(resolution, str) or resolution not in _SOSANA_RESOLUTIONS:
        return None
    aspect_ratio = values.get("aspectRatio")
    if aspect_ratio is not None and aspect_ratio not in _SOSANA_ASPECT_RATIOS:
        return None
    payload: dict[str, Any] = {
        "prompt": _prompt(fal_payload),
        "model": f"{model.id}-{resolution.lower()}",
        "aspect_ratio": _as_str(fal_payload.get("aspect_ratio")) or "auto",
        "prompt_optimization": False,
    }
    image_urls = fal_payload.get("image_urls")
    if isinstance(image_urls, list) and image_urls:
        payload["image_urls"] = list(image_urls)
    return VendorRoute(
        service=SERVICE_SOSANA,
        endpoint=SOSANA_CREATE_IMAGE,
        payload=payload,
        catalog_endpoint=variant.endpoint,
    )


_KieInput = tuple[str, dict[str, Any]]
_KieInputBuilder = Callable[[Mapping[str, Any], str | None], _KieInput]


def _kie_kling_25(fal_payload: Mapping[str, Any], image_url: str | None) -> _KieInput:
    data: dict[str, Any] = {"prompt": _prompt(fal_payload)}
    _put(data, "duration", _duration_label(fal_payload))
    _put(data, "cfg_scale", _as_float(fal_payload.get("cfg_scale")))
    negative_prompt = _as_str(fal_payload.get("negative_prompt"))
    if image_url is not None:
        data["image_url"] = image_url
        if negative_prompt:
            data["negative_prompt"] = clip_video_prompt(
                negative_prompt, limit=_KIE_KLING_25_IMAGE_NEGATIVE_PROMPT_MAX
            )
        return "kling/v2-5-turbo-image-to-video-pro", data
    _put(data, "negative_prompt", negative_prompt or None)
    _put(data, "aspect_ratio", _as_str(fal_payload.get("aspect_ratio")))
    return "kling/v2-5-turbo-text-to-video-pro", data


def _kie_kling_v3(fal_payload: Mapping[str, Any], image_url: str | None) -> _KieInput:
    # Kie lists multi_prompt as required even for single-shot runs, where it is ignored.
    data: dict[str, Any] = {
        "prompt": _prompt(fal_payload),
        "mode": "pro",
        "sound": fal_payload.get("generate_audio") is True,
        "multi_shots": False,
        "multi_prompt": [],
    }
    _put(data, "duration", _duration_label(fal_payload))
    if image_url is not None:
        data["image_urls"] = [image_url]
    else:
        _put(data, "aspect_ratio", _as_str(fal_payload.get("aspect_ratio")))
    return "kling-3.0/video", data


def _kie_veo(fal_payload: Mapping[str, Any], image_url: str | None) -> _KieInput:
    data: dict[str, Any] = {"prompt": _prompt(fal_payload)}
    if image_url is not None:
        data["image_urls"] = [image_url]
        data["generation_type"] = "FIRST_AND_LAST_FRAMES_2_VIDEO"
    else:
        data["generation_type"] = "TEXT_2_VIDEO"
    aspect = _as_str(fal_payload.get("aspect_ratio"))
    _put(data, "aspect_ratio", "Auto" if aspect == "auto" else aspect)
    _put(data, "resolution", _as_str(fal_payload.get("resolution")))
    _put(data, "duration", _seconds(fal_payload))
    return "veo-3-1", data


# Kie market models (POST /api/v1/jobs/createTask with {"model", "input"}).
_KIE_BUILDERS: Mapping[str, _KieInputBuilder] = MappingProxyType(
    {
        "kling-video": _kie_kling_25,
        "kling-video-v3": _kie_kling_v3,
        "veo-3.1": _kie_veo,
    }
)


def _start_frame(fal_payload: Mapping[str, Any], model: FalModel) -> str | None:
    if model.image_field is None:
        return None
    raw = fal_payload.get(model.image_field)
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    return raw if isinstance(raw, str) and raw else None


def _kie_route(
    *,
    model: FalModel,
    variant: FalVariant,
    fal_payload: dict[str, Any],
) -> VendorRoute | None:
    """§2.1: the start frame is the model's ``image_field``; an image variant without it → none."""
    builder = _KIE_BUILDERS.get(model.id)
    if builder is None:
        return None
    image_variant = model.image_variant
    if image_variant is not None and variant.endpoint == image_variant.endpoint:
        image_url = _start_frame(fal_payload, model)
        if image_url is None:
            return None
    else:
        image_url = None
    kie_model, kie_input = builder(fal_payload, image_url)
    return VendorRoute(
        service=SERVICE_KIE,
        endpoint=KIE_CREATE_TASK,
        payload={"model": kie_model, "input": kie_input},
        catalog_endpoint=variant.endpoint,
    )


def candidate_routes(
    *,
    model: FalModel,
    variant: FalVariant,
    values: Mapping[str, Any],
    fal_payload: dict[str, Any],
) -> list[VendorRoute]:
    """The routes of THIS run in fixed order: the vendor route when it is built, then fal."""
    vendor: VendorRoute | None = None
    if model.kind == KIND_IMAGE:
        vendor = _sosana_route(model=model, variant=variant, values=values, fal_payload=fal_payload)
    elif model.kind == KIND_VIDEO:
        vendor = _kie_route(model=model, variant=variant, fal_payload=fal_payload)
    fal = fal_route(endpoint=variant.endpoint, payload=fal_payload)
    return [fal] if vendor is None else [vendor, fal]
