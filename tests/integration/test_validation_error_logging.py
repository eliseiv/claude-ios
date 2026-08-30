"""Ошибка схемы запроса называет поле — в логе сервера, но не в ответе клиенту.

Раньше `exc.errors()` выбрасывался целиком: какое именно поле не прошло, не видел НИКТО —
ни клиент (ответ намеренно безлик, чтобы не раскрывать форму схемы), ни оператор в логах.
Отсюда тупик поддержки: разработчик получает «request validation failed» и двинуться дальше
не может, а сервер ответ знает и молчит. Реальный случай — `POST /v1/subscription/sync`, где
тем же кодом 422 отвечают ДВЕ несвязанные причины: неверная форма тела и непрошедшая проверку
StoreKit-транзакция.

Второе требование теста жёстче первого: в лог обязаны попасть ТОЛЬКО путь до поля и тип
ошибки. Pydantic кладёт в `errors()` ещё и само значение (`input`), а сюда приходят подписанные
StoreKit-транзакции и тела с вложениями — их попадание в лог недопустимо.
"""

from __future__ import annotations

import logging

import pytest
from httpx import AsyncClient

_SENTINEL = "ЗНАЧЕНИЕ-КОТОРОЕ-НЕ-ДОЛЖНО-ПОПАСТЬ-В-ЛОГ"


@pytest.mark.asyncio
async def test_validation_failure_logs_field_but_never_its_value(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("app.main").disabled = False
    with caplog.at_level(logging.WARNING, logger="app.main"):
        r = await client.post("/v1/auth/register", json={"unexpectedField": _SENTINEL})

    assert r.status_code == 422
    # Ответ клиенту не изменился: он не раскрывает ни имя поля, ни форму схемы.
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert "unexpectedField" not in r.text

    records = [x for x in caplog.records if x.getMessage() == "request_validation_failed"]
    assert records, "сбой валидации не попал в лог — диагностировать его снова невозможно"
    fields = getattr(records[0], "extra_fields", {}).get("fields")
    assert fields, "запись есть, но поля не названы — она бесполезна"
    assert any("unexpectedField" in f["loc"] for f in fields), fields
    assert all(set(f) == {"loc", "type"} for f in fields), f"лишние ключи в записи: {fields}"

    # Значение не должно просочиться НИ в одну запись лога, а не только в проверенную выше.
    assert _SENTINEL not in caplog.text
