"""Unit: снимок оверлеев, окно обновления и наблюдаемость отказа (ADR-099 §2, §10).

**Отказ обновления НЕ откатывает цены.** Возврат к env-дефолтам после того, как снимок уже был
получен, — тихая отмена операторской правки, и он запрещён. Кейсы ниже проверяют это на
поведении, а не на наличии ветки в коде.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import Settings
from app.instance_config import snapshot as snapshot_module
from app.instance_config.settings_registry import SETTING_MODERATION_ENABLED
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    ProductOverlay,
    SettingOverlay,
    TariffOverlay,
    get_snapshot,
    install_snapshot,
    overrides_refresh_loop,
    refresh_snapshot,
    refresh_snapshot_from_pool,
    reset_snapshot,
)

_NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.UTC)


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


def _settings(**kwargs: Any) -> Settings:
    return Settings(**{"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-openai-test", **kwargs})


def _loaded_snapshot() -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        products={
            "p": ProductOverlay(
                product_id="p",
                name="Продукт",
                purchase_kind="one_time",
                tokens=5,
                archived=False,
                updated_at=_NOW,
            )
        },
        tariffs={"chat:openai:gpt-4.1": TariffOverlay("chat:openai:gpt-4.1", 9, _NOW)},
        settings={
            SETTING_MODERATION_ENABLED: SettingOverlay(SETTING_MODERATION_ENABLED, True, _NOW)
        },
        loaded_at=time.time(),
    )


def _failures(reason: str) -> float:
    value = REGISTRY.get_sample_value("admin_overrides_refresh_failures_total", {"reason": reason})
    return 0.0 if value is None else value


def _active(scope: str) -> float | None:
    return REGISTRY.get_sample_value("admin_overrides_active", {"scope": scope})


def _age_sample() -> float:
    value = REGISTRY.get_sample_value("admin_overrides_snapshot_age_seconds")
    assert value is not None
    return value


class _BrokenSession:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def scalars(self, _statement: Any) -> Any:
        raise self._exc


# ============================== дефолтное состояние — пустой снимок =========================
def test_the_default_state_is_an_empty_snapshot_not_a_loaded_one() -> None:
    """Процесс, который ещё ничего не прочитал, резолвит каждую величину из env и кода."""
    reset_snapshot()

    current = get_snapshot()

    assert current.products == {} and current.tariffs == {} and current.settings == {}
    assert current.loaded_at is None


def test_installing_a_snapshot_publishes_the_size_of_each_scope() -> None:
    reset_snapshot()
    assert _active("products") == 0

    install_snapshot(_loaded_snapshot())

    assert _active("products") == 1
    assert _active("tariffs") == 1
    assert _active("settings") == 1
    reset_snapshot()
    assert _active("products") == 0


def test_the_composition_is_logged_on_change_and_stays_silent_on_a_repeat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Тик раз в 30 секунд на 41 инстансе — поток строк, в котором изменение уже не видно.

    Обесценивание канала стоит ровно столько же, сколько молчание, поэтому лог пишется на
    ИЗМЕНЕНИИ состава.
    """
    reset_snapshot()
    loaded = _loaded_snapshot()

    with caplog.at_level(logging.INFO, logger="app.instance_config"):
        install_snapshot(loaded)
        install_snapshot(_loaded_snapshot())  # тот же состав, другой объект

    lines = [r for r in caplog.records if r.message == "admin_overrides_snapshot_changed"]
    assert len(lines) == 1
    reset_snapshot()


