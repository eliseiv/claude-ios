"""Integration: ADR-060 — image/video generation over ``/v1/media/*``.

Drives the FULL HTTP path through the real JWT auth + lazy provisioning (shared testcontainers
Postgres), the real wallet debit/refund and the real ``media_jobs`` persistence, while every
outgoing fal call is faked at the ``httpx`` boundary (``app.media_generation.fal_client.httpx`` is
monkeypatched to a ``SimpleNamespace`` whose ``AsyncClient`` records the request and returns a
scripted response). No network to fal; the LLM is never touched.

Covers: §2 catalog + server-side pricing, §3 queue submit/poll contract and the reference-image
field name per model, §4 debit-on-submit / refund-on-failure in one transaction, §5 config gate 503
and upstream error mapping, plus owner isolation (404) and auth (401).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

_MODELS_URL = "/v1/media/models"
_IMAGES_URL = "/v1/media/images"
_VIDEOS_URL = "/v1/media/videos"
_JOBS_URL = "/v1/media/jobs"

_FAL_KEY = "fal-test-key-abc123"  # noqa: S105 - test-only static secret
_QUEUE_BASE = "https://queue.fal.run"
_REQUEST_ID = "764cabcf-b745-4b3e-ae38-1200304cf45b"


def _submit_body(endpoint: str) -> dict[str, Any]:
    base = f"{_QUEUE_BASE}/{endpoint}/requests/{_REQUEST_ID}"
    return {
        "request_id": _REQUEST_ID,
        "status": "IN_QUEUE",
        "status_url": f"{base}/status",
        "response_url": base,
        "queue_position": 0,
    }


# --------------------------- fake outgoing fal client ---------------------------


class _FakeResponse:
    def __init__(
        self, status_code: int, json_data: Any = None, *, json_raises: bool = False
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._json_data


class _Fal:
    """Scripts + records the faked outgoing fal queue calls, keyed by URL suffix."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._submit: _FakeResponse | None = None
        self._status: _FakeResponse | None = None
        self._result: _FakeResponse | None = None
        self._exc: BaseException | None = None

    def on_submit(self, status_code: int, json_data: Any = None) -> None:
        self._submit = _FakeResponse(status_code, json_data)

    def on_status(self, status: str, **extra: Any) -> None:
        self._status = _FakeResponse(200, {"status": status, **extra})

    def on_result(self, json_data: Any, status_code: int = 200) -> None:
        self._result = _FakeResponse(status_code, json_data)

    def on_status_error(self, status_code: int, json_data: Any = None) -> None:
        """Script a non-2xx answer from the status URL (``on_status`` always answers 200)."""
        self._status = _FakeResponse(status_code, json_data)

    def fail(self, exc: BaseException) -> None:
        self._exc = exc

    @property
    def submit_payload(self) -> dict[str, Any]:
        for call in self.calls:
            if call["method"] == "POST":
                return dict(call["json"] or {})
        raise AssertionError("no submit call recorded")

    @property
    def submit_url(self) -> str:
        for call in self.calls:
            if call["method"] == "POST":
                return str(call["url"])
        raise AssertionError("no submit call recorded")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        if self._exc is not None:
            raise self._exc
        if method == "POST":
            assert self._submit is not None, "submit not scripted"
            return self._submit
        if url.endswith("/status"):
            assert self._status is not None, "status not scripted"
            return self._status
        assert self._result is not None, "result not scripted"
        return self._result


def _make_fake_httpx(fal: _Fal) -> SimpleNamespace:
    """A drop-in for the ``httpx`` module name used inside fal_client.py (only that ref)."""

    class _FakeAsyncClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: dict[str, str] | None = None,
            json: dict[str, Any] | None = None,
        ) -> _FakeResponse:
            return await fal._request(method, url, headers=headers, json=json)

    return SimpleNamespace(
        AsyncClient=_FakeAsyncClient,
        TimeoutException=_httpx.TimeoutException,
        RequestError=_httpx.RequestError,
        ConnectError=_httpx.ConnectError,
        Response=_httpx.Response,
    )


# ----------------------------------- fixtures -----------------------------------


