"""Integration: `GET /v1/admin/costs/daily` — периодная разбивка расходов (ADR-092).

Перечень сценариев — `docs/modules/admin/09-testing.md §Integration — GET /v1/admin/costs/daily`.
Нормы — ADR-092 и `docs/modules/admin/02-api-contracts.md §GET /v1/admin/costs/daily`.

Все суммы в ожиданиях посчитаны РУКАМИ по таблице `CHAT_TOKEN_PRICES` (ADR-079 §1) и выписаны
в комментариях: тест, берущий ожидание из того же модуля, который проверяет, согласился бы с
любой его ошибкой.

Период тестов — фиксированные даты марта 2026, а не `now()`: БД чистится перед каждым тестом
(`db_sessionmaker`), и фиксированный календарь делает ожидания по дням дословно проверяемыми.
"""

from __future__ import annotations

import datetime
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, seed_user

_ADMIN_SECRET = "crm-daily-costs-key-integration-0123456789abcdef"
_ADMIN_HEADERS = {"X-Admin-Key": _ADMIN_SECRET}

_PATH = "/v1/admin/costs/daily"


def _utc(day: str, time_part: str = "00:00:00") -> datetime.datetime:
    return datetime.datetime.fromisoformat(f"{day}T{time_part}+00:00")


@pytest.fixture
async def crm_admin_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    settings = get_settings()
    orig_secret = settings.admin_api_secret
    orig_key = settings.admin_api_key
    settings.admin_api_secret = _ADMIN_SECRET
    settings.admin_api_key = ""

    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import admin as admin_router
    from app.api_gateway.routers import crm_admin as crm_admin_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    async def _allow_admin(**_kwargs: Any) -> bool:
        return True

    orig_admin = rate_limit.enforce_admin_limits
    rate_limit.enforce_admin_limits = _allow_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = _allow_admin  # type: ignore[assignment]
    crm_admin_router.enforce_admin_limits = _allow_admin  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    settings.admin_api_secret = orig_secret
    settings.admin_api_key = orig_key
    rate_limit.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    crm_admin_router.enforce_admin_limits = orig_admin  # type: ignore[assignment]


async def _seed_steps(
    session: AsyncSession,
    user_id: uuid.UUID,
    steps: Sequence[tuple[datetime.datetime, dict[str, Any] | None]],
    *,
    message_step_id: uuid.UUID | None = None,
) -> None:
    """Assistant-шаги чата с заданным `created_at` и `usage` (`None` = шаг без вызова LLM)."""
    session_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
            "VALUES (:sid, :uid, 'p', 'credits')"
        ),
        {"sid": str(session_id), "uid": str(user_id)},
    )
    for created_at, usage in steps:
        await session.execute(
            text(
                "INSERT INTO chat_steps "
                "(session_id, message_step_id, role, payload, usage, created_at) "
                "VALUES (:sid, :msid, 'assistant', '{}'::jsonb, CAST(:usage AS jsonb), :ts)"
            ),
            {
                "sid": str(session_id),
                "msid": str(message_step_id or uuid.uuid4()),
                "usage": None if usage is None else json.dumps(usage),
                "ts": created_at,
            },
        )


async def _seed_media_job(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    created_at: datetime.datetime,
    model_id: str = "kling-video",
    credits: int = 28,
    provider_cost_usd: float | None = None,
    assets: int | None = 1,
) -> None:
    result = (
        None if assets is None else json.dumps({"assets": [{"url": "https://x/a.mp4"}] * assets})
    )
    await session.execute(
        text(
            """
            INSERT INTO media_jobs (
                user_id, model_id, kind, fal_endpoint, fal_request_id,
                status_url, response_url, status, prompt,
                credits_charged, credits_refunded, provider_cost_usd, result,
                created_at, updated_at
            ) VALUES (
                :uid, :model, 'video', 'fal-ai/kling', :req,
                'https://q/status', 'https://q', 'completed', 'a cat',
                :credits, false, :cost, CAST(:result AS jsonb),
                :ts, :ts
            )
            """
        ),
        {
            "uid": str(user_id),
            "model": model_id,
            "req": f"req-{uuid.uuid4()}",
            "credits": credits,
            "cost": provider_cost_usd,
            "result": result,
            "ts": created_at,
        },
    )


