"""Уменьшение результата генерации для повторной модерации (ADR-086 §7).

Провайдер модерации отвечает ``400 file_too_large`` на крупный результат. Модерацию не пропускаем:
ассет скачивается (allowlist хостов, ``https``, без редиректов, предел байтов), размер в пикселях
проверяется по заголовку ДО декодирования, картинка уменьшается до ≤ 2048 px по длинной стороне
и уходит на повторную проверку как JPEG data-URI.

Нарушение пределов или нечитаемый файл — ``UncheckableResultError`` (постоянный отказ: задача
``failed`` с возвратом). Таймаут, сеть и ``5xx`` при скачивании — недоступность: исключение
уходит вызывающему как есть и обрабатывается транзиентно до дедлайна.
"""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO

import httpx
from PIL import Image

from app.errors import UpstreamError
from app.media_generation.asset_hosts import fal_asset_host_allowed

_MAX_LONG_SIDE = 2048
_JPEG_QUALITY = 85

REASON_UNTRUSTED_HOST = "untrusted_host"
REASON_DOWNLOAD_STATUS = "download_status"
REASON_TOO_MANY_BYTES = "too_many_bytes"
REASON_TOO_MANY_PIXELS = "too_many_pixels"
REASON_UNDECODABLE = "undecodable"


class UncheckableResultError(Exception):
    """Результат нельзя подготовить к проверке — постоянный отказ, повтор его не изменит."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def downscaled_data_uri(
    url: str, *, max_bytes: int, max_pixels: int, timeout_seconds: float
) -> str:
    """Скачать ассет и вернуть его уменьшенную JPEG-копию как data-URI."""
    content = await _download(url, max_bytes=max_bytes, timeout_seconds=timeout_seconds)
    jpeg = await asyncio.to_thread(_downscale_to_jpeg, content, max_pixels)
    return f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"


async def _download(url: str, *, max_bytes: int, timeout_seconds: float) -> bytes:
    if not fal_asset_host_allowed(url):
        raise UncheckableResultError(REASON_UNTRUSTED_HOST)
    async with (
        httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code >= 500:
            raise UpstreamError("result asset host is unavailable")
        if response.status_code != 200:
            raise UncheckableResultError(REASON_DOWNLOAD_STATUS)
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            raise UncheckableResultError(REASON_TOO_MANY_BYTES)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise UncheckableResultError(REASON_TOO_MANY_BYTES)
            chunks.append(chunk)
    return b"".join(chunks)


def _downscale_to_jpeg(content: bytes, max_pixels: int) -> bytes:
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            # Предел — по заголовку, до декодирования пикселей (анти-bomb).
            if width * height > max_pixels:
                raise UncheckableResultError(REASON_TOO_MANY_PIXELS)
            still = image.convert("RGB")
    except UncheckableResultError:
        raise
    except Image.DecompressionBombError as exc:
        raise UncheckableResultError(REASON_TOO_MANY_PIXELS) from exc
    except Exception as exc:  # noqa: BLE001 — Pillow raises many types on a broken file
        raise UncheckableResultError(REASON_UNDECODABLE) from exc
    still.thumbnail((_MAX_LONG_SIDE, _MAX_LONG_SIDE), Image.Resampling.LANCZOS)
    out = BytesIO()
    still.save(out, format="JPEG", quality=_JPEG_QUALITY)
    return out.getvalue()
