"""Integration: ADR-063 — the generations feed, edit chains and job deletion.

Full HTTP path (real JWT, real wallet, real ``media_jobs``) with the outgoing fal calls faked at
the ``httpx`` boundary, same as the ADR-060 suite. No network, no LLM.

Covers the three things that turn a list of jobs into something a client can render as a feed:
paging that neither duplicates nor skips, a link between a generation and the one it was edited
from, and the ability to remove a job — without ever letting a refund escape.
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

_IMAGES_URL = "/v1/media/images"
_VIDEOS_URL = "/v1/media/videos"
_JOBS_URL = "/v1/media/jobs"

_FAL_KEY = "fal-test-key-abc123"  # noqa: S105 - test-only static secret
_QUEUE_BASE = "https://queue.fal.run"
_ASSET_A = "https://v3.fal.media/files/a/out-a.png"
_ASSET_B = "https://v3.fal.media/files/b/out-b.png"


def _submit_body(endpoint: str, request_id: str) -> dict[str, Any]:
    base = f"{_QUEUE_BASE}/{endpoint}/requests/{request_id}"
    return {
        "request_id": request_id,
        "status": "IN_QUEUE",
        "status_url": f"{base}/status",
        "response_url": base,
    }


class _FakeResponse:
    def __init__(self, status_code: int, json_data: Any = None) -> None:
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> Any:
        return self._json_data


class _Fal:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._submit: _FakeResponse | None = None
        self._status: _FakeResponse | None = None
        self._result: _FakeResponse | None = None

    def on_submit(self, endpoint: str, request_id: str = "req-1") -> None:
        self._submit = _FakeResponse(200, _submit_body(endpoint, request_id))

    def on_status(self, status: str) -> None:
        self._status = _FakeResponse(200, {"status": status})

    def on_result(self, json_data: Any) -> None:
        self._result = _FakeResponse(200, json_data)

    @property
    def submit_payload(self) -> dict[str, Any]:
        for call in reversed(self.calls):
            if call["method"] == "POST":
                return dict(call["json"] or {})
        raise AssertionError("no submit call recorded")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, "json": json})
        if method == "POST":
            assert self._submit is not None, "submit not scripted"
            return self._submit
        if url.endswith("/status"):
            assert self._status is not None, "status not scripted"
            return self._status
        assert self._result is not None, "result not scripted"
        return self._result


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


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> AsyncIterator[AsyncClient]:
    from app import deps
    from app.api_gateway.routers import media as media_router
    from app.main import create_app
    from app.media_generation import fal_client as fal_client_mod

    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    get_settings.cache_clear()


async def _seed(
    db_sessionmaker: async_sessionmaker[AsyncSession], *, balance: int = 10_000
) -> uuid.UUID:
    async with db_sessionmaker() as session:
        return await seed_user(session, balance=balance)


async def _completed_image_job(
    client: AsyncClient, fal: _Fal, uid: uuid.UUID, *, assets: list[str]
) -> str:
    """Submit an image generation and drive it to `completed` with the given assets."""
    fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))
    submitted = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "a cat"},
        headers=auth_headers(uid),
    )
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["jobId"]

    fal.on_status("COMPLETED")
    fal.on_result({"images": [{"url": url} for url in assets]})
    polled = await client.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert polled.json()["status"] == "completed", polled.text
    return str(job_id)


async def _set_status(
    db_sessionmaker: async_sessionmaker[AsyncSession], job_id: str, status: str
) -> None:
    async with db_sessionmaker() as session:
        await session.execute(
            text("UPDATE media_jobs SET status = :s WHERE id = :id"),
            {"s": status, "id": job_id},
        )
        await session.commit()


# ----------------------------------- edit chains -----------------------------------


async def test_source_job_id_feeds_the_parents_assets_to_the_provider(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """ "I generated a photo and then picked edit" — without the client tracking any URL."""
    uid = await _seed(db_sessionmaker)
    parent = await _completed_image_job(client, fal, uid, assets=[_ASSET_A])

    fal.on_submit("fal-ai/nano-banana-2/edit", request_id=str(uuid.uuid4()))
    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "add a hat", "sourceJobId": parent},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_payload["image_urls"] == [_ASSET_A]
    body = resp.json()
    assert body["parentJobId"] == parent
    assert body["inputImageUrls"] == [_ASSET_A]


async def test_the_chain_survives_deleting_the_parent(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """Removing a bad source frame takes it out of the feed; it does not erase the edits."""
    uid = await _seed(db_sessionmaker)
    parent = await _completed_image_job(client, fal, uid, assets=[_ASSET_A])
    fal.on_submit("fal-ai/nano-banana-2/edit", request_id=str(uuid.uuid4()))
    child = (
        await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "add a hat", "sourceJobId": parent},
            headers=auth_headers(uid),
        )
    ).json()["jobId"]

    deleted = await client.delete(f"{_JOBS_URL}/{parent}", headers=auth_headers(uid))
    still_there = await client.get(f"{_JOBS_URL}/{child}", headers=auth_headers(uid))

    assert deleted.status_code == 200, deleted.text
    assert still_there.status_code == 200, still_there.text
    body = still_there.json()
    assert body["parentJobId"] is None
    # What it was made from is still visible — that is why the URLs are persisted, not derived.
    assert body["inputImageUrls"] == [_ASSET_A]


async def test_source_job_id_and_image_urls_together_are_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    parent = await _completed_image_job(client, fal, uid, assets=[_ASSET_A])
    before = len(fal.calls)

    resp = await client.post(
        _IMAGES_URL,
        json={
            "model": "nano-banana-2",
            "prompt": "add a hat",
            "sourceJobId": parent,
            "imageUrls": ["https://cdn.example.com/other.png"],
        },
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text
    assert len(fal.calls) == before


async def test_a_foreign_source_job_is_404_not_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """A foreign job must be indistinguishable from a missing one."""
    owner = await _seed(db_sessionmaker)
    stranger = await _seed(db_sessionmaker)
    parent = await _completed_image_job(client, fal, owner, assets=[_ASSET_A])

    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "add a hat", "sourceJobId": parent},
        headers=auth_headers(stranger),
    )

    assert resp.status_code == 404, resp.text
    missing = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "x", "sourceJobId": str(uuid.uuid4())},
        headers=auth_headers(stranger),
    )
    assert missing.status_code == 404, missing.text


async def test_an_unfinished_source_job_is_422_and_charges_nothing(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker, balance=500)
    fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))
    pending = (
        await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
    ).json()["jobId"]

    async with db_sessionmaker() as session:
        balance_before = await session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )

    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "add a hat", "sourceJobId": pending},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text
    async with db_sessionmaker() as session:
        balance_after = await session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )
    assert balance_after == balance_before


async def test_a_video_source_job_is_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """We do not pull a frame out of a video; both edit modes want a picture in."""
    uid = await _seed(db_sessionmaker)
    fal.on_submit("fal-ai/veo3.1", request_id=str(uuid.uuid4()))
    video = (
        await client.post(
            _VIDEOS_URL,
            json={"model": "veo-3.1", "prompt": "a city", "duration": "4s"},
            headers=auth_headers(uid),
        )
    ).json()["jobId"]
    fal.on_status("COMPLETED")
    fal.on_result({"video": {"url": "https://v3.fal.media/files/v/clip.mp4"}})
    await client.get(f"{_JOBS_URL}/{video}", headers=auth_headers(uid))

    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "edit", "sourceJobId": video},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text


async def test_a_source_job_with_no_output_is_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))
    job = (
        await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
    ).json()["jobId"]
    # A run that failed is terminal but carries nothing to edit.
    fal.on_status("FAILED")
    await client.get(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))

    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "edit", "sourceJobId": job},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 422, resp.text


async def test_a_video_run_takes_only_one_frame_from_a_multi_asset_source(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """maxInputImages is 1 for Veo, so a two-asset parent must not overflow into the payload."""
    uid = await _seed(db_sessionmaker)
    parent = await _completed_image_job(client, fal, uid, assets=[_ASSET_A, _ASSET_B])

    fal.on_submit("fal-ai/veo3.1/image-to-video", request_id=str(uuid.uuid4()))
    resp = await client.post(
        _VIDEOS_URL,
        json={"model": "veo-3.1", "prompt": "pan", "duration": "4s", "sourceJobId": parent},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert fal.submit_payload["image_url"] == _ASSET_A
    assert resp.json()["inputImageUrls"] == [_ASSET_A]


async def test_explicit_image_urls_are_recorded_too(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_submit("fal-ai/nano-banana-2/edit", request_id=str(uuid.uuid4()))
    urls = ["https://cdn.example.com/a.png", "https://cdn.example.com/b.png"]

    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "blend", "imageUrls": urls},
        headers=auth_headers(uid),
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["inputImageUrls"] == urls
    assert resp.json()["parentJobId"] is None


async def test_a_text_only_run_has_an_empty_chain(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))

    resp = await client.post(
        _IMAGES_URL,
        json={"model": "nano-banana-2", "prompt": "a cat"},
        headers=auth_headers(uid),
    )

    assert resp.json()["parentJobId"] is None
    assert resp.json()["inputImageUrls"] == []


# ----------------------------------- feed pagination -----------------------------------


async def _submit_n(client: AsyncClient, fal: _Fal, uid: uuid.UUID, n: int) -> list[str]:
    ids = []
    for _ in range(n):
        fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))
        resp = await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
        assert resp.status_code == 202, resp.text
        ids.append(resp.json()["jobId"])
    return ids


async def test_the_feed_pages_without_duplicates_or_gaps(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    created = await _submit_n(client, fal, uid, 7)

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous bound; the loop must end by nextCursor going null
        url = f"{_JOBS_URL}?limit=3" + (f"&cursor={cursor}" if cursor else "")
        page = await client.get(url, headers=auth_headers(uid))
        assert page.status_code == 200, page.text
        body = page.json()
        seen.extend(job["jobId"] for job in body["jobs"])
        cursor = body["nextCursor"]
        if cursor is None:
            break

    assert cursor is None, "feed never reported a last page"
    assert len(seen) == len(set(seen)) == 7
    # Newest first, so the walk is the reverse of the creation order.
    assert seen == list(reversed(created))


async def test_the_last_page_reports_no_cursor(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    await _submit_n(client, fal, uid, 2)

    page = await client.get(f"{_JOBS_URL}?limit=5", headers=auth_headers(uid))

    assert page.json()["nextCursor"] is None
    assert len(page.json()["jobs"]) == 2


async def test_an_empty_feed_has_no_cursor(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _seed(db_sessionmaker)

    page = await client.get(_JOBS_URL, headers=auth_headers(uid))

    assert page.json() == {"jobs": [], "nextCursor": None}


async def test_the_kind_filter_survives_paging(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    await _submit_n(client, fal, uid, 3)
    fal.on_submit("fal-ai/veo3.1", request_id=str(uuid.uuid4()))
    await client.post(
        _VIDEOS_URL,
        json={"model": "veo-3.1", "prompt": "a city", "duration": "4s"},
        headers=auth_headers(uid),
    )

    first = await client.get(f"{_JOBS_URL}?limit=2&kind=image", headers=auth_headers(uid))
    second = await client.get(
        f"{_JOBS_URL}?limit=2&kind=image&cursor={first.json()['nextCursor']}",
        headers=auth_headers(uid),
    )

    assert [j["kind"] for j in first.json()["jobs"]] == ["image", "image"]
    assert [j["kind"] for j in second.json()["jobs"]] == ["image"]
    assert second.json()["nextCursor"] is None


async def test_the_feed_never_leaks_another_users_jobs(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    owner = await _seed(db_sessionmaker)
    stranger = await _seed(db_sessionmaker)
    await _submit_n(client, fal, owner, 3)

    page = await client.get(_JOBS_URL, headers=auth_headers(stranger))

    assert page.json()["jobs"] == []


async def test_a_broken_cursor_is_422(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Degrading to "start from the top" would loop the feed forever."""
    uid = await _seed(db_sessionmaker)

    page = await client.get(f"{_JOBS_URL}?cursor=not-a-cursor", headers=auth_headers(uid))

    assert page.status_code == 422, page.text
    assert page.json()["error"]["code"] == "validation_error"


