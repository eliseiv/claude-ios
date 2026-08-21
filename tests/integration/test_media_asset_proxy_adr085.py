"""Integration: ADR-085 — signed media download proxy.

Queue calls stay faked inside ``fal_client.httpx``. The download route's outgoing CDN
fetch is faked at ``asset_proxy.httpx``. Postgres is real.
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
from app.media_generation.signed_url import build_token
from app.models import MediaJob
from tests.conftest import auth_headers, seed_user
from tests.integration.test_media_generation_adr060 import (
    _FAL_KEY,
    _QUEUE_BASE,
    _Fal,
    _make_fake_httpx,
    _submit_body,
)

_IMAGES_URL = "/v1/media/images"
_JOBS_URL = "/v1/media/jobs"
_FAL_ASSET = "https://v3.fal.media/files/b/out.mp4"
_SECRET = "preview-secret-adr085-0123456789abcdef0123456789abcdef"
_DOMAIN = "zenquelo.shop"


class _Cdn:
    """Scripts the outgoing GET/HEAD that the download proxy makes to fal CDN."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status_code = 200
        self.body = b"mp4-bytes"
        self.headers: dict[str, str] = {
            "content-type": "video/mp4",
            "content-length": "9",
            "accept-ranges": "bytes",
        }
        self.exc: BaseException | None = None

    def on_range(self) -> None:
        self.status_code = 206
        self.body = b"mp"
        self.headers = {
            "content-type": "video/mp4",
            "content-length": "2",
            "content-range": "bytes 0-1/9",
            "accept-ranges": "bytes",
        }


def _make_cdn_httpx(cdn: _Cdn) -> SimpleNamespace:
    class _StreamResponse:
        def __init__(self) -> None:
            self.status_code = cdn.status_code
            self.headers = _httpx.Headers(cdn.headers)

        async def aiter_bytes(self) -> AsyncIterator[bytes]:
            yield cdn.body

        async def aclose(self) -> None:
            return None

    class _FakeAsyncClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def build_request(
            self, method: str, url: str, headers: dict[str, str] | None = None
        ) -> SimpleNamespace:
            return SimpleNamespace(method=method, url=url, headers=headers or {})

        async def send(self, request: SimpleNamespace, stream: bool = False) -> _StreamResponse:
            cdn.calls.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": dict(request.headers),
                }
            )
            if cdn.exc is not None:
                raise cdn.exc
            return _StreamResponse()

        async def aclose(self) -> None:
            return None

    return SimpleNamespace(
        AsyncClient=_FakeAsyncClient,
        Timeout=_httpx.Timeout,
        TimeoutException=_httpx.TimeoutException,
        HTTPError=_httpx.HTTPError,
    )


@pytest.fixture
def fal() -> _Fal:
    return _Fal()


@pytest.fixture
def cdn() -> _Cdn:
    return _Cdn()


@pytest.fixture
async def proxy_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    cdn: _Cdn,
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.api_gateway.routers import media as media_router
    from app.main import create_app
    from app.media_generation import asset_proxy as asset_proxy_mod
    from app.media_generation import fal_client as fal_client_mod

    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    monkeypatch.setenv("PREVIEW_URL_SECRET", _SECRET)
    monkeypatch.setenv("SERVICE_DOMAIN", _DOMAIN)
    monkeypatch.setenv("MEDIA_DOWNLOAD_TTL_SECONDS", "86400")
    get_settings.cache_clear()

    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))
    monkeypatch.setattr(asset_proxy_mod, "httpx", _make_cdn_httpx(cdn))

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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    get_settings.cache_clear()


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession], *, balance: int) -> uuid.UUID:
    async with db_sessionmaker() as session:
        return await seed_user(session, balance=balance)


async def _complete_video(client: AsyncClient, fal: _Fal, uid: uuid.UUID) -> tuple[str, str]:
    fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
    created = await client.post(
        _IMAGES_URL, json={"model": "nano-banana-2", "prompt": "a clip"}, headers=auth_headers(uid)
    )
    assert created.status_code == 202, created.text
    job_id = str(created.json()["jobId"])
    fal.on_status("COMPLETED")
    fal.on_result(
        {"images": [{"url": _FAL_ASSET, "content_type": "video/mp4", "file_name": "out.mp4"}]}
    )
    poll = await client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert poll.status_code == 200, poll.text
    url = poll.json()["assets"][0]["url"]
    return job_id, url


@pytest.mark.asyncio
async def test_completed_job_rewrites_fal_url_and_keeps_fal_in_db(
    proxy_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, url = await _complete_video(proxy_client, fal, uid)

    assert url.startswith(f"https://{_DOMAIN}/v1/media/jobs/{job_id}/assets/0/")
    assert "fal.media" not in url

    async with db_sessionmaker() as session:
        stored = await session.scalar(
            text("SELECT result FROM media_jobs WHERE id = :id"), {"id": job_id}
        )
    assert stored["assets"][0]["url"] == _FAL_ASSET


@pytest.mark.asyncio
async def test_signed_download_streams_bytes_without_jwt(
    proxy_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    cdn: _Cdn,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    _job_id, url = await _complete_video(proxy_client, fal, uid)
    path = url.removeprefix(f"https://{_DOMAIN}")

    resp = await proxy_client.get(path)

    assert resp.status_code == 200, resp.text
    assert resp.content == b"mp4-bytes"
    assert resp.headers["content-type"].startswith("video/mp4")
    assert cdn.calls[0]["url"] == _FAL_ASSET
    assert "authorization" not in {k.lower() for k in cdn.calls[0]["headers"]}


@pytest.mark.asyncio
async def test_signed_download_range_is_206(
    proxy_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    cdn: _Cdn,
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    _job_id, url = await _complete_video(proxy_client, fal, uid)
    path = url.removeprefix(f"https://{_DOMAIN}")
    cdn.on_range()

    resp = await proxy_client.get(path, headers={"Range": "bytes=0-1"})

    assert resp.status_code == 206, resp.text
    assert resp.content == b"mp"
    assert resp.headers["content-range"] == "bytes 0-1/9"
    assert cdn.calls[0]["headers"]["Range"] == "bytes=0-1"


@pytest.mark.asyncio
async def test_bad_token_is_401(
    proxy_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(proxy_client, fal, uid)

    resp = await proxy_client.get(f"{_JOBS_URL}/{job_id}/assets/0/not-a-token")

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_foreign_owner_token_is_401(
    proxy_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(proxy_client, fal, uid)
    forged = build_token(job_id=uuid.UUID(job_id), owner_user_id=uuid.uuid4(), index=0).token

    resp = await proxy_client.get(f"{_JOBS_URL}/{job_id}/assets/0/{forged}")

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_host_outside_allowlist_is_404(
    proxy_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=100)
    job_id, _url = await _complete_video(proxy_client, fal, uid)
    async with db_sessionmaker() as session:
        row = await session.get(MediaJob, uuid.UUID(job_id))
        assert row is not None
        row.result = {"assets": [{"url": "https://evil.example/x.mp4", "contentType": "video/mp4"}]}
        await session.commit()
    token = build_token(job_id=uuid.UUID(job_id), owner_user_id=uid, index=0).token

    resp = await proxy_client.get(f"{_JOBS_URL}/{job_id}/assets/0/{token}")

    assert resp.status_code == 404
