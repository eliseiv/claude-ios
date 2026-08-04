"""Server-side registry of the fal.ai models exposed by ``/v1/media/*`` (ADR-060 §2).

The registry is the single source of truth for three things the client must never decide:

1. **Which fal endpoint id a public model id resolves to.** The client sends a short stable id
   (``nano-banana-pro``); the server maps it to the vendor endpoint
   (``fal-ai/nano-banana-pro``). A prompt-only request and a request carrying a reference image
   hit *different* fal endpoints, so each model declares both.
2. **Which input fields are forwarded upstream.** fal input schemas differ per model (Veo has no
   ``negative_prompt``, Kling has no ``resolution``, only image models take ``num_images``) and
   reject unknown keys. Each variant declares an allowlist, so an unsupported field is dropped
   instead of turning into an upstream 422.
3. **The default credit price.** Overridable per model via ``MEDIA_MODEL_CREDITS`` but never
   taken from the request body (anti-tamper, symmetric with TOKEN_PRODUCTS/BR-TP-1).

The reference-image field name is NOT uniform upstream: image models take a list
(``image_urls``), Kling v2.5 and Veo take ``image_url``, Kling v3 takes ``start_image_url``.
That asymmetry lives here so the service and the schemas stay model-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_IMAGE = "image"
KIND_VIDEO = "video"

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
    "seed": "seed",
}


@dataclass(frozen=True)
class FalVariant:
    """One fal endpoint of a model plus the request fields it accepts."""

    endpoint: str
    fields: frozenset[str]


@dataclass(frozen=True)
class FalModel:
    """A model as offered by ``GET /v1/media/models``.

    ``text_variant`` is used when the request carries no reference image, ``image_variant`` when
    it does. ``image_variant is None`` means the model is text-only and ``imageUrls`` is a 422.
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
    aspect_ratios: tuple[str, ...]
    resolutions: tuple[str, ...]
    durations: tuple[str, ...]

    def variant_for(self, *, with_image: bool) -> FalVariant | None:
        return self.image_variant if with_image else self.text_variant


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
_IMAGE_FIELDS = frozenset({"prompt", "aspectRatio", "resolution", "numImages", "outputFormat"})

_MODELS: tuple[FalModel, ...] = (
    FalModel(
        id="nano-banana-pro",
        title="Nano Banana Pro (Gemini 3 Pro Image)",
        kind=KIND_IMAGE,
        default_credits=8,
        text_variant=FalVariant(endpoint="fal-ai/nano-banana-pro", fields=_IMAGE_FIELDS),
        image_variant=FalVariant(endpoint="fal-ai/nano-banana-pro/edit", fields=_IMAGE_FIELDS),
        image_field="image_urls",
        image_field_is_list=True,
        max_input_images=14,
        aspect_ratios=_IMAGE_ASPECT_RATIOS,
        resolutions=("1K", "2K", "4K"),
        durations=(),
    ),
    FalModel(
        id="nano-banana-2",
        title="Nano Banana 2 (Gemini 3.1 Flash Image)",
        kind=KIND_IMAGE,
        default_credits=4,
        text_variant=FalVariant(endpoint="fal-ai/nano-banana-2", fields=_IMAGE_FIELDS),
        image_variant=FalVariant(endpoint="fal-ai/nano-banana-2/edit", fields=_IMAGE_FIELDS),
        image_field="image_urls",
        image_field_is_list=True,
        max_input_images=14,
        aspect_ratios=_IMAGE_ASPECT_RATIOS,
        resolutions=("512x512", "1K", "2K", "4K"),
        durations=(),
    ),
    FalModel(
        id="kling-video",
        title="Kling Video 2.5 Turbo Pro",
        kind=KIND_VIDEO,
        default_credits=120,
        text_variant=FalVariant(
            endpoint="fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
            fields=frozenset({"prompt", "negativePrompt", "aspectRatio", "duration"}),
        ),
        image_variant=FalVariant(
            # Aspect ratio is derived from the reference image upstream, so it is not forwarded.
            endpoint="fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            fields=frozenset({"prompt", "negativePrompt", "duration"}),
        ),
        image_field="image_url",
        image_field_is_list=False,
        max_input_images=1,
        aspect_ratios=("16:9", "9:16", "1:1"),
        resolutions=(),
        durations=("5", "10"),
    ),
    FalModel(
        id="kling-video-v3",
        title="Kling Video V3 Pro",
        kind=KIND_VIDEO,
        default_credits=200,
        text_variant=FalVariant(
            endpoint="fal-ai/kling-video/v3/pro/text-to-video",
            fields=frozenset({"prompt", "negativePrompt", "aspectRatio", "duration"}),
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/kling-video/v3/pro/image-to-video",
            fields=frozenset({"prompt", "negativePrompt", "duration"}),
        ),
        # v3 image-to-video names the reference frame start_image_url, not image_url.
        image_field="start_image_url",
        image_field_is_list=False,
        max_input_images=1,
        aspect_ratios=("16:9", "9:16", "1:1"),
        resolutions=(),
        durations=("5", "10"),
    ),
    FalModel(
        id="veo-3.1",
        title="Veo 3.1 (Google)",
        kind=KIND_VIDEO,
        default_credits=300,
        text_variant=FalVariant(
            endpoint="fal-ai/veo3.1",
            fields=frozenset({"prompt", "aspectRatio", "duration", "resolution", "generateAudio"}),
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/veo3.1/image-to-video",
            fields=frozenset({"prompt", "aspectRatio", "duration", "resolution", "generateAudio"}),
        ),
        image_field="image_url",
        image_field_is_list=False,
        max_input_images=1,
        aspect_ratios=("auto", "16:9", "9:16"),
        resolutions=("720p", "1080p"),
        durations=("4s", "6s", "8s"),
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
    for field, value in values.items():
        if value is None or field not in variant.fields:
            continue
        payload[fal_field_name(field)] = value
    if image_urls and model.image_field is not None:
        payload[model.image_field] = (
            list(image_urls) if model.image_field_is_list else image_urls[0]
        )
    return payload