async def test_the_feed_still_does_not_poll_the_provider(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    await _submit_n(client, fal, uid, 3)
    before = len(fal.calls)

    await client.get(f"{_JOBS_URL}?limit=2", headers=auth_headers(uid))

    assert len(fal.calls) == before


# ----------------------------------- deletion -----------------------------------


async def test_deleting_a_completed_job_removes_it_from_the_feed(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    job = await _completed_image_job(client, fal, uid, assets=[_ASSET_A])

    deleted = await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))
    gone = await client.get(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))
    feed = await client.get(_JOBS_URL, headers=auth_headers(uid))

    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}
    assert gone.status_code == 404
    assert feed.json()["jobs"] == []


async def test_a_failed_job_can_be_deleted(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))
    job = (
        await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
    ).json()["jobId"]
    fal.on_status("FAILED")
    await client.get(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))

    resp = await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))

    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("status", ["queued", "running"])
async def test_an_unfinished_job_cannot_be_deleted(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    status: str,
) -> None:
    """Deleting it would destroy the only row the refund can be attributed to."""
    uid = await _seed(db_sessionmaker)
    fal.on_submit("fal-ai/nano-banana-2", request_id=str(uuid.uuid4()))
    job = (
        await client.post(
            _IMAGES_URL,
            json={"model": "nano-banana-2", "prompt": "a cat"},
            headers=auth_headers(uid),
        )
    ).json()["jobId"]
    await _set_status(db_sessionmaker, job, status)

    resp = await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))
    # Checked through the feed, which never polls the provider — the job must still be there.
    feed = await client.get(_JOBS_URL, headers=auth_headers(uid))

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "job_not_terminal"
    assert [j["jobId"] for j in feed.json()["jobs"]] == [job]


