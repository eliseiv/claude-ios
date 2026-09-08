"""Integration: сквозная цепь «правка → применение» (ADR-099 §Тесты п.2, «объявлено ≠ подключено»).

⚠️ **Компонентный тест резолвера этих кейсов НЕ заменяет** — он сам конструирует снимок и
доказывает только устройство потребителя, а не поставку данных ему. Здесь каждая правка идёт
через РЕАЛЬНУЮ admin-ручку, а результат читается на РЕАЛЬНОЙ пользовательской: ход чата,
сабмит генерации, каталог моделей, витрина продуктов.

Каждый кейс называет пару **producer → consumer**: место правки и место, где она обязана
проявиться. Артефакт без такой пары мёртв, сколько бы зелёных компонентных тестов у него ни было.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.instance_config.tariffs import (
    chat_tariff_id,
    photo_tariff_id,
    video_cells,
    video_tariff_id,
)
from app.media_generation.catalog import KIND_IMAGE, KIND_VIDEO, models_of_kind
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user
from tests.integration.test_media_generation_adr060 import (
    _Fal,
    _make_fake_httpx,
    _submit_body,
)

_ADMIN_SECRET = "wiring-admin-key-0123456789abcdef0123"
_H = {"X-Admin-Key": _ADMIN_SECRET}

_ONE_TIME_ID = "tokens_100"
_QUEUE_BASE = "https://queue.fal.run"
# Имя события — из нормы §10; переписанное в каждый ассерт, оно разошлось бы в том кейсе,
# который забыли поправить.
_COMPOSITION_EVENT = "admin_overrides_snapshot_changed"


@pytest.fixture(autouse=True)
def _enable_instance_config_loggers() -> None:
    """Вернуть логгеры `app.instance_config*` во включённое состояние.

    ⚠️ Alembic-миграция вызывает ``fileConfig("alembic.ini")`` с
    ``disable_existing_loggers=True`` и выключает КАЖДЫЙ `app.*`-логгер на весь процесс
    (см. докстроку фикстуры ``_migrated`` в ``tests/conftest.py``). В одиночном прогоне
    ``pytest tests/unit`` эта фикстура не срабатывает вовсе, поэтому кейсы на логах зелены; в
    полном прогоне CI (`tests/e2e` и `tests/integration` собираются РАНЬШЕ `tests/unit`)
    ``caplog.records`` оказывается пустым — тест на НАЛИЧИЕ строки падает, а тест на ОТСУТСТВИЕ
    строки проходит ВХОЛОСТУЮ. Тот же приём уже применён в
    ``test_billing_cloudpayments_payment_type_fallback_adr057.py``.
    """
    for name in ("app.instance_config", "app.instance_config.media_pricing"):
        logging.getLogger(name).disabled = False


@pytest.fixture
def fal() -> _Fal:
    return _Fal()


@pytest.fixture
async def wired(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
    fal: _Fal,
) -> AsyncIterator[AsyncClient]:
    """ОДНО приложение, обслуживающее и admin-поверхность, и пользовательские ручки.

    Два отдельных клиента разошлись бы снимком: правка в одном процессе не была бы видна другому,
    и кейс проверял бы не цепь, а собственную фикстуру.
    """
    from app import deps
    from app.api_gateway import rate_limit
    from app.api_gateway.routers import chat as chat_router
    from app.api_gateway.routers import media as media_router
    from app.api_gateway.routers import models as models_router
    from app.api_gateway.routers import presets as presets_router
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.media_generation import fal_client as fal_client_mod
    from app.subscription import storekit as storekit_mod

    monkeypatch.setenv("ADMIN_API_SECRET", _ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_API_SECRET_PREV", "")
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setenv("TOKEN_PRODUCTS", f'{{"{_ONE_TIME_ID}": 100}}')
    monkeypatch.setenv("PRODUCTS_CATALOG", "")
    monkeypatch.setenv("FAL_API_KEY", "fal-test-key")
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE_BASE)
    monkeypatch.setenv("CHAT_CREDIT_COST_GENERAL", "1")
    monkeypatch.setenv("CHARACTERS_ENABLED", "true")
    monkeypatch.setenv("CHAT_ADVERTISED_GENERATION_MODES", "general,research,reasoning,study_learn")
    get_settings.cache_clear()

    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]
    monkeypatch.setattr(fal_client_mod, "httpx", _make_fake_httpx(fal))

    async def _allow(**_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(rate_limit, "enforce_chat_limits", _allow)
    monkeypatch.setattr(rate_limit, "enforce_other_limits", _allow)
    monkeypatch.setattr(chat_router, "enforce_chat_limits", _allow)
    monkeypatch.setattr(models_router, "enforce_other_limits", _allow)
    monkeypatch.setattr(presets_router, "enforce_other_limits", _allow)
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


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
            or 0
        )


def _chat_rows(response: Any) -> list[dict[str, Any]]:
    """Только chat-модальность `GET /v1/models`.

    Ответ несёт и медиа-строки; настройки витрины чата их не касаются, и сравнение со всем
    списком проверяло бы не тот инвариант.
    """
    return [row for row in response.json()["models"] if row["modality"] == "chat"]


def _media_rows(response: Any) -> list[dict[str, Any]]:
    return [row for row in response.json()["models"] if row["modality"] != "chat"]


def _default_chat_tariff() -> tuple[str, str]:
    settings = get_settings()
    model = settings.default_model()
    return model, chat_tariff_id(settings.credits_provider_for_model(model), model)


async def _run_turn(
    client: AsyncClient,
    fake: FakeAnthropicClient,
    uid: uuid.UUID,
    *,
    model: str | None = None,
    generation_mode: str = "general",
) -> Any:
    fake.responses = [fake.text_result("ok")]
    body: dict[str, Any] = {
        "userId": str(uid),
        "message": "hi",
        "mode": "credits",
        "generationMode": generation_mode,
    }
    if model is not None:
        body["model"] = model
    return await client.post("/v1/chat/v2/run", json=body, headers=auth_headers(uid))


# ============================== цена чата: правка → списание ================================
@pytest.mark.asyncio
async def test_a_tariff_edit_changes_the_real_debit_of_the_next_turn(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """producer: `PATCH /v1/admin/pricing/{id}` → consumer: списание в `POST /v1/chat/v2/run`.

    Кейс падает, если снимок не поставляется оркестратору: тогда ход спишет прежнюю цену при
    успешном `200` на правке — ровно тот класс «объявлено ≠ подключено», ради которого сквозной
    кейс обязателен.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
    model, tariff_id = _default_chat_tariff()

    before = await _run_turn(wired, fake_anthropic, uid, model=model)
    assert before.status_code == 200, before.text
    assert before.json()["usage"]["creditsCharged"] == 1

    patched = await wired.patch(f"/v1/admin/pricing/{tariff_id}", json={"tokens": 7}, headers=_H)
    assert patched.status_code == 200, patched.text

    after = await _run_turn(wired, fake_anthropic, uid, model=model)

    assert after.status_code == 200, after.text
    assert after.json()["usage"]["creditsCharged"] == 7
    assert await _balance(db_sessionmaker, uid) == 50 - 1 - 7