async def _get(
    client: AsyncClient,
    *,
    date_from: str,
    date_to: str,
    limit: int | None = None,
    offset: int | None = None,
) -> Any:
    params: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return await client.get(_PATH, headers=_ADMIN_HEADERS, params=params)


# --------------------------------------------------------------------------------------
# Форма ответа, порядок, сырые ключи провайдеров
# --------------------------------------------------------------------------------------


async def test_daily_costs_shape_order_and_raw_provider_keys(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Два дня × два провайдера → `total` = 4, порядок `date ASC, provider ASC`, ключи СЫРЫЕ."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                ),
                (
                    _utc("2026-03-10", "02:00:00"),
                    {"model": "claude-sonnet-4-5", "inputTokens": 1000, "outputTokens": 100},
                ),
                (
                    _utc("2026-03-11", "03:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                ),
                (
                    _utc("2026-03-11", "04:00:00"),
                    {"model": "claude-sonnet-4-5", "inputTokens": 1000, "outputTokens": 100},
                ),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-11")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 4
    assert [(i["date"], i["provider"]) for i in body["items"]] == [
        ("2026-03-10", "Anthropic"),
        ("2026-03-10", "OpenAI"),
        ("2026-03-11", "Anthropic"),
        ("2026-03-11", "OpenAI"),
    ]
    # Ключ отдаётся СЫРЫМ: нормализацию (`openai`/`anthropic`/`fal`/`other`) делает потребитель.
    assert {i["provider"] for i in body["items"]} == {"Anthropic", "OpenAI"}
    for item in body["items"]:
        assert set(item) == {"date", "provider", "spend_usd", "requests", "tokens"}
        assert item["requests"] == 1

    openai_cell = body["items"][1]
    # gpt-4o: (1000×2.50 + 100×10.00) / 1e6 = 0.0035; кэша нет, tokens = 1000 + 100.
    assert openai_cell["spend_usd"] == pytest.approx(0.0035)
    assert openai_cell["tokens"] == pytest.approx(1100.0)

    anthropic_cell = body["items"][0]
    # claude-sonnet-4-5: (1000×3.00 + 100×15.00) / 1e6 = 0.0045.
    assert anthropic_cell["spend_usd"] == pytest.approx(0.0045)
    assert anthropic_cell["tokens"] == pytest.approx(1100.0)


async def test_daily_costs_empty_period_is_200_with_no_items(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Расхода за период не было → `200` и пустой список, а НЕ `404`."""
    async with db_sessionmaker() as s:
        await seed_user(s)

    r = await _get(crm_admin_client, date_from="2026-03-01", date_to="2026-03-05")
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 0, "items": []}


# --------------------------------------------------------------------------------------
# `requests` = вызовы провайдера, а не ходы пользователя (ADR-092 §2)
# --------------------------------------------------------------------------------------


async def test_daily_costs_requests_count_calls_not_turns(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Один `message_step_id` с тремя assistant-шагами → `requests = 3`, а не `1`."""
    msid = uuid.uuid4()
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", f"0{i}:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                )
                for i in (1, 2, 3)
            ],
            message_step_id=msid,
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    cell = body["items"][0]
    assert cell["requests"] == 3, "единица счёта — ВЫЗОВ провайдера, а не ход пользователя"
    # Три вызова gpt-4o: 3 × (1000×2.50 + 100×10.00) / 1e6 = 0.0105.
    assert cell["spend_usd"] == pytest.approx(0.0105)
    assert cell["tokens"] == pytest.approx(3300.0)


async def test_daily_costs_media_wizard_step_without_usage_is_never_counted(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """⛔ Норма «не чинить» (ADR-092 §6, верхняя строка таблицы).

    Assistant-шаг медиа-визарда «Generation started …» идёт БЕЗ `usage` — это не вызов LLM, а
    объявление уже оплаченной генерации. Оплата этой генерации посчитана строкой `media_jobs`;
    счёт такого шага в `requests` УДВОИЛ бы media и придумал обращение к провайдеру, которого
    не было. Тест фиксирует отсутствие счёта как НОРМУ, чтобы будущий «фикс» не удвоил media.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(s, uid, [(_utc("2026-03-10", "05:00:00"), None)])
        await _seed_media_job(
            s, uid, created_at=_utc("2026-03-10", "05:00:01"), provider_cost_usd=0.35
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    # Ровно одна клетка — Fal. Клетки чата от шага-объявления не появляется ВОВСЕ.
    assert body["total"] == 1
    cell = body["items"][0]
    assert cell["provider"] == "Fal"
    assert cell["requests"] == 1, "шаг-объявление визарда удвоил бы уже посчитанную генерацию"
    assert cell["spend_usd"] == pytest.approx(0.35)
    assert {i["provider"] for i in body["items"]} == {"Fal"}


# --------------------------------------------------------------------------------------
# Арифметика кэша OpenAI — прямые деньги (ADR-092 §3)
# --------------------------------------------------------------------------------------


async def test_daily_costs_openai_cache_is_subtracted_per_call_not_on_totals(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Вычитание кэша ПОФАКТОРНОЕ по каждому вызову, с обрезкой нулём (ADR-092 §3).

    Два вызова gpt-4o одного дня, у ВТОРОГО `cacheRead > input` — именно на нём разность
    ИТОГОВ расходится с ответом по вызовам:

    * вызов 1: input 1000, cacheRead 800 → оплачиваемый вход `max(0, 1000-800)` = 200
    * вызов 2: input  200, cacheRead 1000 → оплачиваемый вход `max(0, 200-1000)` = 0

    Верно (пофакторно):   (200×2.50 + 150×10.00 + 1800×1.25) / 1e6 = 0.00425
    Неверно (по итогам):  вход 1200 − кэш 1800 → clamp 0
                          (0   ×2.50 + 150×10.00 + 1800×1.25) / 1e6 = 0.00375

    Разница — прямые деньги, и она тем больше, чем длиннее период.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {
                        "model": "gpt-4o",
                        "inputTokens": 1000,
                        "outputTokens": 100,
                        "cacheReadTokens": 800,
                        "cacheWriteTokens": 0,
                    },
                ),
                (
                    _utc("2026-03-10", "02:00:00"),
                    {
                        "model": "gpt-4o",
                        "inputTokens": 200,
                        "outputTokens": 50,
                        "cacheReadTokens": 1000,
                        "cacheWriteTokens": 0,
                    },
                ),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    cell = r.json()["items"][0]
    assert cell["requests"] == 2
    assert cell["spend_usd"] == pytest.approx(
        0.00425
    ), "разность ИТОГОВ дала бы 0.00375: кэш вычитается по КАЖДОМУ вызову с обрезкой нулём"
    # tokens по конвенции прайс-строки: у OpenAI кэш УЖЕ внутри `inputTokens`, поэтому
    # input(1200) + output(150) = 1350, а не наивные input + cacheRead + output = 3150.
    assert cell["tokens"] == pytest.approx(1350.0)


async def test_daily_costs_anthropic_tokens_add_cache_because_input_excludes_it(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """У Anthropic `inputTokens` НЕ включает кэш — конвенция обратная, и tokens её следуют."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {
                        "model": "claude-sonnet-4-5",
                        "inputTokens": 1000,
                        "outputTokens": 100,
                        "cacheReadTokens": 500,
                        "cacheWriteTokens": 200,
                    },
                )
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    cell = r.json()["items"][0]
    assert cell["provider"] == "Anthropic"
    # (1000×3.00 + 100×15.00 + 500×0.30 + 200×3.75) / 1e6 = 5400 / 1e6 = 0.0054.
    # Кэш НЕ вычитается из входа: у Anthropic `inputTokens` его не содержит.
    assert cell["spend_usd"] == pytest.approx(0.0054)
    # 1000 + 500 + 200 + 100 = 1800: кэш здесь ДОБАВЛЯЕТСЯ, потому что во входе его нет.
    assert cell["tokens"] == pytest.approx(1800.0)


async def test_daily_costs_dated_snapshot_is_priced_by_its_alias(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Датированный снапшот оценивается по цене алиаса; длиннейший алиас выигрывает."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {
                        "model": "gpt-5-mini-2025-08-07",
                        "inputTokens": 1000,
                        "outputTokens": 1000,
                    },
                )
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    cell = r.json()["items"][0]
    assert cell["provider"] == "OpenAI"
    assert cell["requests"] == 1
    # gpt-5-mini (а НЕ gpt-5): (1000×0.25 + 1000×2.00) / 1e6 = 0.00225.
    # По цене gpt-5 вышло бы (1000×1.25 + 1000×10.00) / 1e6 = 0.01125.
    assert cell["spend_usd"] == pytest.approx(0.00225)
    assert cell["tokens"] == pytest.approx(2000.0)


