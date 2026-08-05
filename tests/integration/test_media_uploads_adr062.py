"""Integration: ADR-062 — `POST /v1/media/uploads`.

Drives the full HTTP path (real JWT + lazy provisioning against the shared testcontainers
Postgres) while the outgoing fal storage calls are faked at the ``httpx`` boundary, exactly as the
ADR-060 suite does. No network, no LLM.

Covers the two-step storage contract (initiate → PUT raw bytes), the limits, the media-type and
magic-byte guards, the host allowlist that keeps a user's file off an arbitrary host, the shared
error map, and the thing the endpoint exists for: the URL it returns is accepted by
`POST /v1/media/images`.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers, seed_user

_UPLOADS_URL = "/v1/media/uploads"
_IMAGES_URL = "/v1/media/images"
_FAL_KEY = "fal-test-key-abc123"  # noqa: S105 - test-only static secret
_QUEUE_BASE = "https://queue.fal.run"
_REST_BASE = "https://rest.fal.ai"
_UPLOAD_URL = "https://v3b.fal.media/upload/slot/abc123"
_FILE_URL = "https://v3b.fal.media/files/b/abc123/photo.png"

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPEG = b"\xff\xd8\xff" + b"\x00" * 64


def _body(raw: bytes = _PNG, *, media_type: str = "image/png", name: str = "photo.png") -> dict:
    return {
        "type": "image",
        "mediaType": media_type,
        "filename": name,
        "data": base64.b64encode(raw).decode(),
    }


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None) -> None:
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data


class _Fal:
    """Scripts and records the faked storage calls (initiate POST + slot PUT)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._initiate = _FakeResponse(200, {"upload_url": _UPLOAD_URL, "file_url": _FILE_URL})
        self._put = _FakeResponse(200, None)
        self._submit: _FakeResponse | None = None
        self._exc: BaseException | None = None

    def on_initiate(self, status_code: int, json_data: Any = None) -> None:
        self._initiate = _FakeResponse(status_code, json_data)

    def on_put(self, status_code: int) -> None:
        self._put = _FakeResponse(status_code, None)

    def on_submit(self, status_code: int, json_data: Any = None) -> None:
        self._submit = _FakeResponse(status_code, json_data)

    def fail(self, exc: BaseException) -> None:
        self._exc = exc

    def call(self, method: str) -> dict[str, Any]:
        for entry in self.calls:
            if entry["method"] == method:
                return entry
        raise AssertionError(f"no {method} call recorded")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> _FakeResponse:
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json, "content": content}
        )
        if self._exc is not None:
            raise self._exc
        if method == "PUT":
            return self._put
        if url.startswith(_QUEUE_BASE):
            assert self._submit is not None, "submit not scripted"
            return self._submit
        return self._initiate


def _make_fake_httpx(fal: _Fal) -> SimpleNamespace:
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
            content: bytes | None = None,
        ) -> _FakeResponse:
            return await fal._request(method, url, headers=headers, json=json, content=content)

    return SimpleNamespace(
        AsyncClient=_FakeAsyncClient,
        TimeoutException=_httpx.TimeoutException,
        RequestError=_httpx.RequestError,
        ConnectError=_httpx.ConnectError,
        Response=_httpx.Response,
    )


@pytest.fixture
def fal() -> _Fal:
    return _Fal()


def _build_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    *,
    fal_key: str = _FAL_KEY,
) -> AsyncClient:
    from app import deps
    from app.api_gateway.routers import media as media_router
    from app.main import create_app
    from app.media_generation import fal_client as fal_client_mod

    monkeypatch.setenv("FAL_API_KEY", fal_key)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    monkeypatch.setenv("FAL_REST_BASE", _REST_BASE)
    get_settings.cache_clear()

    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))

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
async def client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> AsyncIterator[AsyncClient]:
    async with _build_client(monkeypatch, db_sessionmaker, fal) as ac:
        yield ac
    get_settings.cache_clear()


async def _seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with db_sessionmaker() as session:
        return await seed_user(session, balance=100)


# ----------------------------------- happy path -----------------------------------


async def test_upload_returns_an_https_url_for_the_generation_endpoints(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url"] == _FILE_URL
    assert body["url"].startswith("https://")
    assert body["mediaType"] == "image/png"
    assert body["size"] == len(_PNG)


async def test_upload_follows_fals_two_step_storage_contract(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)

    await client.post(_UPLOADS_URL, json=_body(name="cat.png"), headers=auth_headers(uid))

    initiate = fal.call("POST")
    assert initiate["url"].startswith(f"{_REST_BASE}/storage/upload/initiate")
    assert initiate["json"] == {"file_name": "cat.png", "content_type": "image/png"}
    # fal's own auth scheme, not Bearer.
    assert initiate["headers"]["Authorization"] == f"Key {_FAL_KEY}"

    put = fal.call("PUT")
    assert put["url"] == _UPLOAD_URL
    assert put["content"] == _PNG
    assert put["headers"]["Content-Type"] == "image/png"


async def test_our_api_key_is_not_sent_to_the_slot_host(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """The slot URL carries its own authorization; our key has no business leaving the API host."""
    uid = await _seed(db_sessionmaker)

    await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert "Authorization" not in fal.call("PUT")["headers"]


async def test_the_uploaded_url_is_accepted_as_a_reference_image(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """The whole point: a local photo becomes something image-to-image will take."""
    uid = await _seed(db_sessionmaker)
    fal.on_submit(
        200,
        {
            "request_id": "11111111-2222-3333-4444-555555555555",
            "status": "IN_QUEUE",
            "status_url": f"{_QUEUE_BASE}/fal-ai/nano-banana-2/requests/x/status",
            "response_url": f"{_QUEUE_BASE}/fal-ai/nano-banana-2/requests/x",
        },
    )

    uploaded = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))
    url = uploaded.json()["url"]
    generated = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "add a hat", "imageUrls": [url]},
        headers=auth_headers(uid),
    )

    assert generated.status_code == 202, generated.text
    submit = next(
        c for c in fal.calls if c["method"] == "POST" and c["url"].startswith(_QUEUE_BASE)
    )
    assert submit["json"]["image_urls"] == [url]
    assert submit["url"].endswith("/edit")


