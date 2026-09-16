"""Small, deterministic image operations for prepared avatars."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageColor, ImageOps

_MAX_LONG_SIDE = 1920
_MAX_PIXELS = 20_000_000


def validate_avatar_image(content: bytes) -> None:
    """Reject corrupt or pathologically large images before persisting/decoding them later."""
    try:
        image = Image.open(BytesIO(content))
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
            raise ValueError("avatar image dimensions are too large")
        image.verify()
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "avatar image dimensions are too large":
            raise
        raise ValueError("avatar image is not readable") from exc


def compose_avatar(
    cutout_bytes: bytes,
    *,
    background_bytes: bytes | None,
    background_color: str | None,
) -> bytes:
    """Place an RGBA cutout over an optional cover-cropped image or solid color."""
    try:
        cutout = Image.open(BytesIO(cutout_bytes))
        if cutout.width * cutout.height > _MAX_PIXELS:
            raise ValueError("avatar image dimensions are too large")
        cutout.load()
    except (OSError, ValueError) as exc:
        raise ValueError("avatar cutout is not a readable image") from exc
    subject = cutout.convert("RGBA")
    width, height = subject.size
    long_side = max(width, height)
    if long_side > _MAX_LONG_SIDE:
        scale = _MAX_LONG_SIDE / long_side
        subject = subject.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
        width, height = subject.size

    if background_bytes is not None:
        try:
            raw_background = Image.open(BytesIO(background_bytes))
            if raw_background.width * raw_background.height > _MAX_PIXELS:
                raise ValueError("avatar background dimensions are too large")
            raw_background.load()
        except (OSError, ValueError) as exc:
            raise ValueError("avatar background is not a readable image") from exc
        background = ImageOps.fit(
            raw_background.convert("RGBA"),
            (width, height),
            method=Image.Resampling.LANCZOS,
        )
    elif background_color is not None:
        try:
            rgba = ImageColor.getcolor(background_color, "RGBA")
        except ValueError as exc:
            raise ValueError("background color is invalid") from exc
        background = Image.new("RGBA", (width, height), rgba)
    else:
        background = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    background.alpha_composite(subject)
    output = BytesIO()
    background.save(output, format="PNG", optimize=True)
    return output.getvalue()
