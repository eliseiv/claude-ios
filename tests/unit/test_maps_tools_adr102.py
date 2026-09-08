"""Семейство клиентских инструментов карт (ADR-102): реестр, ось E, схема, описание.

Покрывает те пункты
[09-testing.md §Инструменты карт](../../docs/modules/chat-orchestrator/09-testing.md), которые
проверяются без БД: ось E у всех трёх сериализаторов, каталог, ключи схемы, self-contained схема
и доведение контракта до модели через `TOOL_DESCRIPTIONS`. Сценарии, требующие живого хода
(guard, деградация args, приватность, барьер), лежат в
`tests/integration/test_maps_tools_adr102.py`.

Проверяется ЦЕПЬ, а не отдельные звенья: объявленный инструмент бесполезен, если ни один рабочий
путь его не предлагает, а гейт бесполезен, если его не проверяет ни один тест с выключенным
флагом.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.chat.tools import (
    _ARGS_BY_TOOL,
    ALL_TOOL_NAMES,
    ARGS_DEGRADE_TOOLS,
    CONFIRM_TOOLS,
    MAPS_DEFAULT_RADIUS_METERS,
    MAPS_DEFAULT_RESULTS,
    MAPS_LATITUDE_MAX,
    MAPS_LATITUDE_MIN,
    MAPS_LONGITUDE_MAX,
    MAPS_LONGITUDE_MIN,
    MAPS_MAX_RADIUS_METERS,
    MAPS_MAX_RESULTS,
    MAPS_MIN_RADIUS_METERS,
    MAPS_MIN_RESULTS,
    MAPS_NAME_MAX_LENGTH,
    MAPS_QUERY_MAX_LENGTH,
    MAPS_TOOLS,
    MUTATING_TOOLS,
    TOOL_DESCRIPTIONS,
    TOOL_MAPS_GEOCODE,
    TOOL_MAPS_REVERSE_GEOCODE,
    TOOL_MAPS_ROUTE,
    TOOL_MAPS_SEARCH_PLACES,
    TOOL_MAPS_SHOW_PLACE,
    anthropic_tool_definitions,
    neutral_tool_definitions,
    openai_tool_definitions,
    to_anthropic_tool_name,
    to_domain_tool_name,
    tool_catalog,
    tool_input_schema,
    validate_tool_args,
)

_MODES = ("general", "research", "reasoning", "study_learn")

_MAPS_NAMES = frozenset(
    {
        TOOL_MAPS_SHOW_PLACE,
        TOOL_MAPS_GEOCODE,
        TOOL_MAPS_REVERSE_GEOCODE,
        TOOL_MAPS_ROUTE,
        TOOL_MAPS_SEARCH_PLACES,
    }
)


# ============================== реестр: полнота регистрации =================================
def test_family_membership_is_exactly_the_five_named_tools() -> None:
    """Состав семейства — литерально, а не `MAPS_TOOLS` против себя же.

    ADR-102 §1: четыре возможности владельца дают ПЯТЬ инструментов, потому что «геокодирование
    в обе стороны» — это два разных обязательных набора аргументов. Шестой инструмент семейства
    обязан пройти осознанный пересмотр (ось E, деградация args, приватность), а не приехать
    молча вместе с производным множеством.
    """
    assert set(MAPS_TOOLS) == set(_MAPS_NAMES)


@pytest.mark.parametrize("name", sorted(MAPS_TOOLS))
def test_maps_tool_is_registered_everywhere(name: str) -> None:
    """Инструмент объявляется в нескольких местах сразу; пропуск любого не виден при чтении.

    Без имени в таблице Anthropic провайдер отвечает 400 (наружу 502), без модели аргументов
    вызов не разбирается, без описания модель не понимает контракта (ADR-102 §12).
    """
    assert name in ALL_TOOL_NAMES, "инструмент не попал в общий перечень"
    assert name in _ARGS_BY_TOOL, "нет модели аргументов — вызов не разберётся"
    assert name in TOOL_DESCRIPTIONS and TOOL_DESCRIPTIONS[name].strip()
    wire = to_anthropic_tool_name(name)
    assert "." not in wire, "точка в имени: Anthropic ответит 400, наружу уйдёт 502"
    assert to_domain_tool_name(wire) == name, "перевод имени не обратим"
    assert tool_input_schema(name).get("type") == "object", "схема аргументов не построилась"


@pytest.mark.parametrize("name", sorted(MAPS_TOOLS))
def test_maps_tools_are_non_mutating_and_need_no_confirmation(name: str) -> None:
    """ADR-102 §1: ни один не мутирует; диалог на каждый показ карты обесценил бы тот
    единственный диалог, который важен, — перед `git.push` с перезаписью истории."""
    assert name not in MUTATING_TOOLS
    assert name not in CONFIRM_TOOLS


@pytest.mark.parametrize("name", sorted(MAPS_TOOLS))
def test_every_maps_tool_degrades_instead_of_failing_the_turn(name: str) -> None:
    """ADR-102 §8: кросс-полевые правила в JSON Schema не выражаются, значит их нарушение —
    ОЖИДАЕМЫЙ исход. Инструмент вне `ARGS_DEGRADE_TOOLS` уронил бы весь ход в 422."""
    assert name in ARGS_DEGRADE_TOOLS


# ============================== ось E: предлагает и не предлагает ============================
def _neutral(**kw: Any) -> set[str]:
    return {d["name"] for d in neutral_tool_definitions(**kw)}


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("with_project", [True, False])
def test_axis_e_hides_the_family_in_every_mode_and_with_or_without_project(
    mode: str, with_project: bool
) -> None:
    """При выключенном флаге ни одно `maps.*` не уходит провайдеру НИ В ОДНОМ режиме.

    Ось E ортогональна осям A (проект) и C (режим генерации), поэтому проверяется всё
    произведение: гейт, забытый в одной из веток, молча вернул бы неисполнимый инструмент.
    """
    off = _neutral(include_server_side=with_project, generation_mode=mode)
    assert not (off & MAPS_TOOLS), "карты предложены при выключенном флаге"

    on = _neutral(include_server_side=with_project, generation_mode=mode, maps_tools_enabled=True)
    assert on >= MAPS_TOOLS, "при включённом флаге предложены не все пять"
    # Ось E удаляет ИМЕННО карты и ничего больше.
    assert on - off == set(MAPS_TOOLS)


def test_default_call_offers_no_maps_tool() -> None:
    """Вызывающий, не передавший ось вовсе, обязан получить набор БЕЗ карт: дефолт `False`
    охраняет инстанс, чей клиент семейство не исполняет (ход остался бы незавершённым)."""
    assert not (_neutral() & MAPS_TOOLS)


@pytest.mark.parametrize("enabled", [False, True])
def test_axis_e_reaches_every_provider_serializer(enabled: bool) -> None:
    """Ось обязана действовать одинаково у ВСЕХ трёх сериализаторов, а не только у neutral.

    Ровно этот дефект уже случался на оси D: `openai_tool_definitions` принимал флаг, но не
    пробрасывал его дальше, — параметр в сигнатуре есть, вызывающий код выглядит правильным, а
    на OpenAI-инстансе инструментов не появилось бы вовсе.
    """
    neutral = _neutral(maps_tools_enabled=enabled)
    openai = {
        to_domain_tool_name(d["function"]["name"])
        for d in openai_tool_definitions(maps_tools_enabled=enabled)
    }
    anthropic = {
        to_domain_tool_name(d["name"])
        for d in anthropic_tool_definitions(maps_tools_enabled=enabled)
    }
    assert neutral == openai == anthropic, "сериализаторы расходятся по составу"
    assert (neutral & MAPS_TOOLS) == (set(MAPS_TOOLS) if enabled else set())


@pytest.mark.parametrize("assistant_mode", ["chat", "code"])
def test_axis_e_does_not_multiply_with_assistant_mode(
    assistant_mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-102 §10/§11: контраст с осью D — «как доехать» это обычный чат, а не режим.

    Ось D требует И флага, И `assistant_mode == "code"`; ось E — только флага. Поэтому при
    поднятом флаге ОБА режима получают полный набор карт и обе строки промта, а при выключенном
    системный промт обязан остаться БАЙТ-В-БАЙТ прежним: иначе на всём флоте сменился бы префикс
    и обнулился prompt-кэш.
    """
    from app.chat import orchestrator
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        monkeypatch.setenv("MAPS_TOOLS_ENABLED", "false")
        get_settings.cache_clear()
        prompt_off = orchestrator._system_prompt_for(assistant_mode)
        assert "maps." not in prompt_off, "указания по картам при выключенной оси"

        monkeypatch.setenv("MAPS_TOOLS_ENABLED", "true")
        get_settings.cache_clear()
        prompt_on = orchestrator._system_prompt_for(assistant_mode)
        # Указания и сами инструменты включаются ОДНИМ условием: разойдись они — модель получила
        # бы предписание звать инструменты, которых ей не дали (или наоборот).
        assert "maps.geocode" in prompt_on and "maps.route" in prompt_on
        assert _neutral(maps_tools_enabled=True) >= MAPS_TOOLS
        # Выключенный флаг оставляет прежний промт целиком его префиксом — приписка ушла в хвост.
        assert prompt_on.startswith(prompt_off)
    finally:
        get_settings.cache_clear()


