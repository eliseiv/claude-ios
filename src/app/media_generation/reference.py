"""Prepare a reference still so a video model can actually fetch it.

fal image results (``v3b.fal.media``) are public https URLs, but Kling's downloader still fails
on them when the file is huge or ultra-wide. The photo that triggered this was a valid 200 —
20 MB, 11712×1408 — and came back as ``body.image_url: Failed to download the file``.

We never persist the bytes (ADR-060 / ADR-062): shrink in memory, then re-upload to fal storage
so the URL we hand to image-to-video is a compact JPEG on fal-cdn-v3.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

# Kling's fetch budget is well below a 4K nano-banana PNG. 1920 on the long side is enough
# for a 5–10 s clip and keeps the rehosted file under a few hundred KB.
_MAX_LONG_SIDE = 1920
_JPEG_QUALITY = 85


def prepare_reference_jpeg(content: bytes) -> bytes:
    """Decode any still, downscale if needed, and return a JPEG body Kling can fetch."""
    try:
        image = Image.open(BytesIO(content))
        image.load()
    except (OSError, ValueError) as exc:
        raise ValueError("reference image is not a readable still") from exc
    image = image.convert("RGB")
    width, height = image.size
    long_side = max(width, height)
    if long_side > _MAX_LONG_SIDE:
        scale = _MAX_LONG_SIDE / long_side
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    out = BytesIO()
    image.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return out.getvalue()
