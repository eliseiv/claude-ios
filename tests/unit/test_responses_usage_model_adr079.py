"""Себестоимость хода: имя модели в `usage.model` и выключенная provider-side цепочка (ADR-079).

Регрессия, которую закрывают эти тесты: Responses API отвечает ДАТИРОВАННЫМ СНАПШОТОМ модели
(`gpt-5.1-2025-11-13`), а закупочная таблица `CHAT_TOKEN_PRICES` ключуется АЛИАСОМ (`gpt-5.1`).
Пока клиент писал в `chat_steps.usage.model` имя ИЗ ОТВЕТА провайдера, каждый ход OpenAI-инстанса
оказывался непрайсуемым, и колонка «Себестоимость» в CRM была пуста — что неотличимо от «трафика
не было». Здесь фиксируется, что в `usage.model` попадает ЗАПРОШЕННЫЙ алиас, и что это верно на
ОБОИХ путях — обычном и стриминговом (ADR-069): стрим — это тот путь, которым ходит приложение.

Второй предмет — `_CONTINUATION_ENABLED` (TD-032). Цепочка `previous_response_id` выключена ЯВНЫМ
переключателем, а не побочным эффектом сравнения имён моделей. Именно этим побочным эффектом она
и была выключена раньше: снапшот в `provider_state.model` никогда не совпадал с запрошенным
алиасом. Починка себестоимости (алиас вместо снапшота) сама по себе МОЛЧА включила бы цепочку —
то есть сменила бы продуктовое поведение под видом правки учёта. Regression-guard ниже требует,
чтобы включение было осознанным решением, а не побочным следствием чужой правки.

Настоящих вызовов OpenAI нет: SDK-ресурс `responses` подменён in-memory фейком (общий с
`test_openai_client.py`), стриминговый фейк объявлен здесь.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import openai
import pytest

from app.chat.llm_client import NeutralMessage, StreamEvent
from app.chat.openai_responses_client import _CONTINUATION_ENABLED, OpenAIResponsesClient
from app.pricing.provider_prices import resolve_chat_price_model
from tests.unit.test_openai_client import _client_with_responses_fake, _response

# Датированный снапшот, которым Responses API отвечает на запрос алиаса `gpt-5.1`.
_SNAPSHOT = "gpt-5.1-2025-11-13"
_ALIAS = "gpt-5.1"


# --------------------------------- стриминговый фейк ---------------------------------


class _FakeStream:
    """Async-CM, который отдаёт дельты и финальный response — форма SDK `responses.stream(...)`."""

    def __init__(self, deltas: list[str], final: Any) -> None:
        self._deltas = deltas
        self._final = final

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    def __aiter__(self) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            for delta in self._deltas:
                yield SimpleNamespace(type="response.output_text.delta", delta=delta)

        return _gen()

    async def get_final_response(self) -> Any:
        return self._final


class _FakeStreamingResponses:
    """Ресурс `responses` со стримом: пишет kwargs вызова так же, как нестримовый фейк."""

    def __init__(self) -> None:
        self.next_response: Any = None
        self.deltas: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self.deltas, self.next_response)


def _user(text: str = "q") -> NeutralMessage:
    return NeutralMessage(role="user", content_blocks=[{"type": "text", "text": text}])


# ======================= usage.model = запрошенный алиас, не снапшот =======================


@pytest.mark.asyncio
async def test_usage_model_is_the_requested_alias_not_the_snapshot_the_provider_answered() -> None:
    client, fake = _client_with_responses_fake()
    fake.responses.next_response = _response(model=_SNAPSHOT, text="ok")

    result = await client.create_message(
        system_prompt="s", messages=[_user()], tools=[], attachments=None, model=_ALIAS
    )

    # Фикстура обязана подавать ИМЕННО датированное имя: действующая `_response()` по умолчанию
    # хардкодит алиас, и на такой фикстуре дефект был невидим — ответ и запрос совпадали.
    assert fake.responses.next_response.model == _SNAPSHOT
    assert _SNAPSHOT != _ALIAS
    assert fake.responses.calls[0]["model"] == _ALIAS
    assert result.usage.model == _ALIAS
    # И, собственно, ради чего: такое имя тарифицируется.
    assert resolve_chat_price_model(result.usage.model) == _ALIAS


@pytest.mark.asyncio
async def test_streaming_usage_model_is_the_requested_alias_too() -> None:
    """Стрим — путь приложения (ADR-069); дефект учёта на нём стоит ровно столько же."""
    client, fake = _client_with_responses_fake()
    streaming = _FakeStreamingResponses()
    streaming.next_response = _response(model=_SNAPSHOT, text="ok")
    streaming.deltas = ["o", "k"]
    fake.responses = streaming  # type: ignore[assignment]

    events: list[StreamEvent] = []
    async for event in client.stream_message(
        system_prompt="s", messages=[_user()], tools=[], attachments=None, model=_ALIAS
    ):
        events.append(event)

    assert [e.kind for e in events] == ["text_delta", "text_delta", "completed"]
    completed = events[-1].result
    assert completed is not None
    assert streaming.next_response.model == _SNAPSHOT
    assert streaming.calls[0]["model"] == _ALIAS
    assert completed.usage.model == _ALIAS
    assert resolve_chat_price_model(completed.usage.model) == _ALIAS


@pytest.mark.asyncio
async def test_usage_model_is_the_effective_model_after_the_reasoning_remap() -> None:
    """Remap gpt-4o → gpt-5-mini: считаем ту модель, которой РЕАЛЬНО был сделан вызов."""
    client, fake = _client_with_responses_fake()
    fake.responses.next_response = _response(model="gpt-5-mini-2025-08-07", text="ok")

    result = await client.create_message(
        system_prompt="s",
        messages=[_user()],
        tools=[],
        attachments=None,
        model="gpt-4o",
        generation_mode="reasoning",
    )

    assert fake.responses.calls[0]["model"] == "gpt-5-mini"
    assert result.usage.model == "gpt-5-mini"


@pytest.mark.asyncio
async def test_usage_without_counts_still_carries_the_requested_alias() -> None:
    """Ответ без `usage` не должен терять имя модели: иначе шаг непрайсуем по ДВУМ причинам."""
    client, fake = _client_with_responses_fake()
    response = _response(model=_SNAPSHOT, text="ok")
    response.usage = None
    fake.responses.next_response = response

    result = await client.create_message(
        system_prompt="s", messages=[_user()], tools=[], attachments=None, model=_ALIAS
    )

    assert result.usage.model == _ALIAS
    assert result.usage.input_tokens == 0


# ===================== regression-guard: цепочка выключена ЯВНО (TD-032) =====================


def test_continuation_switch_is_off() -> None:
    """Переключатель — предмет теста сам по себе: его значение и есть продуктовое поведение."""
    assert _CONTINUATION_ENABLED is False


@pytest.mark.parametrize(
    ("state", "model", "why"),
    [
        (
            {"provider": "openai", "responseId": "resp_prev", "model": _ALIAS},
            _ALIAS,
            "полностью валидное состояние с СОВПАДАЮЩЕЙ моделью",
        ),
        (
            {"provider": "openai", "responseId": "resp_prev"},
            _ALIAS,
            "валидное состояние без имени модели (старые сессии)",
        ),
        (
            {"provider": "openai", "responseId": "resp_prev", "model": "gpt-5"},
            "gpt-5",
            "валидное состояние на другой модели каталога",
        ),
    ],
)
def test_usable_previous_response_id_is_none_even_for_a_valid_state(
    state: dict[str, Any], model: str, why: str
) -> None:
    """Ни одно состояние не даёт handle, пока `_CONTINUATION_ENABLED` выключен.

    Раньше `None` здесь получался ПОБОЧНО — из-за несовпадения снапшота с алиасом. Теперь имена
    совпадают, и если бы решала только проверка модели, цепочка включилась бы сама собой.
    """
    assert OpenAIResponsesClient._usable_previous_response_id(state, model=model) is None, why


@pytest.mark.asyncio
async def test_valid_state_does_not_reach_the_wire_and_history_is_replayed_in_full() -> None:
    """Тот же инвариант на уровне запроса: ни `previous_response_id`, ни обрезанного входа."""
    client, fake = _client_with_responses_fake()
    fake.responses.next_response = _response(model=_SNAPSHOT, response_id="resp_next", text="ok")
    messages = [
        _user("old user"),
        NeutralMessage(role="assistant", content_blocks=[{"type": "text", "text": "old answer"}]),
        _user("new user"),
    ]

    await client.create_message(
        system_prompt="s",
        messages=messages,
        tools=[],
        attachments=None,
        model=_ALIAS,
        provider_state={"provider": "openai", "responseId": "resp_prev", "model": _ALIAS},
    )

    sent = fake.responses.calls[0]
    assert sent["previous_response_id"] is openai.NOT_GIVEN
    assert len(sent["input"]) == 3


@pytest.mark.asyncio
async def test_streaming_valid_state_does_not_reach_the_wire_either() -> None:
    client, fake = _client_with_responses_fake()
    streaming = _FakeStreamingResponses()
    streaming.next_response = _response(model=_SNAPSHOT, response_id="resp_next", text="ok")
    fake.responses = streaming  # type: ignore[assignment]

    async for _event in client.stream_message(
        system_prompt="s",
        messages=[_user("old user"), _user("new user")],
        tools=[],
        attachments=None,
        model=_ALIAS,
        provider_state={"provider": "openai", "responseId": "resp_prev", "model": _ALIAS},
    ):
        pass

    assert streaming.calls[0]["previous_response_id"] is openai.NOT_GIVEN
    assert len(streaming.calls[0]["input"]) == 2