async def test_deleting_someone_elses_job_is_404_and_leaves_it_alone(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    owner = await _seed(db_sessionmaker)
    stranger = await _seed(db_sessionmaker)
    job = await _completed_image_job(client, fal, owner, assets=[_ASSET_A])

    resp = await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(stranger))
    survived = await client.get(f"{_JOBS_URL}/{job}", headers=auth_headers(owner))

    assert resp.status_code == 404, resp.text
    assert survived.status_code == 200


async def test_deleting_twice_is_404_the_second_time(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    uid = await _seed(db_sessionmaker)
    job = await _completed_image_job(client, fal, uid, assets=[_ASSET_A])

    first = await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))
    second = await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))

    assert first.status_code == 200
    assert second.status_code == 404


async def test_deleting_does_not_refund_or_charge(
    client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """The job succeeded; removing it from the feed is not a reason to give the credits back."""
    uid = await _seed(db_sessionmaker, balance=500)
    job = await _completed_image_job(client, fal, uid, assets=[_ASSET_A])
    async with db_sessionmaker() as session:
        before = await session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )

    await client.delete(f"{_JOBS_URL}/{job}", headers=auth_headers(uid))

    async with db_sessionmaker() as session:
        after = await session.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": str(uid)}
        )
    assert after == before


async def test_delete_requires_a_bearer_token(client: AsyncClient) -> None:
    resp = await client.delete(f"{_JOBS_URL}/{uuid.uuid4()}")

    assert resp.status_code == 401, resp.text
