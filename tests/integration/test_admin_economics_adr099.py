"""Integration: поверхность экономики и настроек инстанса (ADR-099).

Контракт CRM v1.1 + v1.2 + v1.4 + v1.5.

Реальный PostgreSQL (testcontainers), реальные восемь ручек, реальная миграция `0033`. Дом
сценариев — [modules/admin/09-testing.md §Integration — экономика и настройки инстанса].

Сквозная цепь «правка → пользовательская ручка» живёт в
``tests/integration/test_admin_economics_wiring_adr099.py``: компонентный тест резолвера её не
доказывает, а этот файл проверяет ПИШУЩУЮ половину — контракт, коды отказа, аудит и лимит.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, seed_user

_ADMIN_SECRET = "econ-admin-key-integration-0123456789abcdef"
_H = {"X-Admin-Key": _ADMIN_SECRET, "X-Admin-Actor": "operator@example.com"}

_ONE_TIME_ID = "tokens_100"
_CP_SUB_ID = "cp.sub.month"
_ADAPTY_SUB_ID = "adapty.sub.month"
_CATALOG_BARE_ID = "showcase.bare"
# Тот же витринный источник, но с ОБЪЯВЛЕННЫМ классом и без `credits`: он разводит две ветки
# отказа «данные источника», которые на одной записи неразличимы — `purchase_kind` проверяется
# первым, и без второй записи ветка `tokens` недостижима с этого источника вовсе.
_CATALOG_NO_CREDITS_ID = "showcase.no-credits"
# Ещё две витринные строки того же источника, различающиеся ТОЛЬКО сохранённым числом. Обе
# существуют ради archived-правки: прежняя редакция валидировала СОХРАНЁННОЕ значение и роняла
# `422` на правке, которая этого числа не касается вовсе (ADR-099 §6.1, §7.1 — три экземпляра
# пробела, и это второй и третий).
_CATALOG_ZERO_CREDITS_ID = "showcase.zero-credits"
_CATALOG_OVER_MAX_ID = "showcase.over-max"
# Строка с ЧИСЛОМ, но БЕЗ класса — единственная координата, на которой видно правило 4 §6.1:
# `tokens` гасится НЕИЗВЕСТНЫМ КЛАССОМ, а не отсутствием числа. На строке без числа это правило
# неотличимо от «числа и так нет», и кейс проходил бы вхолостую.
_CATALOG_CREDITS_NO_KIND_ID = "showcase.credits-no-kind"
_CREDITS_NO_KIND_VALUE = 300
# Заведомо выше `limits.product_tokens_max` (1 000 000): число, которое оператор прислать не
# может, но которое МОЖЕТ лежать в `.env` витрины.
_OVER_MAX_CREDITS = 2_000_000
# Заведомо устаревшая отметка версии: любая существующая строка новее её.
_STALE_STAMP = "2020-01-01T00:00:00Z"

# Восемь путей поверхности. Перечень собран ОДИН раз и питает сразу три сводных кейса — сводку
# «ни один вход не даёт 404», позитивный лимит и проверку заголовков: список, переписанный в
# каждый кейс отдельно, разошёлся бы с реализацией в том кейсе, который забыли поправить.
_SURFACE: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/v1/admin/capabilities", None),
    ("GET", "/v1/admin/products", None),
    (
        "POST",
        "/v1/admin/products",
        {"product_id": "x", "name": "x", "purchase_kind": "one_time", "tokens": 1},
    ),
    ("PATCH", "/v1/admin/products/no-such-product", {"tokens": 5}),
    ("GET", "/v1/admin/pricing", None),
    ("PATCH", "/v1/admin/pricing/no-such-tariff", {"tokens": 5}),
    ("GET", "/v1/admin/settings", None),
    ("PATCH", "/v1/admin/settings/no.such.setting", {"value": True}),
)


class _FakePipeline:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self._ops: list[tuple[str, str]] = []

    def zremrangebyscore(self, key: str, *_a: Any) -> None:
        self._ops.append(("noop", key))

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        self._ops.append(("noop", key))

    def zcard(self, key: str) -> None:
        self._ops.append(("card", key))

    def expire(self, key: str, _ttl: int) -> None:
        self._ops.append(("noop", key))

    async def execute(self) -> list[Any]:
        return [self._counts.get(key, 0) if op == "card" else None for op, key in self._ops]

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _FakeRedis:
    """Счётчик корзин в памяти: лимитер работает НАСТОЯЩИЙ, отдельно по каждому ключу.

    Fail-open-ветка (нет Redis) сделала бы позитивный кейс `429` недостижимым, а «страница из 6
    вызовов не даёт 429» проходит и при полностью отсутствующем лимитере — то есть проверяет
    форму, а не факт (ADR-099 §10.1).
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self.counts)


@pytest.fixture
async def econ(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[tuple[AsyncClient, _FakeRedis]]:
    from app import deps
    from app.api_gateway import rate_limit
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    monkeypatch.setenv("ADMIN_API_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_API_SECRET_PREV", "")
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("TOKEN_PRODUCTS", f'{{"{_ONE_TIME_ID}": 100}}')
    monkeypatch.setenv("CLOUDPAYMENTS_PRODUCT_TOKENS", f'{{"{_CP_SUB_ID}": 444}}')
    monkeypatch.setenv("ADAPTY_PRODUCT_TOKENS", f'{{"{_ADAPTY_SUB_ID}": 555}}')
    monkeypatch.setenv(
        "PRODUCTS_CATALOG",
        f'[{{"productId": "{_CATALOG_BARE_ID}", "title": "Без класса"}},'
        f' {{"productId": "{_CATALOG_NO_CREDITS_ID}", "title": "Без кредитов",'
        f' "kind": "tokens"}},'
        f' {{"productId": "{_CATALOG_ZERO_CREDITS_ID}", "title": "Ноль кредитов",'
        f' "kind": "tokens", "credits": 0}},'
        f' {{"productId": "{_CATALOG_OVER_MAX_ID}", "title": "Выше потолка",'
        f' "kind": "tokens", "credits": {_OVER_MAX_CREDITS}}},'
        f' {{"productId": "{_CATALOG_CREDITS_NO_KIND_ID}", "title": "Число без класса",'
        f' "credits": {_CREDITS_NO_KIND_VALUE}}}]',
    )
    monkeypatch.setenv("FAL_API_KEY", "fal-test-key")
    get_settings.cache_clear()

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    redis = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: redis)

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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, redis

    get_settings.cache_clear()


async def _audit(maker: async_sessionmaker[AsyncSession], event_type: str) -> list[Any]:
    async with maker() as s:
        return (
            await s.execute(
                text("SELECT payload FROM audit_logs WHERE event_type=:t ORDER BY created_at"),
                {"t": event_type},
            )
        ).all()


