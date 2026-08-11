"""Integration: device-token CRUD + media-ready push once (ADR-067)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx as _httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.notifications.apns_client import ApnsClient
from tests.conftest import auth_headers, make_jwt, seed_user

_TOKEN_URL = "/v1/notifications/device-token"
_FAL_KEY = "fal-test-key-push"  # noqa: S105
_QUEUE_BASE = "https://queue.fal.run"
_REQUEST_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None) -> None:
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data


class _Fal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._endpoint = "fal-ai/flux/dev"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, "json": json})
        if method == "POST":
            # Capture endpoint from submit URL: https://queue.fal.run/<endpoint>
            path = url.removeprefix(_QUEUE_BASE + "/")
            self._endpoint = path
            base = f"{_QUEUE_BASE}/{path}/requests/{_REQUEST_ID}"
            return _FakeResponse(
                200,
                {
                    "request_id": _REQUEST_ID,
                    "status": "IN_QUEUE",
                    "status_url": f"{base}/status",
                    "response_url": base,
                },
            )
        if url.endswith("/status"):
            return _FakeResponse(200, {"status": "COMPLETED"})
        return _FakeResponse(
            200,
            {
                "images": [
                    {"url": "https://v3.fal.media/files/out.png", "content_type": "image/png"}
                ]
            },
        )


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
        ) -> _FakeResponse:
            return await fal._request(method, url, headers=headers, json=json)

    return SimpleNamespace(
        AsyncClient=_FakeAsyncClient,
        TimeoutException=_httpx.TimeoutException,
        RequestError=_httpx.RequestError,
        ConnectError=_httpx.ConnectError,
        HTTPError=_httpx.HTTPError,
        Response=_httpx.Response,
    )


@pytest.mark.asyncio
async def test_register_upsert_and_delete_device_token(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    headers = auth_headers(uid, device_id="dev-ios-1")
    r1 = await client.post(
        _TOKEN_URL,
        json={"pushToken": "aaaaaaaa", "platform": "ios"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json() == {"registered": True}

    r2 = await client.post(
        _TOKEN_URL,
        json={"deviceId": "dev-ios-1", "pushToken": "bbbbbbbb", "platform": "ios"},
        headers=headers,
    )
    assert r2.status_code == 200

    async with db_sessionmaker() as s:
        n = await s.scalar(
            text("SELECT count(*) FROM device_push_tokens WHERE user_id=:u"), {"u": uid}
        )
        tok = await s.scalar(
            text("SELECT push_token FROM device_push_tokens WHERE user_id=:u"), {"u": uid}
        )
    assert int(n or 0) == 1
    assert tok == "bbbbbbbb"

    d = await client.request(
        "DELETE",
        _TOKEN_URL,
        json={"deviceId": "dev-ios-1"},
        headers=headers,
    )
    assert d.status_code == 200
    assert d.json() == {"deleted": True}


@pytest.mark.asyncio
async def test_register_requires_device_id(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    headers = {"Authorization": f"Bearer {make_jwt(uid, device_id=None)}"}
    r = await client.post(
        _TOKEN_URL,
        json={"pushToken": "tok", "platform": "ios"},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_media_completed_sends_push_once(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    from app import deps
    from app.api_gateway.routers import media as media_router
    from app.api_gateway.routers import notifications as notifications_router
    from app.api_gateway.routers import preferences as preferences_router
    from app.main import create_app
    from app.media_generation import fal_client as fal_client_mod

    fal = _Fal()
    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    get_settings.cache_clear()
    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))

    async def _allow(*, user_id: uuid.UUID) -> bool:
        return True

    monkeypatch.setattr(media_router, "enforce_other_limits", _allow)
    monkeypatch.setattr(notifications_router, "enforce_other_limits", _allow)
    monkeypatch.setattr(preferences_router, "enforce_other_limits", _allow)

    fake_apns = AsyncMock(spec=ApnsClient)
    fake_apns.configured = True
    fake_apns.build_media_ready_payload = ApnsClient(get_settings()).build_media_ready_payload
    fake_apns.send = AsyncMock(return_value="sent")
    monkeypatch.setattr(deps, "get_apns_client", lambda: fake_apns)

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

    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)

    headers = auth_headers(uid, device_id="dev-1")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (
            await client.patch(
                "/v1/preferences",
                json={"notificationsEnabled": True},
                headers=headers,
            )
        ).status_code == 200
        assert (
            await client.post(
                _TOKEN_URL,
                json={"pushToken": "push-token-hex", "platform": "ios"},
                headers=headers,
            )
        ).status_code == 200

        submit = await client.post(
            "/v1/media/images",
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=headers,
        )
        assert submit.status_code == 202, submit.text
        job_id = submit.json()["jobId"]

        poll = await client.get(f"/v1/media/jobs/{job_id}", headers=headers)
        assert poll.status_code == 200, poll.text
        assert poll.json()["status"] == "completed"

        assert fake_apns.send.await_count == 1
        kwargs = fake_apns.send.await_args.kwargs
        assert kwargs["device_token"] == "push-token-hex"
        payload = kwargs["payload"]
        assert payload["jobId"] == job_id
        assert payload["kind"] == "image"
        assert payload["mediaUrl"].startswith("https://")
        assert payload["aps"]["mutable-content"] == 1

        poll2 = await client.get(f"/v1/media/jobs/{job_id}", headers=headers)
        assert poll2.status_code == 200
        assert fake_apns.send.await_count == 1

    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_reconciler_advances_and_pushes(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import dispose_engine
    from app.media_generation import fal_client as fal_client_mod
    from app.media_generation.reconciler import reconcile_once

    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    get_settings.cache_clear()
    # Reconciler uses the global app.db sessionmaker; bind a fresh engine to this loop.
    await dispose_engine()

    fal = _Fal()
    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))

    sends: list[dict[str, Any]] = []

    class _Apns:
        configured = True

        def build_media_ready_payload(self, push: Any) -> dict[str, Any]:
            return ApnsClient(get_settings()).build_media_ready_payload(push)

        async def send(self, *, device_token: str, payload: dict[str, Any]) -> str:
            sends.append({"device_token": device_token, "payload": payload})
            return "sent"

    monkeypatch.setattr(
        "app.media_generation.reconciler.ApnsClient",
        lambda _settings: _Apns(),
    )

    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=100)
        await s.execute(
            text(
                "INSERT INTO user_preferences (user_id, notifications_enabled) " "VALUES (:u, true)"
            ),
            {"u": uid},
        )
        await s.execute(
            text(
                "INSERT INTO device_push_tokens (user_id, device_id, push_token, platform) "
                "VALUES (:u, 'd1', 'tok1', 'ios')"
            ),
            {"u": uid},
        )
        job_id = uuid.uuid4()
        await s.execute(
            text(
                """
                INSERT INTO media_jobs (
                    id, user_id, model_id, kind, fal_endpoint, fal_request_id,
                    status_url, response_url, status, prompt, credits_charged
                ) VALUES (
                    :id, :u, 'nano-banana-2', 'image', 'fal-ai/nano-banana', :rid,
                    :su, :ru, 'queued', 'hi', 4
                )
                """
            ),
            {
                "id": job_id,
                "u": uid,
                "rid": _REQUEST_ID,
                "su": f"{_QUEUE_BASE}/fal-ai/nano-banana/requests/{_REQUEST_ID}/status",
                "ru": f"{_QUEUE_BASE}/fal-ai/nano-banana/requests/{_REQUEST_ID}",
            },
        )
        await s.commit()

    advanced = await reconcile_once(get_settings())
    assert advanced == 1
    assert len(sends) == 1
    assert sends[0]["payload"]["jobId"] == str(job_id)

    async with db_sessionmaker() as s:
        status = await s.scalar(text("SELECT status FROM media_jobs WHERE id=:id"), {"id": job_id})
        pushed = await s.scalar(
            text("SELECT push_sent_at IS NOT NULL FROM media_jobs WHERE id=:id"), {"id": job_id}
        )
    assert status == "completed"
    assert pushed is True

    advanced2 = await reconcile_once(get_settings())
    assert advanced2 == 0
    assert len(sends) == 1
    get_settings.cache_clear()
