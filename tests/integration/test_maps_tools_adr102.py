"""Integration: семейство инструментов карт в живом ходе (ADR-102).

Покрывает те пункты
[09-testing.md §Инструменты карт](../../docs/modules/chat-orchestrator/09-testing.md), которые
проверяются только на рабочем пути: ось E доезжает до провайдера, defensive guard, каталог,
деградация аргументов и её content-free сообщение, инвариант приватности и барьер хода.
Реестровые проверки (схема, описания, состав) — в `tests/unit/test_maps_tools_adr102.py`.

Реальный PostgreSQL; Anthropic подменён общим `FakeAnthropicClient` (BUG-4: провайдерские id
реалистичные `toolu_...`, никогда UUID). Проверяется ЦЕПЬ: объявленный инструмент бесполезен,
если ни один рабочий путь его не предлагает, а гейт бесполезен, если его не проверяет ни один
тест с ВЫКЛЮЧЕННЫМ флагом.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.tools import _ARGS_BY_TOOL, MAPS_INVALID_ERROR_CODE, MAPS_TOOLS
from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

# Узнаваемые координаты: длинная дробная часть не встречается больше нигде в дереве, поэтому её
# появление в персистированном тексте — доказательство утечки, а не совпадение.
_MARKER_LAT = 55.123456
_MARKER_LON = 37.987654
_MARKER_STRINGS = ("55.123456", "37.987654")

# Координаты МЕСТА, о котором спросил человек: они по ADR-102 §9 персистируются законно —
# без них нельзя ни построить маршрут, ни поставить булавку.
_DEST_LAT = 55.972642
_DEST_LON = 37.414589

_ROUTE_FROM_ME: dict[str, Any] = {
    "originKind": "current_location",
    "destinationName": "Sheremetyevo, terminal C",
    "destinationLatitude": _DEST_LAT,
    "destinationLongitude": _DEST_LON,
    "transportType": "automobile",
    "departureKind": "now",
}
_SEARCH_NEAR_ME: dict[str, Any] = {"query": "аптека", "centerKind": "current_location"}
_CALENDAR_READ: dict[str, Any] = {"start": "2026-09-09T00:00:00", "end": "2026-09-09T23:59:59"}


@pytest.fixture
def maps_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ось E поднята (ADR-102 §10). Настройки кешируются lru_cache — чистим с обеих сторон."""
    get_settings.cache_clear()
    monkeypatch.setenv("MAPS_TOOLS_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def maps_disabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ось E опущена — дефолт всего флота, и именно на нём обязан работать guard."""
    get_settings.cache_clear()
    monkeypatch.setenv("MAPS_TOOLS_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _run(client: AsyncClient, uid: Any, message: str = "как доехать") -> dict[str, Any]:
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": message, "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _tool_calls_rows(
    maker: async_sessionmaker[AsyncSession], session_id: str
) -> list[tuple[str, Any, str]]:
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT tool_name, args, status FROM tool_calls "
                    "WHERE session_id = :sid ORDER BY created_at"
                ),
                {"sid": session_id},
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def _tool_steps_text(maker: async_sessionmaker[AsyncSession], session_id: str) -> str:
    """Весь персистированный текст tool-шагов одной строкой — то, что реплеится провайдеру."""
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT payload::text FROM chat_steps "
                    "WHERE session_id = :sid AND role = 'tool' ORDER BY seq"
                ),
                {"sid": session_id},
            )
        ).all()
    return "\n".join(str(r[0]) for r in rows)


# ============================== ось E доезжает до провайдера ================================
@pytest.mark.asyncio
async def test_axis_e_off_sends_no_maps_tool_to_the_provider(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_disabled: None,
) -> None:
    """Гейт проверяется на РАБОЧЕМ пути, а не только на функции набора инструментов.

    `calls[*]` несут WIRE-вид ровно того, что ушло бы провайдеру: имена там подчёркнутые
    (`maps_geocode`), потому что Anthropic отвергает точку в имени.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("готово")]

    await _run(client, uid)

    sent = {t["name"] for t in fake_anthropic.calls[0]["tools"]}
    assert not any(n.startswith("maps_") for n in sent), sorted(sent)


@pytest.mark.asyncio
async def test_axis_e_on_sends_all_five_to_the_provider(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """Объявленный инструмент, до которого не доходит ни один рабочий путь, мёртв. Здесь
    проверяется вся цепь: `MAPS_TOOLS_ENABLED` → оркестратор → определения, ушедшие провайдеру."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [fake_anthropic.text_result("готово")]

    await _run(client, uid)

    sent = {t["name"] for t in fake_anthropic.calls[0]["tools"]}
    assert {n.replace(".", "_") for n in MAPS_TOOLS} <= sent, sorted(sent)