async def _audit_count(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as s:
        return int(await s.scalar(text("SELECT count(*) FROM audit_logs")) or 0)


def _emitted_reasons(module: Any, producers: dict[str, int]) -> set[str]:
    """Значения `reason`, которые модуль ФАКТИЧЕСКИ передаёт своим продюсерам.

    Разбор AST, а не текстовый поиск: искомое — АРГУМЕНТ конкретного вызова, и поиск по
    подстроке одинаково нашёл бы его в докстроке, в комментарии и в сигнатуре функции. Тернарная
    форма (`out_of_range if ... else type_mismatch`) даёт ДВА значения одной ветки, и обе
    половины обязаны попасть в множество — иначе кейс объявил бы половину лейблов мёртвыми.

    ``producers`` — имя вызываемого → позиция аргумента `reason` в нём.
    """
    import ast
    import pathlib

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    found: set[str] = set()

    def _values(node: ast.expr) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            return _values(node.body) + _values(node.orelse)
        if isinstance(node, ast.Name):
            value = getattr(module, node.id, None)
            return [value] if isinstance(value, str) else []
        if isinstance(node, ast.Attribute):
            value = getattr(module, node.attr, None)
            return [value] if isinstance(value, str) else []
        return []

    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        index = producers.get(name or "")
        if index is None or len(node.args) <= index:
            continue
        found.update(_values(node.args[index]))
    assert found, module.__name__  # продюсер, у которого не нашлось ни одного вызова, — дефект
    return found


def _chat_tariff_id() -> str:
    settings = get_settings()
    model = settings.default_model()
    return f"chat:{settings.credits_provider_for_model(model)}:{model}"


# ============================== capabilities ================================================
@pytest.mark.asyncio
async def test_capabilities_declares_only_implemented_features(econ: Any) -> None:
    client, _ = econ

    body = (await client.get("/v1/admin/capabilities", headers=_H)).json()

    assert body["contract_version"] == 1
    assert set(body["features"]) == {
        "products.read",
        "products.write_tokens",
        "products.write_archived",
        "products.create",
        "pricing.read",
        "pricing.write_tokens",
        "settings.write",
        "requests.costs",
    }
    # Парной `settings.read` в контракте не существует — и мы её не выдумываем.
    assert "settings.read" not in body["features"]


@pytest.mark.asyncio
async def test_every_declared_feature_has_a_reachable_path(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Кейс-детектор: `features` — единственный источник права записи, и он fail-closed.

    Каждой объявленной возможности сопоставлен РАБОЧИЙ путь; удаление роутера роняет кейс,
    потому что путь начнёт отвечать `404`.
    """
    client, _ = econ
    async with db_sessionmaker() as s:
        user_id = await seed_user(s)
    probes = {
        "products.read": ("GET", "/v1/admin/products", None),
        "products.create": (
            "POST",
            "/v1/admin/products",
            {"product_id": "probe.create", "name": "п", "purchase_kind": "one_time", "tokens": 1},
        ),
        "products.write_tokens": ("PATCH", f"/v1/admin/products/{_ONE_TIME_ID}", {"tokens": 101}),
        "products.write_archived": (
            "PATCH",
            f"/v1/admin/products/{_ONE_TIME_ID}",
            {"archived": True},
        ),
        "pricing.read": ("GET", "/v1/admin/pricing", None),
        "pricing.write_tokens": ("PATCH", f"/v1/admin/pricing/{_chat_tariff_id()}", {"tokens": 2}),
        "settings.write": ("PATCH", "/v1/admin/settings/chat.characters_enabled", {"value": False}),
        # `requests.costs` объявляет уже существующий путь v1.3; проба идёт по РЕАЛЬНОМУ
        # пользователю, потому что на нём `404` означает «нет такого пользователя», а не
        # «путь не реализован» — два разных смысла одного кода на соседних поверхностях.
        "requests.costs": ("GET", f"/v1/admin/users/{user_id}/requests", None),
    }
    declared = set((await client.get("/v1/admin/capabilities", headers=_H)).json()["features"])
    assert declared == set(probes)

    for feature, (method, path, body) in probes.items():
        response = await client.request(method, path, json=body, headers=_H)
        assert response.status_code != 404, f"{feature}: путь объявлен, но не реализован"


@pytest.mark.asyncio
async def test_limits_carry_exactly_three_keys_and_never_the_avatar_one(econ: Any) -> None:
    """⛔ Предикат `limits` — НАЛИЧИЕ ключа.

    Ключ `product_avatar_tokens_max` даже со значением `0` объявил бы вторую валюту существующей,
    и CRM нарисовала бы контрол, который не может предложить ни одного значения. Ассерт — на
    ОТСУТСТВИЕ ключа, а не на значение.
    """
    client, _ = econ

    limits = (await client.get("/v1/admin/capabilities", headers=_H)).json()["limits"]

    assert set(limits) == {"product_tokens_max", "tariff_tokens_max", "tariff_decimal_places"}
    assert "product_avatar_tokens_max" not in limits
    assert limits["tariff_decimal_places"] == 0  # кошелёк целочисленный


@pytest.mark.asyncio
async def test_the_declared_window_equals_the_one_returned_by_every_write(econ: Any) -> None:
    """`cache_effective_after_seconds` == `effective_after_seconds` == настроенное окно."""
    client, _ = econ
    window = get_settings().admin_overrides_refresh_window()

    caps = (await client.get("/v1/admin/capabilities", headers=_H)).json()
    created = await client.post(
        "/v1/admin/products",
        json={"product_id": "window.probe", "name": "п", "purchase_kind": "one_time", "tokens": 5},
        headers=_H,
    )
    patched = await client.patch(
        f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 3}, headers=_H
    )
    setting = await client.patch(
        "/v1/admin/settings/chat.characters_enabled", json={"value": False}, headers=_H
    )

    assert caps["cache_effective_after_seconds"] == window
    assert created.json()["effective_after_seconds"] == window
    assert patched.json()["effective_after_seconds"] == window
    assert setting.json()["effective_after_seconds"] == window


@pytest.mark.asyncio
async def test_a_zero_refresh_window_is_declared_as_one_second_not_as_zero(
    econ: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ADMIN_OVERRIDES_REFRESH_SECONDS=0` не выключает обновитель.

    Наружу уходит ФАКТИЧЕСКОЕ окно, по которому тикает обновитель.
    """
    client, _ = econ
    settings = get_settings()
    original = settings.admin_overrides_refresh_seconds
    settings.admin_overrides_refresh_seconds = 0
    try:
        caps = (await client.get("/v1/admin/capabilities", headers=_H)).json()
        patched = await client.patch(
            f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 3}, headers=_H
        )
    finally:
        settings.admin_overrides_refresh_seconds = original

    assert caps["cache_effective_after_seconds"] == 1
    assert patched.json()["effective_after_seconds"] == 1


# ============================== продукты ====================================================
@pytest.mark.asyncio
async def test_a_created_product_appears_in_the_admin_catalog_with_its_class(econ: Any) -> None:
    client, _ = econ

    created = await client.post(
        "/v1/admin/products",
        json={
            "product_id": "operator.pack",
            "name": "Пакет оператора",
            "purchase_kind": "one_time",
            "tokens": 300,
        },
        headers=_H,
    )

    assert created.status_code == 201, created.text
    assert created.json()["tokens"] == 300
    assert created.json()["avatar_tokens"] is None
    listed = {
        item["product_id"]: item
        for item in (await client.get("/v1/admin/products", headers=_H)).json()["items"]
    }
    assert listed["operator.pack"]["tokens"] == 300
    assert listed["operator.pack"]["purchase_kind"] == "one_time"
    assert listed["operator.pack"]["archived"] is False
    assert listed["operator.pack"]["updated_at"] is not None


@pytest.mark.asyncio
async def test_adapty_products_are_visible_in_the_admin_catalog(econ: Any) -> None:
    """Аддитивное расширение §6: прежде инстанс по ним начислял, а в CRM их не было."""
    client, _ = econ

    items = (await client.get("/v1/admin/products", headers=_H)).json()["items"]

    assert _ADAPTY_SUB_ID in {item["product_id"] for item in items}


@pytest.mark.asyncio
async def test_price_and_period_are_always_null_and_updated_at_is_empty_until_edited(
    econ: Any,
) -> None:
    client, _ = econ

    items = (await client.get("/v1/admin/products", headers=_H)).json()["items"]

    assert items
    for item in items:
        assert item["price"] is None
        assert item["period"] is None
        assert item["avatar_tokens"] is None
        assert item["grantable"] is True
        assert item["updated_at"] is None


@pytest.mark.asyncio
async def test_a_duplicate_product_id_is_400_not_409_and_not_500(econ: Any) -> None:
    """`409` на этом пути занят смыслом «значение изменил другой оператор».

    Оператор получил бы «обновите страницу» там, где нужно СМЕНИТЬ ИДЕНТИФИКАТОР.
    """
    client, _ = econ
    body = {
        "product_id": _ONE_TIME_ID,
        "name": "Дубль env-продукта",
        "purchase_kind": "one_time",
        "tokens": 10,
    }

    env_duplicate = await client.post("/v1/admin/products", json=body, headers=_H)
    await client.post(
        "/v1/admin/products",
        json={"product_id": "made.twice", "name": "п", "purchase_kind": "one_time", "tokens": 1},
        headers=_H,
    )
    created_duplicate = await client.post(
        "/v1/admin/products",
        json={"product_id": "made.twice", "name": "п", "purchase_kind": "one_time", "tokens": 1},
        headers=_H,
    )

    assert env_duplicate.status_code == 400, env_duplicate.text
    assert created_duplicate.status_code == 400, created_duplicate.text


@pytest.mark.asyncio
async def test_concurrent_creates_of_one_product_id_give_one_201_and_one_400(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Гонка на первичном ключе: предписанный контрактом код — `400`, а НЕ `500` и не `409`.

    Проверка дубликата и вставка не атомарны, и два одновременных `POST` расходятся именно здесь.
    В аудите обязана остаться РОВНО ОДНА запись создания: провалившаяся вставка следа не оставляет.
    """
    client, _ = econ
    payload = {
        "product_id": "race.product",
        "name": "Гонка",
        "purchase_kind": "one_time",
        "tokens": 7,
    }

    first, second = await asyncio.gather(
        client.post("/v1/admin/products", json=payload, headers=_H),
        client.post("/v1/admin/products", json=payload, headers=_H),
    )

    assert sorted([first.status_code, second.status_code]) == [201, 400]
    assert len(await _audit(db_sessionmaker, "admin_product_created")) == 1
    async with db_sessionmaker() as s:
        rows = int(
            await s.scalar(
                text("SELECT count(*) FROM admin_products WHERE product_id='race.product'")
            )
            or 0
        )
    assert rows == 1


@pytest.mark.asyncio
async def test_avatar_tokens_is_rejected_with_400_on_both_write_paths(econ: Any) -> None:
    """Молча принять и проигнорировать нельзя — оператор считал бы величину сохранённой."""
    client, _ = econ

    created = await client.post(
        "/v1/admin/products",
        json={
            "product_id": "avatar.probe",
            "name": "п",
            "purchase_kind": "one_time",
            "tokens": 5,
            "avatar_tokens": 10,
        },
        headers=_H,
    )
    patched = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 5, "avatar_tokens": 10}, headers=_H
    )

    assert created.status_code == 400, created.text
    assert patched.status_code == 400, patched.text


@pytest.mark.asyncio
async def test_patch_of_an_unknown_product_is_400_and_creates_nothing(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`404` в этом контракте означает «расширение не реализовано» — отдать его нельзя."""
    client, _ = econ

    response = await client.patch(
        "/v1/admin/products/never.existed", json={"tokens": 5}, headers=_H
    )

    assert response.status_code == 400, response.text
    async with db_sessionmaker() as s:
        assert (
            int(await s.scalar(text("SELECT count(*) FROM admin_products")) or 0) == 0
        )  # `PATCH` НИКОГДА не создаёт


@pytest.mark.asyncio
async def test_patch_of_a_source_without_a_class_is_refused_with_a_reason(econ: Any) -> None:
    """Материализовать недостающий класс догадкой ЗАПРЕЩЕНО (§6): у отказа нет пути вперёд.

    Именно поэтому у двух отказов этого правила разная судьба, и оператору она названа.
    """
    client, _ = econ

    response = await client.patch(
        f"/v1/admin/products/{_CATALOG_BARE_ID}", json={"tokens": 5}, headers=_H
    )

    assert response.status_code == 400, response.text
    assert "класс" in response.json()["detail"]


@pytest.mark.asyncio
async def test_patch_without_any_field_is_422(econ: Any) -> None:
    client, _ = econ

    response = await client.patch(f"/v1/admin/products/{_ONE_TIME_ID}", json={}, headers=_H)

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_post_then_patch_inside_the_refresh_window_succeeds(econ: Any) -> None:
    """⚠️ Решение «продукт неизвестен» принимается ПО БД, а не по снимку.

    Созданный оператором продукт существует ТОЛЬКО в оверлее, и `PATCH` в другом процессе внутри
    окна получил бы `400 «продукт неизвестен»` по продукту, который только что создан. Кейс
    имитирует именно это: снимок процесса сброшен до состояния «правки ещё не видно».
    """
    from app.instance_config import reset_snapshot

    client, _ = econ
    await client.post(
        "/v1/admin/products",
        json={"product_id": "fresh.pack", "name": "п", "purchase_kind": "one_time", "tokens": 5},
        headers=_H,
    )
    reset_snapshot()  # снимок отстал: правка в БД есть, в памяти процесса — нет

    response = await client.patch("/v1/admin/products/fresh.pack", json={"tokens": 9}, headers=_H)

    assert response.status_code == 200, response.text
    assert response.json()["tokens"] == 9
    assert response.json()["previous_tokens"] == 5


@pytest.mark.asyncio
async def test_an_optimistic_conflict_is_checked_against_the_db_row_to_the_second(
    econ: Any,
) -> None:
    """Сверка идёт по СТРОКЕ В БД и с точностью до СЕКУНДЫ — предикат проверяется с ОБЕИХ сторон.

    (а) против ложного конфликта: оператор присылает обратно ровно то, что прочитал, и сравнение
    с микросекундами БД отвергало бы КАЖДУЮ условную правку — то есть ломало бы механизм, который
    защищает. Отметка с дробной частью обязана СОВПАСТЬ.
    (б) против пропуска: отметка другой секунды — конфликт.

    Обе стороны строятся АРИФМЕТИКОЙ от прочитанной отметки, а не ожиданием стенных часов:
    кейс, полагающийся на `sleep`, был бы флапающим.
    """
    client, _ = econ
    first = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 111}, headers=_H
    )
    stamp = first.json()["updated_at"]
    read_at = datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    sub_second = read_at.replace(microsecond=999_000).isoformat().replace("+00:00", "Z")
    other_second = (read_at - datetime.timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%SZ")

    matching = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}",
        json={"tokens": 222, "if_updated_at": sub_second},
        headers=_H,
    )
    stale = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}",
        json={"tokens": 333, "if_updated_at": other_second},
        headers=_H,
    )

    assert matching.status_code == 200, matching.text
    assert matching.json()["tokens"] == 222
    assert stale.status_code == 409, stale.text
    # …и отвергнутая правка ничего не изменила.
    listed = {
        item["product_id"]: item
        for item in (await client.get("/v1/admin/products", headers=_H)).json()["items"]
    }
    assert listed[_ONE_TIME_ID]["tokens"] == 222