# ============================== §10: возраст снимка =========================================
def test_age_grows_from_process_start_when_no_load_ever_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ Ноль здесь был бы не измерением, а МОЛЧАНИЕМ.

    Инстанс, у которого упала и стартовая загрузка, и все последующие, находится ровно в том
    состоянии, ради которого серия заведена — правки оператора не применяются, — и нулём объявил
    бы себя здоровым. Кейс падает при возврате к «нет загрузки ⇒ возраст 0».
    """
    reset_snapshot()
    monkeypatch.setattr(snapshot_module, "_process_started_at", time.time() - 120.0)

    assert get_snapshot().loaded_at is None
    assert _age_sample() >= 120.0


def test_age_is_measured_at_scrape_time_not_written_at_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Записанное при обновлении значение застыло бы, и алерт «обновитель умер» не сработал бы.

    Кейс двигает ЧАСЫ, а не снимок: между двумя скрейпами обновления не происходит, и величина
    обязана вырасти сама. Он падает, если возраст начнут вычислять и запоминать в момент загрузки.
    """
    reset_snapshot()
    loaded_at = 1_000_000.0
    install_snapshot(InstanceConfigSnapshot(loaded_at=loaded_at))

    monkeypatch.setattr(snapshot_module.time, "time", lambda: loaded_at + 300.0)
    first = _age_sample()
    monkeypatch.setattr(snapshot_module.time, "time", lambda: loaded_at + 900.0)
    second = _age_sample()

    assert first == pytest.approx(300.0)
    assert second == pytest.approx(900.0)
    reset_snapshot()


def test_a_fresh_load_resets_the_age_close_to_zero() -> None:
    reset_snapshot()

    install_snapshot(_loaded_snapshot())

    assert _age_sample() < 5.0
    reset_snapshot()