async def test_upload_costs_no_credits(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """Charging for preparation would make a mis-picked reference a paid mistake."""
    from sqlalchemy import text

    uid = await _seed(db_sessionmaker)

    await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    async with db_sessionmaker() as session:
        balance = await session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )
    assert int(balance) == 100


# ----------------------------------- validation -----------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"type": "document", "mediaType": "application/pdf", "filename": "a.pdf", "data": "eA=="},
        {"type": "image", "mediaType": "image/svg+xml", "filename": "a.svg", "data": "eA=="},
        {"type": "image", "mediaType": "image/png", "filename": "", "data": "eA=="},
        {"type": "image", "mediaType": "image/png", "filename": "a.png", "data": "not base64!!"},
    ],
)
async def test_bad_bodies_are_422_and_never_reach_the_provider(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    body: dict[str, str],
) -> None:
    uid = await _seed(db_sessionmaker)

    resp = await client.post(_UPLOADS_URL, json=body, headers=auth_headers(uid))

    assert resp.status_code == 422, resp.text
    assert fal.calls == []


async def test_a_declared_type_that_the_bytes_contradict_is_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """Renaming a JPEG to .png must not get it past the type check."""
    uid = await _seed(db_sessionmaker)

    resp = await client.post(
        _UPLOADS_URL, json=_body(_JPEG, media_type="image/png"), headers=auth_headers(uid)
    )

    assert resp.status_code == 422, resp.text
    assert fal.calls == []


async def test_a_file_over_the_size_cap_is_413_before_the_provider_is_touched(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    monkeypatch.setenv("MEDIA_UPLOAD_MAX_BYTES", "16")  # the fixture PNG is 72 bytes
    uid = await _seed(db_sessionmaker)

    async with _build_client(monkeypatch, db_sessionmaker, fal) as ac:
        resp = await ac.post(_UPLOADS_URL, json=_body(_PNG), headers=auth_headers(uid))
    get_settings.cache_clear()

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert fal.calls == []


async def test_a_body_over_the_transport_limit_is_413_at_the_gateway(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    """The gateway rejects on Content-Length, before the body is parsed at all."""
    monkeypatch.setenv("MEDIA_UPLOAD_REQUEST_BODY_LIMIT", "64")  # the JSON body is ~190 B
    uid = await _seed(db_sessionmaker)

    async with _build_client(monkeypatch, db_sessionmaker, fal) as ac:
        resp = await ac.post(_UPLOADS_URL, json=_body(_PNG), headers=auth_headers(uid))
    get_settings.cache_clear()

    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert fal.calls == []


# ----------------------------------- upstream failures -----------------------------------


async def test_an_upload_url_on_an_unexpected_host_is_502_and_no_bytes_leave(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """A PUT here carries a user's file; an upstream body must not be able to redirect it."""
    uid = await _seed(db_sessionmaker)
    fal.on_initiate(200, {"upload_url": "https://evil.example.com/collect", "file_url": _FILE_URL})

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstream_error"
    assert [c["method"] for c in fal.calls] == ["POST"]


async def test_a_file_url_on_an_unexpected_host_is_502(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_initiate(200, {"upload_url": _UPLOAD_URL, "file_url": "http://evil.example.com/f.png"})

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 502, resp.text


async def test_a_malformed_slot_response_is_502(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_initiate(200, {"upload_url": _UPLOAD_URL})

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 502, resp.text


async def test_a_failed_put_is_502(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_put(500)

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 502, resp.text


async def test_a_timeout_is_502(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.fail(_httpx.TimeoutException("too slow"))

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 502, resp.text


async def test_a_rejected_key_is_503_not_502(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """A bad key is the operator's problem and must stay distinguishable from a fal outage."""
    uid = await _seed(db_sessionmaker)
    fal.on_initiate(401, {"detail": "unauthorized"})

    resp = await client.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"


async def test_an_instance_without_a_key_answers_503(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    uid = await _seed(db_sessionmaker)

    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key="") as ac:
        resp = await ac.post(_UPLOADS_URL, json=_body(), headers=auth_headers(uid))
    get_settings.cache_clear()

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"
    assert fal.calls == []


async def test_upload_requires_a_bearer_token(client: AsyncClient) -> None:
    resp = await client.post(_UPLOADS_URL, json=_body())

    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthorized"
