"""Cross-check the media catalog against the published fal input schemas (ADR-060 §2).

Run manually — it talks to fal.ai over the network, so it is deliberately NOT part of the CI gate
(`make ci` must stay offline and deterministic):

    uv run python tests/e2e_live/verify_fal_catalog.py

Why this exists: fal accepts an out-of-enum value **on submit** (HTTP 200, a request id, credits
already spent) and only rejects it while executing — the status URL then reports ``COMPLETED`` while
the response URL answers ``422 literal_error`` forever. So a catalog value that drifts from the
upstream schema does not surface as a failed submit; it surfaces as a *paid* run that can never
produce output. Verified live on 2026-08-04 with ``resolution: "512x512"`` and Veo
``aspect_ratio: "auto"``, both of which were accepted and then rejected at execution.

That is why the enums are mirrored server-side at all, and why they must be re-checked whenever a
model is added or a provider updates a schema. Exits non-zero on any mismatch; values upstream
offers that we deliberately do not expose are printed as notes, not failures.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

from app.media_generation.catalog import all_models, fal_field_name

_SCHEMA_URL = "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={endpoint}"
_TIMEOUT_SECONDS = 60


def _input_schema(endpoint: str) -> dict[str, Any]:
    with urllib.request.urlopen(  # noqa: S310 — fixed https host, no user input
        _SCHEMA_URL.format(endpoint=endpoint), timeout=_TIMEOUT_SECONDS
    ) as response:
        document = json.load(response)
    for name, node in document.get("components", {}).get("schemas", {}).items():
        if name.endswith("Input"):
            return node
    raise SystemExit(f"no Input schema published for {endpoint}")


def _enum_of(prop: dict[str, Any]) -> list[str] | None:
    """The literal values a property accepts, whether declared directly or inside anyOf."""
    if "enum" in prop:
        return list(prop["enum"])
    for alternative in prop.get("anyOf", []):
        if "enum" in alternative:
            return list(alternative["enum"])
    return None


def main() -> int:
    problems: list[str] = []
    for model in all_models():
        for mode, variant in model.variants():
            properties = _input_schema(variant.endpoint).get("properties", {})

            for request_field in sorted(variant.fields):
                upstream_name = fal_field_name(request_field)
                if upstream_name not in properties:
                    problems.append(f"{model.id}/{mode}: forwards unknown field {upstream_name}")

            if variant is model.image_variant and model.image_field not in properties:
                problems.append(
                    f"{model.id}/{mode}: image field {model.image_field} is not in the schema"
                )

            for request_field in ("aspectRatio", "resolution", "duration"):
                ours = variant.allowed(request_field)
                prop = properties.get(fal_field_name(request_field))
                upstream = _enum_of(prop) if prop else None
                if ours and upstream is None:
                    problems.append(
                        f"{model.id}/{mode}: offers {request_field} but upstream has no such enum"
                    )
                elif ours:
                    unsupported = [value for value in ours if value not in upstream]
                    if unsupported:
                        problems.append(
                            f"{model.id}/{mode}: {request_field} offers {unsupported}, "
                            "which upstream rejects"
                        )
                    withheld = [value for value in upstream if value not in ours]
                    if withheld:
                        print(f"  note {model.id}/{mode}: {request_field} withholds {withheld}")
                elif upstream and request_field in variant.fields:
                    problems.append(
                        f"{model.id}/{mode}: forwards {request_field} but offers no values"
                    )

            # Defaults skip request validation (they are ours, not the client's), so a drifted
            # default reaches fal unchecked — the same paid-run-without-output failure as above,
            # except it hits every request that omits the field rather than a rare bad value.
            for request_field, default in sorted(variant.defaults.items()):
                prop = properties.get(fal_field_name(request_field))
                if prop is None:
                    problems.append(
                        f"{model.id}/{mode}: defaults {request_field}, unknown upstream"
                    )
                    continue
                upstream = _enum_of(prop)
                if upstream is not None and default not in upstream:
                    problems.append(
                        f"{model.id}/{mode}: default {request_field}={default!r} "
                        "is not a value upstream accepts"
                    )

            print(f"checked {model.id}/{mode} -> {variant.endpoint}")

    if problems:
        print("\nMISMATCHES:")
        for problem in problems:
            print(" -", problem)
        return 1
    print("\ncatalog matches the published fal schemas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