# ============================== каталог осями не режется ====================================
def test_catalog_is_not_cut_by_axis_e() -> None:
    """ADR-102 §12: каталог — технический реестр, по которому клиент понимает, что ему предстоит
    реализовать; ось E режет offer-set модели, но не его. Число — против реестра, не литерала."""
    catalog = tool_catalog()
    by_name = {t["name"]: t for t in catalog}
    assert set(MAPS_TOOLS) <= set(by_name)
    assert len(catalog) == len(_ARGS_BY_TOOL) == len(ALL_TOOL_NAMES)
    for name in MAPS_TOOLS:
        entry = by_name[name]
        assert entry["execution"] == "client", "MapKit живёт только на устройстве"
        assert entry["mutating"] is False
        assert entry["requiresConfirmation"] is False


# ============================== ограничения — ключами схемы =================================
def _props(name: str) -> dict[str, Any]:
    return tool_input_schema(name)["properties"]


def _bounds(schema: dict[str, Any]) -> tuple[Any, Any]:
    """Границы поля, устойчиво к обёртке `anyOf` у необязательных (`float | None`)."""
    if "minimum" in schema or "maximum" in schema:
        return schema.get("minimum"), schema.get("maximum")
    for variant in schema.get("anyOf", []):
        if "minimum" in variant or "maximum" in variant:
            return variant.get("minimum"), variant.get("maximum")
    return None, None


