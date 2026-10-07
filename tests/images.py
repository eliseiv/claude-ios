"""Настоящие мини-картинки для тестов вложений.

Валидатор вложений открывает фото Pillow до модерации (ADR-086 §7), поэтому «картинка» из одних
magic bytes отклоняется как битая. Хелпер отдаёт декодируемый 2×2 файл нужного формата.
"""

from __future__ import annotations

import base64
import io
from functools import cache

from PIL import Image

_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}


@cache
def image_bytes(media_type: str = "image/png", size: tuple[int, int] = (2, 2)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(buf, format=_FORMATS[media_type])
    return buf.getvalue()


def image_b64(media_type: str = "image/png", size: tuple[int, int] = (2, 2)) -> str:
    return base64.b64encode(image_bytes(media_type, size)).decode("ascii")


PNG = image_bytes("image/png")
JPEG = image_bytes("image/jpeg")
GIF = image_bytes("image/gif")
WEBP = image_bytes("image/webp")
PNG_B64 = image_b64("image/png")
JPEG_B64 = image_b64("image/jpeg")
