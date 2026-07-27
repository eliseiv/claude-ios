"""Chat routes: /v1/chat/run, /v1/chat/tool-result (chat-orchestrator/02)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request

from app.api_gateway.rate_limit import enforce_chat_limits
from app.chat.orchestrator import ChatOrchestrator, ChatRunOut, ToolResultIn
from app.config import get_settings
from app.deps import (
    CurrentUser,
    client_ip,
    get_orchestrator,
    get_v2_orchestrator,
    require_owner,
)
from app.errors import RateLimitedError
from app.observability.context import set_session_id
from app.schemas.chat import (
    ChatCapabilitiesResponse,
    ChatResponse,
    ChatRunRequest,
    ChatToolResultRequest,
    ChatV2RunRequest,
    GenerationModeCapability,
    ServerToolExecutionSchema,
    ToolCallSchema,
)

router = APIRouter(prefix="/v1/chat", tags=["Chat"])

# --- Согласованные id для end-to-end tool-loop примеров (run -> tool_call -> tool-result) ---
_SESSION_ID = "3f1c2a7e-9b54-4d2e-8a11-6c0d5e7f1a23"
_TOOL_CALL_ID = "a7b9c1d2-3e4f-5061-7283-94a5b6c7d8e9"
# Second parallel tool-call id for the multi-tool (parallel tool use) example (ADR-025).
_TOOL_CALL_ID_2 = "f1e2d3c4-b5a6-4978-8c0d-1e2f3a4b5c6d"
# Один messageStepId на весь ход (стабилен через tool-loop); stepId — у каждого шага свой.
_MESSAGE_STEP_ID = "b1e2d3c4-5f60-4718-9a2b-3c4d5e6f7081"
_STEP_ID_TOOL_CALL = "c2f3e4d5-6071-4829-ab3c-4d5e6f708192"
_STEP_ID_FINAL = "d3041526-7182-493a-bc4d-5e6f708192a3"
_STEP_ID_TOOL_RESULT_FINAL = "e4152637-8293-4a4b-cd5e-6f708192a3b4"

# Tiny valid base64-encoded 1x1 PNG for the Swagger attachment example (not a real photo).
_EXAMPLE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

_RUN_RESPONSE_EXAMPLES = {
    "assistant_message": {
        "summary": "Ответ ассистента (финал)",
        "description": "Модель ответила текстом, генерация списана. Без toolCall/blockReason.",
        "value": {
            "status": "assistant_message",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_FINAL,
            "assistantMessage": "Конечно! Вот краткое содержание вашего файла…",
            "usage": {"inputTokens": 1240, "outputTokens": 320},
        },
    },
    "tool_call": {
        "summary": "Запрос на вызов инструментов",
        "description": (
            "Ассистент просит клиента выполнить инструменты на устройстве. `toolCalls[]` содержит "
            "ВСЕ вызовы хода (здесь — два `files.write` параллельно). Клиент исполняет каждый и "
            "возвращает результаты батчем через `POST /v1/chat/tool-result`. Поле `toolCall` = "
            "`toolCalls[0]` (deprecated, читайте `toolCalls[]`)."
        ),
        "value": {
            "status": "tool_call",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_CALL,
            "toolCalls": [
                {
                    "id": _TOOL_CALL_ID,
                    "name": "files.write",
                    "args": {"path": "index.html", "content": "<!doctype html>…"},
                },
                {
                    "id": _TOOL_CALL_ID_2,
                    "name": "files.write",
                    "args": {"path": "style.css", "content": "body{…}"},
                },
            ],
            "toolCall": {
                "id": _TOOL_CALL_ID,
                "name": "files.write",
                "args": {"path": "index.html", "content": "<!doctype html>…"},
            },
            "usage": {"inputTokens": 980, "outputTokens": 220},
        },
    },
    "blocked": {
        "summary": "Блокировка по бизнес-правилам (HTTP 200)",
        "description": (
            "Баланс кредитов исчерпан. Это успешный ответ 200, а не ошибка. UI показывает "
            "баланс и предлагает пополнение/подписку."
        ),
        "value": {
            "status": "blocked",
            "sessionId": _SESSION_ID,
            "messageStepId": None,
            "stepId": None,
            "blockReason": "credits_empty",
        },
    },
    "blocked_max_tokens": {
        "summary": "Ответ обрезан лимитом токенов (HTTP 200)",
        "description": (
            "Модель не успела завершить ход — ответ обрезан лимитом output-токенов. В отличие от "
            "policy-блокировки: `usage`/`messageStepId`/`stepId` присутствуют, кредит не списан, "
            "`toolCalls`/`toolCall` не отдаются. UI: повторить или сократить запрос."
        ),
        "value": {
            "status": "blocked",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_CALL,
            "assistantMessage": "Вот начало лендинга…",
            "blockReason": "max_tokens",
            "usage": {"inputTokens": 1240, "outputTokens": 16000},
        },
    },
}

_RUN_REQUEST_EXAMPLES = {
    "clean_chat": {
        "summary": "Чистый чат без projectId",
        "description": "Без `projectId` сессия создаётся без проекта.",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "message": "Объясни, как работает async/await в Python.",
            "mode": "credits",
        },
    },
    "credits_mode": {
        "summary": "Запуск шага диалога, режим credits",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "projectId": "my-ios-project",
            "sessionId": _SESSION_ID,
            "message": "Прочитай файл notes.md и сделай краткое содержание.",
            "mode": "credits",
            "context": {"locale": "ru-RU"},
        },
    },
    "with_attachment": {
        "summary": "Сообщение с вложением, фото",
        "description": (
            "Поле `attachments` принимает фото, PDF и текстовые файлы в base64. `type` — класс "
            "вложения, `mediaType` — MIME из allowlist, `data` — содержимое в base64. Вложения "
            "отправляются модели только в первом сообщении; в `/v1/chat/tool-result` не "
            "принимаются. Только base64, URL запрещены."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "projectId": "my-ios-project",
            "message": "Что на этом фото?",
            "mode": "credits",
            "attachments": [
                {
                    "type": "image",
                    "mediaType": "image/png",
                    "filename": "photo.png",
                    "data": _EXAMPLE_PNG_B64,
                }
            ],
        },
    },
}

_V2_RUN_REQUEST_EXAMPLES = {
    **_RUN_REQUEST_EXAMPLES,
    "research_mode": {
        "summary": "V2: research",
        "description": (
            "Один ход с hosted web search. Следующий ход той же сессии может быть general."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "message": "Найди свежие факты и кратко сравни варианты.",
            "mode": "credits",
            "generationMode": "research",
        },
    },
    "reasoning_mode": {
        "summary": "V2: reasoning",
        "description": "Один ход с reasoning/thinking. Уровень effort задаётся серверной ENV.",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "message": "Разбери задачу по шагам и предложи надежное решение.",
            "mode": "credits",
            "generationMode": "reasoning",
        },
    },
}

_TOOL_RESULT_RESPONSE_EXAMPLES = {
    "assistant_message": {
        "summary": "Финал tool-loop",
        "description": (
            "Барьер хода закрыт (получены результаты на все `toolCalls[]`) — модель выдала "
            "итоговый ответ."
        ),
        "value": {
            "status": "assistant_message",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_RESULT_FINAL,
            "assistantMessage": "Готово. Лендинг собран из index.html и style.css.",
            "usage": {"inputTokens": 1500, "outputTokens": 210},
        },
    },
    "awaiting_results": {
        "summary": "Барьер не закрыт — ждём остальные результаты",
        "description": (
            "Прислан результат части вызовов хода. `toolCalls[]` — оставшиеся вызовы, по которым "
            "результаты ещё ожидаются. Модель не вызывается, кредит не списывается, пока барьер "
            "не закрыт."
        ),
        "value": {
            "status": "tool_call",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_CALL,
            "toolCalls": [
                {
                    "id": _TOOL_CALL_ID_2,
                    "name": "files.write",
                    "args": {"path": "style.css", "content": "body{…}"},
                }
            ],
            "toolCall": {
                "id": _TOOL_CALL_ID_2,
                "name": "files.write",
                "args": {"path": "style.css", "content": "body{…}"},
            },
        },
    },
}

_TOOL_RESULT_REQUEST_EXAMPLES = {
    "batch": {
        "summary": "Батч результатов на все вызовы хода (рекомендуется)",
        "description": (
            "Результаты на все `toolCalls[]` хода одним запросом — барьер закрывается сразу. В "
            "каждом элементе ровно одно из `result`/`error`."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "results": [
                {
                    "toolCallId": _TOOL_CALL_ID,
                    "result": {"path": "index.html", "bytesWritten": 512},
                },
                {
                    "toolCallId": _TOOL_CALL_ID_2,
                    "result": {"path": "style.css", "bytesWritten": 64},
                },
            ],
        },
    },
    "single_deprecated": {
        "summary": "Одиночная форма (deprecated)",
        "description": (
            "Старая форма `toolCallId` + `result|error` на верхнем уровне. Эквивалентна батчу из "
            "одного. Поддерживается ради совместимости; используйте `results[]`."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "toolCallId": _TOOL_CALL_ID,
            "result": {"path": "index.html", "bytesWritten": 512},
        },
    },
    "error": {
        "summary": "Ошибка исполнения инструмента (батч)",
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "results": [
                {
                    "toolCallId": _TOOL_CALL_ID,
                    "error": {"code": "not_found", "message": "Файл не найден на устройстве"},
                }
            ],
        },
    },
}

_CHAT_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"description": "Невалидная схема запроса."},
    429: {"description": "Жёсткое превышение rate limit (мягкое приходит как blocked, HTTP 200)."},
}


def _to_response(out: ChatRunOut) -> ChatResponse:
    set_session_id(str(out.session_id))
    tool_call = (
        ToolCallSchema(id=out.tool_call.id, name=out.tool_call.name, args=out.tool_call.args)
        if out.tool_call is not None
        else None
    )
    # ADR-025: surface ALL client-side tool calls of the turn; toolCall (deprecated) = toolCalls[0].
    tool_calls = (
        [ToolCallSchema(id=tc.id, name=tc.name, args=tc.args) for tc in out.tool_calls]
        if out.tool_calls is not None
        else None
    )
    # ADR-028: server-side tools executed by the backend in this call (compact name/status/summary).
    # ADR-030: toolCallId = domain tool_calls.id (uuid → str), correlates with /v1/chats/{id} steps.
    server_tools = [
        ServerToolExecutionSchema(
            toolCallId=str(st.tool_call_id),
            toolName=st.tool_name,
            status=st.status,
            summary=st.summary,
        )
        for st in out.server_tools
    ]
    return ChatResponse(
        status=out.status,
        sessionId=out.session_id,
        messageStepId=out.message_step_id,
        stepId=out.step_id,
        assistantMessage=out.assistant_message,
        toolCalls=tool_calls,
        toolCall=tool_call,
        blockReason=out.block_reason,
        usage=out.usage,
        serverTools=server_tools,
    )


@router.get(
    "/v2/capabilities",
    response_model=ChatCapabilitiesResponse,
    summary="Получить доступные режимы генерации",
    description=(
        "Возвращает режимы генерации, которые backend понимает в `generationMode`: `general`, "
        "`research`, `reasoning`, плюс стоимость каждого режима в кредитах. Это backend contract "
        "для UI single-select; конкретная подписка/баланс проверяются в `/v1/chat/v2/run`."
    ),
)
async def chat_v2_capabilities(current: CurrentUser) -> ChatCapabilitiesResponse:
    _ = current  # endpoint is authenticated but does not need per-user state.
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()
    return ChatCapabilitiesResponse(
        provider=provider,
        defaultGenerationMode="general",
        generationModes=[
            GenerationModeCapability(
                mode="general",
                creditCost=settings.chat_generation_credit_cost("general"),
                available=True,
            ),
            GenerationModeCapability(
                mode="research",
                creditCost=settings.chat_generation_credit_cost("research"),
                available=True,
            ),
            GenerationModeCapability(
                mode="reasoning",
                creditCost=settings.chat_generation_credit_cost("reasoning"),
                available=True,
            ),
        ],
        reasoningLevel=settings.resolved_reasoning_level(),
    )


@router.post(
    "/run",
    response_model=ChatResponse,
    summary="Запустить шаг диалога",
    description=(
        "Legacy-ручка. Принимает сообщение пользователя и возвращает одно из трёх состояний: "
        "`assistant_message` (готовый ответ), `tool_call` (выполните инструмент на устройстве "
        "и пришлите результат в `/v1/chat/tool-result`) или `blocked`. "
        "Не использует `generationMode`, OpenAI Responses API или `previous_response_id`; "
        "контекст полностью собирается из локальной истории. Для режимов `research`/`reasoning` "
        "используйте `/v1/chat/v2/run`. "
        "Блокировки приходят с HTTP 200 и полем `blockReason`; технические ошибки — `4xx`/`5xx`. "
        "Необязательный заголовок `X-Device-Id` задаёт устройство для rate limit."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _RUN_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_run(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_orchestrator)],
    body: Annotated[ChatRunRequest, Body(openapi_examples=_RUN_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    out = await orchestrator.run(
        user_id=current.user_id,
        project_id=body.projectId,
        session_id=body.sessionId,
        message=body.message,
        mode=body.mode,
        assistant_mode=body.assistantMode,
        attachments=body.attachments,
        model=body.model,
        workspace_project_id=body.workspaceProjectId,
        # ADR-037: per-message conversation settings (allowlist + render → injected into the turn-0
        # user message inside orchestrator.run; not session-fixed, not stored).
        context=body.context,
        # ADR-040: edit+regenerate — truncate history from this turn and generate a new one.
        edit_message_step_id=body.editMessageStepId,
        generation_backend="legacy",
    )
    return _to_response(out)


@router.post(
    "/v2/run",
    response_model=ChatResponse,
    summary="Запустить шаг диалога через chat v2",
    description=(
        "Новая provider-neutral ручка для режимов `general`, `research`, `reasoning`. "
        "`generationMode` выбирается на каждый ход и может меняться внутри одной сессии. "
        "OpenAI-ветка использует Responses API и `previous_response_id` там, где он сохранён; "
        "Anthropic-ветка использует Messages API с hosted web search или extended thinking для "
        "соответствующих режимов. Стоимость в credits зависит от режима. "
        "Tool-loop продолжается через `/v1/chat/v2/tool-result`."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _RUN_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_v2_run(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_v2_orchestrator)],
    body: Annotated[ChatV2RunRequest, Body(openapi_examples=_V2_RUN_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    out = await orchestrator.run(
        user_id=current.user_id,
        project_id=body.projectId,
        session_id=body.sessionId,
        message=body.message,
        mode=body.mode,
        assistant_mode=body.assistantMode,
        attachments=body.attachments,
        model=body.model,
        workspace_project_id=body.workspaceProjectId,
        context=body.context,
        edit_message_step_id=body.editMessageStepId,
        generation_mode=body.generationMode,
        generation_backend="v2",
    )
    return _to_response(out)


@router.post(
    "/tool-result",
    response_model=ChatResponse,
    summary="Передать результаты инструментов",
    description=(
        "Пришлите результаты вызовов из предыдущего `tool_call`. Рекомендуемая форма — батч "
        "`results[]` (по элементу на каждый `toolCalls[].id`, в каждом ровно одно из "
        "`result`/`error`); поддерживается deprecated одиночная форма (`toolCallId` + "
        "`result|error`). Продолжение к модели запускается только когда собраны результаты на все "
        "вызовы хода (барьер) — иначе ответ снова `tool_call` с оставшимися `toolCalls[]`. "
        "Блокировки приходят с HTTP 200 и полем `blockReason`; технические ошибки — `4xx`/`5xx`."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _TOOL_RESULT_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_tool_result(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_orchestrator)],
    body: Annotated[ChatToolResultRequest, Body(openapi_examples=_TOOL_RESULT_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    # ADR-025: normalize batch/single forms to a list; map each item's error body to a plain dict.
    normalized = [
        ToolResultIn(
            tool_call_id=item.toolCallId,
            result=item.result,
            error=item.error.model_dump() if item.error is not None else None,
        )
        for item in body.normalized_results()
    ]
    out = await orchestrator.tool_result(
        user_id=current.user_id,
        session_id=body.sessionId,
        results=normalized,
        generation_backend="legacy",
    )
    return _to_response(out)


@router.post(
    "/v2/tool-result",
    response_model=ChatResponse,
    summary="Передать результаты инструментов для chat v2",
    description=(
        "V2 continuation для tool-loop, начатого через `/v1/chat/v2/run`. Режим генерации и "
        "стоимость восстанавливаются из исходного user-step этого хода, поэтому body не содержит "
        "`generationMode`. Legacy tool-call продолжайте через `/v1/chat/tool-result`."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _TOOL_RESULT_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_v2_tool_result(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_v2_orchestrator)],
    body: Annotated[ChatToolResultRequest, Body(openapi_examples=_TOOL_RESULT_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    normalized = [
        ToolResultIn(
            tool_call_id=item.toolCallId,
            result=item.result,
            error=item.error.model_dump() if item.error is not None else None,
        )
        for item in body.normalized_results()
    ]
    out = await orchestrator.tool_result(
        user_id=current.user_id,
        session_id=body.sessionId,
        results=normalized,
        generation_backend="v2",
    )
    return _to_response(out)