_COORDINATE_FIELDS = (
    (TOOL_MAPS_SHOW_PLACE, "latitude", "longitude"),
    (TOOL_MAPS_REVERSE_GEOCODE, "latitude", "longitude"),
    (TOOL_MAPS_SEARCH_PLACES, "centerLatitude", "centerLongitude"),
    (TOOL_MAPS_ROUTE, "originLatitude", "originLongitude"),
    (TOOL_MAPS_ROUTE, "destinationLatitude", "destinationLongitude"),
)


@pytest.mark.parametrize(("tool", "lat_field", "lon_field"), _COORDINATE_FIELDS)
def test_coordinate_ranges_live_in_the_schema(tool: str, lat_field: str, lon_field: str) -> None:
    """ADR-102 §2: диапазоны — КЛЮЧАМИ схемы, а не кастомным валидатором.

    Сравнение идёт с КОНСТАНТАМИ схемы, а не с литералами в тесте: литерал здесь дублировал бы
    реестр на тестовой поверхности. Честная граница меры названа в самом ADR — диапазоны ловят
    только |latitude| > 90, а от перестановки двух значений ≤ 90 защищают ИМЕНА полей.
    """
    props = _props(tool)
    assert _bounds(props[lat_field]) == (MAPS_LATITUDE_MIN, MAPS_LATITUDE_MAX)
    assert _bounds(props[lon_field]) == (MAPS_LONGITUDE_MIN, MAPS_LONGITUDE_MAX)