# ============================== guard, а не только гейт (diff-стойкий) ======================
@pytest.mark.asyncio
async def test_maps_call_with_axis_e_off_is_softly_refused_and_the_turn_survives(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_disabled: None,
) -> None:
    """ADR-102 §10 (diff): удаление ветки guard'а обязано ронять этот тест.

    Гейт лишь НЕ ПОКАЗЫВАЕТ инструмент модели. Если она всё же вернёт `maps.*`, клиентский вызов
    создавать нельзя: приложение исполнить его не умеет, tool-result не придёт НИКОГДА, а барьер
    ADR-025 продолжает ход только когда каждый client-side вызов получил `completed`/`errored` —
    ни таймаута, ни сборщика «протухших» вызовов в коде нет. Без guard'а ответ был бы
    `status="tool_call"` и завис бы навсегда: с точки зрения человека «ассистент не ответил».
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("maps.route", _ROUTE_FROM_ME, tool_id="toolu_mapsguard01"),
        fake_anthropic.text_result("Карты на этом устройстве недоступны."),
    ]

    body = await _run(client, uid)

    # Ход ПРОДОЛЖИЛСЯ и закончился ответом — вот что ломается при снятии guard'а.
    assert body["status"] == "assistant_message", body
    assert body["assistantMessage"] == "Карты на этом устройстве недоступны."
    # Клиентский вызов НЕ создан: приложению нечего исполнять.
    assert body.get("toolCalls") in (None, []), body.get("toolCalls")
    assert body.get("toolCall") is None

    # Отказ отражён как ДЕЙСТВИЕ БЭКЕНДА — ровно так же ведут себя отказы `files.*` по денилисту.
    entries = [e for e in body["serverTools"] if e["toolName"] == "maps.route"]
    assert len(entries) == 1, body["serverTools"]
    assert entries[0]["status"] == "errored"
    assert entries[0]["summary"] == "tool_not_available"

    rows = await _tool_calls_rows(db_sessionmaker, body["sessionId"])
    assert [(name, status) for name, _, status in rows] == [("maps.route", "errored")]


@pytest.mark.asyncio
async def test_guard_refusal_is_not_billed_twice_and_leaves_no_pending_call(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_disabled: None,
) -> None:
    """Ни одного `pending` после хода: именно `pending` и есть механизм зависания."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "maps.show_place",
            {"name": "Кремль", "latitude": 55.75, "longitude": 37.62},
            tool_id="toolu_mapsguard02",
        ),
        fake_anthropic.text_result("ок"),
    ]

    body = await _run(client, uid)
    assert body["status"] == "assistant_message"

    async with db_sessionmaker() as s:
        pending = int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM tool_calls "
                    "WHERE session_id = :sid AND status = 'pending'"
                ),
                {"sid": body["sessionId"]},
            )
            or 0
        )
        debits = int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM ledger_transactions "
                    "WHERE user_id = :u AND type = 'debit'"
                ),
                {"u": str(uid)},
            )
            or 0
        )
    assert pending == 0
    assert debits == 1  # один ход — одно списание, отказ своего не добавляет


# ============================== каталог осями не режется ===================================
@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["false", "true"])
async def test_catalog_carries_the_family_at_both_flag_values(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    """ADR-102 §12: каталог — технический реестр, по которому клиент понимает, ЧТО ему предстоит
    реализовать; ось E режет offer-set модели, но не каталог. Число — против `_ARGS_BY_TOOL`."""
    get_settings.cache_clear()
    monkeypatch.setenv("MAPS_TOOLS_ENABLED", flag)
    get_settings.cache_clear()
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s)
        r = await client.get("/v1/tools", headers=auth_headers(uid))
        assert r.status_code == 200, r.text
        tools = r.json()["tools"]
        assert len(tools) == len(_ARGS_BY_TOOL)
        by_name = {t["name"]: t for t in tools}
        assert set(MAPS_TOOLS) <= set(by_name)
        for name in MAPS_TOOLS:
            entry = by_name[name]
            assert entry["execution"] == "client"
            assert entry["mutating"] is False
            assert entry["requiresConfirmation"] is False
    finally:
        get_settings.cache_clear()


