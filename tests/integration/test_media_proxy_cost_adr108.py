"""Integration: ADR-108 §8/§9 — vendor price as the CRM cost, and ``pending_result`` as SQL NULL.

Norm — ``docs/adr/ADR-108-media-generation-via-proxy.md`` §8 (the callback price REPLACES
``provider_cost_usd`` only for USD-confirmed services ``fal``/``sosana``; ``kie`` keeps the submit
estimate; ``data.creditsConsumed`` is not a price) and §9 / ``04-data-model.md`` (``pending_result``
"no result" is SQL ``NULL``, not JSON ``null``); cases — ``docs/modules/media-generation/
09-testing.md`` §«Себестоимость (§8)» and §«SQL NULL у pending_result».

Every job here is created by the REAL ``POST /v1/media/images`` (so the row, its estimate and its
``pending_result`` come from the ORM, not from hand-written SQL), then completed by a REAL signed
callback. Fakes, fixtures and helpers are the ones of ``test_media_proxy_adr108``.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import auth_headers
from tests.integration.test_media_proxy_adr108 import (  # noqa: F401 - fixtures by import
    _VENDOR_HOSTS,
    _callback,
    _enable_media_loggers,
    _Fal,
    _fal_completed,
    _open,
    _post_image,
    _Proxy,
    _user,
    fal,
    moderation,
    proxy,
    reconciler_env,
)

_ADMIN_SECRET = "crm-proxy-cost-key-adr108-0123456789abcdef"  # noqa: S105 - test-only
_ADMIN = {"X-Admin-Key": _ADMIN_SECRET}
_PRICE = 0.022
_EVIL = "https://evil.example.com/out.png"


@pytest.fixture
async def app_client(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fal: _Fal,  # noqa: F811
    proxy: _Proxy,  # noqa: F811
) -> AsyncIterator[AsyncClient]:
    """Proxy instance with sosana/kie routes on, plus the CRM admin surface."""
    from app.api_gateway.routers import crm_admin as crm_admin_router

    async def _allow(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setenv("ADMIN_API_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setattr(crm_admin_router, "enforce_admin_limits", _allow)
    async with await _open(
        monkeypatch, db_sessionmaker, fal, proxy, result_hosts=_VENDOR_HOSTS
    ) as ac:
        yield ac
    get_settings.cache_clear()


# Request parameters that make the FIRST route the named service (ADR-108 §2.1).
_ROUTE = {
    "sosana": {"resolution": "2K", "aspectRatio": "16:9"},
    "kie": {"resolution": "2K", "aspectRatio": "16:9", "outputFormat": "png"},
    "fal": {"resolution": "2K"},  # no aspectRatio ⇒ the only route is fal
}


async def _cost_row(maker: async_sessionmaker[AsyncSession], job_id: str) -> dict[str, Any]:
    async with maker() as s:
        row = (
            await s.execute(
                text(
                    "SELECT provider, provider_cost_usd, vendor_price, status, "
                    "pending_result IS NULL AS sql_null, jsonb_typeof(pending_result) AS jtype "
                    "FROM media_jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()
    return {
        "provider": row[0],
        "cost": row[1],
        "vendor_price": row[2],
        "status": row[3],
        "sql_null": row[4],
        "jtype": row[5],
    }


async def _submit(client: AsyncClient, uid: uuid.UUID, service: str) -> str:
    resp = await _post_image(client, uid, **_ROUTE[service])
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["jobId"]
    return str(job_id)


def _body(outcome: str, price: Any) -> dict[str, Any]:
    if outcome == "completed":
        return _fal_completed(vendor_price=price)
    if outcome == "failed":
        return {"status": "failed", "error": "vendor said no", "vendor_price": price}
    if outcome == "no_usable_asset":
        return _fal_completed(url=_EVIL, vendor_price=price)
    if outcome == "pending":
        return {"status": "processing", "vendor_price": price}
    raise AssertionError(outcome)  # pragma: no cover


_OUTCOMES = {"completed": "completed", "failed": "failed", "no_usable_asset": "failed"}


# ======================= §8 (а): USD-confirmed services — the price REPLACES the cost =======


@pytest.mark.parametrize("service", ["fal", "sosana"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "no_usable_asset"])
async def test_usd_confirmed_service_price_replaces_the_estimate(
    app_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    proxy: _Proxy,  # noqa: F811
    service: str,
    outcome: str,
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, service)
    before = await _cost_row(db_sessionmaker, job_id)
    assert before["provider"] == service
    assert before["cost"] is not None and before["cost"] != decimal.Decimal(str(_PRICE))

    resp = await _callback(app_client, job_id, _body(outcome, _PRICE))

    assert resp.status_code == 200, resp.text
    after = await _cost_row(db_sessionmaker, job_id)
    assert after["status"] == _OUTCOMES[outcome]
    assert after["vendor_price"] == decimal.Decimal("0.022000")
    assert after["cost"] == decimal.Decimal("0.022000"), "CRM cost is the real vendor price"


# ======================= §8 (б): the estimate stays ============================================


@pytest.mark.parametrize("outcome", ["completed", "failed", "no_usable_asset"])
async def test_kie_price_is_recorded_but_the_estimate_stays(
    app_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    outcome: str,
) -> None:
    """``kie`` units are not confirmed as USD (Q-108-12): only ``vendor_price`` is written."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "kie")
    before = await _cost_row(db_sessionmaker, job_id)
    assert before["provider"] == "kie"

    await _callback(app_client, job_id, _body(outcome, 7))

    after = await _cost_row(db_sessionmaker, job_id)
    assert after["status"] == _OUTCOMES[outcome]
    assert after["vendor_price"] == decimal.Decimal("7.000000")
    assert after["cost"] == before["cost"]