@pytest.fixture
def fal() -> _Fal:
    return _Fal()


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    *,
    fal_key: str,
) -> AsyncClient:
    from app import deps
    from app.api_gateway.routers import media as media_router
    from app.main import create_app
    from app.media_generation import fal_client as fal_client_mod

    monkeypatch.setenv("FAL_API_KEY", fal_key)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    get_settings.cache_clear()

    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))

    # The router imported enforce_other_limits by name at load; patch it there. Default is allow.
    async def _allow(*, user_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(media_router, "enforce_other_limits", _allow)

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def media_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with FAL_API_KEY configured and the outgoing httpx faked."""
    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as ac:
        yield ac
    get_settings.cache_clear()


@pytest.fixture
async def unconfigured_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> AsyncIterator[AsyncClient]:
    """ASGI client for an instance where the operator has NOT set FAL_API_KEY."""
    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key="") as ac:
        yield ac
    get_settings.cache_clear()


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession], *, balance: int) -> uuid.UUID:
    async with db_sessionmaker() as session:
        uid = await seed_user(session, balance=balance)
    return uid


async def _balance(db_sessionmaker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with db_sessionmaker() as session:
        value = await session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )
    return int(value)


# ----------------------------------- catalog -----------------------------------


async def test_models_catalog_lists_the_five_models_with_server_side_prices(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _seed(db_sessionmaker, balance=0)

    resp = await media_client.get(_MODELS_URL, headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    models = resp.json()["models"]
    assert [m["id"] for m in models] == [
        "nano-banana-pro",
        "nano-banana-2",
        "kling-video",
        "kling-video-v3",
        "veo-3.1",
    ]
    # No vendor endpoint or queue host leaks to the client — only the public id.
    payload = resp.text
    assert "fal-ai/" not in payload
    assert "queue.fal.run" not in payload
    for model in models:
        assert model["credits"] > 0
        assert model["kind"] in ("image", "video")


async def test_models_catalog_reports_per_mode_parameters(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The client builds its controls from `modes`, so each mode must carry its own parameter
    list and its own accepted values."""
    uid = await _seed(db_sessionmaker, balance=0)

    resp = await media_client.get(_MODELS_URL, headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["models"]}

    veo = by_id["veo-3.1"]
    assert [m["mode"] for m in veo["modes"]] == ["textToVideo", "imageToVideo"]
    assert veo["baseDurationSeconds"] == 4
    assert veo["supportsAudio"] is True
    assert veo["resolutionCredits"] is None
    assert veo["resolutionMultipliers"] == {"720p": 1, "1080p": 1, "4k": 2}
    assert veo["audioMultiplier"] == 2
    text_mode, image_mode = veo["modes"]
    assert text_mode["durations"] == ["4s", "6s", "8s"]
    assert text_mode["resolutions"] == ["720p", "1080p", "4k"]
    # Same parameter, different accepted values per mode.
    assert text_mode["aspectRatios"] == ["16:9", "9:16"]
    assert image_mode["aspectRatios"] == ["auto", "16:9", "9:16"]
    assert "generateAudio" in text_mode["params"]

    kling = by_id["kling-video-v3"]
    kling_text, kling_image = kling["modes"]
    assert kling["baseDurationSeconds"] == 5
    assert kling["resolutionMultipliers"] is None
    # Kling V3 bills audio at x1.5 upstream, so the multiplier is fractional (ADR-061 §2).
    assert kling["audioMultiplier"] == 1.5
    assert kling_text["durations"] == [str(n) for n in range(3, 16)]
    assert "cfgScale" in kling_text["params"]
    # No aspect ratio at all in image-to-video: it comes from the start frame.
    assert kling_image["aspectRatios"] == []
    assert "aspectRatio" not in kling_image["params"]

    image_model = by_id["nano-banana-2"]
    assert [m["mode"] for m in image_model["modes"]] == ["textToImage", "imageToImage"]
    assert image_model["baseDurationSeconds"] is None
    assert image_model["supportsAudio"] is False
    assert image_model["resolutionCredits"] == {
        "0.5K": 3,
        "1K": 4,
        "2K": 6,
        "4K": 8,
    }
    assert image_model["resolutionMultipliers"] is None
    assert image_model["modes"][0]["resolutions"] == ["0.5K", "1K", "2K", "4K"]
    assert image_model["modes"][0]["durations"] == []
    assert {"numImages", "outputFormat", "seed"} <= set(image_model["modes"][0]["params"])


async def test_models_catalog_honours_the_operator_price_override(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    monkeypatch.setenv("MEDIA_MODEL_CREDITS", '{"veo-3.1":777}')
    uid = await _seed(db_sessionmaker, balance=0)

    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as client:
        resp = await client.get(_MODELS_URL, headers=auth_headers(uid))
    get_settings.cache_clear()

    assert resp.status_code == 200, resp.text
    prices = {m["id"]: m["credits"] for m in resp.json()["models"]}
    assert prices["veo-3.1"] == 777


# ----------------------------------- submit -----------------------------------


async def test_image_submit_returns_202_queued_and_debits_the_model_price(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))

    resp = await media_client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "a cat", "resolution": "2K"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["kind"] == "image"
    assert body["model"] == "nano-banana-2"
    assert body["assets"] == []
    assert body["error"] is None
    assert body["creditsRefunded"] is False
    # nano-banana-2 at 2K is the 6-credit tier (1K would be 4).
    assert body["creditsCharged"] == 6
    assert await _balance(db_sessionmaker, uid) == 94


async def test_image_submit_sends_the_fal_queue_contract(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))

    await media_client.post(
        _IMAGES_URL,
        json={
            "model": "nano-banana-2",
            "prompt": "a cat",
            "aspectRatio": "16:9",
            "resolution": "2K",
            "numImages": 2,
            "outputFormat": "png",
        },
        headers=auth_headers(uid),
    )

    assert fal.submit_url == f"{_QUEUE_BASE}/fal-ai/nano-banana-2"
    assert fal.submit_payload == {
        "prompt": "a cat",
        "aspect_ratio": "16:9",
        "resolution": "2K",
        "num_images": 2,
        "output_format": "png",
    }
    # fal's own auth scheme is "Key", not "Bearer".
    assert fal.calls[0]["headers"]["Authorization"] == f"Key {_FAL_KEY}"