@pytest.mark.parametrize("tool", [TOOL_MAPS_GEOCODE, TOOL_MAPS_SEARCH_PLACES])
def test_max_results_bounds_and_default_live_in_the_schema(tool: str) -> None:
    """`maxResults` — единственный рычаг, которым сервер реально ограничивает РАЗМЕР результата
    (сам результат клиентского инструмента не валидируется вовсе), поэтому его границы обязаны
    доехать до модели схемой (ADR-102 §5)."""
    field = _props(tool)["maxResults"]
    assert _bounds(field) == (MAPS_MIN_RESULTS, MAPS_MAX_RESULTS)
    assert field["default"] == MAPS_DEFAULT_RESULTS


def test_radius_bounds_and_default_live_in_the_schema() -> None:
    field = _props(TOOL_MAPS_SEARCH_PLACES)["radiusMeters"]
    assert _bounds(field) == (MAPS_MIN_RADIUS_METERS, MAPS_MAX_RADIUS_METERS)
    assert field["default"] == MAPS_DEFAULT_RADIUS_METERS


def test_explicit_intent_enums_are_in_the_schema_and_required() -> None:
    """ADR-102 §4: величина, которую подставляет ПРИЛОЖЕНИЕ, обязана быть выбрана моделью ЯВНО.

    Умолчание невидимо: его нет ни в аргументах, ни в истории, ни в ответе, — «кофейни рядом» и
    «кофейни в Берлине» выглядели бы в args одинаково. Поэтому перечисления обязательны
    (`required`), а не необязательны с дефолтом.
    """
    expected = {
        (TOOL_MAPS_REVERSE_GEOCODE, "pointKind"): ["current_location", "coordinates"],
        (TOOL_MAPS_SEARCH_PLACES, "centerKind"): ["current_location", "coordinates"],
        (TOOL_MAPS_ROUTE, "originKind"): ["current_location", "coordinates"],
        (TOOL_MAPS_ROUTE, "departureKind"): ["now", "at"],
        (TOOL_MAPS_ROUTE, "transportType"): ["automobile", "walking", "transit"],
    }
    for (tool, field), values in expected.items():
        schema = tool_input_schema(tool)
        assert schema["properties"][field]["enum"] == values, (tool, field)
        assert field in schema["required"], f"{tool}.{field} обязано быть required"


def test_transport_type_has_no_bicycle() -> None:
    """ADR-102 §3: `MKDirectionsTransportType` велосипед не поддерживает, а значение, которое
    приложение исполнить не может, — гарантированный отказ, оформленный как возможность."""
    values = _props(TOOL_MAPS_ROUTE)["transportType"]["enum"]
    assert not any("bicycle" in v or "cycl" in v for v in values), values


def test_destination_coordinates_are_required_not_optional() -> None:
    """ADR-102 §3: точка «по памяти» отличается от прочих ошибок тем, что НЕ ДАЁТ ошибки —
    маршрут построится, просто не туда. Обязательность выражается ключом `required`."""
    required = set(tool_input_schema(TOOL_MAPS_ROUTE)["required"])
    assert {"destinationName", "destinationLatitude", "destinationLongitude"} <= required
    show = set(tool_input_schema(TOOL_MAPS_SHOW_PLACE)["required"])
    assert {"name", "latitude", "longitude"} <= show


