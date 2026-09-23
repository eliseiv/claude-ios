"""Vendor routing of a media run through the proxy service (ADR-108 §2, §2.1).

Public model ids, variants, field allowlists, price defaults and the credits themselves stay in
``catalog.py`` untouched. This module maps ONE already-resolved run onto the proxy routes that
can execute exactly that run, cheapest first. The prices here are OUR purchase price at the
vendor in USD: they order the routes and never reach the credit price or the client.

The ``fal`` route always exists: same endpoint, same payload that goes to fal directly today.
``sosana``/``kie`` are candidates ONLY when they execute the SAME run (§2.1) — otherwise the
vendor payload would silently drop ``seed``/``outputFormat`` or pick an aspect ratio fal would not
(the sample repository substitutes ``"auto"``), i.e. the same request would give another result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.media_generation.catalog import _IMAGE_ASPECT_RATIOS, FalModel, FalVariant

SERVICE_FAL = "fal"
SERVICE_KIE = "kie"
SERVICE_SOSANA = "sosana"

FAL_QUEUE_HOST = "https://queue.fal.run"
KIE_CREATE_TASK = "https://api.kie.ai/api/v1/jobs/createTask"
SOSANA_CREATE_IMAGE = "https://api.sosana.art/api/image/create-async"

# §2.1 п.2: the only models and resolutions a non-fal vendor may take.
_BANANA_MODELS = frozenset({"nano-banana-2", "nano-banana-pro"})
_VENDOR_RESOLUTIONS = frozenset({"1K", "2K", "4K"})
# §2.1 п.5: the base aspect-ratio set (the panoramic 4:1/1:4/8:1/1:8 of nano-banana-2 stay on fal).
_VENDOR_ASPECT_RATIOS = frozenset(_IMAGE_ASPECT_RATIOS)
# §2.1 п.4: output formats kie can reproduce (sosana takes none — the field must be absent).
_KIE_OUTPUT_FORMATS = {"png": "png", "jpeg": "jpg"}

# Price of the fal route when neither the table nor the override names the run (the table always
# carries `*:*:fal`, so this is only a guard that keeps the fal route alive).
_FAL_FALLBACK_PRICE = 1.0

# Stable tie-break on equal price: sosana, then kie, then fal (the order of the sample repo).
_SERVICE_RANK = {SERVICE_SOSANA: 0, SERVICE_KIE: 1, SERVICE_FAL: 2}


@dataclass(frozen=True)
class VendorRoute:
    """One concrete proxy call for a public model run."""

    service: str
    endpoint: str
    payload: dict[str, Any]
    unit_price: float
    catalog_endpoint: str


def default_vendor_prices() -> dict[str, float]:
    """USD unit costs used to order the routes (snapshot of the sample table, ADR-108 §2).

    Keys for models absent in this repository (upscalers, fps) are not carried over. Every model
    not named here runs through ``*:*:fal``.
    """
    return {
        "nano-banana-2:0.5K:fal": 0.06,
        "nano-banana-2:1K:sosana": 0.022,
        "nano-banana-2:2K:sosana": 0.028,
        "nano-banana-2:4K:sosana": 0.040,
        "nano-banana-2:1K:kie": 0.04,
        "nano-banana-2:2K:kie": 0.06,
        "nano-banana-2:4K:kie": 0.09,
        "nano-banana-2:1K:fal": 0.08,
        "nano-banana-2:2K:fal": 0.12,
        "nano-banana-2:4K:fal": 0.16,
        "nano-banana-pro:1K:sosana": 0.0275,
        "nano-banana-pro:2K:sosana": 0.035,
        "nano-banana-pro:4K:sosana": 0.050,
        "nano-banana-pro:1K:kie": 0.08,
        "nano-banana-pro:2K:kie": 0.12,
        "nano-banana-pro:4K:kie": 0.16,
        "nano-banana-pro:1K:fal": 0.15,
        "nano-banana-pro:2K:fal": 0.15,
        "nano-banana-pro:4K:fal": 0.30,
        "*:*:fal": 1.0,
    }


def merged_vendor_prices(overrides: Mapping[str, float] | None = None) -> dict[str, float]:
    """Code table plus ``MEDIA_VENDOR_PRICES`` overrides (override wins)."""
    merged = default_vendor_prices()
    if overrides:
        merged.update(overrides)
    return merged


def lookup_price(
    prices: Mapping[str, float], *, model_id: str, tier: str, service: str
) -> float | None:
    for key in (
        f"{model_id}:{tier}:{service}",
        f"{model_id}:*:{service}",
        f"*:{tier}:{service}",
        f"*:*:{service}",
    ):
        value = prices.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def fal_queue_url(endpoint: str) -> str:
    """The fal queue URL the proxy calls for a fal route: ``https://queue.fal.run/<endpoint>``."""
    return f"{FAL_QUEUE_HOST}/{endpoint.lstrip('/')}"


