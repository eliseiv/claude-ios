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
    # No vendor endpoint leaks to the client — only the public id.
    assert all("fal" not in str(m).lower() or m["id"] == "veo-3.1" for m in models)
    for model in models:
        assert model["credits"] > 0
        assert model["kind"] in ("image", "video")


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
        json={"model": "nano-banana-2", "prompt": "a cat", "resolution": "2K", "numImages": 2},
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
    # nano-banana-2 catalog default is 4 credits.
    assert body["creditsCharged"] == 4
    assert await _balance(db_sessionmaker, uid) == 96


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
    assert fal.submit_payload == {"prompt": "blend", "image_urls": urls}


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
