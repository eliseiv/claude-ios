"""Chat image attachment refs persisted with a 1-day TTL for later media reuse.

Chat vision still uses same-turn base64 (ADR-020). Separately, when media generation is
configured, each uploaded image is also stored with fal (ADR-062) and a light
``payload.attachmentRefs`` entry is written on the user step so a later turn can ask
``useRecentImage`` without re-attach. App-level TTL is 24h (independent of fal lifecycle).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from app.chat.attachments import ImageAttachmentRef
from app.errors import (
    MediaGenerationNotConfiguredError,
    PayloadTooLargeError,
    UpstreamError,
    ValidationFailedError,
)
from app.media_generation.service import MediaGenerationService

logger = logging.getLogger("app.chat.attachment_refs")

ATTACHMENT_REF_TTL = datetime.timedelta(days=1)
RECENT_USER_STEPS_SCAN = 30
IMAGE_PLACEHOLDER_PREFIX = "[attachment: image/"

MEDIA_NO_RECENT_IMAGE_ERROR_CODE = "no_recent_image"


def _now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)


def build_attachment_ref(
    *,
    media_type: str,
    filename: str,
    url: str,
    expires_at: datetime.datetime | None = None,
) -> dict[str, Any]:
    exp = expires_at if expires_at is not None else _now() + ATTACHMENT_REF_TTL
    return {
        "mediaType": media_type,
        "filename": filename,
        "url": url,
        "expiresAt": exp.isoformat().replace("+00:00", "Z"),
    }


def _parse_expires_at(raw: Any) -> datetime.datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def is_ref_alive(ref: Mapping[str, Any], *, now: datetime.datetime | None = None) -> bool:
    url = ref.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    exp = _parse_expires_at(ref.get("expiresAt"))
    if exp is None:
        return False
    return exp > (now or _now())


async def upload_turn_attachment_refs(
    media: MediaGenerationService | None,
    images: Sequence[ImageAttachmentRef],
) -> list[dict[str, Any]]:
    """Upload same-turn images to fal; return attachmentRefs (empty if media unavailable)."""
    if media is None or not images:
        return []
    refs: list[dict[str, Any]] = []
    for img in images:
        try:
            uploaded = await media.upload_reference_image(
                media_type=img.media_type,
                file_name=img.filename,
                data=img.data,
            )
        except (
            MediaGenerationNotConfiguredError,
            PayloadTooLargeError,
            ValidationFailedError,
            UpstreamError,
        ) as exc:
            logger.warning(
                "chat attachment fal upload skipped",
                extra={"error": type(exc).__name__, "filename": img.filename},
            )
            continue
        refs.append(
            build_attachment_ref(
                media_type=img.media_type,
                filename=img.filename,
                url=uploaded.url,
            )
        )
    return refs


def refs_from_user_payload(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    raw = payload.get("attachmentRefs")
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def latest_alive_image_urls(
    user_steps_newest_first: Sequence[Mapping[str, Any]],
    *,
    now: datetime.datetime | None = None,
    max_urls: int = 1,
) -> list[str]:
    """Pick newest non-expired attachmentRefs urls from recent user step payloads."""
    clock = now or _now()
    urls: list[str] = []
    for payload in user_steps_newest_first:
        for ref in refs_from_user_payload(payload):
            if is_ref_alive(ref, now=clock):
                urls.append(str(ref["url"]))
                if len(urls) >= max_urls:
                    return urls
    return urls


def user_step_has_image_signal(payload: Mapping[str, Any] | None) -> bool:
    """True when the user step had an image (alive ref or vision placeholder)."""
    if not isinstance(payload, Mapping):
        return False
    if any(is_ref_alive(r) for r in refs_from_user_payload(payload)):
        return True
    content = payload.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = str(block.get("text") or "")
        if IMAGE_PLACEHOLDER_PREFIX in text:
            return True
    return False


def recent_image_available(
    user_steps_newest_first: Sequence[Mapping[str, Any]],
    *,
    now: datetime.datetime | None = None,
) -> bool:
    """Whether any of the scanned user steps still has a reusable (or placeholder) photo."""
    clock = now or _now()
    for payload in user_steps_newest_first:
        if any(is_ref_alive(r, now=clock) for r in refs_from_user_payload(payload)):
            return True
        if user_step_has_image_signal(payload):
            # Placeholder-only (upload failed / media off): still ask, but useRecentImage may fail.
            return True
    return False
