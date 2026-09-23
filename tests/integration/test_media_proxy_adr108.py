"""Integration: ADR-108 — generation through the proxy service, completion by webhook.

Norm — ``docs/adr/ADR-108-media-generation-via-proxy.md`` and
``docs/modules/media-generation/09-testing.md`` §«Integration — транспорт через прокси (ADR-108)».

* The proxy is faked at the ``httpx`` boundary (``app.media_generation.proxy_client.httpx``), fal at
  its own (``app.media_generation.fal_client.httpx``), exactly as in the rest of the module; the
  callback is a REAL HTTP request to ``POST /v1/media/webhooks/proxy/{jobId}`` with a token
  computed from the job id. Real JWT, real wallet, real ``media_jobs`` on the shared Postgres.
* The proxy fake models ONLY what ADR-108 fixes normatively: ``POST {PROXY_BASE}/api/v1/tasks``
  with ``{service, endpoint, method, payload, callbackUrl}`` and ``Authorization: Bearer``; the
  answer carries an id in one of ``request_id``/``requestId``/``id``/``uid``/``taskId``. The
  shape of the callback body follows §4.3 (a status token and/or result URLs; fal's form under
  ``payload``/``data``/``result`` or at the top level). ASSUMPTIONS, no bets placed on them
  (Q-108-1…4 open): the proxy's redelivery policy, its exact callback body per vendor, a status API
  — no test depends on any of these.
* Age of a job is its ``created_at`` (fixed clock of the row), never a wait.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.media_generation.webhook import sign_webhook_token
from tests.conftest import auth_headers, seed_user
from tests.integration.test_media_deadline_adr105 import (
    _BlockingModerations,
    _BrokenModerations,
    _moderation,
)
from tests.integration.test_media_generation_adr060 import (
    _QUEUE_BASE,
    _build_client,
    _FakeResponse,
    _Fal,
    _make_fake_httpx,
)

_PROXY_KEY = "px-instance-key-9f3a"  # noqa: S105 - test-only static secret
_WEBHOOK_SECRET = "wh-secret-7c21"  # noqa: S105 - test-only static secret
_FAL_KEY = "fal-test-key-adr108"  # noqa: S105 - test-only static secret
_PREVIEW_SECRET = "preview-secret-adr108"  # noqa: S105 - test-only static secret
_DOMAIN = "gen.example.test"
_PROXY_BASE = "https://proxy.example.test"
_TASKS_URL = f"{_PROXY_BASE}/api/v1/tasks"
_VENDOR_HOSTS = ".sosana.test,.kie.test"
_IMAGES_URL = "/v1/media/images"
_VIDEOS_URL = "/v1/media/videos"
_JOBS_URL = "/v1/media/jobs"
_START_BALANCE = 100
_CREDITS = 4
_DEADLINE = 21600
_OVERDUE = _DEADLINE + 3600
_YOUNG = 60
_FAL_ASSET = "https://v3.fal.media/files/out.png"
_OUTCOME_EVENT = "media_webhook_outcome"
_DEADLINE_EVENT = "media_generation_deadline_exceeded"


# ================================ the proxy fake ================================


class _Proxy:
    """Records every ``POST /api/v1/tasks`` and answers from a script (default: accepted)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[Any] = []

    def answer(self, *items: Any) -> None:
        self.script = list(items)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        item = self.script.pop(0) if self.script else _FakeResponse(200, {"request_id": "px-1"})
        if isinstance(item, BaseException):
            raise item
        return item

    @property
    def services(self) -> list[str]:
        return [call["json"]["service"] for call in self.calls]


def _ok(**body: Any) -> _FakeResponse:
    return _FakeResponse(200, body or {"request_id": "px-1"})


def _err(status: int, body: Any = None) -> _FakeResponse:
    return _FakeResponse(status, body if body is not None else {"error": True, "message": "x"})


# ================================ fixtures & helpers ================================


@pytest.fixture(autouse=True)
def _enable_media_loggers() -> None:
    """Alembic's ``fileConfig`` may have disabled the ``app.*`` loggers (conftest ``_migrated``)."""
    for name in (
        "app.media_generation.service",
        "app.media_generation.webhook",
        "app.media_generation.proxy_client",
        "app.media_generation.reconciler",
        "app.media_generation.fal_client",
    ):
        logging.getLogger(name).disabled = False


@pytest.fixture
def fal() -> _Fal:
    return _Fal()


@pytest.fixture
def proxy() -> _Proxy:
    return _Proxy()


