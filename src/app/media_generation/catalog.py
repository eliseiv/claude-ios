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
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

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

    ``defaults`` covers the fields that CHANGE THE PRICE (ADR-061 §3). An omitted field used to be
    dropped so fal's own default applied — but fal's defaults are not ours (``generate_audio`` is
    ``true`` upstream, ``duration`` is ``8s`` on Veo), so the run we billed and the run we asked
    for were two different runs. Filling the default here, once, before pricing, makes both use the
    same value. Parameters that cannot move the price (``aspectRatio``, ``outputFormat``, ``seed``,
    ``cfgScale``, ``negativePrompt``) deliberately get no default: fal may keep deciding those.
    """

    endpoint: str
    fields: frozenset[str]
    aspect_ratios: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ()
    durations: tuple[str, ...] = ()
    defaults: Mapping[str, str | int | bool] = field(default_factory=dict)

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
    ``numImages`` and ``resolution_credits`` instead.

    ``resolution_credits`` (image) is the whole-credit price of *one* image at each resolution;
    ``default_credits`` is the 1K tier (operator override via MEDIA_MODEL_CREDITS scales the table
    so the 1K cell stays equal to the override). ``resolution_multipliers`` / ``audio_multiplier``
    (video) scale the duration-pack price — both Veo and Kling v3 bill fal more for audio, Veo also
    for 4K. ``audio_multiplier`` is a float because Kling v3's audio surcharge is ×1.5, not ×2; the
    final price is rounded UP so a fractional multiplier can never price a run below its own cost.
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
    resolution_credits: Mapping[str, int] = field(default_factory=dict)
    resolution_multipliers: Mapping[str, int] = field(default_factory=dict)
    audio_multiplier: float | None = None

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

# Server-side defaults for the price-affecting fields (ADR-061 §3). Everything here is sent
# upstream explicitly and priced with the same value, so fal's own defaults never reach the bill.
# `generateAudio` is False on purpose — upstream it is True, which silently doubled (Veo) or
# multiplied by 1.5 (Kling v3) the cost of a request that never asked for sound. The rest match
# fal's defaults, so what the models actually produce does not change.
_IMAGE_DEFAULTS: Mapping[str, str | int | bool] = MappingProxyType(
    {"resolution": "1K", "numImages": 1}
)
_KLING_25_DEFAULTS: Mapping[str, str | int | bool] = MappingProxyType({"duration": "5"})
_KLING_V3_DEFAULTS: Mapping[str, str | int | bool] = MappingProxyType(
    {"duration": "5", "generateAudio": False}
)
_VEO_DEFAULTS: Mapping[str, str | int | bool] = MappingProxyType(
    {"duration": "8s", "resolution": "720p", "generateAudio": False}
)
# Whole-credit quality tiers (plan B). 1K == default_credits; higher res steps up in integers.
_NB2_RESOLUTION_CREDITS: Mapping[str, int] = MappingProxyType(
    {"0.5K": 3, "1K": 4, "2K": 6, "4K": 8}
)
_NB_PRO_RESOLUTION_CREDITS: Mapping[str, int] = MappingProxyType({"1K": 8, "2K": 12, "4K": 16})
_VEO_RESOLUTION_MULTIPLIERS: Mapping[str, int] = MappingProxyType({"720p": 1, "1080p": 1, "4k": 2})

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
            defaults=_IMAGE_DEFAULTS,
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/nano-banana-pro/edit",
            fields=_IMAGE_FIELDS,
            aspect_ratios=_IMAGE_ASPECT_RATIOS,
            resolutions=("1K", "2K", "4K"),
            defaults=_IMAGE_DEFAULTS,
        ),
        image_field="image_urls",
        image_field_is_list=True,
        max_input_images=14,
        resolution_credits=_NB_PRO_RESOLUTION_CREDITS,
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
            defaults=_IMAGE_DEFAULTS,
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/nano-banana-2/edit",
            fields=_IMAGE_FIELDS,
            aspect_ratios=_NB2_ASPECT_RATIOS,
            resolutions=("0.5K", "1K", "2K", "4K"),
            defaults=_IMAGE_DEFAULTS,
        ),
        image_field="image_urls",
        image_field_is_list=True,
        max_input_images=14,
        resolution_credits=_NB2_RESOLUTION_CREDITS,
    ),
    FalModel(
        id="kling-video",
        title="Kling Video 2.5 Turbo Pro",
        kind=KIND_VIDEO,
        # fal bills $0.35 per 5 s pack; 14 credits at the cheapest credit price ($0.05) covers it
        # twice over (ADR-061 §2).
        default_credits=14,
        text_variant=FalVariant(
            endpoint="fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
            fields=_KLING_25_FIELDS | {"aspectRatio"},
            aspect_ratios=_KLING_ASPECT_RATIOS,
            durations=("5", "10"),
            defaults=_KLING_25_DEFAULTS,
        ),
        image_variant=FalVariant(
            # Aspect ratio is derived from the reference image upstream, so it is not accepted.
            endpoint="fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
            fields=_KLING_25_FIELDS,
            durations=("5", "10"),
            defaults=_KLING_25_DEFAULTS,
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
        # fal bills $0.112/s without audio => $0.56 per 5 s pack; 23 credits ≈ 2.05× at $0.05.
        default_credits=23,
        text_variant=FalVariant(
            endpoint="fal-ai/kling-video/v3/pro/text-to-video",
            fields=_KLING_V3_FIELDS | {"aspectRatio"},
            aspect_ratios=_KLING_ASPECT_RATIOS,
            durations=_KLING_V3_DURATIONS,
            defaults=_KLING_V3_DEFAULTS,
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/kling-video/v3/pro/image-to-video",
            fields=_KLING_V3_FIELDS,
            durations=_KLING_V3_DURATIONS,
            defaults=_KLING_V3_DEFAULTS,
        ),
        # v3 image-to-video names the reference frame start_image_url, not image_url.
        image_field="start_image_url",
        image_field_is_list=False,
        max_input_images=1,
        base_duration_seconds=5,
        supports_audio=True,
        # fal charges $0.168/s with audio against $0.112/s without — exactly 1.5x.
        audio_multiplier=1.5,
    ),
    FalModel(
        id="veo-3.1",
        title="Veo 3.1 (Google)",
        kind=KIND_VIDEO,
        # fal bills $0.20/s at 720p/1080p without audio => $0.80 per 4 s pack; 32 credits = 2x.
        default_credits=32,
        text_variant=FalVariant(
            endpoint="fal-ai/veo3.1",
            fields=_VEO_FIELDS,
            # Unlike its image-to-video sibling, text-to-video has no "auto".
            aspect_ratios=("16:9", "9:16"),
            resolutions=_VEO_RESOLUTIONS,
            durations=_VEO_DURATIONS,
            defaults=_VEO_DEFAULTS,
        ),
        image_variant=FalVariant(
            endpoint="fal-ai/veo3.1/image-to-video",
            fields=_VEO_FIELDS,
            aspect_ratios=("auto", "16:9", "9:16"),
            resolutions=_VEO_RESOLUTIONS,
            durations=_VEO_DURATIONS,
            defaults=_VEO_DEFAULTS,
        ),
        image_field="image_url",
        image_field_is_list=False,
        max_input_images=1,
        # 4 s = the shortest run this model offers, so each offered duration (4s/6s/8s) is priced
        # by how many such blocks it needs instead of all three costing the same one block.
        base_duration_seconds=4,
        supports_audio=True,
        resolution_multipliers=_VEO_RESOLUTION_MULTIPLIERS,
        # generateAudio doubles the pack price; Kling also exposes the toggle but does not bill it.
        audio_multiplier=2,
    ),
)

_BY_ID: dict[str, FalModel] = {model.id: model for model in _MODELS}

ALL_MODEL_IDS: frozenset[str] = frozenset(_BY_ID)

# Unified GET /v1/models rows (ADR-075). One row per fal endpoint the registry can actually run.
# Names/variant/family match the 232 catalogue shape the iOS picker already consumes.
DEFAULT_PHOTO_ENDPOINT = "fal-ai/nano-banana-pro"

_FAL_CATALOG_META: dict[str, tuple[str, str, str]] = {
    "fal-ai/nano-banana-2/edit": ("Nano Banana 2", "Image Editing", "nano-banana-2"),
    "fal-ai/nano-banana-pro/edit": ("Nano Banana Pro", "Image Editing", "Nano-Banana-Pro"),
    "fal-ai/nano-banana-2": ("Nano Banana 2", "Text to Image", "nano-banana-2"),
    "fal-ai/nano-banana-pro": ("Nano Banana Pro", "Text to Image", "Nano-Banana-Pro"),
    "fal-ai/kling-video/v3/pro/image-to-video": (
        "Kling Video v3 Image to Video [Pro]",
        "Image to Video (pro)",
        "kling-v3",
    ),
    "fal-ai/kling-video/v2.5-turbo/pro/image-to-video": (
        "Kling Video",
        "2.5 Turbo (Image to Video) Pro",
        "kling-video-v25",
    ),
    "fal-ai/veo3.1/image-to-video": ("Veo 3.1", "Image to Video", "veo3.1"),
    "fal-ai/kling-video/v3/pro/text-to-video": (
        "Kling Video v3 Text to Video [Pro]",
        "Text to Video (pro)",
        "kling-v3",
    ),
    "fal-ai/veo3.1": ("Veo 3.1", "Text to Video", "veo3.1"),
    "fal-ai/kling-video/v2.5-turbo/pro/text-to-video": (
        "Kling v2.5 Text to Video",
        "2.5 Turbo (Text to Video) Pro",
        "kling-video-v25",
    ),
}

# Display order for GET /v1/models: photo edits, photo t2i (default last among photos), then video.
_FAL_CATALOG_ORDER: tuple[str, ...] = (
    "fal-ai/nano-banana-2/edit",
    "fal-ai/nano-banana-pro/edit",
    "fal-ai/nano-banana-2",
    "fal-ai/nano-banana-pro",
    "fal-ai/kling-video/v3/pro/image-to-video",
    "fal-ai/kling-video/v2.5-turbo/pro/image-to-video",
    "fal-ai/veo3.1/image-to-video",
    "fal-ai/kling-video/v3/pro/text-to-video",
    "fal-ai/veo3.1",
    "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
)


@dataclass(frozen=True)
class FalCatalogEntry:
    """One fal endpoint as offered by the unified instance catalog (ADR-075)."""

    id: str
    name: str
    modality: str
    variant: str
    family: str
    default: bool


def fal_catalog_entries() -> tuple[FalCatalogEntry, ...]:
    """Fal rows for GET /v1/models: only endpoints this registry can submit."""
    live_endpoints: set[str] = set()
    kind_by_endpoint: dict[str, str] = {}
    for model in _MODELS:
        live_endpoints.add(model.text_variant.endpoint)
        kind_by_endpoint[model.text_variant.endpoint] = model.kind
        if model.image_variant is not None:
            live_endpoints.add(model.image_variant.endpoint)
            kind_by_endpoint[model.image_variant.endpoint] = model.kind
    rows: list[FalCatalogEntry] = []
    for endpoint in _FAL_CATALOG_ORDER:
        if endpoint not in live_endpoints:
            continue
        name, variant, family = _FAL_CATALOG_META[endpoint]
        kind = kind_by_endpoint[endpoint]
        rows.append(
            FalCatalogEntry(
                id=endpoint,
                name=name,
                modality="photo" if kind == KIND_IMAGE else "video",
                variant=variant,
                family=family,
                default=endpoint == DEFAULT_PHOTO_ENDPOINT,
            )
        )
    return tuple(rows)


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
    """How many *units* of the unit price one run costs (images or duration packs).

    Image quality is priced separately via ``resolution_credits``; this returns only the count of
    images. Video returns ``ceil(seconds / base_duration)``. Never below 1.
    """
    if model.kind == KIND_IMAGE:
        return max(1, num_images or 1)
    base = model.base_duration_seconds
    seconds = duration_seconds(duration) if duration else None
    if base is None or seconds is None:
        return 1
    return max(1, math.ceil(seconds / base))


def image_unit_credits(model: FalModel, resolution: str | None, *, base_credits: int) -> int:
    """Credits for one image at ``resolution``, scaled so the 1K tier equals ``base_credits``."""
    table = model.resolution_credits
    if not table:
        return base_credits
    if resolution is not None and resolution in table:
        listed = table[resolution]
    elif "1K" in table:
        listed = table["1K"]
    else:
        listed = next(iter(table.values()))
    if base_credits == model.default_credits or model.default_credits <= 0:
        return listed
    # Operator override of the 1K / default cell: keep the same integer ratios.
    return max(1, (listed * base_credits + model.default_credits - 1) // model.default_credits)


def resolution_credits_for_api(model: FalModel, *, base_credits: int) -> dict[str, int]:
    """Per-resolution unit prices as exposed on ``GET /v1/media/models`` (empty for video)."""
    if model.kind != KIND_IMAGE or not model.resolution_credits:
        return {}
    return {
        key: image_unit_credits(model, key, base_credits=base_credits)
        for key in model.resolution_credits
    }


def run_price(
    *,
    model: FalModel,
    base_credits: int,
    num_images: int | None = None,
    duration: str | None = None,
    resolution: str | None = None,
    generate_audio: bool | None = None,
) -> int:
    """Total credits for one submit, given the knobs that actually change fal's bill.

    Image: ``resolution_credits[resolution] × numImages``.
    Video: ``base_credits × duration_packs × resolution_mult × audio_mult``.
    Mode (text-to-* vs image-to-*) does not affect the price.
    """
    if model.kind == KIND_IMAGE:
        return image_unit_credits(model, resolution, base_credits=base_credits) * price_multiplier(
            model=model, num_images=num_images, duration=None
        )
    packs = price_multiplier(model=model, num_images=None, duration=duration)
    res_mult = 1
    if model.resolution_multipliers and resolution is not None:
        res_mult = model.resolution_multipliers.get(resolution, 1)
    total = base_credits * packs * res_mult
    if generate_audio and model.audio_multiplier is not None:
        # Kling v3's audio surcharge is x1.5, so the product is not an integer. Round UP: rounding
        # down would price a run below the cost it was calibrated against.
        return max(1, math.ceil(total * model.audio_multiplier))
    return total


def resolve_values(*, variant: FalVariant, values: Mapping[str, object]) -> dict[str, object]:
    """Fill in the variant's defaults for the fields that move the price (ADR-061 §3).

    An omitted field used to be dropped so that fal's own default applied — which meant the run we
    priced and the run we submitted could differ (fal defaults ``generate_audio`` to true and Veo's
    ``duration`` to ``8s``, both more expensive than what we charged). Resolving once, here, and
    using the SAME mapping for both the price and the upstream payload removes that gap entirely.

    Only fields this variant actually forwards get a default: the registry is per-variant precisely
    because modes disagree about which parameters exist.
    """
    resolved = {key: value for key, value in values.items() if value is not None}
    for key, default in variant.defaults.items():
        if key in variant.fields and key not in resolved:
            resolved[key] = default
    return resolved


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