@pytest.mark.parametrize(
    ("tool", "field", "cap"),
    [
        (TOOL_MAPS_GEOCODE, "query", MAPS_QUERY_MAX_LENGTH),
        (TOOL_MAPS_SEARCH_PLACES, "query", MAPS_QUERY_MAX_LENGTH),
        (TOOL_MAPS_ROUTE, "destinationName", MAPS_NAME_MAX_LENGTH),
        (TOOL_MAPS_SHOW_PLACE, "name", MAPS_NAME_MAX_LENGTH),
    ],
)
def test_string_fields_carry_max_length(tool: str, field: str, cap: int) -> None:
    assert _props(tool)[field]["maxLength"] == cap


def test_no_positional_coordinate_pair_anywhere_in_the_family() -> None:
    """ADR-102 §2 (нормативно): ни в одном аргументе семейства нет массива-пары координат.

    Перепутанный порядок «долгота, широта» — самая частая МОЛЧАЛИВАЯ ошибка области: GeoJSON
    пишет `[lon, lat]`, MapKit — `(latitude, longitude)`, и обе конвенции есть в обучающих
    данных. Имя поля — единственная защита, работающая ДО исполнения.
    """
    for name in MAPS_TOOLS:
        props = _props(name)
        for field, schema in props.items():
            assert field not in {"coordinates", "point", "location", "center"}, (name, field)
            lowered = field.lower()
            if "latitude" in lowered or "longitude" in lowered:
                assert "array" not in str(schema), (name, field)


# ============================== схема self-contained ========================================
def _walk(node: Any) -> list[Any]:
    found = [node]
    if isinstance(node, dict):
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


@pytest.mark.parametrize("name", sorted(MAPS_TOOLS))
def test_maps_schema_is_self_contained(name: str) -> None:
    """ADR-102 §3: модели ПЛОСКИЕ, значит `$defs` не порождаются и добавлять семейство в
    `_SELF_CONTAINED_SCHEMA_TOOLS` не требуется. Появись вложенная модель — провайдеру уехал бы
    `$ref`, поддержку которого не гарантирует ни один из двух; этот тест упадёт первым."""
    schema = tool_input_schema(name)
    assert "$defs" not in schema
    for node in _walk(schema):
        if isinstance(node, dict):
            assert "$ref" not in node, (name, node)