def _set_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    proxy_key: str = _PROXY_KEY,
    domain: str = _DOMAIN,
    secret: str = _WEBHOOK_SECRET,
    result_hosts: str = "",
    vendor_prices: str = "{}",
) -> None:
    monkeypatch.setenv("PROXY_API_KEY", proxy_key)
    monkeypatch.setenv("PROXY_BASE", _PROXY_BASE)
    monkeypatch.setenv("PROXY_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("SERVICE_DOMAIN", domain)
    monkeypatch.setenv("PREVIEW_URL_SECRET", _PREVIEW_SECRET)
    monkeypatch.setenv("MEDIA_RESULT_HOST_SUFFIXES", result_hosts)
    monkeypatch.setenv("MEDIA_VENDOR_PRICES", vendor_prices)
    monkeypatch.delenv("MEDIA_JOB_DEADLINE_SECONDS", raising=False)
    get_settings.cache_clear()


def _patch_proxy(monkeypatch: pytest.MonkeyPatch, proxy: _Proxy) -> None:
    from app.media_generation import proxy_client as proxy_client_mod

    monkeypatch.setattr(proxy_client_mod, "httpx", _make_fake_httpx(proxy))  # type: ignore[arg-type]


async def _open(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
    *,
    fal_key: str = _FAL_KEY,
    **env: Any,
) -> AsyncClient:
    _set_env(monkeypatch, **env)
    client = _build_client(monkeypatch, maker, fal, fal_key=fal_key)
    _patch_proxy(monkeypatch, proxy)
    return client


@pytest.fixture
async def media(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> AsyncIterator[AsyncClient]:
    """Instance switched to the proxy: PROXY_API_KEY + SERVICE_DOMAIN + FAL_API_KEY kept."""
    async with await _open(monkeypatch, db_sessionmaker, fal, proxy) as ac:
        yield ac
    get_settings.cache_clear()


@pytest.fixture
async def vendor_media(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> AsyncIterator[AsyncClient]:
    """As ``media`` plus MEDIA_RESULT_HOST_SUFFIXES — sosana/kie routes switched on (§2.1 п.1)."""
    async with await _open(
        monkeypatch, db_sessionmaker, fal, proxy, result_hosts=_VENDOR_HOSTS
    ) as ac:
        yield ac
    get_settings.cache_clear()


@pytest.fixture
def pushes(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Record every ``notify_media_ready`` call (the push still runs through the real service)."""
    from app.notifications.push_service import MediaPushService

    seen: list[uuid.UUID] = []
    original = MediaPushService.notify_media_ready

    async def _spy(self: MediaPushService, **kwargs: Any) -> None:
        seen.append(kwargs["job_id"])
        await original(self, **kwargs)

    monkeypatch.setattr(MediaPushService, "notify_media_ready", _spy)
    return seen


class _Moderation:
    """Switchable post-moderation for the webhook tests: ``None`` = passes, else a fake."""

    def __init__(self) -> None:
        self.mode = "pass"

    def service(self) -> Any:
        if self.mode == "broken":
            return _moderation(_BrokenModerations())
        if self.mode == "block":
            return _moderation(_BlockingModerations())
        return _moderation(_PassingModerations())


class _PassingModerations:
    async def create(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(results=[SimpleNamespace(flagged=False, categories={})])


@pytest.fixture
def moderation(monkeypatch: pytest.MonkeyPatch) -> _Moderation:
    from app import deps

    holder = _Moderation()
    monkeypatch.setattr(deps, "get_moderation_service", holder.service)
    return holder


async def _user(
    maker: async_sessionmaker[AsyncSession], balance: int = _START_BALANCE
) -> uuid.UUID:
    async with maker() as s:
        return await seed_user(s, balance=balance)


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(uid)})
        )


async def _count(maker: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> int:
    async with maker() as s:
        return int(await s.scalar(text(sql), params) or 0)


async def _jobs(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    return await _count(maker, "SELECT count(*) FROM media_jobs WHERE user_id = :u", u=str(uid))


async def _gen_rows(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    return await _count(
        maker,
        "SELECT count(*) FROM ledger_transactions WHERE user_id = :u "
        "AND idempotency_key LIKE 'media-gen:%'",
        u=str(uid),
    )


async def _refunds(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID | str) -> int:
    return await _count(
        maker,
        "SELECT count(*) FROM ledger_transactions WHERE idempotency_key = :k",
        k=f"media-refund:{job_id}",
    )


async def _row(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID | str) -> dict[str, Any]:
    async with maker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT status, error, credits_refunded, result, pending_result, vendor_price, "
                    "provider, status_url, response_url, fal_request_id, push_sent_at, "
                    "credits_charged FROM media_jobs WHERE id = :id"
                ),
                {"id": str(job_id)},
            )
        ).one()
    keys = (
        "status",
        "error",
        "refunded",
        "result",
        "pending_result",
        "vendor_price",
        "provider",
        "status_url",
        "response_url",
        "fal_request_id",
        "push_sent_at",
        "credits_charged",
    )
    return dict(zip(keys, row, strict=True))


async def _seed_job(
    maker: async_sessionmaker[AsyncSession],
    uid: uuid.UUID,
    *,
    age_seconds: int = _YOUNG,
    provider: str = "fal",
    status: str = "running",
    kind: str = "image",
    pending_result: dict[str, Any] | None = None,
    operation: str = "generation",
    operation_input: dict[str, Any] | None = None,
) -> uuid.UUID:
    """A non-terminal job as a precondition (a proxy job when ``provider`` is non-empty)."""
    job_id = uuid.uuid4()
    legacy = provider == ""
    base = f"{_QUEUE_BASE}/fal-ai/nano-banana-2/requests/r-{job_id.hex[:8]}"
    async with maker() as s:
        await s.execute(
            text(
                """
                INSERT INTO media_jobs (
                    id, user_id, model_id, kind, fal_endpoint, fal_request_id, status_url,
                    response_url, status, prompt, credits_charged, provider, pending_result,
                    operation, operation_input, created_at, updated_at
                ) VALUES (
                    :id, :u, 'nano-banana-2', :kind, 'fal-ai/nano-banana-2', :rid, :su, :ru, :st,
                    'a cat', :cr, :prov, CAST(:pr AS JSONB), :op, CAST(:oi AS JSONB),
                    now() - make_interval(secs => :age), now() - make_interval(secs => :age)
                )
                """
            ),
            {
                "id": job_id,
                "u": uid,
                "kind": kind,
                "rid": f"r-{job_id.hex[:8]}",
                "su": f"{base}/status" if legacy else "",
                "ru": base if legacy else "",
                "st": status,
                "cr": _CREDITS,
                "prov": provider,
                "pr": None if pending_result is None else json.dumps(pending_result),
                "op": operation,
                "oi": None if operation_input is None else json.dumps(operation_input),
                "age": age_seconds,
            },
        )
        await s.commit()
    return job_id


def _token(job_id: uuid.UUID | str) -> str:
    return sign_webhook_token(settings=get_settings(), job_id=uuid.UUID(str(job_id)))


async def _callback(
    client: AsyncClient,
    job_id: uuid.UUID | str,
    body: Any = None,
    *,
    token: str | None = "auto",
    raw: bytes | None = None,
) -> _httpx.Response:
    if token == "auto":
        token = _token(job_id)
    url = f"/v1/media/webhooks/proxy/{job_id}"
    if token is not None:
        url += f"?token={token}"
    if raw is not None:
        return await client.post(url, content=raw, headers={"content-type": "application/json"})
    return await client.post(url, json=body)


def _fal_completed(url: str = _FAL_ASSET, **extra: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "payload": {
            "images": [{"url": url, "content_type": "image/png", "file_name": "out.png"}],
            "seed": 42,
        },
        **extra,
    }


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [
        dict(getattr(record, "extra_fields", {}))
        for record in caplog.records
        if record.getMessage() == name
    ]


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [e["outcome"] for e in _events(caplog, _OUTCOME_EVENT)]


async def _post_image(client: AsyncClient, uid: uuid.UUID, **params: Any) -> _httpx.Response:
    body = {"model": "nano-banana-2", "prompt": "a cat", **params}
    return await client.post(_IMAGES_URL, json=body, headers=auth_headers(uid))


# ============================ §1 — transport choice and the gate ============================


async def test_proxy_submit_sends_the_proxy_contract_and_never_calls_fal(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(_ok(request_id="px-abc"))

    resp = await _post_image(media, uid, resolution="2K")

    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
    assert fal.calls == [], "the direct fal submit must not run on a proxy instance"
    assert len(proxy.calls) == 1
    call = proxy.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == _TASKS_URL
    assert call["headers"]["Authorization"] == f"Bearer {_PROXY_KEY}"
    assert set(call["json"]) == {"service", "endpoint", "method", "payload", "callbackUrl"}
    assert call["json"]["service"] == "fal"
    assert call["json"]["method"] == "POST"
    assert call["json"]["endpoint"] == "https://queue.fal.run/fal-ai/nano-banana-2"
    assert call["json"]["callbackUrl"] == (
        f"https://{_DOMAIN}/v1/media/webhooks/proxy/{job_id}?token={_token(job_id)}"
    )
    # No vendor key and no instance key in the body.
    assert _PROXY_KEY not in json.dumps(call["json"])
    assert _FAL_KEY not in json.dumps(call["json"])
    row = await _row(db_sessionmaker, job_id)
    assert row["provider"] == "fal"
    assert row["status_url"] == ""
    assert row["response_url"] == ""
    assert row["fal_request_id"] == "px-abc"
    assert row["status"] == "queued"


async def test_without_proxy_key_the_direct_fal_branch_runs_as_before(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    from tests.integration.test_media_generation_adr060 import _submit_body

    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, proxy_key="") as client:
        uid = await _user(db_sessionmaker)
        fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
        resp = await _post_image(client, uid)
    get_settings.cache_clear()

    assert resp.status_code == 202, resp.text
    assert proxy.calls == []
    assert fal.submit_url == f"{_QUEUE_BASE}/fal-ai/nano-banana-2"
    row = await _row(db_sessionmaker, resp.json()["jobId"])
    assert row["provider"] == ""
    assert row["status_url"].endswith("/status")


async def test_proxy_key_without_service_domain_falls_back_to_direct_fal(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    """The domain is part of ``proxy_configured``: without it no callback can be built."""
    from tests.integration.test_media_generation_adr060 import _submit_body

    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, domain="") as client:
        uid = await _user(db_sessionmaker)
        fal.on_submit(200, _submit_body("fal-ai/nano-banana-2"))
        resp = await _post_image(client, uid)
    get_settings.cache_clear()

    assert resp.status_code == 202, resp.text
    assert proxy.calls == []
    assert (await _row(db_sessionmaker, resp.json()["jobId"]))["provider"] == ""


async def test_proxy_key_without_domain_and_fal_key_is_503_before_any_debit(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    async with await _open(
        monkeypatch, db_sessionmaker, fal, proxy, domain="", fal_key=""
    ) as client:
        uid = await _user(db_sessionmaker)
        resp = await _post_image(client, uid)
    get_settings.cache_clear()

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"
    assert proxy.calls == [] and fal.calls == []
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert await _jobs(db_sessionmaker, uid) == 0


async def test_gate_and_models_catalog_follow_proxy_or_fal(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    """(a) proxy only → /v1/media/* open and media rows in GET /v1/models; (b) neither → 503 and
    no media rows."""
    uid = await _user(db_sessionmaker)
    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, fal_key="") as client:
        media_models = await client.get("/v1/media/models", headers=auth_headers(uid))
        catalog = await client.get("/v1/models", headers=auth_headers(uid))
    assert media_models.status_code == 200, media_models.text
    assert catalog.status_code == 200, catalog.text
    photo_video = [m for m in catalog.json()["models"] if m["modality"] in ("photo", "video")]
    assert photo_video, "proxy alone must expose the media rows"

    async with await _open(
        monkeypatch, db_sessionmaker, fal, proxy, proxy_key="", fal_key=""
    ) as client:
        media_models = await client.get("/v1/media/models", headers=auth_headers(uid))
        catalog = await client.get("/v1/models", headers=auth_headers(uid))
    get_settings.cache_clear()
    assert media_models.status_code == 503
    assert media_models.json()["error"]["code"] == "media_generation_not_configured"
    assert [m for m in catalog.json()["models"] if m["modality"] in ("photo", "video")] == []


async def test_upload_needs_the_fal_key_even_behind_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    import base64

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, fal_key="") as client:
        uid = await _user(db_sessionmaker)
        resp = await client.post(
            "/v1/media/uploads",
            json={"type": "image", "filename": "a.png", "mediaType": "image/png", "data": png},
            headers=auth_headers(uid),
        )
    get_settings.cache_clear()
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"
    assert fal.calls == [] and proxy.calls == []
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE


# ============================ §2 — payload and price do not depend on the transport ============


_PAYLOAD_CASES = [
    ("images", "nano-banana-2", {"resolution": "2K", "aspectRatio": "16:9", "seed": 5}),
    ("images", "nano-banana-2", {"imageUrls": ["https://example.com/a.png"]}),
    ("images", "nano-banana-pro", {"numImages": 2, "outputFormat": "png"}),
    ("images", "nano-banana-pro", {"imageUrls": ["https://example.com/a.png"], "resolution": "4K"}),
    ("videos", "kling-video", {"duration": "10"}),
    ("videos", "kling-video", {"imageUrl": "https://example.com/a.png"}),
    ("videos", "kling-video-v3", {"generateAudio": True, "duration": "7"}),
    ("videos", "kling-video-v3", {"imageUrl": "https://example.com/a.png"}),
    ("videos", "veo-3.1", {"resolution": "4k", "generateAudio": True}),
    ("videos", "veo-3.1", {"imageUrl": "https://example.com/a.png", "aspectRatio": "auto"}),
]


@pytest.mark.parametrize(("route", "model_id", "params"), _PAYLOAD_CASES)
async def test_fal_route_payload_and_price_equal_the_direct_submit(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
    route: str,
    model_id: str,
    params: dict[str, Any],
) -> None:
    """Diff test: the SAME request once through direct fal, once through the proxy — the fal-route
    payload is byte-for-byte the direct one, and ``creditsCharged`` is the same."""
    from tests.integration.test_media_generation_adr060 import _submit_body

    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, proxy_key="") as client:
        uid = await _user(db_sessionmaker, balance=10_000)
        fal.on_submit(200, _submit_body("x"))
        body = {"model": model_id, "prompt": "a cat", **params}
        direct = await client.post(f"/v1/media/{route}", json=body, headers=auth_headers(uid))
        assert direct.status_code == 202, direct.text
        direct_payload = fal.submit_payload
        direct_url = fal.submit_url

        _set_env(monkeypatch)
        via = await client.post(f"/v1/media/{route}", json=body, headers=auth_headers(uid))
    get_settings.cache_clear()

    assert via.status_code == 202, via.text
    assert len(proxy.calls) == 1
    sent = proxy.calls[0]["json"]
    assert json.dumps(sent["payload"], sort_keys=True) == json.dumps(direct_payload, sort_keys=True)
    assert sent["endpoint"] == direct_url
    assert via.json()["creditsCharged"] == direct.json()["creditsCharged"]
    async with db_sessionmaker() as s:
        amounts = (
            (
                await s.execute(
                    text(
                        "SELECT amount FROM ledger_transactions WHERE idempotency_key IN (:a, :b) "
                        "ORDER BY created_at"
                    ),
                    {
                        "a": f"media-gen:{direct.json()['jobId']}",
                        "b": f"media-gen:{via.json()['jobId']}",
                    },
                )
            )
            .scalars()
            .all()
        )
    assert len(amounts) == 2 and amounts[0] == amounts[1]


@pytest.mark.parametrize("model_id", ["nano-banana-2", "nano-banana-pro"])
@pytest.mark.parametrize("resolution", ["1K", "2K", "4K"])
@pytest.mark.parametrize("fal_first", [False, True])
async def test_credits_do_not_depend_on_the_chosen_route(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
    model_id: str,
    resolution: str,
    fal_first: bool,
) -> None:
    """The price is computed before the route is chosen (§3.1): sosana-first and fal-first (by
    ``MEDIA_VENDOR_PRICES``) runs of the same request cost the same as the direct one."""
    from tests.integration.test_media_generation_adr060 import _submit_body

    prices = json.dumps({f"{model_id}:{resolution}:fal": 0.0001}) if fal_first else "{}"
    body = {"model": model_id, "prompt": "a cat", "resolution": resolution, "aspectRatio": "1:1"}
    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, proxy_key="") as client:
        uid = await _user(db_sessionmaker, balance=10_000)
        fal.on_submit(200, _submit_body("x"))
        direct = await client.post(_IMAGES_URL, json=body, headers=auth_headers(uid))
        _set_env(monkeypatch, result_hosts=_VENDOR_HOSTS, vendor_prices=prices)
        via = await client.post(_IMAGES_URL, json=body, headers=auth_headers(uid))
    get_settings.cache_clear()

    assert via.status_code == 202, via.text
    assert proxy.services == ["fal" if fal_first else "sosana"]
    assert via.json()["creditsCharged"] == direct.json()["creditsCharged"]
    assert (await _row(db_sessionmaker, via.json()["jobId"]))["provider"] == proxy.services[0]


# ======================== §2.1 — routing, both sides of the predicate ========================


async def test_eligible_run_goes_to_sosana_first(
    vendor_media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    uid = await _user(db_sessionmaker)
    resp = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    assert resp.status_code == 202, resp.text
    assert proxy.services == ["sosana"]
    assert proxy.calls[0]["json"]["endpoint"] == "https://api.sosana.art/api/image/create-async"


@pytest.mark.parametrize(
    ("case", "hosts", "params"),
    [
        ("MEDIA_RESULT_HOST_SUFFIXES empty", "", {"resolution": "2K", "aspectRatio": "16:9"}),
        ("numImages=2", _VENDOR_HOSTS, {"resolution": "2K", "aspectRatio": "16:9", "numImages": 2}),
        ("0.5K", _VENDOR_HOSTS, {"resolution": "0.5K", "aspectRatio": "16:9"}),
        ("seed", _VENDOR_HOSTS, {"resolution": "2K", "aspectRatio": "16:9", "seed": 1}),
        (
            "outputFormat=webp",
            _VENDOR_HOSTS,
            {"resolution": "2K", "aspectRatio": "16:9", "outputFormat": "webp"},
        ),
        ("aspectRatio=8:1", _VENDOR_HOSTS, {"resolution": "2K", "aspectRatio": "8:1"}),
        ("aspectRatio omitted", _VENDOR_HOSTS, {"resolution": "2K"}),
    ],
)
async def test_any_breach_of_the_route_condition_leaves_only_fal(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
    case: str,
    hosts: str,
    params: dict[str, Any],
) -> None:
    """(b) the single route is fal: a fal 5xx exhausts the list after ONE call."""
    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, result_hosts=hosts) as client:
        uid = await _user(db_sessionmaker)
        proxy.answer(_err(500))
        resp = await _post_image(client, uid, **params)
    get_settings.cache_clear()
    assert resp.status_code == 502, (case, resp.text)
    assert proxy.services == ["fal"], case


async def test_png_output_skips_sosana_and_goes_to_kie(
    vendor_media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    uid = await _user(db_sessionmaker)
    resp = await _post_image(
        vendor_media, uid, resolution="2K", aspectRatio="16:9", outputFormat="png"
    )
    assert resp.status_code == 202, resp.text
    assert proxy.services == ["kie"]


@pytest.mark.parametrize("model_id", ["kling-video", "kling-video-v3", "veo-3.1"])
async def test_video_models_have_only_the_fal_route(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    model_id: str,
) -> None:
    uid = await _user(db_sessionmaker, balance=10_000)
    proxy.answer(_err(503))
    resp = await vendor_media.post(
        _VIDEOS_URL, json={"model": model_id, "prompt": "a city"}, headers=auth_headers(uid)
    )
    assert resp.status_code == 502, resp.text
    assert proxy.services == ["fal"]


async def test_fal_first_by_override_and_its_422_stops_the_loop(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    async with await _open(
        monkeypatch,
        db_sessionmaker,
        fal,
        proxy,
        result_hosts=_VENDOR_HOSTS,
        vendor_prices=json.dumps({"nano-banana-2:2K:fal": 0.0001}),
    ) as client:
        uid = await _user(db_sessionmaker)
        proxy.answer(_FakeResponse(422, {"detail": "prompt: too long"}))
        resp = await _post_image(client, uid, resolution="2K", aspectRatio="16:9")
    get_settings.cache_clear()
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    assert "prompt: too long" in resp.json()["error"]["message"]
    assert proxy.services == ["fal"], "sosana/kie are never tried after fal's validation verdict"
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE


# ============================ §3.3 — submit errors, row by row ============================


_RETRYABLE = [
    ("5xx", _err(502)),
    ("402", _err(402)),
    ("400 without validation marker", _err(400, {"error": True, "message": "vendor exploded"})),
    ("malformed JSON", _FakeResponse(200, None, json_raises=True)),
    ("2xx with error:true", _FakeResponse(200, {"error": True})),
    ("429", _err(429)),
]


@pytest.mark.parametrize(("case", "first"), _RETRYABLE, ids=[c for c, _ in _RETRYABLE])
async def test_retryable_answer_falls_through_to_the_next_route(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    case: str,
    first: _FakeResponse,
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(first, _ok(taskId="kie-7"))
    resp = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    assert resp.status_code == 202, (case, resp.text)
    assert proxy.services == ["sosana", "kie"], case
    row = await _row(db_sessionmaker, resp.json()["jobId"])
    assert row["provider"] == "kie"
    assert row["fal_request_id"] == "kie-7"


@pytest.mark.parametrize(
    ("case", "exc"),
    [
        ("timeout", _httpx.ReadTimeout("slow")),
        ("connect", _httpx.ConnectError("refused")),
    ],
)
async def test_proxy_timeout_or_connect_is_502_without_a_second_call(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    case: str,
    exc: BaseException,
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(exc, _ok())
    resp = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    assert resp.status_code == 502, (case, resp.text)
    assert resp.json()["error"]["code"] == "upstream_error"
    assert proxy.services == ["sosana"], "no fallback: the proxy may already have the task"
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert await _jobs(db_sessionmaker, uid) == 0
    assert await _gen_rows(db_sessionmaker, uid) == 0


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_instance_key_is_503_without_fallback(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    status: int,
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(_err(status), _ok())
    resp = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "media_generation_not_configured"
    assert proxy.services == ["sosana"]
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert await _jobs(db_sessionmaker, uid) == 0


@pytest.mark.parametrize(
    ("case", "first"),
    [
        ("422", _FakeResponse(422, {"detail": "aspect_ratio unsupported"})),
        ("400 + validation", _err(400, {"error": True, "message": "Validation failed: size"})),
    ],
)
async def test_vendor_validation_refusal_falls_through_to_fal(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    case: str,
    first: _FakeResponse,
) -> None:
    """Only the fal route gives the final validity verdict (§3.3 — difference from the sample)."""
    uid = await _user(db_sessionmaker)
    proxy.answer(first, first, _ok())
    resp = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    assert resp.status_code == 202, (case, resp.text)
    assert proxy.services == ["sosana", "kie", "fal"]
    assert (await _row(db_sessionmaker, resp.json()["jobId"]))["provider"] == "fal"


async def test_fal_route_422_is_422_with_the_text(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(_FakeResponse(422, {"detail": [{"loc": ["body", "prompt"], "msg": "too long"}]}))
    resp = await _post_image(media, uid)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    assert "body.prompt: too long" in resp.json()["error"]["message"]
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert await _jobs(db_sessionmaker, uid) == 0
    assert await _gen_rows(db_sessionmaker, uid) == 0


@pytest.mark.parametrize(
    ("case", "script", "status", "code"),
    [
        ("all 429", [_err(429), _err(429), _err(429)], 429, "rate_limited"),
        ("all 5xx", [_err(500), _err(502), _err(503)], 502, "upstream_error"),
        ("429 then 5xx", [_err(429), _err(429), _err(500)], 502, "upstream_error"),
    ],
)
async def test_exhausted_routes_answer_the_last_retryable_error(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    case: str,
    script: list[_FakeResponse],
    status: int,
    code: str,
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(*script)
    resp = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    assert resp.status_code == status, (case, resp.text)
    assert resp.json()["error"]["code"] == code
    assert proxy.services == ["sosana", "kie", "fal"]
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert await _jobs(db_sessionmaker, uid) == 0
    assert await _gen_rows(db_sessionmaker, uid) == 0


async def test_no_proxy_task_id_is_stored_as_an_empty_string(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    uid = await _user(db_sessionmaker)
    proxy.answer(_ok(status="accepted"))
    resp = await _post_image(media, uid)
    assert resp.status_code == 202, resp.text
    assert (await _row(db_sessionmaker, resp.json()["jobId"]))["fal_request_id"] == ""


# ============================ §4.2 — webhook: order of checks ============================


@pytest.fixture
def sql_statements(_engine: Any) -> Iterator[list[str]]:
    """Every SQL statement the shared engine executes while the test runs."""
    seen: list[str] = []

    def _on_execute(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    event.listen(_engine.sync_engine, "before_cursor_execute", _on_execute)
    yield seen
    event.remove(_engine.sync_engine, "before_cursor_execute", _on_execute)


@pytest.mark.parametrize(
    ("case", "token"),
    [
        ("no token", None),
        ("wrong token", "0" * 64),
        ("token of another job", "other"),
        ("too long", "long"),
        ("empty token", ""),
    ],
)
async def test_bad_token_is_401_without_any_sql(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sql_statements: list[str],
    case: str,
    token: str | None,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    if token == "other":
        token = _token(uuid.uuid4())
    elif token == "long":
        token = _token(job_id) + "a" * 100
    before = await _row(db_sessionmaker, job_id)
    sql_statements.clear()

    resp = await _callback(media, job_id, _fal_completed(), token=token)

    assert resp.status_code == 401, (case, resp.text)
    assert resp.json()["error"]["code"] == "unauthorized"
    assert sql_statements == [], f"{case}: SQL ran before the token check: {sql_statements}"
    assert await _row(db_sessionmaker, job_id) == before


async def test_valid_token_request_does_touch_the_db(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sql_statements: list[str],
) -> None:
    """Positive control of the SQL counter: the same listener sees the lookup of an authorized
    callback, so the empty list above is a measurement, not a blind instrument."""
    missing = uuid.uuid4()
    sql_statements.clear()
    resp = await _callback(media, missing, {"status": "processing"})
    assert resp.status_code == 404, resp.text
    assert any("media_jobs" in s for s in sql_statements)


async def test_empty_secret_rejects_even_the_hmac_of_an_empty_key(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    import hashlib
    import hmac

    async with await _open(
        monkeypatch, db_sessionmaker, fal, proxy, proxy_key="", secret=""
    ) as client:
        uid = await _user(db_sessionmaker)
        job_id = await _seed_job(db_sessionmaker, uid)
        forged = hmac.new(b"", str(job_id).encode(), hashlib.sha256).hexdigest()
        resp = await _callback(client, job_id, _fal_completed(), token=forged)
    get_settings.cache_clear()
    assert resp.status_code == 401, resp.text
    assert (await _row(db_sessionmaker, job_id))["status"] == "running"


async def test_non_uuid_path_is_401(media: AsyncClient) -> None:
    resp = await media.post("/v1/media/webhooks/proxy/not-a-uuid?token=abc", json={})
    assert resp.status_code == 401, resp.text


async def test_unknown_job_with_bad_token_is_401_not_404(media: AsyncClient) -> None:
    resp = await _callback(media, uuid.uuid4(), {"status": "completed"}, token="0" * 64)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    ("case", "raw"),
    [("array", b"[1, 2]"), ("not json", b"{nope"), ("empty", b""), ("string", b'"x"')],
)
async def test_non_object_body_is_422_after_auth_and_before_lookup(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    sql_statements: list[str],
    case: str,
    raw: bytes,
) -> None:
    """422 wins over 404: an unknown job id with a non-object body is 422, and no SQL runs."""
    sql_statements.clear()
    resp = await _callback(media, uuid.uuid4(), raw=raw)
    assert resp.status_code == 422, (case, resp.text)
    assert resp.json()["error"]["code"] == "validation_error"
    assert sql_statements == []


async def test_bad_token_wins_over_a_non_object_body(media: AsyncClient) -> None:
    resp = await _callback(media, uuid.uuid4(), raw=b"[1]", token="0" * 64)
    assert resp.status_code == 401


async def test_legacy_row_does_not_accept_callbacks(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, provider="")
    before = await _row(db_sessionmaker, job_id)
    resp = await _callback(media, job_id, _fal_completed())
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"
    assert await _row(db_sessionmaker, job_id) == before


async def test_refused_callbacks_emit_their_outcome_values(
    media: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """``bad_token`` / ``not_json`` / ``unknown_job`` each on its real path; bad_token and
    unknown_job are WARNING, not_json INFO (§10)."""
    caplog.set_level(logging.INFO, logger="app.media_generation.webhook")
    missing = uuid.uuid4()
    assert (await _callback(media, missing, {}, token="0" * 64)).status_code == 401
    assert (await _callback(media, missing, raw=b"[1]")).status_code == 422
    assert (await _callback(media, missing, {"status": "completed"})).status_code == 404
    records = [r for r in caplog.records if r.getMessage() == _OUTCOME_EVENT]
    assert [r.extra_fields["outcome"] for r in records] == [  # type: ignore[attr-defined]
        "bad_token",
        "not_json",
        "unknown_job",
    ]
    assert [r.levelno for r in records] == [logging.WARNING, logging.INFO, logging.WARNING]


async def test_webhook_route_is_not_in_the_openapi_schema(media: AsyncClient) -> None:
    resp = await media.get("/openapi.json")
    assert resp.status_code == 200
    assert not [path for path in resp.json()["paths"] if "webhooks" in path]


# ============================ §4.3 / §5 — applying the outcome ============================


async def test_end_to_end_submit_callback_get_completed_with_signed_url(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
    pushes: list[uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The main path of ADR-108: POST → fake proxy → callback with HMAC → GET → completed."""
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    submitted = await _post_image(media, uid, resolution="2K")
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["jobId"]
    callback_token = _token(job_id)
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM request_logs WHERE media_job_id = :j AND status = 'queued'",
            j=job_id,
        )
        == 1
    )

    resp = await _callback(media, job_id, _fal_completed(vendor_price=0.12))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["status"] == "completed"
    assert body["creditsRefunded"] is False
    asset = body["assets"][0]
    assert asset["url"].startswith(f"https://{_DOMAIN}/v1/media/jobs/{job_id}/assets/0/")
    assert asset["contentType"] == "image/png"
    assert asset["fileName"] == "out.png"
    assert "vendor" not in got.text.lower()
    row = await _row(db_sessionmaker, job_id)
    assert str(row["vendor_price"]) == "0.120000"
    assert row["pending_result"] is None
    assert row["result"]["seed"] == 42
    assert pushes == [uuid.UUID(job_id)]
    assert (
        await _count(
            db_sessionmaker,
            "SELECT count(*) FROM request_logs WHERE media_job_id = :j AND status = 'completed' "
            "AND completed_at IS NOT NULL",
            j=job_id,
        )
        == 1
    )
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE - row["credits_charged"]
    assert _outcomes(caplog) == ["completed"]
    # Never logged: the instance key, the signing secret, the callback token.
    # The test's OWN httpx client logs the URL it calls (that is the proxy's side, not ours).
    app_records = [
        r for r in caplog.records if not (r.name == "httpx" and "http://test/" in r.getMessage())
    ]
    logged = " | ".join(r.getMessage() for r in app_records) + json.dumps(
        [getattr(r, "extra_fields", {}) for r in app_records]
    )
    for secret in (_PROXY_KEY, _WEBHOOK_SECRET, callback_token):
        assert secret not in logged


async def test_vendor_route_result_on_a_result_host_is_served_as_a_signed_url(
    vendor_media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,
) -> None:
    uid = await _user(db_sessionmaker)
    submitted = await _post_image(vendor_media, uid, resolution="2K", aspectRatio="16:9")
    job_id = submitted.json()["jobId"]
    assert proxy.services == ["sosana"]
    vendor_url = "https://cdn.sosana.test/out/x.png"

    resp = await _callback(
        vendor_media, job_id, {"status": "success", "data": {"resultUrls": [vendor_url]}}
    )
    assert resp.status_code == 200, resp.text
    got = await vendor_media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert got.json()["status"] == "completed"
    url = got.json()["assets"][0]["url"]
    assert url.startswith(f"https://{_DOMAIN}/v1/media/jobs/{job_id}/assets/0/")
    assert vendor_url not in got.text, "a vendor CDN URL is never handed out raw"


async def test_result_outside_the_allowlist_fails_and_refunds(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    resp = await _callback(media, job_id, _fal_completed(url="https://evil.example.com/out.png"))
    assert resp.status_code == 200, resp.text
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "failed"
    assert row["error"] == "generation produced no output"
    assert row["refunded"] is True
    assert row["pending_result"] is None
    assert await _refunds(db_sessionmaker, job_id) == 1
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS
    assert _outcomes(caplog) == ["no_usable_asset"]


async def test_result_host_outside_fal_list_is_dropped_without_result_hosts(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """(b) of §7: the vendor host is NOT allowed while MEDIA_RESULT_HOST_SUFFIXES is empty."""
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    await _callback(
        media, job_id, {"status": "success", "resultUrls": ["https://cdn.sosana.test/a.png"]}
    )
    assert (await _row(db_sessionmaker, job_id))["status"] == "failed"


async def test_failed_callback_fails_refunds_and_keeps_vendor_text(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    resp = await _callback(media, job_id, {"status": "failed", "error": "vendor said no"})
    assert resp.status_code == 200
    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert got.json()["status"] == "failed"
    assert got.json()["creditsRefunded"] is True
    assert got.json()["error"] == "vendor said no"
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS
    assert _outcomes(caplog) == ["failed"]


async def test_pending_callback_marks_running(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, status="queued")
    resp = await _callback(media, job_id, {"status": "processing"})
    assert resp.status_code == 200
    assert (await _row(db_sessionmaker, job_id))["status"] == "running"
    assert _outcomes(caplog) == ["pending"]


async def test_callback_to_a_terminal_job_changes_nothing(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    pushes: list[uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated delivery: the second ``completed`` and a late ``failed`` are no-ops."""
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    assert (await _callback(media, job_id, _fal_completed())).status_code == 200
    first = await _row(db_sessionmaker, job_id)
    assert first["status"] == "completed"

    again = await _callback(media, job_id, _fal_completed(url="https://v3.fal.media/files/2.png"))
    late = await _callback(media, job_id, {"status": "failed", "error": "late"})

    assert again.status_code == 200 and late.status_code == 200
    assert await _row(db_sessionmaker, job_id) == first
    assert await _refunds(db_sessionmaker, job_id) == 0
    assert pushes == [job_id]
    assert _outcomes(caplog) == ["completed", "duplicate_terminal", "duplicate_terminal"]


async def test_blocked_by_post_moderation_is_completion_failed_with_refund_and_no_push(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: _Moderation,
    pushes: list[uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    moderation.mode = "block"
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    resp = await _callback(media, job_id, _fal_completed())
    assert resp.status_code == 200
    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert got.json()["status"] == "failed"
    assert got.json()["assets"] == []
    assert got.json()["creditsRefunded"] is True
    assert pushes == []
    assert _outcomes(caplog) == ["completion_failed"]


async def test_handler_validation_failure_is_completion_failed(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The avatar-preparation handler rejects incomplete metadata with ``ValidationFailedError``:
    §5 ends in ``failed`` + refund, and the outcome names the FACT (``completion_failed``)."""
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, operation="avatar_prepare", operation_input={})
    resp = await _callback(media, job_id, _fal_completed())
    assert resp.status_code == 200
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "failed"
    assert row["refunded"] is True
    assert _outcomes(caplog) == ["completion_failed"]


async def test_moderation_unavailable_defers_keeps_the_result_and_next_get_completes(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: _Moderation,
    pushes: list[uuid.UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    moderation.mode = "broken"

    resp = await _callback(media, job_id, _fal_completed(vendor_price="0.5"))

    assert resp.status_code == 200, resp.text
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "running"
    assert row["result"] is None, "no partial write of the completion path survives"
    assert row["pending_result"]["assets"][0]["url"] == _FAL_ASSET
    assert str(row["vendor_price"]) == "0.500000"
    assert pushes == []
    assert _outcomes(caplog) == ["completion_deferred"]

    moderation.mode = "pass"
    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    assert got.json()["status"] == "completed"
    assert (await _row(db_sessionmaker, job_id))["pending_result"] is None
    assert pushes == [job_id]


async def test_transient_handler_fault_rolls_back_only_its_own_partial_writes(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handler that WROTE then raised: the savepoint undoes its write, ``pending_result``
    commits with the ``200`` (§5 — SAVEPOINT in the webhook)."""
    from app.media_generation.features_service import AvatarPreparationCompleter

    async def _write_then_raise(self: Any, job: Any, assets: Any) -> None:
        job.error = "partial write of the handler"
        job.result = {"assets": [{"url": "https://v3.fal.media/files/partial.png"}]}
        # Flushed, i.e. on the request transaction: only the savepoint can undo it.
        await self._repo._session.flush()  # noqa: SLF001
        raise RuntimeError("storage down")

    monkeypatch.setattr(AvatarPreparationCompleter, "complete", _write_then_raise)
    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)

    resp = await _callback(media, job_id, _fal_completed())

    assert resp.status_code == 200, resp.text
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "running"
    assert row["error"] is None
    assert row["result"] is None
    assert row["pending_result"] is not None
    assert _outcomes(caplog) == ["completion_deferred"]


@pytest.mark.parametrize("second", ["failed", "pending"])
async def test_step0_failed_or_pending_after_a_received_result_is_ignored(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: _Moderation,
    caplog: pytest.LogCaptureFixture,
    second: str,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    moderation.mode = "broken"
    await _callback(media, job_id, _fal_completed())
    stored = (await _row(db_sessionmaker, job_id))["pending_result"]
    assert stored is not None
    caplog.set_level(logging.INFO)
    caplog.clear()

    body = {"status": "failed", "error": "vendor"} if second == "failed" else {"status": "queued"}
    resp = await _callback(media, job_id, body)

    assert resp.status_code == 200
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "running"
    assert row["pending_result"] == stored
    assert row["refunded"] is False
    assert await _refunds(db_sessionmaker, job_id) == 0
    assert _outcomes(caplog) == ["result_already_received"]


async def test_step0_second_completed_never_overwrites_the_received_result(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: _Moderation,
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid)
    moderation.mode = "broken"
    await _callback(media, job_id, _fal_completed())
    moderation.mode = "pass"
    caplog.set_level(logging.INFO)
    caplog.clear()

    resp = await _callback(
        media, job_id, _fal_completed(url="https://v3.fal.media/files/other.png")
    )

    assert resp.status_code == 200
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "completed"
    assert row["result"]["assets"][0]["url"] == _FAL_ASSET
    assert _outcomes(caplog) == ["completed"]


async def test_webhook_still_applies_after_the_proxy_key_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    """Rollback of an instance to direct fal (§Порядок выката п.5): jobs in flight still finish —
    the webhook is outside the gate and the dedicated secret keeps signing."""
    async with await _open(monkeypatch, db_sessionmaker, fal, proxy, proxy_key="") as client:
        uid = await _user(db_sessionmaker)
        job_id = await _seed_job(db_sessionmaker, uid)
        resp = await _callback(client, job_id, _fal_completed())
    get_settings.cache_clear()
    assert resp.status_code == 200, resp.text
    assert (await _row(db_sessionmaker, job_id))["status"] == "completed"


# ============================ §6 — deadline of a proxy job ============================


async def test_overdue_proxy_job_without_callback_fails_with_webhook_pending(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)

    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert got.status_code == 200, got.text
    assert got.json()["status"] == "failed"
    assert got.json()["error"] == "generation did not complete in time"
    assert got.json()["creditsRefunded"] is True
    assert fal.calls == [] and proxy.calls == [], "a proxy job is never polled"
    assert [e["lastObservation"] for e in _events(caplog, _DEADLINE_EVENT)] == ["webhook_pending"]
    assert await _refunds(db_sessionmaker, job_id) == 1


async def test_young_proxy_job_is_marked_running_and_not_touched(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_DEADLINE - 60, status="queued")

    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert got.json()["status"] == "running"
    assert got.json()["creditsRefunded"] is False
    assert fal.calls == [] and proxy.calls == []
    assert _events(caplog, _DEADLINE_EVENT) == []
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE


async def test_overdue_job_with_a_pending_result_completes_instead_of_failing(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    uid = await _user(db_sessionmaker)
    pending = {"assets": [{"url": _FAL_ASSET, "contentType": "image/png", "fileName": "o.png"}]}
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE, pending_result=pending)

    got = await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))

    assert got.json()["status"] == "completed"
    assert got.json()["creditsRefunded"] is False
    assert _events(caplog, _DEADLINE_EVENT) == []


async def test_callback_after_the_deadline_close_is_a_duplicate_without_second_refund(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    await media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid))
    caplog.set_level(logging.INFO)
    caplog.clear()

    resp = await _callback(media, job_id, _fal_completed())

    assert resp.status_code == 200
    assert (await _row(db_sessionmaker, job_id))["status"] == "failed"
    assert await _refunds(db_sessionmaker, job_id) == 1
    assert _outcomes(caplog) == ["duplicate_terminal"]


async def _wait_for_lock_waiter(maker: async_sessionmaker[AsyncSession], task: Any) -> bool:
    """Until another backend waits on a lock (``pg_stat_activity``) — a condition, not a sleep."""
    for _ in range(200):
        waiting = await _count(
            maker,
            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND datname = current_database()",
        )
        if waiting:
            return True
        if task.done():
            return False
        await asyncio.sleep(0.05)
    return False


async def test_callback_racing_a_deadline_close_yields_one_terminal(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deadline close holds the row lock (uncommitted); the ``completed`` callback of the
    same job must wait for it and then see a terminal — one terminal, one refund."""
    from app import deps
    from app.request_logs.service import RequestLogWriter

    caplog.set_level(logging.INFO)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)

    async with db_sessionmaker() as session:
        service = deps.build_media_generation_service(session, RequestLogWriter(db_sessionmaker))
        closed = await service.get_job(user_id=uid, job_id=job_id)
        assert closed.job.status == "failed"
        callback = asyncio.create_task(_callback(media, job_id, _fal_completed()))
        assert await _wait_for_lock_waiter(db_sessionmaker, callback), "callback did not wait"
        await session.commit()
    resp = await callback

    assert resp.status_code == 200, resp.text
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "failed"
    assert row["result"] is None
    assert await _refunds(db_sessionmaker, job_id) == 1
    assert _outcomes(caplog) == ["duplicate_terminal"]


# ============================ §6 — reconciler ============================


@pytest.fixture
async def reconciler_env(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> AsyncIterator[None]:
    """The reconciler uses the global ``app.db`` sessionmaker — bind a fresh engine to this loop."""
    from app.db import dispose_engine
    from app.media_generation import fal_client as fal_client_mod

    _set_env(monkeypatch)
    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    get_settings.cache_clear()
    await dispose_engine()
    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))
    _patch_proxy(monkeypatch, proxy)
    yield
    await dispose_engine()
    get_settings.cache_clear()


async def test_reconciler_skips_a_locked_proxy_row_and_closes_it_next_tick(
    reconciler_env: None, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.media_generation.reconciler import reconcile_once

    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)

    async with db_sessionmaker() as holder:
        await holder.execute(
            text("SELECT id FROM media_jobs WHERE id = :id FOR UPDATE"), {"id": job_id}
        )
        # The tick must not wait for the held row (SKIP LOCKED).
        await asyncio.wait_for(reconcile_once(get_settings()), timeout=10)
        assert (await _row(db_sessionmaker, job_id))["status"] == "running"
        await holder.rollback()

    await reconcile_once(get_settings())
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "failed"
    assert await _refunds(db_sessionmaker, job_id) == 1


async def test_get_waits_for_a_locked_proxy_row(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Contrast with the reconciler: the client's GET takes plain FOR UPDATE and waits."""
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)

    async with db_sessionmaker() as holder:
        await holder.execute(
            text("SELECT id FROM media_jobs WHERE id = :id FOR UPDATE"), {"id": job_id}
        )
        get = asyncio.create_task(media.get(f"{_JOBS_URL}/{job_id}", headers=auth_headers(uid)))
        assert await _wait_for_lock_waiter(db_sessionmaker, get), "GET did not wait for the lock"
        assert not get.done()
        await holder.rollback()
    resp = await get
    assert resp.json()["status"] == "failed"


async def test_reconciler_advances_proxy_jobs_without_outgoing_calls_even_without_fal_key(
    reconciler_env: None,
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
    proxy: _Proxy,
) -> None:
    """Empty FAL_API_KEY: a young legacy job is NOT selected, a young proxy job with
    ``pending_result`` IS selected and completed — without a single outgoing call."""
    from app.media_generation.reconciler import reconcile_once

    monkeypatch.setenv("FAL_API_KEY", "")
    get_settings.cache_clear()
    uid = await _user(db_sessionmaker)
    pending = {"assets": [{"url": _FAL_ASSET, "contentType": None, "fileName": None}]}
    proxy_job = await _seed_job(db_sessionmaker, uid, pending_result=pending)
    legacy = await _seed_job(db_sessionmaker, uid, provider="", status="queued")

    await reconcile_once(get_settings())

    assert (await _row(db_sessionmaker, proxy_job))["status"] == "completed"
    assert (await _row(db_sessionmaker, legacy))["status"] == "queued"
    assert fal.calls == [] and proxy.calls == []


async def test_awaiting_callback_gauge_counts_by_its_predicate_and_is_exposed(
    reconciler_env: None,
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,
) -> None:
    """(a) proxy job without a callback for more than an hour is counted; (b) younger, with a
    ``pending_result``, legacy and terminal rows are not."""
    from app.media_generation.reconciler import reconcile_once
    from app.observability.metrics import media_proxy_jobs_awaiting_callback

    uid = await _user(db_sessionmaker)
    two_hours, half_hour = 7200, 1800
    pending = {"assets": [{"url": _FAL_ASSET, "contentType": None, "fileName": None}]}
    await _seed_job(db_sessionmaker, uid, age_seconds=two_hours)  # counted
    await _seed_job(db_sessionmaker, uid, age_seconds=two_hours, status="queued")  # counted
    await _seed_job(db_sessionmaker, uid, age_seconds=half_hour)
    await _seed_job(db_sessionmaker, uid, age_seconds=two_hours, pending_result=pending)
    await _seed_job(db_sessionmaker, uid, age_seconds=two_hours, provider="")
    await _seed_job(db_sessionmaker, uid, age_seconds=two_hours, status="completed")
    fal.on_status("IN_PROGRESS")
    media_proxy_jobs_awaiting_callback.set(-1)

    await reconcile_once(get_settings())

    assert media_proxy_jobs_awaiting_callback._value.get() == 2  # noqa: SLF001
    exposed = await media.get("/metrics")
    assert exposed.status_code == 200
    assert "media_proxy_jobs_awaiting_callback 2.0" in exposed.text


# ======================== §3.1 — debit rollback on a catching caller ========================


@pytest.fixture
async def chat_env(monkeypatch: pytest.MonkeyPatch, proxy: _Proxy) -> AsyncIterator[None]:
    _set_env(monkeypatch)
    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    get_settings.cache_clear()
    _patch_proxy(monkeypatch, proxy)
    yield
    get_settings.cache_clear()


def _tool_turn(fake_anthropic: Any, tag: str) -> None:
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "media.generate_image",
            {"model": "nano-banana-2", "prompt": "a cat", "resolution": "1K"},
            tool_id=f"toolu_adr108_{tag}",
        ),
        fake_anthropic.text_result("done"),
    ]


async def _chat(client: AsyncClient, uid: uuid.UUID) -> dict[str, Any]:
    resp = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "make a photo", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _chat_debit(maker: async_sessionmaker[AsyncSession], step_id: str) -> int:
    return await _count(
        maker,
        "SELECT COALESCE(sum(abs(amount)), 0) FROM ledger_transactions WHERE idempotency_key = :k",
        k=step_id,
    )


@pytest.mark.parametrize(
    ("case", "answer"),
    [
        ("transport 5xx (UpstreamError)", _err(502)),
        ("proxy timeout (ProxyTransportError)", _httpx.ReadTimeout("slow")),
        ("fal route 422 (ValidationFailedError)", _FakeResponse(422, {"detail": "bad"})),
        ("proxy 401 (MediaGenerationNotConfiguredError)", _err(401)),
    ],
)
async def test_chat_tool_submit_failure_rolls_the_media_debit_back(
    chat_env: None,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: Any,
    proxy: _Proxy,
    case: str,
    answer: Any,
) -> None:
    """(a) the tool-loop catches the submit error and the turn commits; the media debit must not."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=_START_BALANCE)
    proxy.answer(answer)
    _tool_turn(fake_anthropic, "fail")

    body = await _chat(client, uid)

    assert body.get("mediaJobs") in (None, []), case
    assert any(st["status"] == "errored" for st in body["serverTools"]), case
    assert await _gen_rows(db_sessionmaker, uid) == 0, case
    assert await _jobs(db_sessionmaker, uid) == 0, case
    chat_cost = await _chat_debit(db_sessionmaker, body["messageStepId"])
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE - chat_cost, case


async def test_chat_tool_successful_submit_commits_the_media_debit(
    chat_env: None,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: Any,
) -> None:
    """(b) the success side: the savepoint releases into the turn transaction."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=_START_BALANCE)
    _tool_turn(fake_anthropic, "ok")

    body = await _chat(client, uid)

    jobs = body["mediaJobs"]
    assert len(jobs) == 1
    charged = jobs[0]["creditsCharged"]
    assert await _gen_rows(db_sessionmaker, uid) == 1
    assert await _jobs(db_sessionmaker, uid) == 1
    chat_cost = await _chat_debit(db_sessionmaker, body["messageStepId"])
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE - chat_cost - charged


async def test_chat_tool_insufficient_credits_leaves_nothing(
    chat_env: None,
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: Any,
    proxy: _Proxy,
) -> None:
    """``409 insufficient_credits`` inside ``consume`` (TD-048 carrier): no row, no call."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=1)
    _tool_turn(fake_anthropic, "poor")

    body = await _chat(client, uid)

    assert body.get("mediaJobs") in (None, [])
    assert await _gen_rows(db_sessionmaker, uid) == 0
    assert await _jobs(db_sessionmaker, uid) == 0
    assert proxy.calls == []


async def _feature_submit(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, *, catch: type[BaseException]
) -> bool:
    """``submit_custom`` on a caller that CATCHES the error and commits its own transaction."""
    from app import deps
    from app.request_logs.service import RequestLogWriter

    async with maker() as session:
        service = deps.build_media_generation_service(session, RequestLogWriter(maker))
        try:
            await service.submit_custom(
                user_id=uid,
                kind="image",
                model_id="makeup",
                endpoint="fal-ai/image-apps-v2/makeup-application",
                payload={"image_url": "https://example.com/face.png"},
                prompt="",
                image_urls=[],
                credits=10,
                operation="makeup",
                operation_input=None,
                visible_in_history=True,
            )
            accepted = True
        except catch:
            accepted = False
        await session.commit()
    return accepted


async def test_submit_custom_failure_rolls_its_debit_back_for_a_catching_caller(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    from app.errors import UpstreamError

    uid = await _user(db_sessionmaker)
    proxy.answer(_err(502))
    assert await _feature_submit(db_sessionmaker, uid, catch=UpstreamError) is False
    assert await _gen_rows(db_sessionmaker, uid) == 0
    assert await _jobs(db_sessionmaker, uid) == 0
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert proxy.services == ["fal"]


async def test_submit_custom_success_commits_debit_and_row(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    from app.errors import UpstreamError

    uid = await _user(db_sessionmaker)
    assert await _feature_submit(db_sessionmaker, uid, catch=UpstreamError) is True
    assert await _gen_rows(db_sessionmaker, uid) == 1
    assert await _jobs(db_sessionmaker, uid) == 1
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE - 10
    assert proxy.calls[0]["json"]["endpoint"] == (
        "https://queue.fal.run/fal-ai/image-apps-v2/makeup-application"
    )


async def test_rest_submit_failure_is_502_and_charges_nothing(
    media: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], proxy: _Proxy
) -> None:
    """Control: REST rolls back the whole request anyway — green with or without the savepoint."""
    uid = await _user(db_sessionmaker)
    proxy.answer(_err(502))
    resp = await _post_image(media, uid)
    assert resp.status_code == 502
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert await _jobs(db_sessionmaker, uid) == 0