# --------------------------------------------------------------------------------------
# `null` ≠ 0 ≠ «нет строки» (ADR-092 §4)
# --------------------------------------------------------------------------------------


async def test_daily_costs_fal_tokens_are_zero_not_null(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`Fal` отдаёт `tokens = 0.0` — это ИЗМЕРЕНИЕ, а не пробел (ADR-092 §3).

    Заодно закрыты обе половины media-цены: строка с записанной на сабмите ценой берётся как
    есть, историческая (`provider_cost_usd IS NULL`) восстанавливается из кредитов.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_media_job(
            s, uid, created_at=_utc("2026-03-10", "01:00:00"), provider_cost_usd=0.35
        )
        # Историческая строка: Kling 2.5, 28 кредитов = 2 пачки × $0.35 = $0.70 (ADR-079 §2).
        await _seed_media_job(
            s, uid, created_at=_utc("2026-03-10", "02:00:00"), credits=28, provider_cost_usd=None
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    cell = body["items"][0]
    assert cell["provider"] == "Fal"
    assert cell["requests"] == 2
    assert cell["spend_usd"] == pytest.approx(0.35 + 0.70)
    assert cell["tokens"] == 0.0
    assert cell["tokens"] is not None, "0.0 — измерение; null пометил бы сводку CRM неполной"


async def test_daily_costs_call_without_token_counters_is_counted_but_not_priced(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Вызов с известной моделью, но БЕЗ единого счётчика токенов (`no_token_counts`).

    Умножать цену не на что, и подстановка нулей опубликовала бы «$0.00» как ИЗМЕРЕНИЕ для
    вызова, который наверняка чего-то стоил. Обращение при этом было — значит `requests` растёт.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (_utc("2026-03-10", "01:00:00"), {"model": "gpt-4o"}),
                (_utc("2026-03-10", "02:00:00"), {"model": "gpt-4o", "inputTokens": None}),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    cell = body["items"][0]
    assert cell["provider"] == "OpenAI"
    assert cell["requests"] == 2
    assert cell["spend_usd"] is None
    assert cell["tokens"] is None


async def test_daily_costs_null_when_not_a_single_call_could_be_priced(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Ни один вызов клетки не оценён → клетка ЕСТЬ, `requests > 0`, но обе величины `null`.

    Неизвестная модель попадает ТОЛЬКО в `requests`: подставить ноль за неизвестную цену —
    записать факт, которого нет (ADR-079 §5).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {"model": "gpt-5-pro", "inputTokens": 1000, "outputTokens": 100},
                ),
                (
                    _utc("2026-03-10", "02:00:00"),
                    {"model": "gpt-5-pro", "inputTokens": 2000, "outputTokens": 200},
                ),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1, "клетка обязана существовать: трафик был"
    cell = body["items"][0]
    assert cell["provider"] == "OpenAI"
    assert cell["requests"] == 2
    assert cell["spend_usd"] is None, "0.0 объявил бы вызовы бесплатными — это факт, которого нет"
    assert cell["tokens"] is None


async def test_daily_costs_partially_priced_cell_understates_honestly(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Частично оценённая клетка отдаёт сумму ОЦЕНЁННОГО — не `null` и не сумму с нулём."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                ),
                (
                    _utc("2026-03-10", "02:00:00"),
                    {"model": "gpt-5-pro", "inputTokens": 9000, "outputTokens": 9000},
                ),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    cell = body["items"][0]
    assert cell["requests"] == 2, "неоценимый вызов остаётся видимым в счётчике обращений"
    # Только gpt-4o: (1000×2.50 + 100×10.00) / 1e6 = 0.0035. Неоценимый вклад не подмешан.
    assert cell["spend_usd"] == pytest.approx(0.0035)
    assert cell["tokens"] == pytest.approx(1100.0)


async def test_daily_costs_day_without_traffic_has_no_row_at_all(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """«Нет строки» ≠ нулевая строка: день без трафика внутри периода клетки НЕ даёт."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                ),
                (
                    _utc("2026-03-12", "01:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                ),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-09", date_to="2026-03-13")
    assert r.status_code == 200, r.text
    body = r.json()
    dates = [i["date"] for i in body["items"]]
    assert dates == ["2026-03-10", "2026-03-12"]
    assert body["total"] == 2
    assert "2026-03-11" not in dates, "нулевую строку за день без трафика отдавать запрещено"


async def test_daily_costs_unattributable_call_becomes_unknown_cell(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ADR-092 §6: вызов с `usage`, но без строкового `model`, отдаётся клеткой `"Unknown"`.

    Молчание превратило бы день, весь трафик которого неатрибутируем, в `$0.00` — измеренный
    ноль там, где расход был. Клетки других провайдеров того же дня обязаны остаться
    НЕИЗМЕННЫМИ: неатрибутируемый вызов в них не подмешивается.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_steps(
            s,
            uid,
            [
                (
                    _utc("2026-03-10", "01:00:00"),
                    {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100},
                ),
                (_utc("2026-03-10", "02:00:00"), {"inputTokens": 5000, "outputTokens": 500}),
                (_utc("2026-03-10", "03:00:00"), {"inputTokens": 7000, "outputTokens": 700}),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    by_provider = {i["provider"]: i for i in body["items"]}
    assert set(by_provider) == {"OpenAI", "Unknown"}
    assert body["total"] == 2

    unknown = by_provider["Unknown"]
    assert unknown["requests"] == 2
    assert unknown["spend_usd"] is None
    assert unknown["tokens"] is None

    openai = by_provider["OpenAI"]
    assert openai["requests"] == 1, "неатрибутируемый вызов не подмешивается в верную клетку"
    assert openai["spend_usd"] == pytest.approx(0.0035)
    assert openai["tokens"] == pytest.approx(1100.0)

    # Порядок полный и после появления четвёртого ключа: "OpenAI" < "Unknown".
    assert [i["provider"] for i in body["items"]] == ["OpenAI", "Unknown"]


# --------------------------------------------------------------------------------------
# Границы периода: включительность обеих дат, календарь UTC
# --------------------------------------------------------------------------------------


async def test_daily_costs_period_bounds_are_inclusive_on_utc_calendar(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`date_from 00:00:00Z` и `date_to 23:59:59Z` ВХОДЯТ; `00:00:00Z` следующих суток — нет."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        usage = {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100}
        await _seed_steps(
            s,
            uid,
            [
                (_utc("2026-03-09", "23:59:59"), usage),  # сутки ДО периода — не входит
                (_utc("2026-03-10", "00:00:00"), usage),  # ровно левая граница — входит
                (_utc("2026-03-12", "23:59:59"), usage),  # ровно правая граница — входит
                (_utc("2026-03-13", "00:00:00"), usage),  # первая секунда следующих суток — нет
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-12")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["date"] for i in body["items"]] == ["2026-03-10", "2026-03-12"]
    assert body["total"] == 2
    # Календарь UTC: 23:59:59Z и следующая 00:00:00Z — РАЗНЫЕ дни, а не один.
    assert all(i["requests"] == 1 for i in body["items"])


async def test_daily_costs_single_day_period_is_the_whole_utc_day(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`date_from == date_to` — легальный период в одни сутки, от `00:00:00Z` до `23:59:59Z`."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        usage = {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100}
        await _seed_steps(
            s,
            uid,
            [
                (_utc("2026-03-10", "00:00:00"), usage),
                (_utc("2026-03-10", "23:59:59"), usage),
            ],
        )
        await s.commit()

    r = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["requests"] == 2


async def test_daily_costs_period_of_92_days_is_accepted_93_is_rejected(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Предел периода — 92 дня (`date_to − date_from + 1`); граница проверена с обеих сторон."""
    async with db_sessionmaker() as s:
        await seed_user(s)

    start = datetime.date(2026, 3, 1)
    ok = await _get(
        crm_admin_client,
        date_from=start.isoformat(),
        date_to=(start + datetime.timedelta(days=91)).isoformat(),
    )
    assert ok.status_code == 200, ok.text

    too_long = await _get(
        crm_admin_client,
        date_from=start.isoformat(),
        date_to=(start + datetime.timedelta(days=92)).isoformat(),
    )
    assert too_long.status_code == 400, too_long.text
    assert too_long.status_code != 404


# --------------------------------------------------------------------------------------
# Пагинация
# --------------------------------------------------------------------------------------


async def _seed_four_cells(session: AsyncSession, user_id: uuid.UUID) -> None:
    usage_openai = {"model": "gpt-4o", "inputTokens": 1000, "outputTokens": 100}
    usage_anthropic = {"model": "claude-sonnet-4-5", "inputTokens": 1000, "outputTokens": 100}
    await _seed_steps(
        session,
        user_id,
        [
            (_utc("2026-03-10", "01:00:00"), usage_openai),
            (_utc("2026-03-10", "02:00:00"), usage_anthropic),
            (_utc("2026-03-11", "03:00:00"), usage_openai),
            (_utc("2026-03-11", "04:00:00"), usage_anthropic),
        ],
    )


async def test_daily_costs_total_is_period_wide_and_pages_are_stable(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`total` не зависит от `limit`/`offset`; страницы стабильны и не пересекаются."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        await _seed_four_cells(s, uid)
        await s.commit()

    full = await _get(crm_admin_client, date_from="2026-03-10", date_to="2026-03-11")
    assert full.status_code == 200, full.text
    ordered = [(i["date"], i["provider"]) for i in full.json()["items"]]
    assert full.json()["total"] == 4
    assert len(ordered) == 4

    page = await _get(
        crm_admin_client, date_from="2026-03-10", date_to="2026-03-11", limit=1, offset=1
    )
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 4, "`total` — число клеток за ВЕСЬ период, не размер страницы"
    assert [(i["date"], i["provider"]) for i in page.json()["items"]] == [ordered[1]]

    collected: list[tuple[str, str]] = []
    for offset in (0, 2, 4):
        chunk = await _get(
            crm_admin_client,
            date_from="2026-03-10",
            date_to="2026-03-11",
            limit=2,
            offset=offset,
        )
        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["total"] == 4
        collected.extend((i["date"], i["provider"]) for i in chunk.json()["items"])
    assert collected == ordered, "страницы не пересекаются и покрывают весь порядок ровно раз"

    beyond = await _get(
        crm_admin_client, date_from="2026-03-10", date_to="2026-03-11", limit=10, offset=100
    )
    assert beyond.status_code == 200
    assert beyond.json() == {"total": 4, "items": []}


# --------------------------------------------------------------------------------------
# Коды отказа: `422` — схема, `400` — период, `404` не возникает НИКОГДА
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"date_to": "2026-03-10"}, id="missing-date_from"),
        pytest.param({"date_from": "2026-03-10"}, id="missing-date_to"),
        pytest.param({}, id="missing-both"),
        pytest.param(
            {"date_from": "2026-03-10", "date_to": "2026-03-10", "limit": 0}, id="limit-0"
        ),
        pytest.param(
            {"date_from": "2026-03-10", "date_to": "2026-03-10", "limit": 1001}, id="limit-1001"
        ),
        pytest.param(
            {"date_from": "2026-03-10", "date_to": "2026-03-10", "limit": "abc"}, id="limit-nan"
        ),
        pytest.param(
            {"date_from": "2026-03-10", "date_to": "2026-03-10", "offset": -1}, id="offset-negative"
        ),
    ],
)
async def test_daily_costs_schema_violations_are_422(
    crm_admin_client: AsyncClient, params: dict[str, Any]
) -> None:
    """Отсутствующий обязательный query и `limit`/`offset` вне диапазона → `422`, не `404`."""
    r = await crm_admin_client.get(_PATH, headers=_ADMIN_HEADERS, params=params)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"date_from": "2026-8-1", "date_to": "2026-08-10"}, id="from-not-iso"),
        pytest.param({"date_from": "2026-08-01", "date_to": "10.08.2026"}, id="to-not-iso"),
        pytest.param({"date_from": "yesterday", "date_to": "2026-08-10"}, id="from-garbage"),
        pytest.param(
            {"date_from": "2026-08-10T00:00:00Z", "date_to": "2026-08-11"}, id="from-datetime"
        ),
        pytest.param({"date_from": "2026-08-11", "date_to": "2026-08-10"}, id="from-after-to"),
        pytest.param({"date_from": "2026-01-01", "date_to": "2026-12-31"}, id="period-too-long"),
    ],
)
async def test_daily_costs_period_violations_are_400_never_404(
    crm_admin_client: AsyncClient, params: dict[str, Any]
) -> None:
    """Разобранный, но невалидный период → `400`.

    Регресс-защита: `404` на этом пути означает для CRM «расширение v1.3 не реализовано» —
    она выключила бы опрос бэка целиком (`daily_costs_supported = false`). Поэтому кривой
    формат даты — `400`, а НЕ `404` и не `422`.
    """
    r = await crm_admin_client.get(_PATH, headers=_ADMIN_HEADERS, params=params)
    assert r.status_code == 400, r.text
    assert r.status_code != 404