# ============================== деградация args, а не 422 ==================================
_DEGRADE_CASES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "coordinates без пары",
        "maps.search_places",
        {"query": "аптека", "centerKind": "coordinates", "centerLatitude": _MARKER_LAT},
    ),
    (
        "at без departureTimeLocal",
        "maps.route",
        {**_ROUTE_FROM_ME, "departureKind": "at"},
    ),
    (
        "current_location С координатами",
        "maps.search_places",
        {
            "query": "аптека",
            "centerKind": "current_location",
            "centerLatitude": _MARKER_LAT,
            "centerLongitude": _MARKER_LON,
        },
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "tool_name", "args"), _DEGRADE_CASES, ids=[c[0] for c in _DEGRADE_CASES]
)
async def test_pairing_violation_degrades_and_the_turn_survives(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
    case: str,
    tool_name: str,
    args: dict[str, Any],
) -> None:
    """ADR-102 §8: кросс-полевые правила в JSON Schema без `oneOf`/`if-then` не выражаются, а
    опираться на их поддержку двумя провайдерами контракт не должен. Значит нарушение парности —
    ОЖИДАЕМЫЙ исход: для инструмента ВНЕ `ARGS_DEGRADE_TOOLS` он дал бы `422` на весь ход, то
    есть человек остался бы без ответа из-за перепутанной пары полей.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(tool_name, args, tool_id="toolu_mapsdegrade1"),
        fake_anthropic.text_result("Уточню, откуда считать."),
    ]

    body = await _run(client, uid)

    assert body["status"] == "assistant_message", body  # НЕ 422 и не обрыв хода
    entries = [e for e in body["serverTools"] if e["toolName"] == tool_name]
    assert len(entries) == 1, body["serverTools"]
    assert entries[0]["status"] == "errored"
    assert entries[0]["summary"] == MAPS_INVALID_ERROR_CODE
    # Код отказа СВОЙ, а не media-шный: по нему модель понимает, ЧТО именно переспросить.
    assert entries[0]["summary"] == "invalid_maps_args"


@pytest.mark.asyncio
async def test_degrade_message_carries_the_pairing_hint_but_never_the_coordinate(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """Content-free (ADR-102 §8): `str(exc)` у pydantic печатает ЗНАЧЕНИЯ, вызвавшие отказ, — то
    есть координаты, — а этот текст персистируется в шаге и реплеится провайдеру на КАЖДОМ
    следующем витке хода и каждом последующем ходе сессии.

    Проверяется то, что исполнитель проверял инлайн-прогоном валидатора: узнаваемая координата не
    появляется ни в персистированном tool-шаге, ни в сводке ответа. Подсказка о парности при этом
    обязана присутствовать — без неё модель видит «неверные аргументы» и не знает, чем именно.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "maps.search_places",
            {
                "query": "аптека",
                "centerKind": "current_location",
                "centerLatitude": _MARKER_LAT,
                "centerLongitude": _MARKER_LON,
            },
            tool_id="toolu_mapscontentfree",
        ),
        fake_anthropic.text_result("Скажите, около какого места искать."),
    ]

    body = await _run(client, uid)
    assert body["status"] == "assistant_message", body

    # 1) Сводка ответа — только короткий код отказа, без чего-либо из аргументов.
    serialized_response = json.dumps(body, ensure_ascii=False)
    for marker in _MARKER_STRINGS:
        assert marker not in serialized_response, f"координата уехала в ответ: {marker}"

    # 2) Персистированный tool-шаг — то, что реплеится провайдеру на каждом следующем витке.
    steps = await _tool_steps_text(db_sessionmaker, body["sessionId"])
    assert MAPS_INVALID_ERROR_CODE in steps
    for marker in _MARKER_STRINGS:
        assert marker not in steps, f"координата уехала в персистированный шаг: {marker}"

    # 3) Подсказка о парности доехала до модели: иначе отказ ничему её не учит.
    assert "current_location" in steps and "departureTimeLocal" in steps


# ============================== приватность (инвариант) ====================================
def _numeric_coordinate_fields(args: Any) -> list[str]:
    """Поля аргументов, чьё имя говорит о координате, а значение — ЧИСЛО.

    `None` не считается: у валидного `current_location` вызова поля присутствуют пустыми, и это
    ровно то, чего требует ADR-102 §9 — вместо координаты в аргументах стоит значение
    перечисления, а точку резолвит приложение на устройстве.
    """
    if not isinstance(args, dict):
        return []
    out: list[str] = []
    for field, value in args.items():
        lowered = field.lower()
        if ("latitude" in lowered or "longitude" in lowered) and isinstance(value, int | float):
            out.append(field)
    return out