# ============================== валидатор: кросс-полевые правила ============================
_VALID_ROUTE = {
    "originKind": "current_location",
    "destinationName": "Sheremetyevo",
    "destinationLatitude": 55.97,
    "destinationLongitude": 37.41,
    "transportType": "automobile",
    "departureKind": "now",
}


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (TOOL_MAPS_GEOCODE, {"query": "Tverskaya 12, Moscow"}),
        (TOOL_MAPS_REVERSE_GEOCODE, {"pointKind": "current_location"}),
        (
            TOOL_MAPS_REVERSE_GEOCODE,
            {"pointKind": "coordinates", "latitude": 55.76, "longitude": 37.61},
        ),
        (TOOL_MAPS_SEARCH_PLACES, {"query": "аптека", "centerKind": "current_location"}),
        (TOOL_MAPS_ROUTE, _VALID_ROUTE),
        (TOOL_MAPS_SHOW_PLACE, {"name": "Кремль", "latitude": 55.75, "longitude": 37.62}),
    ],
)
def test_valid_args_pass(tool: str, args: dict[str, Any]) -> None:
    assert validate_tool_args(tool, args)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        # `coordinates` без пары — половина точки бессмысленна.
        (TOOL_MAPS_REVERSE_GEOCODE, {"pointKind": "coordinates", "latitude": 55.76}),
        (
            TOOL_MAPS_SEARCH_PLACES,
            {"query": "аптека", "centerKind": "coordinates", "centerLatitude": 55.76},
        ),
        # `current_location` С координатами — точный фикс человека окольным путём (ADR-102 §9).
        (
            TOOL_MAPS_REVERSE_GEOCODE,
            {"pointKind": "current_location", "latitude": 55.76, "longitude": 37.61},
        ),
        (
            TOOL_MAPS_SEARCH_PLACES,
            {
                "query": "аптека",
                "centerKind": "current_location",
                "centerLatitude": 55.76,
                "centerLongitude": 37.61,
            },
        ),
        # `at` без времени — ровно та ловушка «на сейчас», ради которой перечисление и введено.
        (TOOL_MAPS_ROUTE, {**_VALID_ROUTE, "departureKind": "at"}),
        # Диапазон координаты — ключом схемы.
        (
            TOOL_MAPS_SHOW_PLACE,
            {"name": "нигде", "latitude": 91.0, "longitude": 37.62},
        ),
    ],
)
def test_cross_field_and_range_violations_are_rejected(tool: str, args: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_tool_args(tool, args)


def test_validator_message_never_quotes_the_coordinate() -> None:
    """ADR-102 §8: `str(exc)` у pydantic печатает ЗНАЧЕНИЯ, вызвавшие отказ, то есть координаты.

    Сообщения наших кросс-полевых валидаторов — ФИКСИРОВАННЫЕ строки: они уходят модели,
    персистируются в шаге и реплеятся провайдеру на каждом следующем витке.
    """
    from app.chat.tools import content_free_args_error

    marker_lat, marker_lon = 55.123456, 37.987654
    try:
        validate_tool_args(
            TOOL_MAPS_SEARCH_PLACES,
            {
                "query": "аптека",
                "centerKind": "current_location",
                "centerLatitude": marker_lat,
                "centerLongitude": marker_lon,
            },
        )
    except ValueError as exc:
        message = content_free_args_error(exc)
    else:  # pragma: no cover - defended by the test above
        pytest.fail("невалидные аргументы приняты")

    assert "55.123456" not in message and "37.987654" not in message, message
    assert "current_location" in message, "модель обязана понять, ЧТО именно нарушено"


# ============================== описание доводит контракт до модели =========================
# Регресс ADR-027: `02-api-contracts.md` уже декларировал ISO8601, а в схеме стоял голый `str`
# без описания — и модель подставляла date-only. Норма, записанная только в документацию, для
# модели НЕ СУЩЕСТВУЕТ. Проверяется ПРИСУТСТВИЕ утверждения, а не дословный текст.
_REQUIRED_CLAIMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("координаты не выдумывать", ("never invent coordinates",)),
    ("конвенция времени: локальное, без offset", ("local time without a timezone offset",)),
    ("пример времени в описании", ("2026-09-09T07:10:00",)),
    ("локализованные строки цитировать дословно", ("verbatim",)),
    ("пустой список — не ошибка", ("empty list is not an error",)),
    ("не повторять тот же вызов", ("never repeat the same call without new input",)),
    ("огрублённая точность названа", ("locationaccuracy",)),
)

_REFUSAL_CODES = (
    "location_permission_denied",
    "location_permission_not_determined",
    "location_unavailable",
    "transport_unavailable",
    "maps_unavailable",
)


@pytest.mark.parametrize("name", sorted(MAPS_TOOLS))
@pytest.mark.parametrize(("claim", "needles"), _REQUIRED_CLAIMS, ids=lambda v: str(v)[:40])
def test_description_carries_the_contract(name: str, claim: str, needles: tuple[str, ...]) -> None:
    text = TOOL_DESCRIPTIONS[name].lower()
    assert any(n.lower() in text for n in needles), f"{name}: не доведено до модели — {claim}"


@pytest.mark.parametrize("name", sorted(MAPS_TOOLS))
@pytest.mark.parametrize("code", _REFUSAL_CODES)
def test_description_prescribes_behaviour_for_every_refusal_code(name: str, code: str) -> None:
    """ADR-102 §6: клиентские витки НЕ ограничены `MAX_SERVER_TOOL_ROUNDS` — счётчик считает
    только раунды, исполняемые бэкендом. От зацикливания «модель зовёт → приложение отказывает»
    защищает ТОЛЬКО предписание в описании инструмента, поэтому оно обязано покрывать каждый код.
    """
    assert code in TOOL_DESCRIPTIONS[name], f"{name}: нет поведения на {code}"