async def test_callback_without_price_leaves_the_estimate(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "fal")
    before = await _cost_row(db_sessionmaker, job_id)
    await _callback(app_client, job_id, _fal_completed(vendor_price=None))
    after = await _cost_row(db_sessionmaker, job_id)
    assert after["status"] == "completed"
    assert after["vendor_price"] is None
    assert after["cost"] == before["cost"] and after["cost"] is not None


async def test_credits_consumed_is_not_a_price(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A price only in ``data.creditsConsumed`` (vendor credits, not money) is not read."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "fal")
    before = await _cost_row(db_sessionmaker, job_id)
    body = _fal_completed()
    body["data"] = {"creditsConsumed": 12}
    await _callback(app_client, job_id, body)
    after = await _cost_row(db_sessionmaker, job_id)
    assert after["status"] == "completed"
    assert after["vendor_price"] is None
    assert after["cost"] == before["cost"]


async def test_price_the_cost_column_cannot_hold_keeps_the_estimate(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """``provider_cost_usd`` is NUMERIC(12,6): a price ≥ 1e6 is kept in ``vendor_price`` only."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "fal")
    before = await _cost_row(db_sessionmaker, job_id)
    resp = await _callback(app_client, job_id, _fal_completed(vendor_price=2_000_000))
    assert resp.status_code == 200, resp.text
    after = await _cost_row(db_sessionmaker, job_id)
    assert after["status"] == "completed"
    assert after["vendor_price"] == decimal.Decimal("2000000.000000")
    assert after["cost"] == before["cost"]


async def test_redelivery_to_a_terminal_job_changes_no_price(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "sosana")
    await _callback(app_client, job_id, _fal_completed(vendor_price=_PRICE))
    first = await _cost_row(db_sessionmaker, job_id)
    await _callback(app_client, job_id, _fal_completed(vendor_price=0.9))
    await _callback(app_client, job_id, _body("failed", 0.9))
    assert await _cost_row(db_sessionmaker, job_id) == first


@pytest.mark.parametrize("second", ["pending", "failed", "completed"])
async def test_step0_callback_with_a_price_records_it(
    app_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: Any,  # noqa: F811
    second: str,
) -> None:
    """Result already received (``pending_result`` set, completion deferred): a further callback
    carrying a price still records it — before branching (§8) — and changes nothing else."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "sosana")
    moderation.mode = "broken"
    await _callback(app_client, job_id, _fal_completed(vendor_price=_PRICE))
    deferred = await _cost_row(db_sessionmaker, job_id)
    # Non-terminal. NOTE: the row stays `queued` here, not `running` as 09-testing.md words the
    # deferred case — reported as a docs↔code discrepancy, not asserted either way.
    assert deferred["status"] in ("queued", "running") and deferred["sql_null"] is False
    assert deferred["cost"] == decimal.Decimal("0.022000")

    await _callback(app_client, job_id, _body(second, 0.03))

    after = await _cost_row(db_sessionmaker, job_id)
    assert after["status"] == deferred["status"]
    assert after["sql_null"] is False
    assert after["vendor_price"] == decimal.Decimal("0.030000")
    assert after["cost"] == decimal.Decimal("0.030000")


# ======================= §8 — what CRM reads (end to end) ======================================


async def _crm_views(client: AsyncClient, uid: uuid.UUID) -> tuple[float, float]:
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    daily = await client.get(
        "/v1/admin/costs/daily", headers=_ADMIN, params={"date_from": today, "date_to": today}
    )
    assert daily.status_code == 200, daily.text
    cells = [c for c in daily.json()["items"] if c["provider"] == "Fal"]
    assert len(cells) == 1, daily.json()
    history = await client.get(f"/v1/admin/users/{uid}/requests", headers=_ADMIN)
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert len(items) == 1, items
    return float(cells[0]["spend_usd"]), float(items[0]["provider_cost_usd"])


async def test_crm_daily_costs_and_request_history_show_the_real_price(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """(а) sosana: both CRM views show the callback price, not the submit estimate."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "sosana")
    estimate = float((await _cost_row(db_sessionmaker, job_id))["cost"])
    assert estimate != pytest.approx(_PRICE)

    await _callback(app_client, job_id, _fal_completed(vendor_price=_PRICE))

    daily, history = await _crm_views(app_client, uid)
    assert daily == pytest.approx(_PRICE)
    assert history == pytest.approx(_PRICE)


async def test_crm_keeps_the_estimate_for_kie(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """(б) kie: both CRM views keep the submit estimate — never the unconfirmed-unit price."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "kie")
    estimate = float((await _cost_row(db_sessionmaker, job_id))["cost"])

    await _callback(app_client, job_id, _fal_completed(vendor_price=7))

    daily, history = await _crm_views(app_client, uid)
    assert daily == pytest.approx(estimate)
    assert history == pytest.approx(estimate)
    assert daily != pytest.approx(7.0)


# ======================= §9 — pending_result "no result" is SQL NULL ==========================


async def test_a_job_created_by_post_has_sql_null_pending_result(
    app_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "fal")
    row = await _cost_row(db_sessionmaker, job_id)
    assert row["sql_null"] is True, "JSON null would hide the job from every IS NULL predicate"
    assert row["jtype"] is None


@pytest.mark.parametrize("terminal", ["completed", "failed"])
async def test_terminal_job_has_sql_null_pending_result(
    app_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: Any,  # noqa: F811
    terminal: str,
) -> None:
    """``mark_completed`` / ``mark_failed`` clear a RECORDED pending_result to SQL NULL."""
    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "fal")
    moderation.mode = "broken"
    await _callback(app_client, job_id, _fal_completed())
    assert (await _cost_row(db_sessionmaker, job_id))["sql_null"] is False
    if terminal == "completed":
        moderation.mode = "pass"
        await app_client.get(f"/v1/media/jobs/{job_id}", headers=auth_headers(uid))
    else:
        moderation.mode = "block"
        await app_client.get(f"/v1/media/jobs/{job_id}", headers=auth_headers(uid))
    row = await _cost_row(db_sessionmaker, job_id)
    assert row["status"] == terminal
    assert row["sql_null"] is True
    assert row["jtype"] is None


async def test_gauge_counts_a_post_created_job_after_an_hour_and_not_after_its_result(
    reconciler_env: None,  # noqa: F811
    app_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    moderation: Any,  # noqa: F811
) -> None:
    """(а) the ORM-created row without a callback, older than an hour, IS counted; (б) the same
    row once its ``pending_result`` is stored is NOT counted."""
    from app.media_generation.reconciler import reconcile_once
    from app.observability.metrics import media_proxy_jobs_awaiting_callback

    uid = await _user(db_sessionmaker)
    job_id = await _submit(app_client, uid, "fal")
    async with db_sessionmaker() as s:
        # Age the row (the clock of the job is its created_at); nothing else is touched.
        await s.execute(
            text("UPDATE media_jobs SET created_at = now() - interval '2 hours' WHERE id = :id"),
            {"id": job_id},
        )
        await s.commit()

    media_proxy_jobs_awaiting_callback.set(-1)
    await reconcile_once(get_settings())
    assert media_proxy_jobs_awaiting_callback._value.get() == 1  # noqa: SLF001

    moderation.mode = "broken"
    await _callback(app_client, job_id, _fal_completed())
    assert (await _cost_row(db_sessionmaker, job_id))["sql_null"] is False
    await reconcile_once(get_settings())
    assert media_proxy_jobs_awaiting_callback._value.get() == 0  # noqa: SLF001
