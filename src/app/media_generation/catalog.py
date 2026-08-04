"""Server-side registry of the fal.ai models exposed by ``/v1/media/*`` (ADR-060 §2).

The registry is the single source of truth for four things the client must never decide:

1. **Which fal endpoint id a public model id resolves to.** The client sends a short stable id
   (``nano-banana-pro``); the server maps it to the vendor endpoint
   (``fal-ai/nano-banana-pro``). A prompt-only request and a request carrying a reference image
   hit *different* fal endpoints, so each model declares both.
2. **Which input fields are forwarded upstream.** fal input schemas differ per model *and per
   endpoint* (Veo image-to-video takes ``aspect_ratio``, Kling image-to-video does not; only image
   models take ``num_images``) and reject unknown keys. Each variant declares an allowlist, so an
   unsupported field is dropped instead of turning into an upstream 422.
3. **Which values each parameter accepts.** The enums live on the *variant*, not the model, because
   they genuinely differ between modes: Veo text-to-video allows ``16:9``/``9:16`` while its
   image-to-video variant also allows ``auto``. Validating against the variant is what lets us
   reject a bad value before spending credits instead of paying for an upstream 422.
4. **The default credit price** and how it scales with the requested output (more images / longer
   video cost fal more). Overridable per model via ``MEDIA_MODEL_CREDITS`` but never taken from the
   request body (anti-tamper, symmetric with TOKEN_PRODUCTS/BR-TP-1).

Values here mirror the published fal input schemas
(``fal.ai/api/openapi/queue/openapi.json?endpoint_id=…``); they are deliberately a *subset*.
Moderation and prompt-plumbing knobs (``safety_tolerance``, ``system_prompt``) are not exposed —
letting a client weaken safety limits or inject a system prompt is not a generation parameter.
``sync_mode`` is likewise withheld: it would defeat the queue contract the whole module is built on.

The reference-image field name is NOT uniform upstream: image models take a list
(``image_urls``), Kling v2.5 and Veo take ``image_url``, Kling v3 takes ``start_image_url``.
That asymmetry lives here so the service and the schemas stay model-agnostic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

KIND_IMAGE = "image"
KIND_VIDEO = "video"

# Generation modes as reported by GET /v1/media/models. The client picks one by whether it sends a
# reference image; naming them per kind keeps the iOS UI honest about what each mode does.
MODE_TEXT_TO_IMAGE = "textToImage"
MODE_IMAGE_TO_IMAGE = "imageToImage"
MODE_TEXT_TO_VIDEO = "textToVideo"
MODE_IMAGE_TO_VIDEO = "imageToVideo"

# Public request field -> fal input field. Our wire format is camelCase (schemas/common.py
# convention), fal's is snake_case.
_FAL_FIELD_NAMES: dict[str, str] = {
    "prompt": "prompt",
    "negativePrompt": "negative_prompt",
    "aspectRatio": "aspect_ratio",
    "resolution": "resolution",
    "numImages": "num_images",
    "outputFormat": "output_format",
    "duration": "duration",
    "generateAudio": "generate_audio",
    "cfgScale": "cfg_scale",
    "seed": "seed",
}


@dataclass(frozen=True)
class FalVariant:
    """One fal endpoint of a model: the fields it accepts and the values they may take.

    ``fields`` is the forwarding allowlist. The three tuples are the accepted values of the
    same-named request fields; an empty tuple means "this endpoint has no such parameter", which is
    also how ``GET /v1/media/models`` tells the client not to render that control.
    """

    endpoint: str
    fields: frozenset[str]
    aspect_ratios: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    durations: tuple[str, ...] = ()

    def allowed(self, request_field: str) -> tuple[str, ...]:
        return {
            "aspectRatio": self.aspect_ratios,
            "resolution": self.resolutions,
            "duration": self.durations,
        }[request_field]


@dataclass(frozen=True)
class FalModel:
    """A model as offered by ``GET /v1/media/models``.

    ``text_variant`` is used when the request carries no reference image, ``image_variant`` when
    it does. ``image_variant is None`` means the model is text-only and ``imageUrls`` is a 422.

    ``base_duration_seconds`` is the video length the price covers; a longer run scales the price
    proportionally (fal bills per second of output). ``None`` for image models, which scale by
    ``numImages`` instead.
    """

    id: str
    title: str
    kind: str
    default_credits: int
    text_variant: FalVariant
    image_variant: FalVariant | None
    image_field: str | None
    image_field_is_list: bool
    max_input_images: int
    base_duration_seconds: int | None = None
    supports_audio: bool = field(default=False)

    def variant_for(self, *, with_image: bool) -> FalVariant | None:
        return self.image_variant if with_image else self.text_variant

    def mode_for(self, *, with_image: bool) -> str:
        if self.kind == KIND_IMAGE:
            return MODE_IMAGE_TO_IMAGE if with_image else MODE_TEXT_TO_IMAGE
        return MODE_IMAGE_TO_VIDEO if with_image else MODE_TEXT_TO_VIDEO

    def variants(self) -> tuple[tuple[str, FalVariant], ...]:
        """(mode, variant) pairs in display order — the shape the catalog endpoint reports."""
        pairs = [(self.mode_for(with_image=False), self.text_variant)]
        if self.image_variant is not None:
            pairs.append((self.mode_for(with_image=True), self.image_variant))
        return tuple(pairs)


# Gemini-image aspect ratios. nano-banana-2 additionally accepts the extreme panoramic ratios.
_IMAGE_ASPECT_RATIOS = (
    "auto",
    "21:9",
    "16:9",
    "3:2",
    "4:3",
    "5:4",
    "1:1",
    "4:5",
    "3:4",
    "2:3",
    "9:16",
)
_NB2_ASPECT_RATIOS = (*_IMAGE_ASPECT_RATIOS, "4:1", "1:4", "8:1", "1:8")
_IMAGE_FIELDS = frozenset(
    {"prompt", "aspectRatio", "resolution", "numImages", "outputFormat", "seed"}
)
# Kling exposes cfg_scale (prompt adherence) on every variant; aspect_ratio only text-to-video,
# where there is no reference frame to take it from.
_KLING_25_FIELDS = frozenset({"prompt", "negativePrompt", "duration", "cfgScale"})
_KLING_V3_FIELDS = frozenset({"prompt", "negativePrompt", "duration", "cfgScale", "generateAudio"})
_KLING_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
_KLING_V3_DURATIONS = tuple(str(n) for n in range(3, 16))
_VEO_FIELDS = frozenset(
    {"prompt", "negativePrompt", "aspectRatio", "duration", "resolution", "generateAudio", "seed"}
)
_VEO_RESOLUTIONS = ("720p", "1080p", "4k")
_VEO_DURATIONS = ("4s", "6s", "8s")

_MODELS: tuple[FalModel, ...] = (
    FalModel(
        id="nano-banana-pro",
        title="Nano Banana Pro (Gemini 3 Pro Image)",
        kind=KIND_IMAGE,
        default_credits=8,
        text_variant=FalVariant(
            endpoint="fal-ai/nano-banana-pro",
            fields=_IMAGE_FIELDS,
            aspect_ratios=_IMAGE_ASPECT_RATIOS,
            resolutions=("1K", "2K", "4K"),
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/nano-banana-pro/edit",
            fields=_IMAGE_FIELDS,
            aspect_ratios=_IMAGE_ASPECT_RATIOS,
            resolutions=("1K", "2K", "4K"),
        ),
        image_field="image_urls",
        image_field_is_list=True,
        max_input_images=14,
    ),
    FalModel(
        id="nano-banana-2",
        title="Nano Banana 2 (Gemini 3.1 Flash Image)",
        kind=KIND_IMAGE,
        default_credits=4,
        text_variant=FalVariant(
            endpoint="fal-ai/nano-banana-2",
            fields=_IMAGE_FIELDS,
            aspect_ratios=_NB2_ASPECT_RATIOS,
            resolutions=("0.5K", "1K", "2K", "4K"),
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/nano-banana-2/edit",
            fields=_IMAGE_FIELDS,
            aspect_ratios=_NB2_ASPECT_RATIOS,
            resolutions=("0.5K", "1K", "2K", "4K"),
        ),
        image_field="image_urls",
        image_field_is_list=True,
        max_input_images=14,
    ),
    FalModel(
        id="kling-video",
        title="Kling Video 2.5 Turbo Pro",
        kind=KIND_VIDEO,
        default_credits=120,
        text_variant=FalVariant(
            endpoint="fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
            fields=_KLING_25_FIELDS | {"aspectRatio"},
            aspect_ratios=_KLING_ASPECT_RATIOS,
            durations=("5", "10"),
        ),
        image_variant=FalVariant(
            # Aspect ratio is derived from the reference image upstream, so it is not accepted.
            endpoint="fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            fields=_KLING_25_FIELDS,
            durations=("5", "10"),
        ),
        image_field="image_url",
        image_field_is_list=False,
        max_input_images=1,
        base_duration_seconds=5,
    ),
    FalModel(
        id="kling-video-v3",
        title="Kling Video V3 Pro",
        kind=KIND_VIDEO,
        default_credits=200,
        text_variant=FalVariant(
            endpoint="fal-ai/kling-video/v3/pro/text-to-video",
            fields=_KLING_V3_FIELDS | {"aspectRatio"},
            aspect_ratios=_KLING_ASPECT_RATIOS,
            durations=_KLING_V3_DURATIONS,
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/kling-video/v3/pro/image-to-video",
            fields=_KLING_V3_FIELDS,
            durations=_KLING_V3_DURATIONS,
        ),
        # v3 image-to-video names the reference frame start_image_url, not image_url.
        image_field="start_image_url",
        image_field_is_list=False,
        max_input_images=1,
        base_duration_seconds=5,
        supports_audio=True,
    ),
    FalModel(
        id="veo-3.1",
        title="Veo 3.1 (Google)",
        kind=KIND_VIDEO,
        default_credits=300,
        text_variant=FalVariant(
            endpoint="fal-ai/veo3.1",
            fields=_VEO_FIELDS,
            # Unlike its image-to-video sibling, text-to-video has no "auto".
            aspect_ratios=("16:9", "9:16"),
            resolutions=_VEO_RESOLUTIONS,
            durations=_VEO_DURATIONS,
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/veo3.1/image-to-video",
            fields=_VEO_FIELDS,
            aspect_ratios=("auto", "16:9", "9:16"),
            resolutions=_VEO_RESOLUTIONS,
            durations=_VEO_DURATIONS,
        ),
        image_field="image_url",
        image_field_is_list=False,
        max_input_images=1,
        base_duration_seconds=8,
        supports_audio=True,
    ),
)

_BY_ID: dict[str, FalModel] = {model.id: model for model in _MODELS}

ALL_MODEL_IDS: frozenset[str] = frozenset(_BY_ID)


def all_models() -> tuple[FalModel, ...]:
    """Every registered model, in catalog (display) order."""
    return _MODELS


def models_of_kind(kind: str) -> tuple[FalModel, ...]:
    return tuple(model for model in _MODELS if model.kind == kind)


def find_model(model_id: str) -> FalModel | None:
    return _BY_ID.get(model_id)


def fal_field_name(request_field: str) -> str:
    """Translate a public camelCase request field to its fal snake_case input key."""
    return _FAL_FIELD_NAMES.get(request_field, request_field)


def duration_seconds(value: str) -> int | None:
    """Seconds encoded in a fal duration value (``"5"`` → 5, ``"8s"`` → 8).

    The two families spell it differently, and the price scales with the real length, so the
    number has to be recovered rather than compared as a string.
    """
    digits = value.rstrip("s")
    return int(digits) if digits.isdigit() else None


def price_multiplier(*, model: FalModel, num_images: int | None, duration: str | None) -> int:
    """How many base prices one run costs, given what the client asked for.

    fal bills per produced image and per second of video, so a run asking for 4 images or a 15 s
    clip costs it 4x/3x — charging the flat base price would make the longest options the cheapest
    per unit and lose money on them. Rounded up, and never below 1, so a shorter-than-base clip is
    not free.
    """
    if model.kind == KIND_IMAGE:
        return max(1, num_images or 1)
    base = model.base_duration_seconds
    seconds = duration_seconds(duration) if duration else None
    if base is None or seconds is None:
        return 1
    return max(1, math.ceil(seconds / base))


def build_fal_input(
    *,
    model: FalModel,
    variant: FalVariant,
    values: dict[str, object],
    image_urls: list[str],
) -> dict[str, object]:
    """Project a validated request into the fal input object for ``variant``.

    Only fields in ``variant.fields`` survive: a field the client sent that this particular
    endpoint does not accept is dropped rather than forwarded (fal rejects unknown keys). ``None``
    values are dropped too, so upstream defaults apply instead of an explicit null.
    """
    payload: dict[str, object] = {}
    for field_name, value in values.items():
        if value is None or field_name not in variant.fields:
            continue
        payload[fal_field_name(field_name)] = value
    if image_urls and model.image_field is not None:
        payload[model.image_field] = (
            list(image_urls) if model.image_field_is_list else image_urls[0]
        )
    return payload
