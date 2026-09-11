"""Integration: оплаченная медиа-задача завершается за конечное время (ADR-105 §B).

Норма — ``docs/modules/media-generation/09-testing.md`` §«Integration — дедлайн задачи (ADR-105)»
и ``docs/06-testing-strategy.md`` §«Отказ поставщика (ADR-105)».

* Возраст задачи задаётся ``created_at`` строки, а не ожиданием; fal подменён на границе ``httpx``
  (``app.media_generation.fal_client.httpx``), как во всём модуле.
* **Каждое наблюдение гоняется ДВАЖДЫ на ОДНОМ И ТОМ ЖЕ скрипте fal** — у задачи старше дедлайна
  (половина (а), против недооценки: задача вечна) и у задачи моложе дедлайна (половина (б), против
  переоценки: живая задача провалена).
* Путь ``GET /v1/media/jobs/{id}`` и путь ``reconcile_once`` — оба рабочие, на реальной БД.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx as _httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from tests.conftest import seed_user
from tests.integration.test_media_generation_adr060 import (
    _QUEUE_BASE,
    _build_client,
    _FakeResponse,
    _Fal,
    _make_fake_httpx,
)

_FAL_KEY = "fal-test-key-deadline"
_DEADLINE_ERROR = "generation did not complete in time"
_DEADLINE_EVENT = "media_generation_deadline_exceeded"
_DEFAULT_DEADLINE = 21600
_OVERDUE = _DEFAULT_DEADLINE + 3600  # 7 h — past the default deadline
_YOUNG = 60  # one minute — a live job
_CREDITS = 4
_START_BALANCE = 100
_IMAGE_RESULT = {
    "images": [
        {"url": "https://v3.fal.media/files/out.png", "content_type": "image/png", "file_name": "o"}
    ]
}


class _DeadlineFal(_Fal):
    """``_Fal`` with a per-URL exception, so a COMPLETED status can meet a failing result."""

    def __init__(self) -> None:
        super().__init__()
        self.status_exc: BaseException | None = None
        self.result_exc: BaseException | None = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        if url.endswith("/status") and self.status_exc is not None:
            self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
            raise self.status_exc
        if method == "GET" and not url.endswith("/status") and self.result_exc is not None:
            self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
            raise self.result_exc
        return await super()._request(method, url, headers=headers, json=json)


# ================================ fixtures & helpers ================================


@pytest.fixture(autouse=True)
def _enable_media_loggers() -> None:
    """Alembic's ``fileConfig`` may have disabled the ``app.*`` loggers (conftest ``_migrated``)."""
    for name in (
        "app.media_generation.service",
        "app.media_generation.reconciler",
        "app.media_generation.fal_client",
    ):
        logging.getLogger(name).disabled = False


@pytest.fixture
def fal() -> _DeadlineFal:
    return _DeadlineFal()


@pytest.fixture
async def media(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.delenv("MEDIA_JOB_DEADLINE_SECONDS", raising=False)
    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as ac:
        yield ac
    get_settings.cache_clear()


@pytest.fixture
async def reconciler_env(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
) -> AsyncIterator[None]:
    """The reconciler uses the global ``app.db`` sessionmaker — bind a fresh engine to this loop."""
    from app.db import dispose_engine
    from app.media_generation import fal_client as fal_client_mod

    monkeypatch.setenv("FAL_API_KEY", _FAL_KEY)
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    monkeypatch.delenv("MEDIA_JOB_DEADLINE_SECONDS", raising=False)
    get_settings.cache_clear()
    await dispose_engine()
    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))
    yield
    await dispose_engine()
    get_settings.cache_clear()


