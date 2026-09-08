"""Unit: ручки экспериментов пейволла и признак продукта по умолчанию (ADR-098)."""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.billing_cloudpayments.experiments import (
    BroadappsExperimentsClient,
    resolve_experiment_locale,
)
from app.config import get_settings
from app.errors import UpstreamError


class _FakeResponse:
    def __init__(self, status: int, body: object | None = None, raw: str | None = None) -> None:
        self.status_code = status
        self._body = body
        self._raw = raw

    def json(self) -> object:
        if self._raw is not None:
            raise ValueError("not json")
        return self._body


class _FakeClient:
    """Подменяет httpx.AsyncClient: запоминает вызов или бросает заданный отказ."""

    calls: list[dict[str, object]] = []

    def __init__(self, *, response=None, raise_exc=None, timeout=None) -> None:
        self._response = response
        self._raise = raise_exc
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": self.timeout}
        )
        if self._raise is not None:
            raise self._raise
        return self._response


def _patch(monkeypatch, *, response=None, raise_exc=None):
    _FakeClient.calls = []

    def factory(timeout=None):
        return _FakeClient(response=response, raise_exc=raise_exc, timeout=timeout)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _client() -> BroadappsExperimentsClient:
    return BroadappsExperimentsClient(get_settings())


_OK = {
    "assignment": {
        "segment": {"code": "b", "is_control": False},
        "requested_segment_matches": True,
        "created": True,
    }
}


# ---- локаль -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,default,expected",
    [
        ("ru-RU,en;q=0.8", "en", "ru"),
        ("zh_Hans", "en", "zh"),
        # Немецкий НЕ подменяется на en: это размерность аналитики поставщика, а не наш текст.
        ("de-DE", "en", "de"),
        (None, "RU", "ru"),
        ("", "zh-Hans", "zh"),
        ("*", "en", "en"),
        ("123", "en", "en"),
        (None, "", "en"),
    ],
)
def test_locale_resolution(header: str | None, default: str, expected: str) -> None:
    assert resolve_experiment_locale(header, default) == expected


def test_only_the_first_language_tag_is_parsed() -> None:
    # Битый первый тег обязан упасть на СЛЕДУЮЩИЙ ИСТОЧНИК, а не искать разбираемый дальше в списке.
    assert resolve_experiment_locale("*,fr;q=0.9", "en") == "en"


# ---- исходящий запрос ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_sends_expected_body(monkeypatch) -> None:
    _patch(monkeypatch, response=_FakeResponse(200, _OK))
    uid = uuid.uuid4()
    await _client().assign(
        user_id=uid,
        experiment_code="yearly_monthly_dojim2",
        segment_code="b",
        placement="onbording",
        locale="ru",
    )
    call = _FakeClient.calls[0]
    body = call["json"]
    assert call["url"].endswith("/experiments/assignments")
    assert body["user_id"] == str(uid)
    # Коды уходят ДОСЛОВНО: опечатку заказчика мы не «чиним», иначе разъедется с панелью.
    assert body["experiment_code"] == "yearly_monthly_dojim2"
    assert body["segment_code"] == "b"
    assert body["context"]["paywall"]["placement"] == "onbording"
    assert body["context"]["platform"] == "ios"
    assert body["context"]["locale"] == "ru"
    # Показ пейволла на пути отрисовки экрана: ждать 15 секунд статистику дороже, чем потерять её.
    assert call["timeout"] == 5.0


@pytest.mark.asyncio
async def test_assign_reads_booleans_strictly(monkeypatch) -> None:
    body = {
        "assignment": {
            "segment": {"code": "b", "is_control": "false"},
            "requested_segment_matches": "true",
            "created": 1,
        }
    }
    _patch(monkeypatch, response=_FakeResponse(200, body))
    res = await _client().assign(
        user_id=uuid.uuid4(), experiment_code="e", segment_code="b", placement="p", locale="ru"
    )
    # Строка "false" не должна читаться как истина, а 1 — как True.
    assert res.is_control is False
    assert res.requested_segment_matches is False
    assert res.created is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"raise_exc": httpx.TimeoutException("timeout")},
        {"raise_exc": httpx.ConnectError("refused")},
        {"response": _FakeResponse(500, {})},
        {"response": _FakeResponse(200, None, raw="<html>")},
        {"response": _FakeResponse(200, {"assignment": {"segment": {}}})},
    ],
)
async def test_assign_never_invents_a_segment(monkeypatch, kwargs) -> None:
    _patch(monkeypatch, **kwargs)
    with pytest.raises(UpstreamError) as exc:
        await _client().assign(
            user_id=uuid.uuid4(), experiment_code="e", segment_code="b", placement="p", locale="ru"
        )
    # Наружу не просачиваются ни тело, ни статус, ни токен поставщика.
    assert "500" not in str(exc.value)
    assert "html" not in str(exc.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"raise_exc": httpx.TimeoutException("timeout")},
        {"raise_exc": httpx.ConnectError("refused")},
        {"response": _FakeResponse(503, {})},
    ],
)
async def test_paywall_shown_never_raises(monkeypatch, kwargs) -> None:
    _patch(monkeypatch, **kwargs)
    # Потерянная запись телеметрии не имеет права ломать показ пейволла — ключевой инвариант.
    ok = await _client().paywall_shown(
        user_id=uuid.uuid4(), experiment_code="e", segment_code="b", placement="p", locale="ru"
    )
    assert ok is False