@pytest.mark.asyncio
async def test_repeating_the_same_value_reports_changed_false(econ: Any) -> None:
    client, _ = econ
    await client.patch(f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 150}, headers=_H)

    repeat = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 150}, headers=_H
    )

    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["changed"] is False


@pytest.mark.asyncio
async def test_the_two_product_token_bounds_answer_two_different_questions(econ: Any) -> None:
    """Верхняя и нижняя границы `tokens` живут в РАЗНЫХ мирах (ADR-099 §11) — код и лейбл разные.

    Верхняя ОБЪЯВЛЕНА нами в `limits.product_tokens_max`: CRM могла отклонить значение в своей
    форме ⇒ `422` + `out_of_range`. Нижняя зависит от `purchase_kind` и в замороженном наборе
    `limits` невыразима в принципе (ключа под нижнюю границу там нет, а «зависит от второго поля
    того же тела» не выражается ничем) ⇒ `400` + `undeclared_bound`: клиент прислал законное по
    всему, что мы объявили.

    ⚠️ Кейс падает при откате к прежней паре (`422`/`out_of_range` на нуле) в ОБЕ стороны: и по
    коду, и по лейблу. Проверять только код было бы мало — лейбл и есть единственный носитель
    практического смысла серии `out_of_range` («клиентская проверка CRM не сработала»), и
    подмешать в неё отказ, который CRM предотвратить не могла, значит обесценить её целиком.
    """
    client, _ = econ
    limits = (await client.get("/v1/admin/capabilities", headers=_H)).json()["limits"]
    before_out_of_range = _rejected("out_of_range")
    before_undeclared = _rejected("undeclared_bound")

    too_big = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}",
        json={"tokens": limits["product_tokens_max"] + 1},
        headers=_H,
    )
    zero_one_time = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 0}, headers=_H
    )
    zero_subscription = await client.patch(
        f"/v1/admin/products/{_CP_SUB_ID}", json={"tokens": 0}, headers=_H
    )

    assert too_big.status_code == 422, too_big.text
    assert zero_one_time.status_code == 400, zero_one_time.text
    # …а у подписки ноль допустим: нижняя граница ЗАВИСИТ ОТ КЛАССА (CHECK в БД разводит их так
    # же). Именно эта зависимость и делает границу невыразимой в `limits`.
    assert zero_subscription.status_code == 200, zero_subscription.text
    assert _rejected("out_of_range") == before_out_of_range + 1
    assert _rejected("undeclared_bound") == before_undeclared + 1


@pytest.mark.asyncio
async def test_a_negative_product_tokens_is_422_from_the_typed_schema_not_from_the_service(
    econ: Any,
) -> None:
    """Граница `>= 0` — САМОГО контракта, её дом схема ручки (§11), поэтому `422` без лейбла.

    Соседство с нулём несущее: `-1` и `0` дают РАЗНЫЕ коды, и слить их значило бы потерять
    единственный практический сигнал `422` — «клиентская проверка CRM не сработала».
    """
    client, _ = econ
    before = {reason: _rejected(reason) for reason in _REJECT_REASONS}

    response = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": -1}, headers=_H
    )

    assert response.status_code == 422, response.text
    # До сервиса запрос не дошёл — ни одна серия нашего словаря не тронута.
    assert {reason: _rejected(reason) for reason in _REJECT_REASONS} == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "product_id",
    [
        _CATALOG_BARE_ID,  # источник не задаёт ни класса, ни числа
        _CATALOG_NO_CREDITS_ID,  # класс есть, числа нет
        _CATALOG_ZERO_CREDITS_ID,  # число есть и оно НИЖЕ нашей нижней границы
        _CATALOG_OVER_MAX_ID,  # число есть и оно ВЫШЕ объявленного `product_tokens_max`
    ],
)
async def test_archiving_never_fails_no_matter_how_incomplete_or_illegal_the_source_row_is(
    econ: Any, product_id: str
) -> None:
    """⛔ У archived-правки НЕТ ветки отказа ни при какой полноте источника (ADR-099 §6.1).

    Это главный класс функции, а не крайний случай: `CRM ADR-073 §Контекст` называет архивацию
    основным сценарием — 30 позиций из 39. Прежняя редакция отвергала её на каждой из четырёх
    строк ниже: у первых двух колонка оверлея была `NOT NULL` и материализовать значение было
    нечем, у вторых двух валидация трогала СОХРАНЁННОЕ, неприсланное число. Оба отказа — ровно
    то, что fail-closed норма `CRM ADR-072 §7` запрещает: контрол показан, операция падает.

    ⚠️ Кейс падает при возврате ЛЮБОЙ проверки класса или числа на этот путь, и ассерт по
    словарю отказов — не украшение: `200` можно получить и молча ничего не записав, а нулевая
    дельта по ВСЕМ семи сериям утверждает, что путь не только прошёл, но и не отверг ничего
    попутно.
    """
    client, _ = econ
    before = {reason: _rejected(reason) for reason in _REJECT_REASONS}

    response = await client.patch(
        f"/v1/admin/products/{product_id}", json={"archived": True}, headers=_H
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["archived"] is True
    assert body["changed"] is True
    assert body["updated_at"] is not None
    assert {reason: _rejected(reason) for reason in _REJECT_REASONS} == before
    # …и строка каталога действительно помечена, а не «принята и не сохранена».
    listed = {
        item["product_id"]: item
        for item in (await client.get("/v1/admin/products", headers=_H)).json()["items"]
    }
    assert listed[product_id]["archived"] is True


@pytest.mark.asyncio
async def test_a_field_the_patch_did_not_carry_keeps_following_its_source(econ: Any) -> None:
    """Слияние ПОФИЛДОВОЕ: `NULL` в колонке = «оверлей этого поля не задаёт» (ADR-099 §6.1).

    Правка одного `archived` не имеет права затереть пустотой ни число, ни класс, ни название:
    иначе операция, витрины не касавшаяся, обнулила бы цену — принята и применена не туда.
    Кейс падает при возврате к безусловной подстановке всех полей строки оверлея.
    """
    client, _ = econ

    archived = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"archived": True}, headers=_H
    )
    assert archived.status_code == 200, archived.text
    listed = {
        item["product_id"]: item
        for item in (await client.get("/v1/admin/products", headers=_H)).json()["items"]
    }

    # Число и класс — по-прежнему ИСТОЧНИКА (`TOKEN_PRODUCTS`), название тоже.
    assert listed[_ONE_TIME_ID]["tokens"] == 100
    assert listed[_ONE_TIME_ID]["purchase_kind"] == "one_time"
    assert listed[_ONE_TIME_ID]["name"] == "100 tokens"
    assert archived.json()["tokens"] == 100
    # …а заданное поле оверлея побеждает — и следующая правка числа его не теряет.
    priced = await client.patch(
        f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 321}, headers=_H
    )
    assert priced.status_code == 200, priced.text
    assert priced.json()["tokens"] == 321
    assert priced.json()["archived"] is True