async def test_image_price_scales_with_num_images(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """fal bills per produced image, so four images cost four times the base (1K) price."""
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))

    resp = await media_client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "a cat", "numImages": 4},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["creditsCharged"] == 16
    assert await _balance(db_sessionmaker, uid) == 84


async def test_image_price_scales_with_resolution(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """0.5K and 4K are different whole-credit tiers — not the same flat image price."""
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
    cheap = await media_client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "a cat", "resolution": "0.5K"},
        headers=auth_headers(uid),
    )
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
    pricey = await media_client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "a cat", "resolution": "4K"},
        headers=auth_headers(uid),
    )

    assert cheap.status_code == 202, cheap.text
    assert pricey.status_code == 202, pricey.text
    assert cheap.json()["creditsCharged"] == 3
    assert pricey.json()["creditsCharged"] == 8
    assert await _balance(db_sessionmaker, uid) == 89


async def test_veo_price_scales_with_4k_and_audio(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=200)
    fal.on_submit(200, _submit_body("fal-ai/veo3.1"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "veo-3.1",
            "prompt": "a river",
            "duration": "4s",
            "resolution": "4k",
            "generateAudio": True,
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    # 32 (one 4 s pack) x 2 (4k) x 2 (audio).
    assert resp.json()["creditsCharged"] == 128
    assert await _balance(db_sessionmaker, uid) == 72


async def test_video_price_scales_with_duration(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """A 15 s Kling v3 clip is three base durations, so it costs three base prices."""
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v3/pro/text-to-video"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={"model": "kling-video-v3", "prompt": "a river", "duration": "15"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["creditsCharged"] == 69
    assert await _balance(db_sessionmaker, uid) == 31


async def test_a_scaled_price_over_the_balance_is_409_without_submitting(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """The balance is checked against the scaled price, not the base one."""
    uid = await _seed(db_sessionmaker, balance=50)  # enough for 5s and 10s, not for 15s
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v3/pro/text-to-video"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={"model": "kling-video-v3", "prompt": "a river", "duration": "15"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "insufficient_credits"
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 50


async def test_video_submit_forwards_cfg_scale_seed_and_negative_prompt(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v3/pro/text-to-video"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "kling-video-v3",
            "prompt": "a boat",
            "negativePrompt": "blur",
            "cfgScale": 0.8,
            "generateAudio": True,
            "duration": "5",
            # Kling has no resolution/seed: both must be dropped, not forwarded.
            "seed": 42,
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_payload == {
        "prompt": "a boat",
        "negative_prompt": "blur",
        "cfg_scale": 0.8,
        "generate_audio": True,
        "duration": "5",
    }


async def test_veo_takes_a_negative_prompt_and_a_seed(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)
    fal.on_submit(200, _submit_body("fal-ai/veo3.1"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "veo-3.1",
            "prompt": "a city",
            "negativePrompt": "text overlays",
            "seed": 7,
            "resolution": "1080p",
            # Veo has no cfg_scale: dropped rather than forwarded.
            "cfgScale": 0.3,
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    # duration/generateAudio were omitted, so the variant defaults are sent EXPLICITLY — the
    # run we bill and the run we ask for must be the same one (ADR-061 §3).
    assert fal.submit_payload == {
        "prompt": "a city",
        "negative_prompt": "text overlays",
        "seed": 7,
        "resolution": "1080p",
        "duration": "8s",
        "generate_audio": False,
    }


async def test_aspect_ratio_auto_is_rejected_for_veo_text_to_video_but_accepted_with_an_image(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """Values are validated per mode: upstream accepts "auto" only in image-to-video."""
    uid = await _seed(db_sessionmaker, balance=1000)

    rejected = await media_client.post(
        _VIDEOS_URL,
        json={"model": "veo-3.1", "prompt": "a city", "aspectRatio": "auto"},
        headers=auth_headers(uid),
    )
    assert rejected.status_code == 422, rejected.text
    assert "16:9" in rejected.json()["error"]["message"]
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 1000

    fal.on_submit(200, _submit_body("fal-ai/veo3.1/image-to-video"))
    accepted = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "veo-3.1",
            "prompt": "a city",
            "aspectRatio": "auto",
            "imageUrl": "https://cdn.example.com/frame.png",
        },
        headers=auth_headers(uid),
    )
    assert accepted.status_code == 202, accepted.text
    assert fal.submit_payload["aspect_ratio"] == "auto"


async def test_image_submit_with_reference_images_uses_the_edit_endpoint(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-pro/edit"))
    urls = ["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"]

    resp = await media_client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-pro", "prompt": "blend", "imageUrls": urls},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_url == f"{_QUEUE_BASE}/fal-ai/nano-banana-pro/edit"
    assert fal.submit_payload == {
        "prompt": "blend",
        "image_urls": urls,
        "resolution": "1K",
        "num_images": 1,
    }


async def test_video_submit_uses_the_text_to_video_endpoint(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)
    fal.on_submit(200, _submit_body("fal-ai/veo3.1"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "veo-3.1",
            "prompt": "a city at dusk",
            "duration": "8s",
            "resolution": "720p",
            "generateAudio": True,
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["kind"] == "video"
    assert fal.submit_url == f"{_QUEUE_BASE}/fal-ai/veo3.1"
    assert fal.submit_payload == {
        "prompt": "a city at dusk",
        "duration": "8s",
        "resolution": "720p",
        "generate_audio": True,
    }


async def test_kling_v3_image_to_video_sends_start_image_url(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v3/pro/image-to-video"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "kling-video-v3",
            "prompt": "pan right",
            "imageUrl": "https://cdn.example.com/frame.png",
            "duration": "5",
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_url == f"{_QUEUE_BASE}/fal-ai/kling-video/v3/pro/image-to-video"
    assert fal.submit_payload == {
        "prompt": "pan right",
        "duration": "5",
        "generate_audio": False,
        "start_image_url": "https://cdn.example.com/frame.png",
    }


async def test_kling_v25_image_to_video_sends_image_url(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v2.5-turbo/pro/image-to-video"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "kling-video",
            "prompt": "pan right",
            "imageUrl": "https://cdn.example.com/frame.png",
            "negativePrompt": "blurry",
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_payload == {
        "prompt": "pan right",
        "negative_prompt": "blurry",
        "duration": "5",
        "image_url": "https://cdn.example.com/frame.png",
    }


# ----------------------------------- submit validation -----------------------------------


async def test_unknown_model_is_422_and_charges_nothing(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)

    resp = await media_client.post(
        _IMAGES_URL,
        json={"model": "stable-diffusion", "prompt": "a cat"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 100


async def test_video_model_posted_to_the_images_route_is_422(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)

    resp = await media_client.post(
        _IMAGES_URL, json={"model": "veo-3.1", "prompt": "a city"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 422, resp.text
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 1000


async def test_unsupported_parameter_value_is_422_before_any_debit(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)

    # Veo supports 4s/6s/8s; "12s" must be refused here, not upstream after the debit.
    resp = await media_client.post(
        _VIDEOS_URL,
        json={"model": "veo-3.1", "prompt": "a city", "duration": "12s"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 1000


async def test_parameter_unsupported_by_the_model_is_422(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)

    # Kling has no resolution knob at all.
    resp = await media_client.post(
        _VIDEOS_URL,
        json={"model": "kling-video", "prompt": "a city", "resolution": "720p"},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text
    assert fal.calls == []


async def test_insufficient_credits_is_409_and_never_submits(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1)

    resp = await media_client.post(
        _VIDEOS_URL, json={"model": "veo-3.1", "prompt": "a city"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "insufficient_credits"
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 1


# ----------------------------------- upstream failures -----------------------------------


async def test_upstream_5xx_on_submit_is_502_and_rolls_the_debit_back(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(500, {"detail": "internal"})

    resp = await media_client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"
    # The debit shared the request transaction with the (never created) job, so it rolled back.
    assert await _balance(db_sessionmaker, uid) == 100
    async with db_sessionmaker() as session:
        jobs = await session.scalar(text("SELECT count(*) FROM media_jobs"))
    assert int(jobs) == 0


async def test_upstream_timeout_on_submit_is_502_without_charging(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.fail(_httpx.TimeoutException("timeout"))

    resp = await media_client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 502, resp.text
    assert await _balance(db_sessionmaker, uid) == 100


async def test_upstream_401_is_503_not_configured(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(401, {"detail": "invalid key"})

    resp = await media_client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"
    assert await _balance(db_sessionmaker, uid) == 100


async def test_upstream_422_forwards_the_offending_parameter(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    fal.on_submit(
        422, {"detail": [{"loc": ["body", "resolution"], "msg": "value is not a valid enum"}]}
    )

    resp = await media_client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 422, resp.text
    message = resp.json()["error"]["message"]
    assert "resolution" in message
    assert await _balance(db_sessionmaker, uid) == 100


async def test_unconfigured_instance_returns_503_on_submit(
    unconfigured_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)

    resp = await unconfigured_client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"
    assert fal.calls == []
    assert await _balance(db_sessionmaker, uid) == 100


@pytest.mark.parametrize("path", [_MODELS_URL, _JOBS_URL, f"{_JOBS_URL}/{uuid.uuid4()}"])
async def test_unconfigured_instance_returns_503_on_every_read_route(
    unconfigured_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], path: str
) -> None:
    # The catalog must not advertise models an unconfigured instance cannot run: the client decides
    # whether to show the generation section from one call, instead of hitting 503 after the user
    # picked a model.
    uid = await _seed(db_sessionmaker, balance=100)

    resp = await unconfigured_client.get(path, headers=auth_headers(uid))

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"


# ----------------------------------- polling -----------------------------------


async def _submit_image(client: AsyncClient, fal: _Fal, uid: uuid.UUID) -> str:
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
    resp = await client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=auth_headers(uid)
    )
    assert resp.status_code == 202, resp.text
    return str(resp.json()["jobId"])


async def test_polling_a_queued_job_reports_running(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    fal.on_status("IN_PROGRESS")

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "running"
    assert body["assets"] == []


async def test_polling_a_completed_job_returns_normalized_assets(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    fal.on_status("COMPLETED")
    fal.on_result(
        {
            "images": [
                {"url": "https://cdn/a.png", "content_type": "image/png", "file_name": "a.png"}
            ],
            "description": "a cat",
        }
    )

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["assets"] == [
        {"url": "https://cdn/a.png", "contentType": "image/png", "fileName": "a.png"}
    ]
    assert body["creditsRefunded"] is False
    assert await _balance(db_sessionmaker, uid) == 96


async def test_a_completed_job_is_served_from_the_database_without_polling_again(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    fal.on_status("COMPLETED")
    fal.on_result({"images": [{"url": "https://cdn/a.png"}]})
    await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    calls_after_first_poll = len(fal.calls)

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"
    assert len(fal.calls) == calls_after_first_poll


async def test_a_failed_run_refunds_the_credits_once(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    assert await _balance(db_sessionmaker, uid) == 96
    fal.on_status("FAILED", error="content policy violation")

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["creditsRefunded"] is True
    assert body["error"] == "content policy violation"
    assert body["assets"] == []
    assert await _balance(db_sessionmaker, uid) == 100

    # Re-polling a failed job must not refund twice.
    again = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert again.status_code == 200
    assert await _balance(db_sessionmaker, uid) == 100


async def test_a_completed_run_with_no_output_fails_and_refunds(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    fal.on_status("COMPLETED")
    fal.on_result({"images": []})

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["creditsRefunded"] is True
    assert await _balance(db_sessionmaker, uid) == 100


async def test_a_run_rejected_while_executing_fails_and_refunds_instead_of_422(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """fal reports COMPLETED but serves 422 from the result URL (observed with an unreachable
    reference image). The client's GET is valid, so it must see a terminal failed job with a
    refund — not a 422 that repeats on every poll and never returns the credits."""
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    assert await _balance(db_sessionmaker, uid) == 96
    fal.on_status("COMPLETED")
    fal.on_result(
        {"detail": [{"loc": ["body", "image_url"], "msg": "Failed to download the file."}]},
        status_code=422,
    )

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["creditsRefunded"] is True
    assert "image_url" in body["error"]
    assert await _balance(db_sessionmaker, uid) == 100

    # Terminal now: re-polling neither calls fal again nor refunds twice.
    calls = len(fal.calls)
    again = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert again.json()["status"] == "failed"
    assert len(fal.calls) == calls
    assert await _balance(db_sessionmaker, uid) == 100


async def test_a_422_from_the_status_url_also_fails_the_run(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    fal.on_status_error(422, {"detail": "input validation failed"})

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "failed"
    assert body["creditsRefunded"] is True
    assert await _balance(db_sessionmaker, uid) == 100


async def test_a_transient_upstream_error_while_polling_keeps_the_job_pollable(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """A 5xx is not a verdict on the run: the job must stay non-terminal (and unrefunded) so the
    next poll can still pick up the real outcome."""
    uid = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, uid)
    fal.on_status_error(503, {"detail": "upstream down"})

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert resp.status_code == 502, resp.text
    assert await _balance(db_sessionmaker, uid) == 96

    fal.on_status("COMPLETED")
    fal.on_result({"images": [{"url": "https://cdn/a.png"}]})
    recovered = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "completed"
    assert await _balance(db_sessionmaker, uid) == 96


# ----------------------------------- listing & isolation -----------------------------------


async def test_jobs_listing_is_newest_first_and_does_not_poll_upstream(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    first = await _submit_image(media_client, fal, uid)
    second = await _submit_image(media_client, fal, uid)
    calls_after_submits = len(fal.calls)

    resp = await media_client.get(_JOBS_URL, headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    jobs = resp.json()["jobs"]
    assert [j["jobId"] for j in jobs] == [second, first]
    assert all(j["status"] == "queued" for j in jobs)
    assert len(fal.calls) == calls_after_submits


async def test_jobs_listing_filters_by_kind(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=1000)
    image_job = await _submit_image(media_client, fal, uid)
    fal.on_submit(200, _submit_body("fal-ai/veo3.1"))
    await media_client.post(
        _VIDEOS_URL, json={"model": "veo-3.1", "prompt": "a city"}, headers=auth_headers(uid)
    )

    resp = await media_client.get(_JOBS_URL, params={"kind": "image"}, headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    jobs = resp.json()["jobs"]
    assert [j["jobId"] for j in jobs] == [image_job]


async def test_a_foreign_job_is_404_and_absent_from_the_listing(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    owner = await _seed(db_sessionmaker, balance=100)
    other = await _seed(db_sessionmaker, balance=100)
    job_id = await _submit_image(media_client, fal, owner)

    resp = await media_client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(other))
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"

    listing = await media_client.get(_JOBS_URL, headers=auth_headers(other))
    assert listing.json()["jobs"] == []


async def test_a_missing_job_is_404(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)

    resp = await media_client.get(f"{_JOBS_URL}/{uuid.uuid4()}", headers=auth_headers(uid))

    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", _MODELS_URL),
        ("post", _IMAGES_URL),
        ("post", _VIDEOS_URL),
        ("get", _JOBS_URL),
        ("get", f"{_JOBS_URL}/00000000-0000-0000-0000-000000000000"),
    ],
)
async def test_every_media_route_requires_a_bearer_token(
    media_client: AsyncClient, method: str, url: str
) -> None:
    resp = await media_client.request(method, url, json={})

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"


# ------------------------- ADR-061: priced defaults + asset retention -------------------------


async def test_catalog_exposes_the_defaults_the_server_will_substitute(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The client prices a run before submitting it, so it needs the same defaults the server
    applies — otherwise its estimate and `creditsCharged` disagree (ADR-061 §3)."""
    uid = await _seed(db_sessionmaker, balance=0)

    resp = await media_client.get(_MODELS_URL, headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text
    by_id = {m["id"]: m for m in resp.json()["models"]}
    veo_text, veo_image = by_id["veo-3.1"]["modes"]
    assert veo_text["defaults"] == {
        "duration": "8s",
        "resolution": "720p",
        "generateAudio": False,
    }
    assert veo_image["defaults"] == veo_text["defaults"]
    assert by_id["kling-video-v3"]["modes"][0]["defaults"] == {
        "duration": "5",
        "generateAudio": False,
    }
    assert by_id["nano-banana-2"]["modes"][0]["defaults"] == {
        "resolution": "1K",
        "numImages": 1,
    }


async def test_an_omitted_knob_is_sent_explicitly_and_billed_as_sent(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """The leak ADR-061 closes: fal defaults Veo to 8 s WITH audio, so a bare request used to
    generate a $3.20 run while being charged for a silent 4 s one."""
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit(200, _submit_body("fal-ai/veo3.1"))

    resp = await media_client.post(
        _VIDEOS_URL, json={"model": "veo-3.1", "prompt": "a city"}, headers=auth_headers(uid)
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_payload == {
        "prompt": "a city",
        "duration": "8s",
        "resolution": "720p",
        "generate_audio": False,
    }
    # 32 per 4 s pack x 2 packs, no resolution or audio multiplier.
    assert resp.json()["creditsCharged"] == 64
    assert await _balance(db_sessionmaker, uid) == 436


async def test_omitting_a_knob_costs_the_same_as_sending_its_default(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v3/pro/text-to-video"))

    bare = await media_client.post(
        _VIDEOS_URL,
        json={"model": "kling-video-v3", "prompt": "a river"},
        headers=auth_headers(uid),
    )
    spelled_out = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "kling-video-v3",
            "prompt": "a river",
            "duration": "5",
            "generateAudio": False,
        },
        headers=auth_headers(uid),
    )

    assert bare.status_code == 202 and spelled_out.status_code == 202
    assert bare.json()["creditsCharged"] == spelled_out.json()["creditsCharged"] == 23


async def test_kling_v3_audio_costs_one_and_a_half_rounded_up(
    media_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit(200, _submit_body("fal-ai/kling-video/v3/pro/text-to-video"))

    resp = await media_client.post(
        _VIDEOS_URL,
        json={
            "model": "kling-video-v3",
            "prompt": "a river",
            "duration": "5",
            "generateAudio": True,
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    # 23 x 1.5 = 34.5, rounded UP so the run is never priced below its cost.
    assert resp.json()["creditsCharged"] == 35


async def test_asset_retention_header_is_sent_on_submit_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    """fal deletes generated files after ~7 days; the preference keeps the job list's URLs alive."""
    monkeypatch.setenv("FAL_ASSET_RETENTION_SECONDS", "0")
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))

    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as client:
        resp = await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
    get_settings.cache_clear()

    assert resp.status_code == 202, resp.text
    header = fal.calls[0]["headers"]["X-Fal-Object-Lifecycle-Preference"]
    assert header == '{"expiration_duration_seconds": null}'


async def test_no_retention_header_when_the_operator_expressed_no_preference(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    monkeypatch.setenv("FAL_ASSET_RETENTION_SECONDS", "")
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))

    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as client:
        resp = await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
    get_settings.cache_clear()

    assert resp.status_code == 202, resp.text
    assert "X-Fal-Object-Lifecycle-Preference" not in fal.calls[0]["headers"]


async def test_polling_never_carries_the_retention_preference(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    """The preference belongs to object creation; repeating it on GETs would be noise."""
    monkeypatch.setenv("FAL_ASSET_RETENTION_SECONDS", "86400")
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
    fal.on_status("IN_PROGRESS")

    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as client:
        headers = auth_headers(uid)
        submitted = await client.post(
            _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a cat"}, headers=headers
        )
        job_id = submitted.json()["jobId"]
        polled = await client.get(f"{_JOBS_URL}/{job_id}", headers=headers)
    get_settings.cache_clear()

    assert polled.status_code == 200, polled.text
    submit_call, poll_call = fal.calls[0], fal.calls[1]
    assert (
        submit_call["headers"]["X-Fal-Object-Lifecycle-Preference"]
        == '{"expiration_duration_seconds": 86400}'
    )
    assert "X-Fal-Object-Lifecycle-Preference" not in poll_call["headers"]