async def _user(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with maker() as s:
        return await seed_user(s, balance=_START_BALANCE)


async def _seed_job(
    maker: async_sessionmaker[AsyncSession],
    uid: uuid.UUID,
    *,
    age_seconds: int,
    status: str = "running",
    kind: str = "image",
) -> uuid.UUID:
    job_id = uuid.uuid4()
    rid = f"req-{job_id.hex[:12]}"
    base = f"{_QUEUE_BASE}/fal-ai/nano-banana-2/requests/{rid}"
    async with maker() as s:
        await s.execute(
            text(
                """
                INSERT INTO media_jobs (
                    id, user_id, model_id, kind, fal_endpoint, fal_request_id,
                    status_url, response_url, status, prompt, credits_charged,
                    created_at, updated_at
                ) VALUES (
                    :id, :u, 'nano-banana-2', :kind, 'fal-ai/nano-banana-2', :rid,
                    :su, :ru, :st, 'a cat', :cr,
                    now() - make_interval(secs => :age), now() - make_interval(secs => :age)
                )
                """
            ),
            {
                "id": job_id,
                "u": uid,
                "kind": kind,
                "rid": rid,
                "su": f"{base}/status",
                "ru": base,
                "st": status,
                "cr": _CREDITS,
                "age": age_seconds,
            },
        )
        await s.commit()
    return job_id


async def _row(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> dict[str, Any]:
    async with maker() as s:
        row = (
            await s.execute(
                text("SELECT status, error, credits_refunded, result FROM media_jobs WHERE id=:id"),
                {"id": job_id},
            )
        ).one()
    return {"status": row[0], "error": row[1], "refunded": row[2], "result": row[3]}


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        value = await s.scalar(
            text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": str(uid)}
        )
    return int(value)


async def _refund_rows(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> int:
    async with maker() as s:
        value = await s.scalar(
            text("SELECT count(*) FROM ledger_transactions WHERE idempotency_key = :k"),
            {"k": f"media-refund:{job_id}"},
        )
    return int(value)


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for record in caplog.records:
        if record.getMessage() == name:
            fields = getattr(record, "extra_fields", {})
            out.append(dict(fields))
    return out


def _headers(uid: uuid.UUID) -> dict[str, str]:
    from tests.conftest import auth_headers

    return auth_headers(uid)


def _script(fal: _DeadlineFal, observation: str) -> None:
    """Script ONE observation of the poll (the same script serves both halves of the pair)."""
    if observation.startswith("status_"):
        fal.on_status_error(int(observation.removeprefix("status_")), {"detail": "boom"})
    elif observation == "timeout":
        fal.status_exc = _httpx.ReadTimeout("slow")
    elif observation == "connection_drop":
        fal.status_exc = _httpx.ConnectError("reset")
    elif observation == "bad_json":
        fal._status = _FakeResponse(200, None, json_raises=True)
    elif observation in ("IN_QUEUE", "IN_PROGRESS"):
        fal.on_status(observation)
    elif observation == "result_504":
        fal.on_status("COMPLETED")
        fal.on_result({"detail": "gateway"}, status_code=504)
    elif observation == "result_timeout":
        fal.on_status("COMPLETED")
        fal.result_exc = _httpx.ReadTimeout("slow")
    else:  # pragma: no cover - table guard
        raise AssertionError(observation)


# (observation, lastObservation, upstreamStatus, HTTP status of the YOUNG half, young row status)
_TABLE = [
    ("status_500", "upstream_error", 500, 502, "running"),
    ("status_502", "upstream_error", 502, 502, "running"),
    ("status_504", "upstream_error", 504, 502, "running"),
    ("status_402", "upstream_error", 402, 502, "running"),
    ("status_429", "upstream_error", 429, 429, "running"),
    ("status_401", "upstream_error", 401, 503, "running"),
    ("timeout", "upstream_error", None, 502, "running"),
    ("connection_drop", "upstream_error", None, 502, "running"),
    ("bad_json", "upstream_error", None, 502, "running"),
    ("IN_QUEUE", "upstream_pending", None, 200, "running"),
    ("IN_PROGRESS", "upstream_pending", None, 200, "running"),
    ("result_504", "upstream_error", 504, 502, "running"),
    ("result_timeout", "upstream_error", None, 502, "running"),
]


# ========================= the table: every observation × both halves =========================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observation", "last", "upstream_status", "young_code", "young_row"), _TABLE
)
async def test_overdue_half_closes_the_job_with_refund(
    observation: str,
    last: str,
    upstream_status: int | None,
    young_code: int,
    young_row: str,
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(а) против недооценки: старше дедлайна, опрос без конечного состояния ⇒ failed + возврат."""
    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    _script(fal, observation)

    r = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["error"] == _DEADLINE_ERROR
    assert body["creditsRefunded"] is True
    assert body["assets"] == []
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS
    assert fal.calls, "the poll must happen past the deadline too (last chance)"
    events = _events(caplog, _DEADLINE_EVENT)
    assert len(events) == 1
    event = events[0]
    assert event["jobId"] == str(job_id)
    assert event["model"] == "nano-banana-2"
    assert event["lastObservation"] == last
    assert event["ageSeconds"] >= _OVERDUE
    if upstream_status is None:
        assert "upstreamStatus" not in event
    else:
        assert event["upstreamStatus"] == upstream_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observation", "last", "upstream_status", "young_code", "young_row"), _TABLE
)
async def test_young_half_is_left_alone(
    observation: str,
    last: str,
    upstream_status: int | None,
    young_code: int,
    young_row: str,
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(б) против переоценки: моложе дедлайна ⇒ правило не трогает, какой бы ни была ошибка."""
    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_YOUNG)
    _script(fal, observation)

    r = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))

    assert r.status_code == young_code, r.text
    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == young_row
    assert row["refunded"] is False
    assert row["error"] is None
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE
    assert _events(caplog, _DEADLINE_EVENT) == []