async def test_daily_costs_never_returns_404_on_any_input(
    crm_admin_client: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Сводная регресс-защита: ни один вход перечня не даёт `404` на этом пути."""
    async with db_sessionmaker() as s:
        await seed_user(s)

    inputs: list[dict[str, Any]] = [
        {},
        {"date_to": "2026-03-10"},
        {"date_from": "2026-03-10"},
        {"date_from": "2026-3-1", "date_to": "2026-03-10"},
        {"date_from": "2026-03-11", "date_to": "2026-03-10"},
        {"date_from": "2026-01-01", "date_to": "2026-12-31"},
        {"date_from": "2026-03-10", "date_to": "2026-03-10", "limit": 0},
        {"date_from": "2026-03-10", "date_to": "2026-03-10", "offset": -1},
        {"date_from": "2026-03-10", "date_to": "2026-03-10"},
    ]
    codes = []
    for params in inputs:
        r = await crm_admin_client.get(_PATH, headers=_ADMIN_HEADERS, params=params)
        codes.append(r.status_code)
    assert 404 not in codes, f"404 выключил бы опрос бэка со стороны CRM; получены {codes}"
    assert set(codes) <= {200, 400, 422}


# --------------------------------------------------------------------------------------
# Авторизация
# --------------------------------------------------------------------------------------


async def test_daily_costs_requires_admin_credentials(crm_admin_client: AsyncClient) -> None:
    """Ни один заголовок → `403`; переданный, но неверный → `401`."""
    missing = await crm_admin_client.get(
        _PATH, params={"date_from": "2026-03-10", "date_to": "2026-03-10"}
    )
    assert missing.status_code == 403

    wrong = await crm_admin_client.get(
        _PATH,
        headers={"X-Admin-Key": "wrong"},
        params={"date_from": "2026-03-10", "date_to": "2026-03-10"},
    )
    assert wrong.status_code == 401