@pytest.mark.asyncio
async def test_own_position_never_appears_in_persisted_args(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """ADR-102 §9 (инвариант): координаты СОБСТВЕННОГО положения не появляются в аргументах.

    Ход зовёт оба инструмента, принимающих `current_location`. Поля собственного положения
    (`centerLatitude`/`centerLongitude`, `originLatitude`/`originLongitude`) обязаны остаться
    пустыми; координаты НАЗНАЧЕНИЯ — присутствовать: без них нельзя построить маршрут, и §9
    называет их неустранимыми. Разница между «район» и «точка» здесь и есть предмет решения.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("maps.search_places", _SEARCH_NEAR_ME), ("maps.route", _ROUTE_FROM_ME)],
            tool_ids=["toolu_mapspriv01", "toolu_mapspriv02"],
        ),
    ]

    body = await _run(client, uid)
    assert body["status"] == "tool_call", body  # оба вызова ушли устройству
    assert {tc["name"] for tc in body["toolCalls"]} == {"maps.search_places", "maps.route"}

    rows = await _tool_calls_rows(db_sessionmaker, body["sessionId"])
    by_tool = {name: args for name, args, _ in rows}

    # Собственное положение — ни одного числового поля координат.
    assert _numeric_coordinate_fields(by_tool["maps.search_places"]) == []
    assert by_tool["maps.search_places"]["centerKind"] == "current_location"
    origin_numeric = [
        f for f in _numeric_coordinate_fields(by_tool["maps.route"]) if f.startswith("origin")
    ]
    assert origin_numeric == []
    assert by_tool["maps.route"]["originKind"] == "current_location"

    # Координаты МЕСТА, о котором спросил человек, законно на месте.
    assert by_tool["maps.route"]["destinationLatitude"] == _DEST_LAT
    assert by_tool["maps.route"]["destinationLongitude"] == _DEST_LON


@pytest.mark.asyncio
async def test_current_location_with_coordinates_never_reaches_the_device(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """diff: снятие ветки `current_location` из `_check_point_pairing` роняет этот тест.

    Без неё вызов с явной координатой И признаком «моё место» был бы ПРИНЯТ, уехал бы устройству
    в `toolCalls[]` и реплеился бы провайдеру на каждом следующем витке — то есть точный фикс
    человека вернулся бы окольным путём ровно там, где §9 обещает, что он не появится.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "maps.route",
            {
                **_ROUTE_FROM_ME,
                "originLatitude": _MARKER_LAT,
                "originLongitude": _MARKER_LON,
            },
            tool_id="toolu_mapspriv03",
        ),
        fake_anthropic.text_result("Уточню точку отправления."),
    ]

    body = await _run(client, uid)

    # Вызов не принят: устройству ничего не ушло.
    assert body["status"] == "assistant_message", body
    assert body.get("toolCalls") in (None, [])
    assert json.dumps(body, ensure_ascii=False).count("55.123456") == 0
    entries = [e for e in body["serverTools"] if e["toolName"] == "maps.route"]
    assert entries and entries[0]["summary"] == MAPS_INVALID_ERROR_CODE


@pytest.mark.asyncio
async def test_reverse_geocode_of_current_location_keeps_null_coordinates(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """ADR-102 §9: у `maps.reverse_geocode` с `pointKind="current_location"` результат несёт
    адрес, а `latitude`/`longitude` в нём — `null`; иначе точный фикс вернулся бы окольным путём.

    Сервер результат клиентского инструмента не валидирует вовсе (единственная проверка —
    размер), поэтому здесь проверяются обе половины реально доступной защиты: контракт доведён до
    модели описанием инструмента, и результат по контракту проезжает через ход, не приобретая
    координат ни в аргументах, ни в персистированном шаге.
    """
    from app.chat.tools import TOOL_DESCRIPTIONS, TOOL_MAPS_REVERSE_GEOCODE

    # Единственный канал, доводящий контракт до модели, — текст описания (ADR-102 Факт 2).
    description = TOOL_DESCRIPTIONS[TOOL_MAPS_REVERSE_GEOCODE].lower()
    assert "null" in description and "current_location" in description

    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "maps.reverse_geocode",
            {"pointKind": "current_location"},
            tool_id="toolu_mapsrev01",
        ),
        fake_anthropic.text_result("Вы на Тверской."),
    ]

    run = await _run(client, uid, message="где я")
    assert run["status"] == "tool_call", run
    call_id = run["toolCalls"][0]["id"]

    tr = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": run["sessionId"],
            "toolCallId": call_id,
            "result": {
                "places": [
                    {
                        "name": "Тверская, 12",
                        "address": "Москва, Тверская, 12",
                        "latitude": None,
                        "longitude": None,
                    }
                ],
                "locationAccuracy": "reduced",
            },
        },
        headers=auth_headers(uid),
    )
    assert tr.status_code == 200, tr.text
    assert tr.json()["status"] == "assistant_message"

    rows = await _tool_calls_rows(db_sessionmaker, run["sessionId"])
    assert len(rows) == 1
    assert _numeric_coordinate_fields(rows[0][1]) == []
    steps = await _tool_steps_text(db_sessionmaker, run["sessionId"])
    assert '"latitude": null' in steps or '"latitude":null' in steps