# ----------------------------- moderation unavailable (fail-closed) -----------------------------


class _BrokenModerations:
    async def create(self, **_kwargs: Any) -> Any:
        raise RuntimeError("moderation provider down")


class _BlockingModerations:
    async def create(self, **_kwargs: Any) -> Any:
        return SimpleNamespace(results=[SimpleNamespace(flagged=True, categories={"sexual": True})])


def _moderation(moderations: Any) -> Any:
    from app.moderation import ModerationService

    svc = ModerationService(
        settings=Settings(  # type: ignore[call-arg]
            MODERATION_ENABLED="true", MODERATION_API_KEY="sk-moderation-test"
        )
    )
    svc._client = SimpleNamespace(moderations=moderations)
    return svc


@pytest.mark.asyncio
@pytest.mark.parametrize("overdue", [True, False])
async def test_moderation_unavailable_pair(
    overdue: bool,
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """COMPLETED with an image, post-moderation unavailable (ADR-086 §7 fail-closed)."""
    from app import deps

    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    monkeypatch.setattr(deps, "get_moderation_service", lambda: _moderation(_BrokenModerations()))
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE if overdue else _YOUNG)
    fal.on_status("COMPLETED")
    fal.on_result(_IMAGE_RESULT)

    r = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))

    if overdue:
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "failed"
        assert r.json()["error"] == _DEADLINE_ERROR
        assert r.json()["assets"] == []
        events = _events(caplog, _DEADLINE_EVENT)
        assert [e["lastObservation"] for e in events] == ["moderation_unavailable"]
        download = await media.get(f"/v1/media/jobs/{job_id}/assets/0/any-token")
        assert download.status_code == 404
        assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS
    else:
        assert r.status_code == 503, r.text
        row = await _row(db_sessionmaker, job_id)
        assert row["status"] == "running"
        assert row["result"] is None
        assert _events(caplog, _DEADLINE_EVENT) == []


# ----------------------------- exception of our own code -----------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("overdue", [True, False])
async def test_internal_error_pair(
    overdue: bool,
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception in result normalization (our code between the poll and the outcome)."""
    from app.media_generation import service as service_mod

    def _broken(body: dict[str, Any], *, kind: str) -> dict[str, Any]:
        raise RuntimeError("normalization bug")

    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    monkeypatch.setattr(service_mod, "_normalize_result", _broken)
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE if overdue else _YOUNG)
    fal.on_status("COMPLETED")
    fal.on_result(_IMAGE_RESULT)

    if overdue:
        r = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "failed"
        events = _events(caplog, _DEADLINE_EVENT)
        assert [e["lastObservation"] for e in events] == ["internal_error"]
        assert "upstreamStatus" not in events[0]
    else:
        with pytest.raises(RuntimeError, match="normalization bug"):
            await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))
        row = await _row(db_sessionmaker, job_id)
        assert row["status"] == "running"
        assert _events(caplog, _DEADLINE_EVENT) == []


# ============================ last chance (the second side of (b)) ============================


@pytest.mark.asyncio
async def test_last_chance_completed_past_the_deadline_is_completed(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Older than the deadline, but the poll says COMPLETED with assets ⇒ ``completed``."""
    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    fal.on_status("COMPLETED")
    fal.on_result(_IMAGE_RESULT)

    r = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert len(body["assets"]) == 1
    assert body["creditsRefunded"] is False
    assert _events(caplog, _DEADLINE_EVENT) == []
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE


@pytest.mark.asyncio
async def test_last_chance_failed_past_the_deadline_keeps_the_fal_text(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    fal.on_status("FAILED", error="upstream model crashed")

    r = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    assert r.json()["error"] == "upstream model crashed"
    assert _events(caplog, _DEADLINE_EVENT) == []


# ============================ refund exactly once ============================


@pytest.mark.asyncio
async def test_closed_job_is_not_polled_again_and_refunded_once(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    fal.on_status_error(504)

    first = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))
    calls_after_first = len(fal.calls)
    second = await media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid))

    assert first.json()["status"] == second.json()["status"] == "failed"
    assert len(fal.calls) == calls_after_first
    assert await _refund_rows(db_sessionmaker, job_id) == 1
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS


@pytest.mark.asyncio
async def test_two_parallel_advances_of_one_overdue_job_refund_once(
    media: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    fal.on_status_error(504)

    responses = await asyncio.gather(
        media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid)),
        media.get(f"/v1/media/jobs/{job_id}", headers=_headers(uid)),
    )

    assert [r.json()["status"] for r in responses] == ["failed", "failed"]
    assert await _refund_rows(db_sessionmaker, job_id) == 1
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS


# ============================ setting ============================


@pytest.mark.asyncio
async def test_configured_deadline_of_60_seconds_applies_on_the_path(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
) -> None:
    """``MEDIA_JOB_DEADLINE_SECONDS=60`` ⇒ a 2-minute-old job is overdue, a 30-second one is not."""
    monkeypatch.setenv("MEDIA_JOB_DEADLINE_SECONDS", "60")
    async with _build_client(monkeypatch, db_sessionmaker, fal, fal_key=_FAL_KEY) as ac:
        uid = await _user(db_sessionmaker)
        old = await _seed_job(db_sessionmaker, uid, age_seconds=120)
        fresh = await _seed_job(db_sessionmaker, uid, age_seconds=30)
        fal.on_status_error(504)

        closed = await ac.get(f"/v1/media/jobs/{old}", headers=_headers(uid))
        untouched = await ac.get(f"/v1/media/jobs/{fresh}", headers=_headers(uid))

    get_settings.cache_clear()
    assert closed.status_code == 200 and closed.json()["status"] == "failed"
    assert untouched.status_code == 502
    assert (await _row(db_sessionmaker, fresh))["status"] == "running"


# ========================= the reconciler — the real path, one assembly =========================


async def _seed_request_log(
    maker: async_sessionmaker[AsyncSession], uid: uuid.UUID, job_id: uuid.UUID
) -> uuid.UUID:
    log_id = uuid.uuid4()
    async with maker() as s:
        await s.execute(
            text(
                "INSERT INTO request_logs (id, user_id, endpoint, status, status_code, refunded, "
                "media_job_id, tokens_spent) VALUES (:id, :u, '/v1/media/images', 'queued', 202, "
                "false, :j, :t)"
            ),
            {"id": log_id, "u": uid, "j": job_id, "t": _CREDITS},
        )
        await s.commit()
    return log_id