@pytest.mark.asyncio
async def test_paywall_shown_does_not_parse_the_body(monkeypatch) -> None:
    # Форма ответа поставщика не задокументирована: читать её значило бы выдумывать отказы.
    _patch(monkeypatch, response=_FakeResponse(204, None, raw="не json"))
    assert (
        await _client().paywall_shown(
            user_id=uuid.uuid4(), experiment_code="e", segment_code="b", placement="p", locale="ru"
        )
        is True
    )


# ---- разделение корзин ограничения частоты -------------------------------------------------


@pytest.mark.asyncio
async def test_experiment_bucket_is_separate_from_the_payment_bucket(monkeypatch) -> None:
    """Показы пейволла не имеют права выбирать бюджет у оплаты.

    Общая корзина обслуживает и POST /checkout: частый пейволл запер бы пользователю оплату
    ровно на следующем шаге. Проверяется именно КЛЮЧ — при подмене на общую корзину падает.
    """
    from app.api_gateway import rate_limit

    seen: list[str] = []

    async def fake_allow(client, key, limit, window):
        seen.append(key)
        return True

    monkeypatch.setattr(rate_limit, "_allow", fake_allow)
    monkeypatch.setattr(rate_limit, "get_redis", lambda: object())
    uid = uuid.uuid4()
    await rate_limit.enforce_experiment_limits(user_id=uid)
    await rate_limit.enforce_other_limits(user_id=uid)
    assert seen == [f"rl:experiments:{uid}", f"rl:other:{uid}"]
    assert seen[0] != seen[1]


@pytest.mark.asyncio
async def test_experiment_bucket_fails_open_when_redis_is_down(monkeypatch) -> None:
    import redis.asyncio as redis_asyncio

    from app.api_gateway import rate_limit

    async def boom(*a, **k):
        raise redis_asyncio.RedisError("нет соединения")

    monkeypatch.setattr(rate_limit, "_allow", boom)
    monkeypatch.setattr(rate_limit, "get_redis", lambda: object())
    # Авария кеша не должна закрывать пейволл.
    assert await rate_limit.enforce_experiment_limits(user_id=uuid.uuid4()) is True


# ---- продукт по умолчанию ------------------------------------------------------------------


def test_provider_default_field_is_not_read_at_all() -> None:
    """Признак не читается у поставщика: такого поля у него нет.

    Живой каталог broadapps (novirell, 2026-09-08) отдаёт `is_special_offer`, но поля со
    значением «по умолчанию» не содержит вовсе. Чтение несуществующего поля выглядело бы как
    поддержка, которой нет: оператор ждал бы, что «проставлю в панели — заработает».
    """
    from app.api_gateway.routers.token_purchase import _from_broadapps

    tp = {"100_Tokens_9.99": 100}
    base = {
        "code": "100_Tokens_9.99",
        "title": "100",
        "is_active": True,
        "price_amount": 999,
        "price_currency": "RUB",
    }
    # Даже если поставщик однажды начнёт отдавать поле — оно не влияет: источник у нас.
    assert _from_broadapps({**base, "is_default": True}, tp).isDefault is False
    assert _from_broadapps(base, tp).isDefault is False


def test_default_flag_comes_from_our_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api_gateway.routers.token_purchase import _catalog_response
    from app.schemas.token_purchase import TokenProduct

    monkeypatch.setenv("TOKEN_PRODUCTS_DEFAULT", '["b","c"]')
    get_settings.cache_clear()
    out = _catalog_response(
        [
            TokenProduct(productId="a", credits=100),
            TokenProduct(productId="b", credits=250),
            TokenProduct(productId="c", credits=500),
        ]
    )
    assert [p.isDefault for p in out.products] == [False, True, True]
    # Порядок витрины задаёт каталог, а не список: он лишь помечает.
    assert [p.productId for p in out.products] == ["a", "b", "c"]
    get_settings.cache_clear()