@pytest.mark.asyncio
async def test_the_gate_the_debit_and_the_advertised_cost_share_one_source(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """ADR-064 §9 / ADR-099 §5.1: мост один, и правка обязана сдвинуть ВСЕ ТРИ его конца.

    Режим не может быть допущен по одной цене и списан по другой, а `creditCost` не может
    показывать третью. Кейс падает, если хоть один потребитель вычисляет цену иначе, чем вызовом
    единственного резолвера.
    """
    async with db_sessionmaker() as s:
        rich = await seed_user(s, subscription="active", balance=50)
        poor = await seed_user(s, subscription="active", balance=6)
    model, tariff_id = _default_chat_tariff()
    assert (
        await wired.patch(f"/v1/admin/pricing/{tariff_id}", json={"tokens": 7}, headers=_H)
    ).status_code == 200

    # (1) объявленная цена
    caps = await wired.get(f"/v1/chat/v2/capabilities?model={model}", headers=auth_headers(rich))
    listed = await wired.get("/v1/models", headers=auth_headers(rich))
    # (2) балансовый гейт: 6 < 7 ⇒ блок по ТОЙ ЖЕ величине
    blocked = await _run_turn(wired, fake_anthropic, poor, model=model)
    # (3) фактическое списание
    charged = await _run_turn(wired, fake_anthropic, rich, model=model)

    assert {mode["creditCost"] for mode in caps.json()["generationModes"]} == {7}
    assert [row["creditCost"] for row in listed.json()["models"] if row["id"] == model] == [7]
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["blockReason"] == "credits_empty"
    assert charged.json()["usage"]["creditsCharged"] == 7


@pytest.mark.asyncio
async def test_two_models_with_different_prices_debit_differently_in_the_same_mode(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Решение владельца №1: цена — функция МОДЕЛИ. Режим у обоих ходов один и тот же."""
    settings = get_settings()
    union = list(settings.allowed_models_union())
    assert len(union) >= 2
    premium, basic = union[0], union[1]
    async with db_sessionmaker() as s:
        one = await seed_user(s, subscription="active", balance=50)
        two = await seed_user(s, subscription="active", balance=50)
    for model_id, tokens in ((premium, 9), (basic, 2)):
        provider = settings.credits_provider_for_model(model_id)
        response = await wired.patch(
            f"/v1/admin/pricing/{chat_tariff_id(provider, model_id)}",
            json={"tokens": tokens},
            headers=_H,
        )
        assert response.status_code == 200, response.text

    expensive = await _run_turn(wired, fake_anthropic, one, model=premium)
    cheap = await _run_turn(wired, fake_anthropic, two, model=basic)

    assert expensive.json()["usage"]["creditsCharged"] == 9
    assert cheap.json()["usage"]["creditsCharged"] == 2
    assert expensive.json()["usage"]["generationMode"] == cheap.json()["usage"]["generationMode"]


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_mode", ["general", "research", "reasoning", "study_learn"])
async def test_the_same_model_costs_the_same_in_every_generation_mode(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    generation_mode: str,
) -> None:
    """Регрессия на возврат надбавки за режим: четыре режима — одно и то же списание."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)
    _model, tariff_id = _default_chat_tariff()
    assert (
        await wired.patch(f"/v1/admin/pricing/{tariff_id}", json={"tokens": 6}, headers=_H)
    ).status_code == 200

    response = await _run_turn(wired, fake_anthropic, uid, generation_mode=generation_mode)

    assert response.status_code == 200, response.text
    assert await _balance(db_sessionmaker, uid) == 44


@pytest.mark.asyncio
async def test_capabilities_without_a_model_returns_the_catalog_ceiling(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ADR-099 §5.2: агрегат, который читает клиент, не имеет права ЗАНИЗИТЬ списание.

    Альтернатива «цена дефолтной модели» отвергнута: пользователь на премиум-модели увидел бы
    единицу и получил списание больше — скрытая переплата.
    """
    settings = get_settings()
    union = list(settings.allowed_models_union())
    assert len(union) >= 2
    premium = next(m for m in union if m != settings.default_model())
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    provider = settings.credits_provider_for_model(premium)
    assert (
        await wired.patch(
            f"/v1/admin/pricing/{chat_tariff_id(provider, premium)}",
            json={"tokens": 11},
            headers=_H,
        )
    ).status_code == 200

    ceiling = await wired.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))
    exact = await wired.get(
        f"/v1/chat/v2/capabilities?model={settings.default_model()}", headers=auth_headers(uid)
    )
    unknown = await wired.get(
        "/v1/chat/v2/capabilities?model=model-that-does-not-exist", headers=auth_headers(uid)
    )

    assert {mode["creditCost"] for mode in ceiling.json()["generationModes"]} == {11}
    assert {mode["creditCost"] for mode in exact.json()["generationModes"]} == {1}
    assert unknown.status_code == 422, unknown.text


@pytest.mark.asyncio
async def test_the_ceiling_counts_a_model_taken_off_the_shelf_not_only_the_shop_window(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ADR-099 §5.2: множество агрегата = ТАРИФИЦИРУЕМОЕ, а не ПРЕДЛАГАЕМОЕ.

    ⚠️ Соседний кейс потолка этого НЕ ловит по построению: там дорогая модель остаётся на
    витрине, поэтому максимум по каталогу и максимум по витрине совпадают, и подмена одного
    множества другим невидима. Здесь они РАЗВЕДЕНЫ: дорогая модель снята с витрины, и потолок
    по витрине дал бы цену дешёвой оставшейся.

    Занижение здесь не эстетическое: снятая с витрины модель намеренно продолжает обслуживать
    уже созданные сессии и списывать по своей цене (§4.3, §8) — объявили бы `1`, списали `50`.
    """
    settings = get_settings()
    union = list(settings.allowed_models_union())
    assert len(union) >= 2
    default_id = settings.default_model()
    premium = next(m for m in union if m != default_id)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    premium_tariff = chat_tariff_id(settings.credits_provider_for_model(premium), premium)
    assert (
        await wired.patch(f"/v1/admin/pricing/{premium_tariff}", json={"tokens": 50}, headers=_H)
    ).status_code == 200
    narrowed = await wired.patch(
        "/v1/admin/settings/chat.models_offered", json={"value": [default_id]}, headers=_H
    )
    assert narrowed.status_code == 200, narrowed.text

    listed = _chat_rows(await wired.get("/v1/models", headers=auth_headers(uid)))
    caps = await wired.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))

    # предусловие: дорогая модель ДЕЙСТВИТЕЛЬНО ушла с витрины — иначе кейс тавтологичен
    assert [row["id"] for row in listed] == [default_id]
    # …и всё равно задаёт потолок, потому что по ней по-прежнему списывают
    assert {mode["creditCost"] for mode in caps.json()["generationModes"]} == {50}
    # …а точная цена оставшейся модели по-прежнему её собственная — потолок не подменяет её
    exact = await wired.get(
        f"/v1/chat/v2/capabilities?model={default_id}", headers=auth_headers(uid)
    )
    assert {mode["creditCost"] for mode in exact.json()["generationModes"]} == {1}


@pytest.mark.asyncio
async def test_a_session_on_the_default_model_passes_model_none_to_the_provider(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Подстановка дефолтной модели остаётся УСЛОВНОЙ: при пустом оверлее провайдер получает `None`.

    Кейс падает, если подстановка станет безусловной: тогда в вызов уедет строковый идентификатор
    там, где сегодня уходит пустое значение, — изменение исходящего контракта, которого выкат не
    объявлял.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)

    response = await _run_turn(wired, fake_anthropic, uid, model=None)

    assert response.status_code == 200, response.text
    assert fake_anthropic.calls[-1]["model"] is None


# ============================== цена медиа: ячейка → списание ===============================
@pytest.mark.asyncio
async def test_a_photo_cell_edit_changes_the_real_charge_of_the_submit(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """producer: `PATCH /v1/admin/pricing/photo:*`.

    consumer: `creditsCharged` ответа `POST /v1/media/images`.
    """
    model = models_of_kind(KIND_IMAGE)[0]
    resolution = next(iter(model.resolution_credits))
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=200)
    assert (
        await wired.patch(
            f"/v1/admin/pricing/{photo_tariff_id(model.id, resolution)}",
            json={"tokens": 13},
            headers=_H,
        )
    ).status_code == 200
    fal.on_submit(200, _submit_body(f"fal-ai/{model.id}"))

    response = await wired.post(
        "/v1/media/images",
        json={"model": model.id, "prompt": "a cat", "resolution": resolution},
        headers=auth_headers(uid),
    )

    assert response.status_code == 202, response.text
    assert response.json()["creditsCharged"] == 13
    assert await _balance(db_sessionmaker, uid) == 187


@pytest.mark.asyncio
async def test_a_photo_cell_price_is_multiplied_by_num_images_because_the_unit_says_so(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """Единица `image` НАЗЫВАЕТ множитель: запуск с `numImages=3` стоит втрое, и это не спрятано."""
    model = models_of_kind(KIND_IMAGE)[0]
    resolution = next(iter(model.resolution_credits))
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=200)
    assert (
        await wired.patch(
            f"/v1/admin/pricing/{photo_tariff_id(model.id, resolution)}",
            json={"tokens": 13},
            headers=_H,
        )
    ).status_code == 200
    fal.on_submit(200, _submit_body(f"fal-ai/{model.id}"))

    response = await wired.post(
        "/v1/media/images",
        json={
            "model": model.id,
            "prompt": "a cat",
            "resolution": resolution,
            "numImages": 3,
        },
        headers=auth_headers(uid),
    )

    assert response.status_code == 202, response.text
    assert response.json()["creditsCharged"] == 39


@pytest.mark.asyncio
async def test_a_video_cell_edit_changes_the_real_charge_of_the_submit(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """Видео: ячейка — ПОЛНАЯ цена запуска, множителей не остаётся ни одного."""
    model = models_of_kind(KIND_VIDEO)[0]
    cell = video_cells(model)[0]
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=400)
    tariff_id = video_tariff_id(model.id, cell.resolution, cell.seconds, audio=cell.audio)
    assert (
        await wired.patch(f"/v1/admin/pricing/{tariff_id}", json={"tokens": 91}, headers=_H)
    ).status_code == 200
    fal.on_submit(200, _submit_body(f"fal-ai/{model.id}"))
    payload: dict[str, Any] = {"model": model.id, "prompt": "a cat", "duration": cell.duration}
    if cell.resolution is not None:
        payload["resolution"] = cell.resolution

    response = await wired.post("/v1/media/videos", json=payload, headers=auth_headers(uid))

    assert response.status_code == 202, response.text
    assert response.json()["creditsCharged"] == 91


@pytest.mark.asyncio
async def test_the_advertised_price_cells_equal_what_the_submit_actually_charges(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession], fal: _Fal
) -> None:
    """`prices[]` в `GET /v1/media/models` СОВПАДАЕТ со списанием — по каждой комбинации модели.

    Правка одной ячейки проверяется в тех же координатах, что и объявление: расхождение здесь
    означало бы, что пользователь видит одну цену, а платит другую.
    """
    model = models_of_kind(KIND_IMAGE)[0]
    resolution = next(iter(model.resolution_credits))
    async with db_sessionmaker() as s:
        uid = await seed_user(s, balance=500)
    assert (
        await wired.patch(
            f"/v1/admin/pricing/{photo_tariff_id(model.id, resolution)}",
            json={"tokens": 17},
            headers=_H,
        )
    ).status_code == 200

    catalog = await wired.get("/v1/media/models", headers=auth_headers(uid))
    schema = next(m for m in catalog.json()["models"] if m["id"] == model.id)
    advertised = {cell["resolution"]: cell["credits"] for cell in schema["prices"]}
    fal.on_submit(200, _submit_body(f"fal-ai/{model.id}"))
    charged = await wired.post(
        "/v1/media/images",
        json={"model": model.id, "prompt": "a cat", "resolution": resolution},
        headers=auth_headers(uid),
    )

    assert advertised[resolution] == 17
    assert charged.json()["creditsCharged"] == advertised[resolution]
    # У ФОТО деградации нет: поячеечная карта легаси-поля отображает точно.
    assert schema["resolutionCredits"][resolution] == 17