def fal_route(
    *, model_id: str, tier: str, endpoint: str, payload: dict[str, Any], prices: Mapping[str, float]
) -> VendorRoute:
    """The fal route — always present; its payload is EXACTLY the direct fal payload."""
    price = lookup_price(prices, model_id=model_id, tier=tier, service=SERVICE_FAL)
    return VendorRoute(
        service=SERVICE_FAL,
        endpoint=fal_queue_url(endpoint),
        payload=dict(payload),
        unit_price=_FAL_FALLBACK_PRICE if price is None else price,
        catalog_endpoint=endpoint,
    )


def _vendor_eligible(
    *, model: FalModel, values: Mapping[str, Any], result_hosts_configured: bool
) -> bool:
    """§2.1 conditions 1, 2, 3, 5 — common to sosana and kie (condition 4 is per service)."""
    if not result_hosts_configured:
        return False
    if model.id not in _BANANA_MODELS:
        return False
    num_images = values.get("numImages")
    if isinstance(num_images, bool) or num_images != 1:
        return False
    if values.get("resolution") not in _VENDOR_RESOLUTIONS:
        return False
    if values.get("seed") is not None:
        return False
    aspect_ratio = values.get("aspectRatio")
    return isinstance(aspect_ratio, str) and aspect_ratio in _VENDOR_ASPECT_RATIOS


def _sosana_route(
    *,
    model: FalModel,
    variant: FalVariant,
    values: Mapping[str, Any],
    fal_payload: dict[str, Any],
    prices: Mapping[str, float],
) -> VendorRoute | None:
    if values.get("outputFormat") is not None:
        return None
    resolution = str(values["resolution"])
    price = lookup_price(prices, model_id=model.id, tier=resolution, service=SERVICE_SOSANA)
    if price is None:
        return None
    suffix = resolution.lower()
    payload: dict[str, Any] = {
        "prompt": fal_payload.get("prompt") or "",
        "model": f"{model.id}-{suffix}",
        "aspect_ratio": values["aspectRatio"],
        "prompt_optimization": False,
    }
    image_urls = fal_payload.get("image_urls")
    if isinstance(image_urls, list) and image_urls:
        payload["image_urls"] = list(image_urls)
    return VendorRoute(
        service=SERVICE_SOSANA,
        endpoint=SOSANA_CREATE_IMAGE,
        payload=payload,
        unit_price=price,
        catalog_endpoint=variant.endpoint,
    )


def _kie_route(
    *,
    model: FalModel,
    variant: FalVariant,
    values: Mapping[str, Any],
    fal_payload: dict[str, Any],
    prices: Mapping[str, float],
) -> VendorRoute | None:
    requested = values.get("outputFormat")
    if requested is None:
        output = "png"
    elif isinstance(requested, str) and requested in _KIE_OUTPUT_FORMATS:
        output = _KIE_OUTPUT_FORMATS[requested]
    else:
        return None
    resolution = str(values["resolution"])
    price = lookup_price(prices, model_id=model.id, tier=resolution, service=SERVICE_KIE)
    if price is None:
        return None
    inner: dict[str, Any] = {
        "prompt": fal_payload.get("prompt") or "",
        "aspect_ratio": values["aspectRatio"],
        "resolution": resolution,
        "output_format": output,
    }
    image_urls = fal_payload.get("image_urls")
    if isinstance(image_urls, list) and image_urls:
        inner["image_input"] = list(image_urls)
    return VendorRoute(
        service=SERVICE_KIE,
        endpoint=KIE_CREATE_TASK,
        payload={"model": model.id, "input": inner},
        unit_price=price,
        catalog_endpoint=variant.endpoint,
    )


def candidate_routes(
    *,
    model: FalModel,
    variant: FalVariant,
    values: Mapping[str, Any],
    fal_payload: dict[str, Any],
    prices: Mapping[str, float],
    result_hosts_configured: bool,
) -> list[VendorRoute]:
    """Every route that executes THIS run, cheapest first; the fal route is always among them."""
    routes: list[VendorRoute] = []
    if _vendor_eligible(
        model=model, values=values, result_hosts_configured=result_hosts_configured
    ):
        for build in (_sosana_route, _kie_route):
            route = build(
                model=model, variant=variant, values=values, fal_payload=fal_payload, prices=prices
            )
            if route is not None:
                routes.append(route)
    resolution = values.get("resolution")
    tier = resolution if isinstance(resolution, str) and resolution else "*"
    routes.append(
        fal_route(
            model_id=model.id,
            tier=tier,
            endpoint=variant.endpoint,
            payload=fal_payload,
            prices=prices,
        )
    )
    routes.sort(key=lambda route: (route.unit_price, _SERVICE_RANK.get(route.service, 9)))
    return routes