@pytest.mark.asyncio
async def test_audit_of_a_maps_turn_carries_no_arguments(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """ADR-102 §9: аудит несёт ТОЛЬКО `toolCallId`/`toolName`/`status` — ни аргументов, ни
    результата. Аудит живёт дольше чата и читается операционно, поэтому проверяется отдельно."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.tool_result("maps.route", _ROUTE_FROM_ME, tool_id="toolu_mapsaudit01"),
    ]

    body = await _run(client, uid)
    assert body["status"] == "tool_call"

    async with db_sessionmaker() as s:
        payloads = (
            (
                await s.execute(
                    text(
                        "SELECT payload::text FROM audit_logs "
                        "WHERE user_id = :u AND event_type LIKE 'tool_call%'"
                    ),
                    {"u": str(uid)},
                )
            )
            .scalars()
            .all()
        )
    assert payloads, "аудит хода не записан вовсе"
    # `requestId` дописывает инфраструктура аудита всем событиям — это корреляционный
    # идентификатор запроса, а не содержимое вызова; предметом §9 являются остальные ключи.
    allowed = {"toolCallId", "toolName", "status", "requestId"}
    for raw in payloads:
        assert set(json.loads(raw)) <= allowed, raw
        # Ни одного поля аргументов — ни имени, ни значения.
        for field in _ROUTE_FROM_ME:
            assert field not in raw, (field, raw)
        assert str(_DEST_LAT) not in raw and str(_DEST_LON) not in raw


# ============================== барьер хода не меняется ====================================
@pytest.mark.asyncio
async def test_turn_barrier_waits_for_both_maps_and_calendar_results(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    maps_enabled: None,
) -> None:
    """ADR-102 §10 / ADR-025: барьер хода семейством карт НЕ меняется.

    На одном результате из двух ответ — снова `status=tool_call` с ОСТАВШИМСЯ вызовом, обращения
    к провайдеру нет и списания нет; ход продолжается только после результатов на ОБА. Ровно этот
    барьер и делает неисполнимый клиентский вызов вечным `pending` — потому guard §10 обязателен.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        fake_anthropic.parallel_tool_result(
            [("maps.geocode", {"query": "Тверская 12, Москва"}), ("calendar.read", _CALENDAR_READ)],
            tool_ids=["toolu_mapsbar01", "toolu_mapsbar02"],
        ),
        fake_anthropic.text_result("Успеваете."),
    ]

    run = await _run(client, uid)
    assert run["status"] == "tool_call", run
    assert [tc["name"] for tc in run["toolCalls"]] == ["maps.geocode", "calendar.read"]
    id_maps, id_cal = run["toolCalls"][0]["id"], run["toolCalls"][1]["id"]
    calls_after_run = len(fake_anthropic.calls)

    partial = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": run["sessionId"],
            "toolCallId": id_maps,
            "result": {
                "query": "Тверская 12, Москва",
                "places": [
                    {
                        "name": "Тверская, 12",
                        "address": "Москва, Тверская, 12",
                        "latitude": 55.76419,
                        "longitude": 37.60567,
                    }
                ],
            },
        },
        headers=auth_headers(uid),
    )
    assert partial.status_code == 200, partial.text
    partial_body = partial.json()
    assert partial_body["status"] == "tool_call", partial_body
    assert [tc["id"] for tc in partial_body["toolCalls"]] == [id_cal]
    # Провайдера не звали и не списывали: барьер ещё открыт.
    assert len(fake_anthropic.calls) == calls_after_run
    async with db_sessionmaker() as s:
        debits = int(
            await s.scalar(
                text(
                    "SELECT count(*) FROM ledger_transactions "
                    "WHERE user_id = :u AND type = 'debit'"
                ),
                {"u": str(uid)},
            )
            or 0
        )
    assert debits == 0

    final = await client.post(
        "/v1/chat/tool-result",
        json={
            "userId": str(uid),
            "sessionId": run["sessionId"],
            "toolCallId": id_cal,
            "result": {"events": []},
        },
        headers=auth_headers(uid),
    )
    assert final.status_code == 200, final.text
    assert final.json()["status"] == "assistant_message"
    assert len(fake_anthropic.calls) == calls_after_run + 1