def test_comma_separated_form_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api_gateway.routers.token_purchase import _catalog_response
    from app.schemas.token_purchase import TokenProduct

    # Величина правится руками в .env; требовать JSON ради списка строк значит напрашиваться
    # на сломанную кавычку.
    monkeypatch.setenv("TOKEN_PRODUCTS_DEFAULT", " a , c ")
    get_settings.cache_clear()
    out = _catalog_response([TokenProduct(productId=x, credits=100) for x in ("a", "b", "c")])
    assert [p.isDefault for p in out.products] == [True, False, True]
    get_settings.cache_clear()


def test_empty_configuration_marks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api_gateway.routers.token_purchase import _catalog_response
    from app.schemas.token_purchase import TokenProduct

    monkeypatch.setenv("TOKEN_PRODUCTS_DEFAULT", "")
    get_settings.cache_clear()
    out = _catalog_response([TokenProduct(productId="a", credits=100)])
    assert out.products[0].isDefault is False
    get_settings.cache_clear()


def test_unknown_id_in_the_list_is_harmless(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api_gateway.routers.token_purchase import _catalog_response
    from app.schemas.token_purchase import TokenProduct

    # Список задаётся руками и переживает смену каталога: лишний идентификатор не должен
    # ни ронять ответ, ни помечать чужой продукт.
    monkeypatch.setenv("TOKEN_PRODUCTS_DEFAULT", '["a","product_that_left"]')
    get_settings.cache_clear()
    out = _catalog_response(
        [TokenProduct(productId="a", credits=100), TokenProduct(productId="b", credits=250)]
    )
    assert [p.isDefault for p in out.products] == [True, False]
    get_settings.cache_clear()


# ---- форма тела поставщика ------------------------------------------------------------------

_FLAT = {
    "app_id": "994e9fc9-2e68-405f-b880-e528a2115d2f",
    "user_id": "1103022e-3061-4bcc-b4de-5d8a86e48593",
    "created": False,
    "experiment": {"code": "yearly_monthly_dojim3", "name": "A vs B"},
    "segment": {"code": "a", "name": "main"},
    "requested_segment_matches": True,
    "assigned_at": "2026-09-08T08:13:21+00:00",
}


@pytest.mark.asyncio
async def test_flat_provider_body_is_accepted(monkeypatch) -> None:
    """Живое тело broadapps приходит БЕЗ обёртки assignment.

    Прод 2026-09-08, novirell: поставщик отвечал 200 плоским телом, а мы искали обёртку из
    примера в задании и признавали ответ неразборным. Наружу это выглядело как «провайдер
    недоступен», хотя он ответил и назначение состоялось.
    """
    _patch(monkeypatch, response=_FakeResponse(200, _FLAT))
    res = await _client().assign(
        user_id=uuid.uuid4(),
        experiment_code="yearly_monthly_dojim3",
        segment_code="a",
        placement="onboarding",
        locale="ru",
    )
    assert res.segment_code == "a"
    assert res.requested_segment_matches is True
    assert res.created is False
    # Признака контрольной группы поставщик не отдаёт вовсе — читается как False, а не как ошибка.
    assert res.is_control is False


@pytest.mark.asyncio
async def test_wrapped_provider_body_still_works(monkeypatch) -> None:
    # Обёртку тоже принимаем: различать эти два случая незачем, нужен один набор полей.
    _patch(monkeypatch, response=_FakeResponse(200, {"assignment": _FLAT}))
    res = await _client().assign(
        user_id=uuid.uuid4(),
        experiment_code="e",
        segment_code="a",
        placement="p",
        locale="ru",
    )
    assert res.segment_code == "a"


@pytest.mark.asyncio
async def test_body_without_a_segment_is_still_malformed(monkeypatch) -> None:
    # Послабление формы не должно превратиться в «принимаем что угодно»: без кода сегмента
    # выдумывать назначение по-прежнему нельзя.
    _patch(monkeypatch, response=_FakeResponse(200, {"app_id": "x", "created": True}))
    with pytest.raises(UpstreamError):
        await _client().assign(
            user_id=uuid.uuid4(),
            experiment_code="e",
            segment_code="a",
            placement="p",
            locale="ru",
        )
