"""Unit: shrink + rehost a fal result still before image-to-video.

The novirell incident: a just-generated nano-banana PNG (20 MB, 11712×1408) was passed
straight to Kling as ``image_url``. The URL was a public 200; Kling still answered
``Failed to download the file``. Credits refunded, no video.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.media_generation.fal_client import FalClient
from app.media_generation.reference import prepare_reference_jpeg


def _png(*, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color=(20, 80, 20))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_prepare_reference_jpeg_downscales_ultra_wide_png() -> None:
    raw = _png(width=4000, height=400)
    out = prepare_reference_jpeg(raw)
    jpeg = Image.open(BytesIO(out))
    assert jpeg.format == "JPEG"
    assert max(jpeg.size) == 1920
    assert jpeg.size[0] > jpeg.size[1]
    assert len(out) < len(raw)


def test_prepare_reference_jpeg_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a readable still"):
        prepare_reference_jpeg(b"not-an-image")


class _ScriptedResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"",
        json_data: Any = None,
    ) -> None:
        self.status_code = status_code
        self._content = content
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data

    async def aiter_bytes(self):
        yield self._content

    async def __aenter__(self) -> _ScriptedResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _ScriptedClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)

    def stream(self, method: str, url: str, **kwargs: Any) -> _ScriptedResponse:
        self.calls.append({"method": method, "url": url, "stream": True, **kwargs})
        return self._responses.pop(0)

    async def __aenter__(self) -> _ScriptedClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        FAL_API_KEY="k",
        FAL_REST_BASE="https://rest.fal.ai",
        FAL_UPLOAD_HOST_SUFFIXES=".fal.media,.fal.ai",
    )


@pytest.mark.asyncio
async def test_rehost_skips_non_fal_hosts() -> None:
    client = FalClient(_settings())
    assert await client.rehost_reference_image("https://cdn.example.com/cat.png") == (
        "https://cdn.example.com/cat.png"
    )


@pytest.mark.asyncio
async def test_rehost_uploads_a_jpeg_to_fal_cdn(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "https://v3b.fal.media/files/b/0aa6d694/wide.png"
    hosted = "https://v3b.fal.media/files/b/rehosted/reference.jpg"
    raw = _png(width=3000, height=400)
    scripted = _ScriptedClient(
        [
            _ScriptedResponse(content=raw),
            _ScriptedResponse(
                json_data={
                    "upload_url": "https://v3b.fal.media/upload/slot/1",
                    "file_url": hosted,
                }
            ),
            _ScriptedResponse(status_code=200),
        ]
    )
    monkeypatch.setattr(
        "app.media_generation.fal_client.httpx",
        SimpleNamespace(
            AsyncClient=lambda timeout: scripted,
            TimeoutException=httpx.TimeoutException,
            RequestError=httpx.RequestError,
        ),
    )
    client = FalClient(_settings())
    assert await client.rehost_reference_image(source) == hosted
    put = next(c for c in scripted.calls if c["method"] == "PUT")
    assert put["headers"]["Content-Type"] == "image/jpeg"
    assert len(put["content"]) < len(raw)