@pytest.mark.asyncio
async def test_three_catalog_reads_on_defaults_emit_no_non_representable_line(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Лог непредставимости — НА ПЕРЕХОДЕ, а не на каждом обращении.

    Вывод тройки выполняется на КАЖДОМ обращении к пользовательскому каталогу; строка на каждый
    вызов превратила бы событие в поток, в котором сам переход уже не виден. Ложный сигнал
    обесценивает канал ровно так же, как молчание.
    """
    from app.instance_config import media_pricing

    media_pricing._reported_non_representable.clear()  # noqa: SLF001
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    with caplog.at_level(logging.INFO, logger="app.instance_config.media_pricing"):
        for _ in range(3):
            response = await wired.get("/v1/media/models", headers=auth_headers(uid))
            assert response.status_code == 200, response.text

    assert not [
        record
        for record in caplog.records
        if record.message == "media_price_table_non_representable"
    ]

    # ⚠️ ПОЛОЖИТЕЛЬНЫЙ КОНТРОЛЬ: «строк нет» истинно и при молчащем канале, и тогда кейс
    # проверяет собственную слепоту, а не молчание продюсера. Заставляем тот же канал заговорить.
    model = next(m for m in models_of_kind(KIND_VIDEO) if m.resolution_multipliers)
    top = max(model.resolution_multipliers, key=lambda key: model.resolution_multipliers[key])
    catalog = (await wired.get("/v1/media/models", headers=auth_headers(uid))).json()
    listed = next(m for m in catalog["models"] if m["id"] == model.id)
    for cell in video_cells(model):
        if cell.resolution != top:
            continue
        current = next(
            c["credits"]
            for c in listed["prices"]
            if c["resolution"] == cell.resolution and c["durationSeconds"] == cell.seconds
        )
        bumped = await wired.patch(
            "/v1/admin/pricing/"
            f"{video_tariff_id(model.id, cell.resolution, cell.seconds, audio=cell.audio)}",
            json={"tokens": current + 7},
            headers=_H,
        )
        assert bumped.status_code == 200, bumped.text
    with caplog.at_level(logging.WARNING, logger="app.instance_config.media_pricing"):
        assert (await wired.get("/v1/media/models", headers=auth_headers(uid))).status_code == 200
    assert [
        record
        for record in caplog.records
        if record.message == "media_price_table_non_representable"
    ]
    media_pricing._reported_non_representable.clear()  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_non_representable_edit_never_under_quotes_and_raises_the_series(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Непредставимая правка: агрегат клиента ≥ фактического списания по КАЖДОЙ комбинации.

    Серия обязана присутствовать в экспозиции `/metrics` ПОСЛЕ реального вызова, а не только в
    юните продюсера (ADR-099 §Тесты п.3).
    """
    import math

    from app.instance_config import media_pricing

    media_pricing._reported_non_representable.clear()  # noqa: SLF001
    model = next(m for m in models_of_kind(KIND_VIDEO) if m.resolution_multipliers)
    top = max(model.resolution_multipliers, key=lambda key: model.resolution_multipliers[key])
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    catalog = (await wired.get("/v1/media/models", headers=auth_headers(uid))).json()
    before = next(m for m in catalog["models"] if m["id"] == model.id)
    bumped = {
        (cell["resolution"], cell["durationSeconds"], cell["audio"]): cell["credits"] + 7
        for cell in before["prices"]
        if cell["resolution"] == top
    }
    for cell in video_cells(model):
        key = (cell.resolution, cell.seconds, cell.audio if cell.model.supports_audio else None)
        if key in bumped:
            response = await wired.patch(
                f"/v1/admin/pricing/"
                f"{video_tariff_id(model.id, cell.resolution, cell.seconds, audio=cell.audio)}",
                json={"tokens": bumped[key]},
                headers=_H,
            )
            assert response.status_code == 200, response.text

    after = next(
        m
        for m in (await wired.get("/v1/media/models", headers=auth_headers(uid))).json()["models"]
        if m["id"] == model.id
    )
    metrics = await wired.get("/metrics")

    packs = {
        cell.seconds: math.ceil(cell.seconds / (model.base_duration_seconds or cell.seconds))
        for cell in video_cells(model)
    }
    for cell in after["prices"]:
        client_price = (
            after["credits"]
            * packs[cell["durationSeconds"]]
            * (after["resolutionMultipliers"] or {}).get(cell["resolution"], 1)
        )
        if cell["audio"] and after["audioMultiplier"]:
            client_price = math.ceil(client_price * after["audioMultiplier"])
        assert client_price >= cell["credits"], cell
    assert f'media_price_legacy_overquote{{model="{model.id}"}} 1.0' in metrics.text
    media_pricing._reported_non_representable.clear()  # noqa: SLF001


# ============================== настройки: правка → ответ ручки =============================
@pytest.mark.asyncio
async def test_characters_setting_actually_gates_the_user_catalog(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.characters_enabled` → consumer: `GET /v1/characters`."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    before = await wired.get("/v1/characters", headers=auth_headers(uid))
    assert (
        await wired.patch(
            "/v1/admin/settings/chat.characters_enabled", json={"value": False}, headers=_H
        )
    ).status_code == 200
    after = await wired.get("/v1/characters", headers=auth_headers(uid))

    assert before.json()["characters"]
    assert after.json()["characters"] == []


@pytest.mark.asyncio
async def test_models_offered_setting_actually_narrows_the_user_catalog(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.models_offered` → consumer: состав `GET /v1/models`."""
    settings = get_settings()
    default_id = settings.default_model()
    keep = [default_id]
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    before = await wired.get("/v1/models", headers=auth_headers(uid))
    assert (
        await wired.patch(
            "/v1/admin/settings/chat.models_offered", json={"value": keep}, headers=_H
        )
    ).status_code == 200
    after = await wired.get("/v1/models", headers=auth_headers(uid))

    # Витрина настройки гейтит ТОЛЬКО chat-модальность: медиа-каталог она не касается вовсе
    # (ADR-099 §8.2 (з) — каталог media-моделей в состав этой волны не включён).
    assert len(_chat_rows(before)) > 1
    assert [row["id"] for row in _chat_rows(after)] == keep
    assert _media_rows(before) == _media_rows(after)


@pytest.mark.asyncio
async def test_default_model_setting_actually_moves_the_default_row(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.default_model` → consumer: `default: true` в `GET /v1/models`."""
    settings = get_settings()
    other = next(m for m in settings.allowed_models_union() if m != settings.default_model())
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    assert (
        await wired.patch(
            "/v1/admin/settings/chat.default_model", json={"value": other}, headers=_H
        )
    ).status_code == 200
    rows = _chat_rows(await wired.get("/v1/models", headers=auth_headers(uid)))

    assert [row["id"] for row in rows if row["default"]] == [other]
    assert rows[0]["id"] == other


@pytest.mark.asyncio
async def test_advertised_modes_setting_actually_changes_the_capabilities_list(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.advertised_generation_modes` → consumer: `generationModes[]`.

    Здесь же видна НОРМАЛИЗАЦИЯ, и она происходит на ЗАПИСИ, а не на объявлении (ADR-099 §8.1
    п. 1): оператор снял `general`, `patch_setting` вернул его ДО сохранения, и ответ `PATCH`
    несёт `general` как СЛЕДСТВИЕ хранимого значения. Read-time барьер в
    `values.advertised_generation_modes()` при этом остаётся — но у него другая зона действия
    (env и прямая запись в БД), и покрыт он отдельно, в
    `tests/unit/test_instance_config_settings_adr099.py`.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    before = await wired.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))
    patched = await wired.patch(
        "/v1/admin/settings/chat.advertised_generation_modes",
        json={"value": ["reasoning"]},
        headers=_H,
    )
    after = await wired.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))

    assert len(before.json()["generationModes"]) == 4
    assert patched.status_code == 200, patched.text
    assert [mode["mode"] for mode in after.json()["generationModes"]] == ["general", "reasoning"]
    # …и ответ `PATCH` несёт ФАКТИЧЕСКИ ЗАПИСАННОЕ значение — с `general`, потому что именно он
    # лёг в оверлей. Ассерт на сырой ввод прошёл бы и в мире, где нормализация живёт ТОЛЬКО на
    # чтении, — то есть перестал бы стеречь п. 1 §8.1 вовсе.
    assert patched.json()["value"] == ["general", "reasoning"]
    assert next(
        item["description"]
        for item in (await wired.get("/v1/admin/settings", headers=_H)).json()["items"]
        if item["setting_id"] == "chat.advertised_generation_modes"
    )


@pytest.mark.asyncio
async def test_reasoning_level_setting_actually_changes_the_capabilities_answer(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.reasoning_level` → consumer: `reasoningLevel` в capabilities."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    before = (await wired.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))).json()
    target = "high" if before["reasoningLevel"] != "high" else "low"

    assert (
        await wired.patch(
            "/v1/admin/settings/chat.reasoning_level", json={"value": target}, headers=_H
        )
    ).status_code == 200
    after = await wired.get("/v1/chat/v2/capabilities", headers=auth_headers(uid))

    assert after.json()["reasoningLevel"] == target


@pytest.mark.asyncio
async def test_disabled_tool_families_setting_actually_removes_tools_from_the_catalog(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.disabled_tool_families` → consumer: `GET /v1/tools`."""
    from app.chat.tools import DISABLEABLE_TOOL_FAMILIES

    family = sorted(DISABLEABLE_TOOL_FAMILIES)[0]
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    before = await wired.get("/v1/tools", headers=auth_headers(uid))
    assert (
        await wired.patch(
            "/v1/admin/settings/chat.disabled_tool_families",
            json={"value": [family]},
            headers=_H,
        )
    ).status_code == 200
    after = await wired.get("/v1/tools", headers=auth_headers(uid))

    before_names = {tool["name"] for tool in before.json()["tools"]}
    after_names = {tool["name"] for tool in after.json()["tools"]}
    assert after_names < before_names
    assert all(not name.startswith(f"{family}.") for name in after_names)


@pytest.mark.asyncio
async def test_presets_locale_setting_actually_changes_the_catalog_default(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH catalog.presets_default_locale` → consumer: `GET /v1/presets`."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    before = (await wired.get("/v1/presets", headers=auth_headers(uid))).json()
    target = "ru" if before["locale"] != "ru" else "en"

    assert (
        await wired.patch(
            "/v1/admin/settings/catalog.presets_default_locale",
            json={"value": target},
            headers=_H,
        )
    ).status_code == 200
    after = await wired.get("/v1/presets", headers=auth_headers(uid))

    assert before["locale"] != target  # предусловие: правка действительно что-то меняет
    assert after.json()["locale"] == target


@pytest.mark.asyncio
async def test_presets_locale_setting_actually_changes_the_voices_catalog(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH catalog.presets_default_locale` → consumer: `locale` в `GET /v1/voices`.

    ⚠️ ТРЕТИЙ каталог той же цепочки `resolve_presets_locale`, и он появился ПОЗЖЕ остальных
    (ADR-100). Кейс на пресетах его не покрывает: цепочку можно провести до двух каталогов и
    забыть третий — ровно та форма, ради которой consumer называется поимённо.

    Локаль резолвится и при выключенной озвучке, поэтому кейс не зависит ни от ключа
    провайдера, ни от флага инстанса — он проверяет ровно ту величину, которую правит оператор.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    before = (await wired.get("/v1/voices", headers=auth_headers(uid))).json()
    target = "ru" if before["locale"] != "ru" else "en"

    assert (
        await wired.patch(
            "/v1/admin/settings/catalog.presets_default_locale",
            json={"value": target},
            headers=_H,
        )
    ).status_code == 200
    after = await wired.get("/v1/voices", headers=auth_headers(uid))

    assert before["locale"] != target  # предусловие: правка действительно что-то меняет
    assert after.json()["locale"] == target


@pytest.mark.asyncio
async def test_memory_setting_actually_changes_the_preferences_answer(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `PATCH chat.memory_enabled` → consumer: `GET /v1/preferences`."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s)

    before = await wired.get("/v1/preferences", headers=auth_headers(uid))
    assert (
        await wired.patch(
            "/v1/admin/settings/chat.memory_enabled", json={"value": True}, headers=_H
        )
    ).status_code == 200
    after = await wired.get("/v1/preferences", headers=auth_headers(uid))

    assert before.json()["memoryEnabled"] is False
    assert after.json()["memoryEnabled"] is True


@pytest.mark.asyncio
async def test_voice_input_setting_actually_gates_the_audio_attachment(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """producer: `PATCH chat.voice_input_enabled` → consumer: приём аудио-вложения в ходе."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=20)
    assert (
        await wired.patch(
            "/v1/admin/settings/chat.voice_input_enabled", json={"value": False}, headers=_H
        )
    ).status_code == 200
    fake_anthropic.responses = [fake_anthropic.text_result("ok")]

    response = await wired.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "message": "",
            "mode": "credits",
            "generationMode": "general",
            "attachments": [
                {"type": "audio", "mediaType": "audio/mp4", "data": "AAAA", "filename": "a.m4a"}
            ],
        },
        headers=auth_headers(uid),
    )

    assert response.status_code in (400, 415, 422), response.text


# ============================== продукты: создание → начисление и витрина ===================
@pytest.mark.asyncio
async def test_a_created_product_reaches_the_user_shop_window(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """«Создан» ≠ «начисляет» ≠ «в витрине» — три РАЗНЫЕ проверки (09-testing.md).

    producer: `POST /v1/admin/products` → consumer: `GET /v1/tokens/products` (ветка, производная
    от `TOKEN_PRODUCTS`, — витриной владеем МЫ, значит созданный продукт в неё включается).
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    created = await wired.post(
        "/v1/admin/products",
        json={
            "product_id": "operator.pack.500",
            "name": "Пакет 500",
            "purchase_kind": "one_time",
            "tokens": 500,
        },
        headers=_H,
    )
    assert created.status_code == 201, created.text

    admin_catalog = await wired.get("/v1/admin/products", headers=_H)
    shop = await wired.get("/v1/tokens/products", headers=auth_headers(uid))

    assert "operator.pack.500" in {item["product_id"] for item in admin_catalog.json()["items"]}
    listed = {p["productId"]: p for p in shop.json()["products"]}
    assert listed["operator.pack.500"]["credits"] == 500
    assert listed["operator.pack.500"]["title"] == "Пакет 500"
    # Цена пуста, пока оператор не завёл продукт в панели поставщика.
    assert listed["operator.pack.500"].get("price") in (None, 0)


@pytest.mark.asyncio
async def test_an_overlay_refines_an_env_product_in_the_shop_window(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    assert (
        await wired.patch(f"/v1/admin/products/{_ONE_TIME_ID}", json={"tokens": 175}, headers=_H)
    ).status_code == 200

    shop = await wired.get("/v1/tokens/products", headers=auth_headers(uid))

    listed = {p["productId"]: p for p in shop.json()["products"]}
    assert listed[_ONE_TIME_ID]["credits"] == 175


@pytest.mark.asyncio
async def test_archiving_removes_the_product_from_the_window_but_not_from_granting(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """⚠️ Вторая и третья строки таблицы §6 — не исключение, а ДРУГОЙ АДРЕСАТ.

    «Перестал выдаваться» относится к клиенту приложения; вебхук оплаты и оператор, выдающий
    план вручную, выполняют законные операции, и копировать на них правило витрины запрещено.
    Иначе архив ломал бы уже оплаченное и активные подписки.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
        target = await seed_user(s, balance=0)
    assert (
        await wired.patch(f"/v1/admin/products/{_ONE_TIME_ID}", json={"archived": True}, headers=_H)
    ).status_code == 200

    shop = await wired.get("/v1/tokens/products", headers=auth_headers(uid))
    admin_catalog = await wired.get("/v1/admin/products", headers=_H)
    granted = await wired.post(
        f"/v1/admin/users/{target}/subscription",
        json={"product_id": _ONE_TIME_ID, "expires_in_days": 30, "grant_id": str(uuid.uuid4())},
        headers=_H,
    )

    # (витрина) продукта нет…
    assert _ONE_TIME_ID not in {p["productId"] for p in shop.json()["products"]}
    # (каталог оператора) строка осталась с признаком архива…
    archived_row = next(
        item for item in admin_catalog.json()["items"] if item["product_id"] == _ONE_TIME_ID
    )
    assert archived_row["archived"] is True
    # (ручная выдача плана) продукт по-прежнему ПРИНИМАЕТСЯ.
    assert granted.status_code == 200, granted.text


@pytest.mark.asyncio
async def test_archiving_a_product_does_not_zero_the_grant_of_a_real_purchase(
    wired: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_storekit: FakeStoreKitVerifier,
) -> None:
    """producer: `PATCH {"archived": true}` → consumer: РЕАЛЬНАЯ покупка `POST /v1/tokens/purchase`.

    ⚠️ Существующий кейс про ручную выдачу плана этого НЕ доказывает: ручная выдача идёт через
    ``subscription_credits`` (канал `manual`), а разовая покупка — через ``one_time_credits``, и
    это разные резолверы с разными фолбэками. Именно на втором `NULL` в колонке оверлея имел
    право превратиться в «продукт неизвестен» (`422`) или в нулевой грант: строка оверлея после
    archived-правки существует, но числа не несёт, и читатель ОБЯЗАН провалиться к источнику.

    Кейс падает, если из ``one_time_credits`` убрать условие «И ``tokens`` задан»: правка,
    начисления не касавшаяся, сломала бы оплаченную покупку.
    """
    from app.subscription.storekit import VerifiedTransaction

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=0)
    assert (
        await wired.patch(f"/v1/admin/products/{_ONE_TIME_ID}", json={"archived": True}, headers=_H)
    ).status_code == 200
    fake_storekit.next_transaction = VerifiedTransaction(
        transaction_id="tx-archived-grant",
        original_transaction_id="tx-archived-grant",
        product_id=_ONE_TIME_ID,
        expires_at=None,
        revoked=False,
        environment="Sandbox",
    )

    purchased = await wired.post(
        "/v1/tokens/purchase",
        json={"userId": str(uid), "transaction": "jws-ignored-by-the-fake"},
        headers=auth_headers(uid),
    )
    shop = await wired.get("/v1/tokens/products", headers=auth_headers(uid))

    # Витрина продукт потеряла…
    assert _ONE_TIME_ID not in {p["productId"] for p in shop.json()["products"]}
    # …а начисление по нему идёт РОВНО по значению источника (`TOKEN_PRODUCTS`).
    assert purchased.status_code == 200, purchased.text
    assert purchased.json()["creditsAdded"] == 100
    assert purchased.json()["newBalance"] == 100


@pytest.mark.asyncio
async def test_the_scalar_credits_of_an_image_model_equals_its_base_cell_after_an_edit(
    wired: AsyncClient, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: ячейка `photo:*:1K` → consumer: СКАЛЯР `credits` в `GET /v1/media/models`.

    ⚠️ Отдельный кейс, а не следствие соседнего про `prices[]`/`resolutionCredits`: скаляр
    считается ДРУГОЙ величиной (``media_base_credits``), и именно он подписывает выбор модели
    «от N кредитов». Пока он брался из реестра, первая же правка ячейки разводила показанное и
    списанное — ответ противоречил бы сам себе в двух соседних полях.

    Базовая ступень — `1K`: та же лестница фолбэка, по которой тарифицируется запуск без явно
    выбранного качества.
    """
    model = next(m for m in models_of_kind(KIND_IMAGE) if "1K" in m.resolution_credits)
    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    assert (
        await wired.patch(
            f"/v1/admin/pricing/{photo_tariff_id(model.id, '1K')}",
            json={"tokens": 41},
            headers=_H,
        )
    ).status_code == 200

    catalog = await wired.get("/v1/media/models", headers=auth_headers(uid))

    schema = next(m for m in catalog.json()["models"] if m["id"] == model.id)
    assert schema["resolutionCredits"]["1K"] == 41
    assert schema["credits"] == schema["resolutionCredits"]["1K"]
    # Предусловие: реестровая база даёт ДРУГОЕ число, иначе кейс слеп к развязке величин.
    assert model.default_credits != 41


# ============================== наблюдаемость: серия после РЕАЛЬНОГО вызова =================
@pytest.mark.asyncio
async def test_a_real_edit_logs_the_composition_once_and_a_value_only_edit_stays_silent(
    wired: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """producer: `PATCH /v1/admin/settings/{id}` → consumer: `admin_overrides_snapshot_changed`.

    ⚠️ Носитель меры §2 (б), а не отдельная фича: без него класс ошибки «правлю `.env`, ничего не
    меняется» остаётся вообще без наблюдателя — оверлей выигрывает у env молча.

    ⚠️ Юнит на `install_snapshot` этот кейс НЕ заменяет, и это ровно форма «объявлено ≠
    подключено»: он сам конструирует снимок и доказывает устройство продюсера, а не то, что
    рабочая правка через ручку до него доходит. Здесь снимок обновляет сама пишущая ручка.

    Кейс двусторонний, и односторонний здесь бесполезен: «событие когда-нибудь появилось»
    проходит и в мире, где строка пишется на КАЖДОМ тике окна, — то есть ровно при том дефекте,
    ради которого запись сделана на изменении состава. Тик раз в 30 с на 41 инстансе дал бы
    поток, в котором настоящее изменение уже не видно, а обесценивание канала стоит столько же,
    сколько молчание.
    """
    with caplog.at_level(logging.INFO, logger="app.instance_config"):
        # СОСТАВ меняется: строки оверлея не было, стала одна.
        first = await wired.patch(
            "/v1/admin/settings/chat.memory_enabled", json={"value": True}, headers=_H
        )
        assert first.status_code == 200, first.text
        assert first.json()["changed"] is True
        after_first = [record for record in caplog.records if record.message == _COMPOSITION_EVENT]

        # …а тут меняется ЗНАЧЕНИЕ той же строки: состав оверлеев прежний, снимок обновляется.
        second = await wired.patch(
            "/v1/admin/settings/chat.memory_enabled", json={"value": False}, headers=_H
        )
        assert second.status_code == 200, second.text
        assert second.json()["changed"] is True  # правка состоялась, снимок ПЕРЕЧИТАН

    lines = [record for record in caplog.records if record.message == _COMPOSITION_EVENT]

    assert len(after_first) == 1  # состав изменился ⇒ строка есть…
    assert after_first[0].extra_fields["settings"] == ["chat.memory_enabled"]  # type: ignore[attr-defined]
    assert len(lines) == 1  # …и второе обновление её НЕ добавило


@pytest.mark.asyncio
async def test_the_override_series_appear_in_the_exposition_after_a_real_write(
    wired: AsyncClient,
) -> None:
    """Серия обязана присутствовать в `/metrics` ПОСЛЕ реального вызова, а не только в юните.

    Кейс падает при удалении строки эмиссии: юнит продюсера сам конструирует значение и такого
    удаления не заметил бы (ADR-099 §Тесты п.3).
    """
    _model, tariff_id = _default_chat_tariff()
    assert (
        await wired.patch(f"/v1/admin/pricing/{tariff_id}", json={"tokens": 4}, headers=_H)
    ).status_code == 200
    # Нулевой тариф нарушает границу, которой в нашем `limits` нет и быть не может (§11) ⇒
    # `400` + `undeclared_bound`, а НЕ `422` + `out_of_range`.
    rejected = await wired.patch(f"/v1/admin/pricing/{tariff_id}", json={"tokens": 0}, headers=_H)
    assert rejected.status_code == 400

    metrics = (await wired.get("/metrics")).text

    assert 'admin_overrides_active{scope="tariffs"} 1.0' in metrics
    assert 'admin_override_rejected_total{reason="undeclared_bound",scope="tariffs"}' in metrics
    assert "admin_overrides_snapshot_age_seconds" in metrics
