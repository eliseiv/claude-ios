"""Непрайсуемый шаг слышен с ПИШУЩЕГО пути реального хода чата (ADR-079).

Юнит-тесты (`tests/unit/test_chat_unpriced_metric_adr079.py`) закрепляют поведение самой функции
`report_chat_step_pricing`. Здесь проверяется то, чего юнит проверить не может: что она
действительно стоит НА ПУТИ ЗАПИСИ реального хода — рядом с `token_usage_total`, — и срабатывает
ровно один раз на КАЖДЫЙ вызов LLM, а не на HTTP-запрос и не на ход целиком.

Реальный PostgreSQL (testcontainers), Anthropic подменён на границе клиента. Счётчик
процесс-глобальный, поэтому измеряется ДЕЛЬТА, а не абсолют: так тест не зависит от того, что
насчитали соседние тесты.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.anthropic_client import AnthropicResult, AnthropicUsage
from app.observability.metrics import chat_unpriced_steps_total
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

# Имя, которого закупочная таблица не знает и знать не может: суффикс не датированный, поэтому
# резолв к алиасу не сработает и шаг останется непрайсуемым.
_UNPRICED_MODEL = "claude-unreleased-x-preview"
_PRICED_MODEL = "claude-sonnet-4-5"


def _usage(model: str) -> AnthropicUsage:
    return AnthropicUsage(
        input_tokens=10,
        output_tokens=5,
        model=model,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )


def _with_model(result: Any, model: str) -> Any:
    """Тот же результат, что отдаёт conftest-фейк, но с подменённым именем модели в usage."""
    assert isinstance(result, AnthropicResult)
    return dataclasses.replace(result, usage=_usage(model))


def _total() -> float:
    return sum(
        sample.value
        for metric in chat_unpriced_steps_total.collect()
        for sample in metric.samples
        if sample.name == "chat_unpriced_steps_total"
    )


def _count(model: str, reason: str) -> float:
    return chat_unpriced_steps_total.labels(model=model, reason=reason)._value.get()  # noqa: SLF001


@pytest.mark.asyncio
async def test_unpriceable_turn_increments_the_counter_exactly_once(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [_with_model(fake_anthropic.text_result("ok"), _UNPRICED_MODEL)]
    before_total = _total()
    before_labelled = _count(_UNPRICED_MODEL, "unknown_model")

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"
    assert len(fake_anthropic.calls) == 1
    assert _count(_UNPRICED_MODEL, "unknown_model") - before_labelled == 1
    assert _total() - before_total == 1


@pytest.mark.asyncio
async def test_priceable_turn_moves_nothing(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Серия — сигнал дефекта: обычный оплаченный ход не должен в ней появляться."""
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [_with_model(fake_anthropic.text_result("ok"), _PRICED_MODEL)]
    before_total = _total()

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "hi", "mode": "credits"},
        headers=auth_headers(uid),
    )

    assert r.status_code == 200, r.text
    assert _total() == before_total


@pytest.mark.asyncio
async def test_tool_loop_counts_every_llm_call_not_the_request(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Один HTTP-запрос, два вызова LLM (server-side раунд `time.now`) → две записи в серии.

    Ровно это отличает «шаги» от «ходов»: провайдер выставил счёт за КАЖДЫЙ вызов, и пробел в
    прайсе у каждого из них — отдельный факт.
    """
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)
    fake_anthropic.responses = [
        _with_model(
            fake_anthropic.tool_result("time.now", {}, tool_id="toolu_unpriced01"),
            _UNPRICED_MODEL,
        ),
        _with_model(fake_anthropic.text_result("today is ..."), _UNPRICED_MODEL),
    ]
    before = _count(_UNPRICED_MODEL, "unknown_model")

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "what day is it?", "mode": "credits"},
        headers=auth_headers(uid),
    )

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assistant_message"
    assert len(fake_anthropic.calls) == 2  # предпосылка теста: ход действительно был двухшаговым
    assert _count(_UNPRICED_MODEL, "unknown_model") - before == 2