@pytest.mark.asyncio
async def test_tokens_is_null_when_the_class_is_unknown_even_though_the_source_has_a_number(
    econ: Any,
) -> None:
    """Правило 4 §6.1: число принадлежит ПАРЕ «класс + число» — без класса отдаётся `null`.

    ⚠️ Проверяется на строке, у которой число ЕСТЬ: на строке без числа правило неотличимо от
    «числа и так нет», и кейс проходил бы вхолостую. Отдать здесь `300` значило бы предложить
    CRM правку, которую мы обязаны отвергнуть, — она рисует карандаш ровно при непустом
    `tokens`, и оператор получил бы `400` на контрол, который ему показали (§7.1).
    """
    client, _ = econ

    listed = {
        item["product_id"]: item
        for item in (await client.get("/v1/admin/products", headers=_H)).json()["items"]
    }

    row = listed[_CATALOG_CREDITS_NO_KIND_ID]
    assert row["purchase_kind"] is None
    assert row["tokens"] is None
    # …и то же значение уходит в `previous_tokens` пишущего ответа.
    archived = await client.patch(
        f"/v1/admin/products/{_CATALOG_CREDITS_NO_KIND_ID}",
        json={"archived": True},
        headers=_H,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["tokens"] is None
    assert archived.json()["previous_tokens"] is None
    # Контраст: у строки С КЛАССОМ число отдаётся — значит `null` выше следует из класса, а не
    # из того, что мы вообще перестали отдавать `tokens`.
    assert listed[_ONE_TIME_ID]["tokens"] == 100


# ============================== тарифы ======================================================
@pytest.mark.asyncio
async def test_pricing_declares_a_row_for_chat_photo_and_video_with_honest_units(econ: Any) -> None:
    client, _ = econ

    items = (await client.get("/v1/admin/pricing", headers=_H)).json()["items"]

    units = {item["kind"]: item["unit"] for item in items}
    assert units["chat"] == "message"
    assert (
        units["photo"] == "image"
    )  # единица объявлена честно: запуск с numImages=4 стоит вчетверо
    assert units["video"] == "generation"  # не `second`: сервис тарифицирует пачками
    assert len(items) <= 500  # контрактный потолок списка
    assert all(item["updated_at"] is None for item in items)


@pytest.mark.asyncio
async def test_a_zero_tariff_is_impossible_and_three_refusals_come_from_three_places(
    econ: Any,
) -> None:
    """Ноль отвергается не «для строгости»: гейт баланса его пропускает, а списание берёт ноль.

    Три отказа одного поля приходят из ТРЁХ РАЗНЫХ мест, и ADR-099 §11 требует их различать:

    * `-1` — граница **самого контракта** (`tokens: number >= 0`), её дом — типизированная схема
      ручки, отказ отдаёт штатный конвейер FastAPI ⇒ `422` **без** лейбла нашего словаря;
    * `0` — граница, которой в нашем объявлении **нет и быть не может** (в замороженном наборе
      `limits` ключа под нижнюю границу нет вовсе) ⇒ `400` + `undeclared_bound`;
    * выше `limits.tariff_tokens_max` — граница, **объявленная** нами ⇒ `422` + `out_of_range`.

    ⚠️ Соседство `-1` и `0` — несущее: оба «маленькие числа», но клиент о первом знал из своего
    же контракта, а о втором знать было неоткуда, и `422` на нуле утверждал бы, что CRM могла
    отклонить его в форме. Кейс падает при откате нуля к `422`/`out_of_range` и при переносе
    границы `>= 0` из схемы в сервис.
    """
    client, _ = econ
    limits = (await client.get("/v1/admin/capabilities", headers=_H)).json()["limits"]
    before_out_of_range = _rejected("out_of_range", "tariffs")
    before_undeclared = _rejected("undeclared_bound", "tariffs")

    zero = await client.patch(
        f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 0}, headers=_H
    )
    negative = await client.patch(
        f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": -1}, headers=_H
    )
    too_big = await client.patch(
        f"/v1/admin/pricing/{_chat_tariff_id()}",
        json={"tokens": limits["tariff_tokens_max"] + 1},
        headers=_H,
    )

    assert zero.status_code == 400, zero.text
    # `-1` не доходит до сервиса вовсе: `ge=0` стоит в схеме ручки, поэтому наш словарь лейблов
    # об этом отказе не высказывается — и ассерт ниже это утверждает, а не умалчивает.
    assert negative.status_code == 422, negative.text
    assert too_big.status_code == 422, too_big.text
    assert _rejected("undeclared_bound", "tariffs") == before_undeclared + 1
    assert _rejected("out_of_range", "tariffs") == before_out_of_range + 1