@pytest.mark.asyncio
async def test_reconciler_closes_overdue_job_and_its_request_log_leaves_young_one(
    reconciler_env: None,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``reconcile_once`` on the real DB, fal ``status`` → 504, one overdue and one young job in the
    SAME tick: the overdue job is failed + refunded and its ``request_logs`` row is closed (it
    stayed ``queued`` with the old hand-built service); the young one is not touched."""
    from app.media_generation.reconciler import reconcile_once

    caplog.set_level(logging.WARNING)
    uid = await _user(db_sessionmaker)
    overdue = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    young = await _seed_job(db_sessionmaker, uid, age_seconds=_YOUNG)
    log_id = await _seed_request_log(db_sessionmaker, uid, overdue)
    fal.on_status_error(504)

    await reconcile_once(get_settings())

    assert (await _row(db_sessionmaker, overdue))["status"] == "failed"
    assert (await _row(db_sessionmaker, young))["status"] == "running"
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS
    async with db_sessionmaker() as s:
        log = (
            await s.execute(
                text("SELECT status, refunded, completed_at FROM request_logs WHERE id=:id"),
                {"id": log_id},
            )
        ).one()
    assert log[0] == "failed"
    assert log[1] is True
    assert log[2] is not None
    events = _events(caplog, _DEADLINE_EVENT)
    assert [(e["jobId"], e["lastObservation"], e["upstreamStatus"]) for e in events] == [
        (str(overdue), "upstream_error", 504)
    ]
    errors = _events(caplog, "media_reconcile_job_error")
    assert [(e["jobId"], e["exceptionClass"]) for e in errors] == [(str(young), "UpstreamError")]


@pytest.mark.asyncio
async def test_reconciler_post_moderates_a_ready_image(
    reconciler_env: None,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A picture whose readiness the reconciler discovers goes through post-moderation (ADR-086 §5):
    verdict ``blocked`` ⇒ ``failed`` without assets, as on ``GET``."""
    from app import deps
    from app.media_generation.reconciler import reconcile_once

    monkeypatch.setattr(deps, "get_moderation_service", lambda: _moderation(_BlockingModerations()))
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_YOUNG)
    fal.on_status("COMPLETED")
    fal.on_result(_IMAGE_RESULT)

    await reconcile_once(get_settings())

    row = await _row(db_sessionmaker, job_id)
    assert row["status"] == "failed"
    assert row["result"] == {"assets": []}
    assert row["refunded"] is True


@pytest.mark.asyncio
async def test_reconciler_without_fal_key_closes_only_overdue_jobs_without_calls(
    reconciler_env: None,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.media_generation.reconciler import reconcile_once

    monkeypatch.setenv("FAL_API_KEY", "")
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    uid = await _user(db_sessionmaker)
    overdue = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE)
    young = await _seed_job(db_sessionmaker, uid, age_seconds=_YOUNG, status="queued")

    await reconcile_once(get_settings())

    assert fal.calls == []
    assert (await _row(db_sessionmaker, overdue))["status"] == "failed"
    assert (await _row(db_sessionmaker, young))["status"] == "queued"
    assert await _balance(db_sessionmaker, uid) == _START_BALANCE + _CREDITS
    events = _events(caplog, _DEADLINE_EVENT)
    assert [(e["jobId"], e["lastObservation"]) for e in events] == [
        (str(overdue), "not_configured")
    ]
    assert "upstreamStatus" not in events[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("overdue", [True, False])
async def test_get_path_service_without_fal_key(
    overdue: bool,
    reconciler_env: None,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _DeadlineFal,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``GET`` path at the service (``get_job``) with an empty key: an overdue job is closed
    (``not_configured``) without an outgoing call; a young one keeps the previous 503."""
    from app import deps
    from app.errors import MediaGenerationNotConfiguredError

    monkeypatch.setenv("FAL_API_KEY", "")
    get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="app.media_generation.service")
    uid = await _user(db_sessionmaker)
    job_id = await _seed_job(db_sessionmaker, uid, age_seconds=_OVERDUE if overdue else _YOUNG)

    async with db_sessionmaker() as session:
        service = deps.build_media_generation_service(session, deps.get_request_log_writer(session))
        if overdue:
            view = await service.get_job(user_id=uid, job_id=job_id)
            await session.commit()
            assert view.job.status == "failed"
            assert view.job.error == _DEADLINE_ERROR
        else:
            with pytest.raises(MediaGenerationNotConfiguredError):
                await service.get_job(user_id=uid, job_id=job_id)
            await session.rollback()

    assert fal.calls == []
    events = _events(caplog, _DEADLINE_EVENT)
    if overdue:
        assert [e["lastObservation"] for e in events] == ["not_configured"]
    else:
        assert events == []
        assert (await _row(db_sessionmaker, job_id))["status"] == "running"


# ========================= setting normalization (unit-level, no I/O) =========================


@pytest.mark.parametrize(
    ("env", "expected"),
    [(None, _DEFAULT_DEADLINE), ("60", 60), ("0", _DEFAULT_DEADLINE), ("-5", _DEFAULT_DEADLINE)],
)
def test_deadline_setting_cannot_be_switched_off(
    env: str | None, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    if env is None:
        monkeypatch.delenv("MEDIA_JOB_DEADLINE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("MEDIA_JOB_DEADLINE_SECONDS", env)
    assert Settings().media_job_deadline_seconds == expected