# ============================== отказ обновления ============================================
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (
            ProgrammingError("select", {}, Exception("relation admin_products does not exist")),
            "schema_mismatch",
        ),
        (OperationalError("select", {}, Exception("connection refused")), "db_error"),
    ],
)
async def test_a_failed_refresh_keeps_the_previous_values_and_counts_the_failure(
    exc: BaseException, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Прежний снимок СОХРАНЁН: возврат к env после успешной загрузки запрещён (ADR-099 §2)."""
    reset_snapshot()
    loaded = _loaded_snapshot()
    install_snapshot(loaded)
    before = _failures(reason)

    with caplog.at_level(logging.ERROR, logger="app.instance_config"):
        ok = await refresh_snapshot(_BrokenSession(exc), _settings())  # type: ignore[arg-type]

    assert ok is False
    assert get_snapshot() is loaded  # значения НЕ откатились к env
    assert get_snapshot().tariffs["chat:openai:gpt-4.1"].tokens == 9
    assert _failures(reason) == before + 1
    assert "admin_overrides_refresh_failed" in {r.message for r in caplog.records}
    reset_snapshot()


@pytest.mark.asyncio
async def test_a_failed_refresh_lets_the_age_keep_growing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Возраст растёт — это и есть сигнал «правки оператора не применяются»."""
    reset_snapshot()
    install_snapshot(InstanceConfigSnapshot(loaded_at=time.time() - 400.0))

    await refresh_snapshot(
        _BrokenSession(OperationalError("select", {}, Exception("down"))),  # type: ignore[arg-type]
        _settings(),
    )

    assert _age_sample() >= 400.0
    reset_snapshot()


@pytest.mark.asyncio
async def test_refresh_from_pool_reports_failure_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_snapshot()

    class _Session(_BrokenSession):
        async def rollback(self) -> None:
            """Чтение не оставляет открытой транзакции на всё окно — даже когда оно упало."""
            self.rolled_back = True

    session = _Session(OperationalError("select", {}, Exception("down")))

    class _Ctx:
        async def __aenter__(self) -> Any:
            return session

        async def __aexit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr("app.db.get_sessionmaker", lambda: (lambda: _Ctx()))
    before = _failures("db_error")

    ok = await refresh_snapshot_from_pool(_settings())

    assert ok is False
    assert _failures("db_error") == before + 1
    assert getattr(session, "rolled_back", False) is True
    reset_snapshot()


# ============================== §2: окно обновления =========================================
def test_a_zero_refresh_window_is_clamped_to_one_second_not_to_a_spin() -> None:
    """`ADMIN_OVERRIDES_REFRESH_SECONDS=0` не выключает обновитель и не превращает его в busy-loop.

    Ноль без зажатия дал бы либо процесс, который не обновится никогда, либо цикл без паузы;
    объявляемое наружу `effective_after_seconds` обязано совпадать с фактическим окном.
    """
    assert _settings(ADMIN_OVERRIDES_REFRESH_SECONDS=0).admin_overrides_refresh_window() == 1
    assert _settings(ADMIN_OVERRIDES_REFRESH_SECONDS=-5).admin_overrides_refresh_window() == 1
    assert _settings(ADMIN_OVERRIDES_REFRESH_SECONDS=30).admin_overrides_refresh_window() == 30


@pytest.mark.asyncio
async def test_the_background_loop_actually_refreshes_and_honours_the_clamped_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Объявлено ≠ подключено: у цикла обязана быть достижимая точка вызова обновления."""
    reset_snapshot()
    stop = asyncio.Event()
    calls: list[int] = []

    async def _fake_refresh(_settings_arg: Any) -> bool:
        calls.append(1)
        stop.set()
        return True

    monkeypatch.setattr(snapshot_module, "refresh_snapshot_from_pool", _fake_refresh)

    await asyncio.wait_for(
        overrides_refresh_loop(stop, _settings(ADMIN_OVERRIDES_REFRESH_SECONDS=0)), timeout=10
    )

    assert calls == [1]


@pytest.mark.asyncio
async def test_the_background_loop_survives_an_iteration_that_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Умерший обновитель не должен ронять процесс, но обязан быть ВИДЕН метрикой отказа.

    Лейбл — `unexpected`, а НЕ `db_error` (ADR-099 §10.0). Предикат вычисляется из
    наблюдаемого факта ветки: `refresh_snapshot` обрабатывает `ProgrammingError` и
    `SQLAlchemyError` ВНУТРИ себя и наружу их не выпускает, поэтому во внешний `except`
    доходит ровно и только то, что ошибкой БД не является, — дефект нашего кода. `db_error`
    здесь ложен ВСЕГДА и отправил бы дежурного чинить базу вместо разбора нашего дефекта.

    Ассерт двусторонний намеренно: рост `unexpected` ловит недооценку (лейбл вообще не тот),
    неподвижность `db_error` ловит откат к прежнему поведению. Односторонний ассерт по
    `unexpected` прошёл бы и в мире, где ветка инкрементирует ОБЕ серии.
    """
    reset_snapshot()
    stop = asyncio.Event()
    calls: list[int] = []
    before_unexpected = _failures("unexpected")
    before_db_error = _failures("db_error")

    async def _boom(_settings_arg: Any) -> bool:
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError("iteration exploded")

    monkeypatch.setattr(snapshot_module, "refresh_snapshot_from_pool", _boom)

    await asyncio.wait_for(
        overrides_refresh_loop(stop, _settings(ADMIN_OVERRIDES_REFRESH_SECONDS=0)), timeout=15
    )

    assert len(calls) == 2  # цикл пережил первую ошибку
    assert _failures("unexpected") == before_unexpected + 2
    # …и КОНТРАСТ помечен со второй стороны: ветки внутри `refresh_snapshot` /
    # `refresh_snapshot_from_pool` остаются `db_error` (кейсы выше), а эта — не трогает их серию.
    assert _failures("db_error") == before_db_error


@pytest.mark.asyncio
async def test_the_loop_returns_immediately_when_stop_is_already_set() -> None:
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(overrides_refresh_loop(stop, _settings()), timeout=5)


# ============================== состав снимка ===============================================
def test_composition_is_stable_and_sorted() -> None:
    snapshot = InstanceConfigSnapshot(
        products={
            "b": ProductOverlay("b", "b", "one_time", 1, False, _NOW),
            "a": ProductOverlay("a", "a", "one_time", 1, False, _NOW),
        },
        tariffs={"t2": TariffOverlay("t2", 1, _NOW), "t1": TariffOverlay("t1", 1, _NOW)},
        settings={},
    )

    assert snapshot.composition() == (("a", "b"), ("t1", "t2"), ())
    assert EMPTY_SNAPSHOT.composition() == ((), (), ())