@pytest.mark.asyncio
async def test_a_fractional_tariff_is_rejected_by_the_typed_schema(econ: Any) -> None:
    client, _ = econ

    response = await client.patch(
        f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 1.5}, headers=_H
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_patch_of_an_unknown_tariff_is_400_and_creates_nothing(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = econ

    response = await client.patch(
        "/v1/admin/pricing/video:never-existed:1080p:8:1", json={"tokens": 5}, headers=_H
    )

    assert response.status_code == 400, response.text
    async with db_sessionmaker() as s:
        assert int(await s.scalar(text("SELECT count(*) FROM admin_tariffs")) or 0) == 0


@pytest.mark.asyncio
async def test_the_zero_check_is_also_enforced_by_the_database(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Второй барьер §5.1: величина, обнуление которой делает генерацию бесплатной, не может
    зависеть от того, ошибся ли оператор в поле формы."""
    from sqlalchemy.exc import IntegrityError

    async with db_sessionmaker() as s:
        with pytest.raises(IntegrityError):
            await s.execute(
                text("INSERT INTO admin_tariffs (tariff_id, tokens) VALUES ('chat:openai:x', 0)")
            )
            await s.commit()


# ============================== настройки ===================================================
@pytest.mark.asyncio
async def test_settings_are_self_describing_and_carry_a_typed_value(econ: Any) -> None:
    client, _ = econ

    items = (await client.get("/v1/admin/settings", headers=_H)).json()["items"]

    assert items
    for item in items:
        assert item["setting_id"] and item["type"] and item["label"]
        assert item["updated_at"] is None
        if item["type"] == "bool":
            assert isinstance(item["value"], bool)
        elif item["type"] == "multi_enum":
            assert isinstance(item["value"], list)
        else:
            assert isinstance(item["value"], str)


@pytest.mark.asyncio
async def test_the_settings_surface_carries_no_forbidden_class_of_variable(econ: Any) -> None:
    """Негативный контракт-тест поверхности (§8.2), обязательный по 09-testing.md.

    Соблюдение перечня — НАША обязанность: CRM не отличит секрет от флага в самоописываемом
    ответе и не пытается.
    """
    client, _ = econ
    forbidden = (
        "api_key",
        "secret",
        "private_key",
        "master_key",
        "token_products",
        "credit_cost",
        "database",
        "redis",
        "rate_limit",
        "webhook",
        "jwt",
        "bundle",
    )

    items = (await client.get("/v1/admin/settings", headers=_H)).json()["items"]

    for item in items:
        for token in forbidden:
            assert token not in item["setting_id"].lower()
        assert _ADMIN_SECRET not in str(item["value"])


@pytest.mark.asyncio
async def test_block_categories_declares_max_length_and_rejects_a_longer_value(econ: Any) -> None:
    client, _ = econ
    items = {
        item["setting_id"]: item
        for item in (await client.get("/v1/admin/settings", headers=_H)).json()["items"]
    }
    spec = items["moderation.block_categories"]
    limit = spec["constraints"]["max_length"]

    assert spec["type"] == "string"
    assert spec["options"] is None
    ok = await client.patch(
        "/v1/admin/settings/moderation.block_categories", json={"value": "x" * limit}, headers=_H
    )
    too_long = await client.patch(
        "/v1/admin/settings/moderation.block_categories",
        json={"value": "x" * (limit + 1)},
        headers=_H,
    )

    assert ok.status_code == 200, ok.text
    assert too_long.status_code == 422, too_long.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_id", "value"),
    [
        ("chat.characters_enabled", "да"),
        ("chat.reasoning_level", "extreme"),
        ("catalog.presets_default_locale", "kl"),
        ("chat.models_offered", "gpt-4.1"),
        ("chat.disabled_tool_families", ["no-such-family"]),
    ],
)
async def test_a_value_violating_our_own_declaration_is_422(
    econ: Any, setting_id: str, value: Any
) -> None:
    """Предикат §11: `422` ⟺ нарушено то, что мы САМИ объявили — CRM могла отклонить это в форме."""
    client, _ = econ

    response = await client.patch(
        f"/v1/admin/settings/{setting_id}", json={"value": value}, headers=_H
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("setting_id", ["chat.models_offered", "chat.advertised_generation_modes"])
async def test_an_empty_multi_enum_is_422_and_is_not_a_reset_to_the_default(
    econ: Any, setting_id: str, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Пустой env значит «оператор ничего не сказал», пустой оверлей — «оператор выбрал ничего».

    Ловушка ADR-065 §1: у `CHAT_ADVERTISED_GENERATION_MODES` пустой env штатно означает
    fail-closed набор, и реализация почти наверняка перенесёт правило на оверлей «по аналогии».
    Кейс падает, если перенесла: тогда ответ был бы `200`, а строка — записанной.
    """
    client, _ = econ
    before = (await client.get("/v1/admin/settings", headers=_H)).json()["items"]

    response = await client.patch(
        f"/v1/admin/settings/{setting_id}", json={"value": []}, headers=_H
    )

    assert response.status_code == 422, response.text
    after = (await client.get("/v1/admin/settings", headers=_H)).json()["items"]
    assert after == before  # значение НЕ изменилось
    async with db_sessionmaker() as s:
        assert int(await s.scalar(text("SELECT count(*) FROM admin_settings")) or 0) == 0
    assert await _audit_count(db_sessionmaker) == 0  # отказ события не создаёт


@pytest.mark.asyncio
async def test_dropping_the_default_model_from_the_window_is_400_not_422(econ: Any) -> None:
    """МЕЖЭЛЕМЕНТНЫЙ инвариант: он связывает две РАЗНЫЕ строки настроек.

    Выразить его ни в `options`, ни в `constraints` одной строки нечем, поэтому CRM не могла
    отклонить такую правку в форме — отсюда `400`, а не `422`. Соседняя проверка той же настройки
    (пустой список) даёт `422`, и различает их не строгость, а наличие объявления.
    """
    client, _ = econ
    settings = get_settings()
    default_id = settings.default_model()
    without_default = [m for m in settings.allowed_models_union() if m != default_id]
    assert without_default

    response = await client.patch(
        "/v1/admin/settings/chat.models_offered", json={"value": without_default}, headers=_H
    )

    assert response.status_code == 400, response.text
    assert default_id in response.json()["detail"]


@pytest.mark.asyncio
async def test_setting_a_default_model_outside_the_window_is_400(econ: Any) -> None:
    """Тот же инвариант с ДРУГОЙ стороны — два отдельных кейса, как требует 09-testing.md."""
    client, _ = econ
    settings = get_settings()
    default_id = settings.default_model()
    other = next(m for m in settings.allowed_models_union() if m != default_id)
    narrowed = await client.patch(
        "/v1/admin/settings/chat.models_offered", json={"value": [default_id]}, headers=_H
    )
    assert narrowed.status_code == 200, narrowed.text

    response = await client.patch(
        "/v1/admin/settings/chat.default_model", json={"value": other}, headers=_H
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_the_cross_element_invariant_reads_the_db_not_the_stale_snapshot(econ: Any) -> None:
    """Соседняя настройка, изменённая другим процессом и ещё не попавшая в снимок.

    Барьер, сверяющийся по снимку, пропустил бы правку, ради отклонения которой он и стоит.
    """
    from app.instance_config import reset_snapshot

    client, _ = econ
    settings = get_settings()
    default_id = settings.default_model()
    other = next(m for m in settings.allowed_models_union() if m != default_id)
    narrowed = await client.patch(
        "/v1/admin/settings/chat.models_offered", json={"value": [default_id]}, headers=_H
    )
    assert narrowed.status_code == 200, narrowed.text
    reset_snapshot()  # правка соседа есть в БД, но снимку этого процесса ещё не видно

    response = await client.patch(
        "/v1/admin/settings/chat.default_model", json={"value": other}, headers=_H
    )

    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_dropping_general_from_the_advertised_set_is_normalized_visibly(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Код вправе нормализовать значение, но НЕ вправе делать это молча (ADR-099 §8.1).

    Нормализация выполняется на ЗАПИСИ: в оверлей ложится уже нормализованное значение, а ответ
    `PATCH` несёт `general` КАК СЛЕДСТВИЕ, а не как отдельная мера. Здесь не симметричный `400`,
    потому что `defaultGenerationMode` — константа кода, и отказ не оставил бы оператору ни
    одного шага.

    Кейс держит ЧЕТЫРЕ поверхности одного хранимого значения — ответ, `GET`, строку БД и дельту
    аудита. Проверка одного лишь ответа прошла бы и в мире «дорисовали в ответе, сохранили
    сырое», который ADR прямо запрещает: тогда `previous_value` соседней правки разошёлся бы с
    показанным `value`.
    """
    from app.schemas.chat import DEFAULT_GENERATION_MODE, GENERATION_MODE_ORDER

    client, _ = econ
    sent = ["reasoning", "study_learn"]
    expected = [m for m in GENERATION_MODE_ORDER if m in {*sent, DEFAULT_GENERATION_MODE}]
    assert DEFAULT_GENERATION_MODE not in sent  # предусловие: оператор снял режим по умолчанию

    response = await client.patch(
        "/v1/admin/settings/chat.advertised_generation_modes",
        json={"value": sent},
        headers=_H,
    )

    assert response.status_code == 200, response.text
    # 1. Ответ несёт ФАКТИЧЕСКИ ЗАПИСАННОЕ значение, а не то, что отправил оператор.
    assert response.json()["value"] == expected
    # 2. То же значение читается обратно с поверхности.
    items = {
        item["setting_id"]: item
        for item in (await client.get("/v1/admin/settings", headers=_H)).json()["items"]
    }
    assert items["chat.advertised_generation_modes"]["value"] == expected
    # 3. …и лежит в ОВЕРЛЕЕ нормализованным — именно из него считаются `previous_value`,
    #    `changed` и дельта аудита. Хранение сырого развело бы их с показанным.
    async with db_sessionmaker() as s:
        stored = await s.scalar(
            text("SELECT value FROM admin_settings WHERE setting_id=:i"),
            {"i": "chat.advertised_generation_modes"},
        )
    assert (json.loads(stored) if isinstance(stored, str) else stored) == expected
    # 4. Дельта аудита указывает на хранимое, а не на присланное.
    rows = await _audit(db_sessionmaker, "admin_setting_updated")
    assert len(rows) == 1
    assert json.loads(rows[0][0]["next"]) == expected


@pytest.mark.asyncio
async def test_patch_of_an_unknown_setting_is_400_and_creates_nothing(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = econ

    response = await client.patch(
        "/v1/admin/settings/chat.invented_by_crm", json={"value": True}, headers=_H
    )

    assert response.status_code == 400, response.text
    async with db_sessionmaker() as s:
        assert int(await s.scalar(text("SELECT count(*) FROM admin_settings")) or 0) == 0


# ============================== гонка на трёх PATCH-путях ===================================
async def _service_with_blind_read(
    session: AsyncSession,
) -> Any:
    """Сервис, чей резолвер «строки оверлея нет» — то есть конкурент уже вставил, а мы не видим.

    Это ТОЧНАЯ форма гонки: оба запроса проходят проверку версии («строки нет»), оба вставляют, и
    второй падает на первичном ключе. Имитация детерминирована, тогда как две параллельные
    HTTP-корутины расходятся по времени и давали бы флапающий кейс.
    """
    from app.admin.economics_service import AdminEconomicsService
    from app.models import AdminProduct, AdminSetting, AdminTariff

    service = AdminEconomicsService(session, get_settings())
    original = session.scalar
    hidden = (AdminProduct, AdminSetting, AdminTariff)

    async def _blind(statement: Any, *args: Any, **kwargs: Any) -> Any:
        entity = getattr(statement, "column_descriptions", [{}])
        if entity and entity[0].get("entity") in hidden:
            return None
        return await original(statement, *args, **kwargs)

    session.scalar = _blind  # type: ignore[method-assign]
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "table"),
    [
        ("products", {"tokens": 42}, "admin_products"),
        ("pricing", {"tokens": 42}, "admin_tariffs"),
        ("settings", {"value": False}, "admin_settings"),
    ],
)
async def test_a_lost_pk_race_on_patch_is_409_not_500(
    econ: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    path: str,
    payload: dict[str, Any],
    table: str,
) -> None:
    """Гонка на PK на КАЖДОМ из трёх `PATCH`-путей → `409`, а не `500` и не `200`.

    Код именно `409`, а не `400` дубликата: на `PATCH` это ровно «значение изменил другой
    оператор», тот же смысл, что у явной проверки `if_updated_at`. Соседний `POST /products`
    отвечает `400`, и ветка обязана различать МЕТОД, а не только код.

    Кейс падает при откате перехвата `IntegrityError` в no-op: тогда наружу летит голое
    исключение без `status_code`.
    """
    from app.admin.economics_service import AdminEconomicsService
    from app.schemas.admin_economics import (
        AdminProductPatchRequest,
        AdminSettingPatchRequest,
        AdminTariffPatchRequest,
    )

    client, _ = econ
    target = {
        "products": _ONE_TIME_ID,
        "pricing": _chat_tariff_id(),
        "settings": "chat.characters_enabled",
    }[path]

    # Конкурент выиграл гонку: строка уже в БД.
    winner = await client.patch(f"/v1/admin/{path}/{target}", json=payload, headers=_H)
    assert winner.status_code == 200, winner.text
    audit_before = await _audit_count(db_sessionmaker)

    async with db_sessionmaker() as session:
        service = await _service_with_blind_read(session)
        assert isinstance(service, AdminEconomicsService)
        call = {
            "products": lambda: service.patch_product(
                target, AdminProductPatchRequest(tokens=99), actor_claim="loser"
            ),
            "pricing": lambda: service.patch_tariff(
                target, AdminTariffPatchRequest(tokens=99), actor_claim="loser"
            ),
            "settings": lambda: service.patch_setting(
                target, AdminSettingPatchRequest(value=True), actor_claim="loser"
            ),
        }[path]
        with pytest.raises(Exception) as raised:  # noqa: B017 — важен ИМЕННО status_code
            await call()

    assert getattr(raised.value, "status_code", None) == 409, repr(raised.value)
    # Провалившаяся попытка не оставляет следа в аудите…
    assert await _audit_count(db_sessionmaker) == audit_before
    # …а данные конкурента целы, и повтор операции видит их как «прежнее значение».
    retry = await client.patch(f"/v1/admin/{path}/{target}", json=payload, headers=_H)
    assert retry.status_code == 200, retry.text
    async with db_sessionmaker() as s:
        assert int(await s.scalar(text(f"SELECT count(*) FROM {table}")) or 0) == 1


@pytest.mark.asyncio
async def test_the_loser_of_a_patch_race_sees_the_winner_value_as_previous(econ: Any) -> None:
    """Второй инвариант отката: повтор проходит штатно и видит значение КОНКУРЕНТА."""
    client, _ = econ
    tariff = _chat_tariff_id()
    winner = await client.patch(f"/v1/admin/pricing/{tariff}", json={"tokens": 42}, headers=_H)
    assert winner.status_code == 200, winner.text

    retry = await client.patch(f"/v1/admin/pricing/{tariff}", json={"tokens": 77}, headers=_H)

    assert retry.status_code == 200, retry.text
    assert retry.json()["previous_tokens"] == 42


# ============================== аудит =======================================================
@pytest.mark.asyncio
async def test_each_successful_edit_writes_the_event_that_names_what_changed(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Имя события называет ИЗМЕНЁННОЕ: записать смену настройки действием тарифа запрещено."""
    client, _ = econ

    await client.post(
        "/v1/admin/products",
        json={"product_id": "audit.new", "name": "п", "purchase_kind": "one_time", "tokens": 5},
        headers=_H,
    )
    await client.patch("/v1/admin/products/audit.new", json={"tokens": 6}, headers=_H)
    await client.patch("/v1/admin/products/audit.new", json={"archived": True}, headers=_H)
    await client.patch(f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 4}, headers=_H)
    await client.patch(
        "/v1/admin/settings/chat.characters_enabled", json={"value": False}, headers=_H
    )

    for event_type in (
        "admin_product_created",
        "admin_product_updated",
        "admin_product_archived",
        "admin_tariff_updated",
        "admin_setting_updated",
    ):
        rows = await _audit(db_sessionmaker, event_type)
        assert len(rows) == 1, event_type


@pytest.mark.asyncio
async def test_the_audit_carries_the_delta_and_the_actor_claim_but_never_the_key(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`X-Admin-Actor` пишется как ЗАЯВЛЕНИЕ, а не аутентификация.

    Значение ничем не подтверждено; единственная аутентификация — admin-ключ.
    """
    client, _ = econ
    await client.patch(f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 2}, headers=_H)
    await client.patch(f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 8}, headers=_H)

    rows = await _audit(db_sessionmaker, "admin_tariff_updated")

    assert len(rows) == 2
    last = rows[-1][0]
    assert last["previous"] == "2"
    assert last["next"] == "8"
    assert last["actorClaim"] == "operator@example.com"
    assert _ADMIN_SECRET not in str(last)


@pytest.mark.asyncio
async def test_a_refused_edit_creates_no_audit_event(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = econ

    # ⚠️ Код ожидается ТОЧНЫЙ, а не «любой из двух». Допуск `in (400, 422)` пережил бы взаимную
    # подмену кодов и тем самым обесценил бы предикат §11, который их и разводит: `422` ⟺ мы это
    # объявили, `400` ⟺ правила в объявлении нет. Нулевой тариф стоит здесь именно ради этого —
    # он единственный в перечне сменил код с `422` на `400`.
    for method, path, payload, expected in (
        ("PATCH", "/v1/admin/pricing/chat:openai:no-such", {"tokens": 5}, 400),
        ("PATCH", f"/v1/admin/pricing/{_chat_tariff_id()}", {"tokens": 0}, 400),
        ("PATCH", "/v1/admin/settings/chat.reasoning_level", {"value": "extreme"}, 422),
        (
            "POST",
            "/v1/admin/products",
            {"product_id": _ONE_TIME_ID, "name": "п", "purchase_kind": "one_time", "tokens": 1},
            400,
        ),
    ):
        response = await client.request(method, path, json=payload, headers=_H)
        assert response.status_code == expected, (path, response.text)

    assert await _audit_count(db_sessionmaker) == 0


@pytest.mark.asyncio
async def test_an_admin_edit_writes_an_audit_row_without_a_target_user(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Правка каталога субъекта-пользователя не имеет вовсе (миграция `0033` ослабила NOT NULL).

    Подставить «какой-нибудь» `userId` значило бы записать в аудит факт, которого не было.
    """
    client, _ = econ

    await client.patch(f"/v1/admin/pricing/{_chat_tariff_id()}", json={"tokens": 3}, headers=_H)

    async with db_sessionmaker() as s:
        user_ids = (
            await s.execute(
                text("SELECT user_id FROM audit_logs WHERE event_type='admin_tariff_updated'")
            )
        ).all()
    assert user_ids == [(None,)]


# ============================== авторизация и коды ==========================================
@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "payload"), _SURFACE)
async def test_no_input_on_the_surface_ever_returns_404(
    econ: Any, method: str, path: str, payload: dict[str, Any] | None
) -> None:
    """`404` означает ровно «расширение не реализовано» — и выключил бы опрос бэка целиком.

    Сводный регресс-кейс по образцу ADR-092: неизвестный идентификатор на реализованном пути
    обязан давать `400`, а не `404`.
    """
    client, _ = econ

    response = await client.request(method, path, json=payload, headers=_H)

    assert response.status_code != 404, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "payload"), _SURFACE)
