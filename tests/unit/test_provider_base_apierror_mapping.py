"""Отказ провайдера, пришедший СОБЫТИЕМ ВНУТРИ потока, обязан давать 502, а не 500.

Прод 2026-09-09, avelyra: «You have no credits remaining» прилетело от OpenAI не HTTP-статусом,
а событием потока, и SDK поднял ГОЛЫЙ `APIError`. Перехваты в клиентах ловили только его
подклассы (`APIStatusError`, `APITimeoutError`, `APIConnectionError`), поэтому ошибка ушла наружу
сырой: `/v1/chat/v2/run` отдал штатный 503, а `/v1/chat/v2/run/stream` — 500 unhandled_error.

Каждый кейс здесь падает при откате СВОЕГО перехвата: убрать `except openai.APIError` — падают
openai-кейсы; убрать `except anthropic.APIError` — падают anthropic-кейсы; убрать разворачивание
группы — падает кейс `_unwrap_exception_group`.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import openai
import pytest

from app.api_gateway.routers.chat import _unwrap_exception_group
from app.chat.anthropic_client import AnthropicClient
from app.chat.openai_responses_client import OpenAIResponsesClient
from app.errors import AppError, UpstreamError

_OPENAI_REQ = httpx.Request("POST", "https://api.openai.com/v1/responses")
_ANTHROPIC_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

_NO_CREDITS = "You have no credits remaining."


def _bare_openai_error() -> openai.APIError:
    """Ровно та форма, что пришла на проде: базовый класс, БЕЗ http-статуса."""
    return openai.APIError(_NO_CREDITS, request=_OPENAI_REQ, body=None)


def _bare_anthropic_error() -> anthropic.APIError:
    return anthropic.APIError(_NO_CREDITS, request=_ANTHROPIC_REQ, body=None)


def test_bare_openai_error_is_not_a_status_error() -> None:
    """Предпосылка кейсов ниже: голый APIError НЕ подклассы, которые ловились раньше."""
    exc = _bare_openai_error()
    assert not isinstance(exc, openai.APIStatusError)
    assert not isinstance(exc, openai.APIConnectionError)


def test_bare_anthropic_error_is_not_a_status_error() -> None:
    exc = _bare_anthropic_error()
    assert not isinstance(exc, anthropic.APIStatusError)
    assert not isinstance(exc, anthropic.APIConnectionError)


@pytest.mark.asyncio
async def test_anthropic_create_maps_bare_apierror_to_upstream() -> None:
    client = AnthropicClient()
    client._client.messages.create = AsyncMock(side_effect=_bare_anthropic_error())  # type: ignore[method-assign]
    with pytest.raises(UpstreamError):
        await client.create_message(system_prompt="sys", messages=[], tools=[])


@pytest.mark.asyncio
async def test_anthropic_stream_maps_bare_apierror_to_upstream() -> None:
    """Путь ПОТОКА — тот самый, на котором дефект и проявился."""
    client = AnthropicClient()
    client._client.messages.stream = MagicMock(side_effect=_bare_anthropic_error())  # type: ignore[method-assign]
    with pytest.raises(UpstreamError):
        async for _ in client.stream_message(system_prompt="sys", messages=[], tools=[]):
            pass


@pytest.mark.asyncio
async def test_openai_stream_maps_bare_apierror_to_upstream(monkeypatch: Any) -> None:
    client = OpenAIResponsesClient()
    responses_api = MagicMock()
    responses_api.stream = MagicMock(side_effect=_bare_openai_error())
    fake_sdk = MagicMock()
    fake_sdk.responses = responses_api
    monkeypatch.setattr(client, "_client_for", lambda *a, **k: fake_sdk, raising=False)
    monkeypatch.setattr(client, "_client", fake_sdk, raising=False)
    with pytest.raises(UpstreamError):
        async for _ in client.stream_message(system_prompt="sys", messages=[], tools=[]):
            pass


# --------------------------- разворачивание группы исключений ---------------------------


def test_unwrap_prefers_app_error_inside_group() -> None:
    inner = UpstreamError("openai upstream error")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    assert _unwrap_exception_group(group) is inner


def test_unwrap_descends_through_nested_groups() -> None:
    inner = UpstreamError("openai upstream error")
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [inner])])
    unwrapped = _unwrap_exception_group(group)
    assert isinstance(unwrapped, AppError)
    assert unwrapped is inner


def test_unwrap_returns_single_non_app_error() -> None:
    inner = _bare_openai_error()
    group = ExceptionGroup("outer", [inner])
    assert _unwrap_exception_group(group) is inner


def test_unwrap_keeps_group_when_several_unrelated_errors() -> None:
    """Выбрать «главное» из нескольких нельзя — молча взять первое значило бы соврать."""
    group = ExceptionGroup("outer", [ValueError("a"), KeyError("b")])
    assert _unwrap_exception_group(group) is group


def test_unwrap_passes_plain_exception_through() -> None:
    exc = UpstreamError("x")
    assert _unwrap_exception_group(exc) is exc