async def test_the_surface_refuses_a_missing_header_with_403_and_a_wrong_key_with_401(
    econ: Any, method: str, path: str, payload: dict[str, Any] | None
) -> None:
    client, _ = econ

    missing = await client.request(method, path, json=payload)
    wrong = await client.request(method, path, json=payload, headers={"X-Admin-Key": "nope"})

    assert missing.status_code == 403, missing.text
    assert wrong.status_code == 401, wrong.text


@pytest.mark.asyncio
async def test_a_user_jwt_does_not_authorize_the_surface(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from tests.conftest import auth_headers

    client, _ = econ
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    response = await client.get("/v1/admin/settings", headers=auth_headers(uid))

    assert response.status_code == 403, response.text


# ============================== §10.1: корзина `rl:admin_econ` ==============================
@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "payload"), _SURFACE)
async def test_every_one_of_the_eight_paths_enforces_the_economics_bucket(
    econ: Any, method: str, path: str, payload: dict[str, Any] | None
) -> None:
    """⚠️ ПОЗИТИВНЫЙ кейс на КАЖДОМ пути: превышение корзины → `429`.

    Лимит не middleware, а явный вызов в теле хендлера: забытый вызов оставляет путь без лимита
    МОЛЧА. Кейс «страница из 6 вызовов не даёт 429» проходит и при полностью отсутствующем
    лимитере, поэтому сам по себе не засчитывается.
    """
    client, redis = econ
    settings = get_settings()
    original = settings.admin_economics_rate_limit_per_min
    settings.admin_economics_rate_limit_per_min = 2
    try:
        seen = [
            (await client.request(method, path, json=payload, headers=_H)).status_code
            for _ in range(4)
        ]
    finally:
        settings.admin_economics_rate_limit_per_min = original

    assert 429 in seen, seen
    assert any(key.startswith("rl:admin_econ:") for key in redis.counts)


@pytest.mark.asyncio
async def test_the_two_admin_buckets_do_not_share_a_budget(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Изоляция корзин: отрисовка страницы каталога не съедает бюджет ДЕНЕЖНЫХ операций."""
    client, redis = econ
    settings = get_settings()
    original = settings.admin_economics_rate_limit_per_min
    settings.admin_economics_rate_limit_per_min = 2
    try:
        for _ in range(5):
            await client.get("/v1/admin/pricing", headers=_H)
        exhausted = await client.get("/v1/admin/pricing", headers=_H)
        # Денежная ручка живёт в СВОЕЙ корзине и продолжает работать.
        money = await client.get(f"/v1/admin/wallet/{uuid.uuid4()}", headers=_H)
    finally:
        settings.admin_economics_rate_limit_per_min = original

    assert exhausted.status_code == 429
    assert money.status_code != 429
    assert any(key.startswith("rl:admin_econ:") for key in redis.counts)
    assert any(key.startswith("rl:admin:") for key in redis.counts)


@pytest.mark.asyncio
async def test_the_code_default_lets_a_whole_crm_page_through_without_editing_env(
    econ: Any,
) -> None:
    """Дефолт применяется БЕЗ правки `.env`: переменной в существующих файлах нет.

    Кейс закрывает ровно тот сценарий, ради которого заведена отдельная корзина: 6 вызовов
    отрисовки плюс правки настроек подряд не дают `429` на потолке 120.
    """
    client, _ = econ
    assert get_settings().admin_economics_rate_limit_per_min == 120

    page = [
        (await client.get("/v1/admin/capabilities", headers=_H)).status_code,
        (await client.get("/v1/admin/products", headers=_H)).status_code,
        (await client.get("/v1/admin/pricing", headers=_H)).status_code,
        (await client.get("/v1/admin/settings", headers=_H)).status_code,
        (await client.get("/v1/admin/capabilities", headers=_H)).status_code,
        (await client.get("/v1/admin/capabilities", headers=_H)).status_code,
    ]
    edits = [
        (
            await client.patch(
                "/v1/admin/settings/chat.characters_enabled",
                json={"value": index % 2 == 0},
                headers=_H,
            )
        ).status_code
        for index in range(14)
    ]

    assert page == [200] * 6
    assert edits == [200] * 14


# ============================== отказ обновления снимка =====================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "field", "expected"),
    [
        (
            "POST",
            "/v1/admin/products",
            {"product_id": "stale.new", "name": "п", "purchase_kind": "one_time", "tokens": 12},
            "tokens",
            12,
        ),
        ("PATCH", f"/v1/admin/products/{_ONE_TIME_ID}", {"tokens": 34}, "tokens", 34),
        ("PATCH", "/v1/admin/pricing/PLACEHOLDER", {"tokens": 56}, "tokens", 56),
        (
            "PATCH",
            "/v1/admin/settings/chat.characters_enabled",
            {"value": False},
            "value",
            False,
        ),
    ],
)
async def test_a_failed_snapshot_refresh_never_turns_a_committed_edit_into_an_error(
    econ: Any,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    payload: dict[str, Any],
    field: str,
    expected: Any,
) -> None:
    """⚠️ Отказ обновления снимка не имеет права превратить состоявшуюся правку в ошибку.

    Тело ответа собирается из ЗАПИСАННЫХ значений, а не из перечитанного снимка. Кейс падает,
    если сборка тела вернётся к снимку: тогда ответ либо `500` по уже закоммиченной и уже
    зааудированной правке, либо старое значение при `changed: true` — и оператор в обоих случаях
    нажимает «Сохранить» второй раз.
    """
    from app.admin import economics_service

    client, _ = econ
    resolved_path = path.replace("PLACEHOLDER", _chat_tariff_id())

    async def _refuse(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(economics_service, "refresh_snapshot", _refuse)

    response = await client.request(method, resolved_path, json=payload, headers=_H)

    assert response.status_code in (200, 201), response.text
    assert response.json()[field] == expected


# ============================== §10.1: строгий предел тела admin-поверхности ================
# ШЕСТЬ пишущих ручек admin-поверхности. Гейт размера тела — НЕ middleware, а явный вызов в теле
# хендлера: забытый вызов оставляет путь без предела МОЛЧА, без ошибки и без лога, — ровно как
# лимит частоты. Поэтому перечень собран один раз и каждая ручка проверяется отдельным кейсом:
# сводная проверка «на одной из них 413» прошла бы при пяти незакрытых путях.
_WRITING_SURFACE: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("tokens", "POST", "/v1/admin/users/USER/tokens", {"amount": 1}),
    (
        "subscription",
        "POST",
        "/v1/admin/users/USER/subscription",
        {"product_id": _ONE_TIME_ID, "expires_in_days": 30, "grant_id": "grant-body-limit"},
    ),
    (
        "product_create",
        "POST",
        "/v1/admin/products",
        {"product_id": "body.limit", "name": "Предел", "purchase_kind": "one_time", "tokens": 5},
    ),
    ("product_patch", "PATCH", f"/v1/admin/products/{_ONE_TIME_ID}", {"tokens": 5}),
    ("tariff_patch", "PATCH", "/v1/admin/pricing/TARIFF", {"tokens": 5}),
    ("setting_patch", "PATCH", "/v1/admin/settings/chat.memory_enabled", {"value": True}),
)


def _padded(payload: dict[str, Any], settings: Any) -> bytes:
    """То же тело, раздутое НАСТОЯЩИМИ байтами сверх предела.

    Пробельный отступ, а не выдуманный `Content-Length`: подделанный заголовок проверял бы
    доверчивость гейта, а не предел, — и прошёл бы даже там, где сервис читает тело целиком.
    JSON-разбор к трейлингу пробелов безразличен, поэтому объект остаётся схемно валидным и
    отказ может прийти ТОЛЬКО от предела размера.
    """
    return json.dumps(payload).encode() + b" " * (settings.admin_size_limit_body + 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "method", "path", "payload"),
    _WRITING_SURFACE,
    ids=[row[0] for row in _WRITING_SURFACE],
)
async def test_a_body_over_the_admin_limit_is_413_on_every_writing_handle(
    econ: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    name: str,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    """ADR-009 §6 на поверхности ADR-099: строгий предел тела ≤ 8 КБ на КАЖДОЙ пишущей ручке.

    ⚠️ Кейс парный намеренно. Один лишь ассерт «раздутое тело → 413» прошёл бы и на ручке,
    которая отвечает `413` по любой причине; один лишь ассерт «обычное тело → не 413» прошёл бы
    при полностью отсутствующем гейте. Различает их только пара: то же самое тело, отличающееся
    ТОЛЬКО размером, меняет исход.
    """
    _ = name
    client, _redis = econ
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    resolved = path.replace("USER", str(uid)).replace("TARIFF", _chat_tariff_id())
    settings = get_settings()
    headers = {**_H, "Content-Type": "application/json"}

    oversized = await client.request(
        method, resolved, content=_padded(payload, settings), headers=headers
    )
    normal = await client.request(method, resolved, json=payload, headers=_H)

    assert oversized.status_code == 413, oversized.text
    assert normal.status_code != 413, normal.text


@pytest.mark.asyncio
async def test_the_first_edit_matching_env_still_writes_moves_the_stamp_and_audits(
    econ: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """«Что-то меняет» — это ДВА разных изменения, и второе легко потерять (ADR-099 §8).

    Правка ЗНАЧЕНИЕМ, совпадающим с env, при ОТСУТСТВУЮЩЕЙ строке оверлея меняет не значение, а
    СОСТАВ оверлеев: с этого момента `updated_at` перестаёт быть пустым (а именно им оператор
    видит, что строку трогали), и величина уходит из-под `.env`. Повтор той же правки не меняет
    уже ничего — и не пишет, не двигает отметку, не оставляет следа в аудите.

    Кейс падает в ОБЕ стороны: если `changed` считается только по значению — красен первый
    блок; если строка переписывается на каждый `PATCH` — красен второй.
    """
    client, _ = econ
    items = {
        item["setting_id"]: item
        for item in (await client.get("/v1/admin/settings", headers=_H)).json()["items"]
    }
    target = "chat.memory_enabled"
    env_value = items[target]["value"]
    assert items[target]["updated_at"] is None  # предусловие: строки оверлея ещё нет
    async with db_sessionmaker() as s:
        assert int(await s.scalar(text("SELECT count(*) FROM admin_settings")) or 0) == 0

    first = await client.patch(
        f"/v1/admin/settings/{target}", json={"value": env_value}, headers=_H
    )

    assert first.status_code == 200, first.text
    assert first.json()["value"] == env_value  # значение то же самое…
    assert first.json()["changed"] is True  # …а СОСТАВ оверлеев изменился
    assert first.json()["updated_at"] is not None
    async with db_sessionmaker() as s:
        stamp_after_first = await s.scalar(
            text("SELECT updated_at FROM admin_settings WHERE setting_id=:i"), {"i": target}
        )
    assert stamp_after_first is not None
    assert len(await _audit(db_sessionmaker, "admin_setting_updated")) == 1

    repeat = await client.patch(
        f"/v1/admin/settings/{target}", json={"value": env_value}, headers=_H
    )

    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["changed"] is False
    async with db_sessionmaker() as s:
        stamp_after_repeat = await s.scalar(
            text("SELECT updated_at FROM admin_settings WHERE setting_id=:i"), {"i": target}
        )
    # Отметка НЕ сдвинулась: иначе повтор выдавал бы соседу ложный `409` на строку, которой
    # никто не правил.
    assert stamp_after_repeat == stamp_after_first
    assert len(await _audit(db_sessionmaker, "admin_setting_updated")) == 1  # следа не прибавилось


# ====== §10.0/§10.2: лейбл отказа следует из ФАКТА ветки, а не из ближайшего значения ========
# СЕМЬ мест несоответствия — семь значений, и все семь достижимы на ОДНОЙ области (`products`).
# Перечень собран так намеренно: он проверяется как РАЗБИЕНИЕ, а не как набор отдельных кейсов.
_REJECT_CASES: tuple[tuple[str, str, dict[str, Any], int, str, str], ...] = (
    # (id кейса, product_id, тело, код, ожидаемый reason, фрагмент detail)
    ("unknown_id", "never.existed", {"tokens": 5}, 400, "unknown_id", ""),
    # Форма ТЕЛА: ни одного изменяемого поля. Проверяется без обращения к данным инстанса.
    ("empty_body", _ONE_TIME_ID, {}, 422, "type_mismatch", ""),
    # Объявленная ГРАНИЦА `limits.product_tokens_max` — форма при этом безупречна.
    ("over_limits", _ONE_TIME_ID, {"tokens": 10**9}, 422, "out_of_range", ""),
    # Данные ИСТОЧНИКА: класс покупки телом `PATCH` не передаётся вообще — пути вперёд нет.
    ("kind_missing", _CATALOG_BARE_ID, {"tokens": 5}, 400, "source_kind_missing", "класс"),
    # НЕобъявленная граница — ОТДЕЛЬНОЕ место, и оно соседствует с `out_of_range` намеренно:
    # оба про число, но одно про границу, которую мы CRM отдали, а другое про границу, которой
    # в замороженном наборе `limits` выразить нечем (у продукта она вдобавок зависит от
    # `purchase_kind` — от ВТОРОГО поля того же тела).
    # ⚠️ Прежде на этом месте стоял `source_tokens_missing` — отказ archived-правки строки, чей
    # источник не несёт `credits`. Он снят вместе со своей веткой (§6.1): archived-правка числа
    # не требует вовсе, и её отказ был ровно тем пробелом fail-closed нормы, ради которого
    # редакция и написана. Кейс на её УСПЕХ живёт ниже отдельно и обязателен: удалить отказ и
    # не проверить проход значило бы снять покрытие, а не перенести его.
    ("undeclared_bound", _ONE_TIME_ID, {"tokens": 0}, 400, "undeclared_bound", "не меньше"),
    # Сервис НЕ ВЕДЁТ величину вовсе — единственный законный producer этого значения.
    ("avatar_tokens", _ONE_TIME_ID, {"avatar_tokens": 5}, 400, "unsupported_field", ""),
    # Версия разошлась: соседний элемент изменил кто-то другой.
    (
        "stale_version",
        _ONE_TIME_ID,
        {"tokens": 7, "if_updated_at": _STALE_STAMP},
        409,
        "conflict",
        "",
    ),
)

_REJECT_REASONS: tuple[str, ...] = tuple(row[4] for row in _REJECT_CASES)


def _rejected(reason: str, scope: str = "products") -> float:
    value = REGISTRY.get_sample_value(
        "admin_override_rejected_total", {"scope": scope, "reason": reason}
    )
    return 0.0 if value is None else value


# Та же проверка на области `settings`. Отдельный перечень, а не параметр к предыдущему: у
# настройки НЕТ «источника элемента» и она не несёт неподдерживаемых полей, поэтому три из семи
# значений здесь недостижимы ПО ПОСТРОЕНИЮ (§10.0, контраст с соседом) — и кейс утверждает
# именно это, а не «мы их не проверяли».
_SETTING_REJECT_CASES: tuple[tuple[str, str, Any, int, str], ...] = (
    ("unknown_setting", "no.such.setting", True, 400, "unknown_id"),
    # Форма верна (список строк), нарушена объявленная граница `min_items: 1`.
    ("min_items", "chat.models_offered", [], 422, "out_of_range"),
    # Не тот ТИП: строка там, где объявлен bool.
    ("wrong_type", "moderation.enabled", "да", 422, "type_mismatch"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("product_id", "payload", "status", "expected_reason", "detail_fragment"),
    [row[1:] for row in _REJECT_CASES],
    ids=[row[0] for row in _REJECT_CASES],
)
async def test_each_rejection_increments_exactly_its_own_reason_and_no_other(
    econ: Any,
    product_id: str,
    payload: dict[str, Any],
    status: int,
    expected_reason: str,
    detail_fragment: str,
) -> None:
    """ADR-099 §10.0: `reason` — РАЗБИЕНИЕ по «где лежит несоответствие», кейс на каждое значение.

    ⚠️ Ассерт двусторонний, и это главное в кейсе. Проверка «своя серия выросла» ловит только
    НЕДООЦЕНКУ и прошла бы в мире, где отказ инкрементирует половину словаря разом; проверка
    «ни одна чужая не выросла» ловит ПЕРЕОЦЕНКУ — лейбл, назначенный «по похожести». Ошибки эти
    одного веса: ложный сигнал обесценивает серию так же надёжно, как молчание.

    Ловушка `source_kind_missing` — именно `unsupported_field`: прежде эту ветку метили им, и
    лейбл был ложен в обе стороны сразу — обвинял безупречный запрос и разбавлял серию,
    единственный законный producer которой — `avatar_tokens`. Кейс `avatar_tokens` держит вторую
    сторону контраста: без него перечень утверждал бы лишь «мы перестали писать
    `unsupported_field`». Вторая ловушка того же вида — `undeclared_bound` против `out_of_range`:
    оба про число и оба соседствуют на одном поле, но первый метит границу, которой CRM знать
    было неоткуда, а второй — ту, что мы ей сами отдали.

    ⚠️ HTTP-код проверяется НЕЗАВИСИМО от лейбла и совпадать с ним не обязан (§11): код отвечает
    на «могла ли CRM отклонить это в форме», лейбл — на «где лежит несоответствие». Два `422` и
    четыре `400` соседствуют здесь с шестью разными лейблами именно поэтому.
    """
    client, _ = econ
    before = {reason: _rejected(reason) for reason in _REJECT_REASONS}

    if expected_reason == "conflict":
        # Версионный конфликт требует СУЩЕСТВУЮЩЕЙ строки оверлея: без неё резолвер видит
        # «строки нет», и правка проходит.
        seeded = await client.patch(
            f"/v1/admin/products/{product_id}", json={"tokens": 6}, headers=_H
        )
        assert seeded.status_code == 200, seeded.text
        before = {reason: _rejected(reason) for reason in _REJECT_REASONS}

    response = await client.patch(f"/v1/admin/products/{product_id}", json=payload, headers=_H)

    assert response.status_code == status, response.text
    if detail_fragment:
        assert detail_fragment in response.json()["detail"]
    assert _rejected(expected_reason) == before[expected_reason] + 1
    # …и НИ ОДНА соседняя серия того же словаря этим отказом не тронута.
    assert {
        reason: _rejected(reason) - before[reason]
        for reason in _REJECT_REASONS
        if reason != expected_reason
    } == {reason: 0.0 for reason in _REJECT_REASONS if reason != expected_reason}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_id", "value", "status", "expected_reason"),
    [row[1:] for row in _SETTING_REJECT_CASES],
    ids=[row[0] for row in _SETTING_REJECT_CASES],
)
async def test_a_setting_rejection_is_labelled_by_its_own_branch_too(
    econ: Any, setting_id: str, value: Any, status: int, expected_reason: str
) -> None:
    """Тот же предикат на области `settings` — и он НЕ следует из кейсов по продуктам.

    ⚠️ Ветка вычисления лейбла у настроек СВОЯ (`exc.constraint is not None` в `patch_setting`),
    и подмена её одним значением на кейсах по продуктам невидима: там `out_of_range` приходит
    из другой ветки. Именно так этот пробел и был найден — мутацией, которую перечень по
    продуктам пропустил.

    Три значения из семи здесь НЕДОСТИЖИМЫ по построению: `source_kind_missing` требует
    источника элемента, которого у настройки нет; `unsupported_field` — величины, которую сервис
    не ведёт; `undeclared_bound` — границы, которой мы не объявляли, а у настройки объявлены ВСЕ
    её границы (`type`/`options`/`constraints`), то есть необъявленной границы у неё нет вовсе.
    Ассерт «ни одна чужая серия не выросла» покрывает и их: недостижимое обязано оставаться на
    нуле, а не «просто не проверяться».
    """
    client, _ = econ
    before = {reason: _rejected(reason, "settings") for reason in _REJECT_REASONS}

    response = await client.patch(
        f"/v1/admin/settings/{setting_id}", json={"value": value}, headers=_H
    )

    assert response.status_code == status, response.text
    assert _rejected(expected_reason, "settings") == before[expected_reason] + 1
    assert {
        reason: _rejected(reason, "settings") - before[reason]
        for reason in _REJECT_REASONS
        if reason != expected_reason
    } == {reason: 0.0 for reason in _REJECT_REASONS if reason != expected_reason}


def test_the_seven_rejection_reasons_are_covered_one_case_each(econ: Any) -> None:
    """Перечень кейсов выше — РАЗБИЕНИЕ, а не выборка: семь значений, семь кейсов, без повторов.

    Без этого утверждения перечень мог бы молча потерять значение (кейс удалён вместе со
    строкой) или задвоить одно за счёт другого, и параметризация продолжила бы зеленеть.
    """
    from app.admin import economics_service as econ_mod

    _ = econ
    declared = {
        value
        for name, value in vars(econ_mod).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }

    assert len(_REJECT_REASONS) == len(set(_REJECT_REASONS)) == 7
    assert set(_REJECT_REASONS) == declared


def test_the_declared_reason_vocabulary_and_the_emitted_one_are_the_same_set() -> None:
    """Разность множеств «объявлено ↔ эмитируется» пуста В ОБЕ СТОРОНЫ (ADR-099 §10.2).

    Объявленное без producer — мёртвый лейбл: он попадает в дашборд и в алерт, никогда не
    загорается, и его молчание читают как «всё хорошо». Эмитируемое без объявления — второй
    словарь об одном факте, который разойдётся с первым.

    ⚠️ Обе стороны ВЫЧИСЛЯЮТСЯ, а не переписываются в тест. Объявленное — константы `REASON_*`
    модуля отказов плюс перечень отказов обновления снимка; эмитируемое — фактические
    аргументы КАЖДОГО вызова трёх продюсеров, снятые разбором AST. Список, набранный в тесте
    руками, был бы ТРЕТЬИМ словарём об одном факте и устарел бы первым — ровно та форма,
    которую §10.2 и запрещает.

    Кейс падает в обе стороны: новая константа без вызова красит «объявлено без producer»,
    новый литерал в продюсере — «эмитируется без объявления».
    """
    from app.admin import economics_service as econ_mod
    from app.instance_config import snapshot as snapshot_mod

    declared = {
        value
        for name, value in vars(econ_mod).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }
    assert len(declared) == 7, sorted(declared)  # семь мест несоответствия правки (§10.0)
    # Перечень отказов обновления снимка живёт литералами у самих веток (константы ему не
    # заведены), поэтому объявление берётся из нормы §10.0 — единственного его дома.
    refresh_declared = {"schema_mismatch", "db_error", "unexpected"}

    emitted = _emitted_reasons(econ_mod, {"_reject": 1}) | _emitted_reasons(
        snapshot_mod, {"_record_refresh_failure": 0, "_log_ignored_setting": 1}
    )

    assert declared | refresh_declared == emitted, {
        "объявлено без producer": sorted((declared | refresh_declared) - emitted),
        "эмитируется без объявления": sorted(emitted - (declared | refresh_declared)),
    }
