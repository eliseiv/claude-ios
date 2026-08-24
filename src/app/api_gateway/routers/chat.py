"""Chat routes: /v1/chat/run, /v1/chat/tool-result (chat-orchestrator/02)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated, Any, cast

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import StreamingResponse

from app.api_gateway.rate_limit import enforce_chat_limits
from app.chat.orchestrator import ChatOrchestrator, ChatRunOut, ChatStreamEvent, ToolResultIn
from app.config import get_settings
from app.deps import (
    CurrentUser,
    client_ip,
    get_db,
    get_orchestrator,
    get_request_log_writer,
    get_v2_orchestrator,
    require_owner,
)
from app.errors import AppError, RateLimitedError
from app.observability.context import set_session_id
from app.request_logs.service import RequestLogWriter
from app.schemas.chat import (
    DEFAULT_GENERATION_MODE,
    ChatCapabilitiesResponse,
    ChatResponse,
    ChatRunRequest,
    ChatToolResultRequest,
    ChatV2RunRequest,
    GenerationMode,
    GenerationModeCapability,
    MediaChoicesSchema,
    MediaJobRefSchema,
    QuizSchema,
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
    "study_learn_mode": {
        "summary": "V2: study_learn (квиз)",
        "description": (
            "Обучающий ход: ответ приходит с полем `quiz` (пул вопросов для карточек), а "
            "`assistantMessage` при этом `null`. Режим выбирается на конкретный ход."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "sessionId": _SESSION_ID,
            "message": "Объясни async/await в Swift и проверь меня.",
            "mode": "credits",
            "generationMode": "study_learn",
        },
    },
    "temporary_chat": {
        "summary": "V2: temporary chat",
        "description": (
            "Новая сессия не появится в GET /v1/chats. Удаляйте через DELETE /v1/chats/{id}."
        ),
        "value": {
            "userId": "11111111-2222-3333-4444-555555555555",
            "message": "Быстрый вопрос без сохранения в истории.",
            "mode": "credits",
            "generationMode": "general",
            "temporary": True,
        },
    },
}

# Quiz pool shared by the v2 run and v2 tool-result examples: the SAME pool of the SAME turn comes
# back on every leg (turn-scoped field), which is exactly what the two examples must demonstrate.
_EXAMPLE_QUIZ = {
    "questions": [
        {
            "question": "Что делает оператор `await` в Swift?",
            "options": [
                "Блокирует поток",
                "Приостанавливает задачу до готовности результата",
                "Создаёт новый поток",
            ],
            "correctIndex": 1,
            "explanation": "`await` приостанавливает текущую задачу, не блокируя поток.",
        },
        {
            "question": "Где можно вызывать `await`?",
            "options": ["В любой функции", "Только в async-контексте"],
            "correctIndex": 1,
            "explanation": "`await` допустим только внутри `async`-функции или Task.",
        },
        {
            "question": "Что помечает ключевое слово `async`?",
            "options": [
                "Функцию, которая может приостанавливаться",
                "Функцию, которая всегда выполняется в фоне",
            ],
            "correctIndex": 0,
            "explanation": "`async` помечает функцию, которая может приостановить выполнение.",
        },
    ]
}

_V2_RUN_RESPONSE_EXAMPLES = {
    **_RUN_RESPONSE_EXAMPLES,
    "study_learn_quiz": {
        "summary": "Ответ с квизом (режим study_learn)",
        "description": (
            "Ход в режиме `study_learn`: содержимое ответа несёт `quiz.questions[]`, а "
            "`assistantMessage` = `null` — иначе модель продублировала бы вопросы текстом и "
            "раскрыла правильные ответы. Клиент рендерит карточки и проверяет ответы локально по "
            "`correctIndex`; отправлять их назад не нужно. Тот же пул придёт во всех ответах "
            "этого `messageStepId` — карточки заменяются, а не накапливаются."
        ),
        "value": {
            "status": "assistant_message",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_FINAL,
            "assistantMessage": None,
            "usage": {
                "inputTokens": 1310,
                "outputTokens": 540,
                "generationMode": "study_learn",
                "creditsCharged": 2,
            },
            "quiz": _EXAMPLE_QUIZ,
            "serverTools": [
                {
                    "toolCallId": _TOOL_CALL_ID,
                    "toolName": "quiz.generate",
                    "status": "completed",
                    "summary": "ok",
                }
            ],
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

_V2_TOOL_RESULT_RESPONSE_EXAMPLES = {
    **_TOOL_RESULT_RESPONSE_EXAMPLES,
    "study_learn_quiz": {
        "summary": "Continuation квиз-хода: тот же quiz",
        "description": (
            "Продолжение того же хода (`messageStepId` не менялся). Поле `quiz` — содержимое "
            "ХОДА, а не дельта запроса: приходит тот же пул, что и в ответе `/v1/chat/v2/run`, "
            "и `assistantMessage` так же `null`. Клиент заменяет карточки тем же содержимым — "
            "трактовать поле как «новый квиз» нельзя. `serverTools` при этом относится только к "
            "текущему вызову и может быть пустым."
        ),
        "value": {
            "status": "assistant_message",
            "sessionId": _SESSION_ID,
            "messageStepId": _MESSAGE_STEP_ID,
            "stepId": _STEP_ID_TOOL_RESULT_FINAL,
            "assistantMessage": None,
            "usage": {
                "inputTokens": 1620,
                "outputTokens": 180,
                "generationMode": "study_learn",
                "creditsCharged": 2,
            },
            "quiz": _EXAMPLE_QUIZ,
            "serverTools": [],
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
    # ADR-064 §7: the quiz pool of the TURN (turn-scoped — the orchestrator already resolved it for
    # this leg, whether it was produced in this call or recovered from the turn's steps).
    quiz = QuizSchema.model_validate(out.quiz) if out.quiz is not None else None
    # ADR-070: catalog-backed media picker (not the study_learn quiz).
    media_choices = (
        MediaChoicesSchema.model_validate(out.media_choices)
        if out.media_choices is not None
        else None
    )
    # ADR-068: media jobs of the TURN (submit-only refs; client polls /v1/media/jobs/{id}).
    media_jobs = (
        [MediaJobRefSchema.model_validate(item) for item in out.media_jobs]
        if out.media_jobs is not None
        else None
    )
    # ADR-064 §7, HARD half of the anti-spoiler guarantee — the single point where it is applied.
    # Keyed on the PRESENCE OF A QUIZ, not on the endpoint, the status or the generation mode:
    # - not on the mode, because a study_learn turn where the model produced NO quiz must still
    #   return its text (otherwise the user gets nothing at all);
    # - not on the status, because a truncated turn (blocked+max_tokens) would otherwise leak the
    #   partial text with duplicated questions and revealed answers. Every OTHER max_tokens rule
    #   (usage/messageStepId/stepId present, no debit) is untouched.
    # Because the key is the quiz itself, this can never fire on a legacy turn (quiz is always null
    # there), and non-quiz turns keep the ADR-024 п.3 behaviour verbatim.
    # History is NOT rewritten: chat_steps keeps the assistant step with its text as-is.
    assistant_message = None if quiz is not None else out.assistant_message
    return ChatResponse(
        status=out.status,
        sessionId=out.session_id,
        messageStepId=out.message_step_id,
        stepId=out.step_id,
        assistantMessage=assistant_message,
        toolCalls=tool_calls,
        toolCall=tool_call,
        blockReason=out.block_reason,
        usage=out.usage,
        quiz=quiz,
        mediaChoices=media_choices,
        mediaJobs=media_jobs,
        serverTools=server_tools,
    )


@router.get(
    "/v2/capabilities",
    response_model=ChatCapabilitiesResponse,
    summary="Получить доступные режимы генерации",
    description=(
        "Возвращает режимы генерации, которые ЭТОТ инстанс объявляет для UI-переключателя, плюс "
        "стоимость каждого режима в кредитах. Состав списка настраивается на инстансе: режим, "
        "который инстанс не объявляет, в массиве ОТСУТСТВУЕТ — читайте гейт как наличие/отсутствие "
        "элемента, а не как значение `available`. Отсутствие режима в списке НЕ означает, что "
        "`/v1/chat/v2/run` его не примет: это гейт объявления, а не поведения. Порядок элементов "
        "фиксирован, новые режимы добавляются в конец — клиент обязан игнорировать неизвестные ему "
        "значения `mode`. Подписка и баланс здесь не проверяются — это решает `/v1/chat/v2/run`."
    ),
)
async def chat_v2_capabilities(current: CurrentUser) -> ChatCapabilitiesResponse:
    _ = current  # endpoint is authenticated but does not need per-user state.
    settings = get_settings()
    provider = settings.llm_provider.strip().lower()
    # ADR-065 §1: the ADVERTISED set, not «every mode the backend understands». A mode outside the
    # instance allowlist is ABSENT from the array — deliberately not `available: false`, because the
    # clients this protects are already-released binaries that may ignore that field. The list is
    # already in canonical order and always contains `general` (= defaultGenerationMode).
    # This does NOT gate behaviour: /v1/chat/v2/run accepts every mode on every instance.
    # `available` is `true` for every element by construction — there is no producer of `false`
    # (ADR-065 §1.8); the field is kept for compatibility and reserved for a future, separate
    # «advertised but temporarily unavailable» decision.
    return ChatCapabilitiesResponse(
        provider=provider,
        defaultGenerationMode=DEFAULT_GENERATION_MODE,
        generationModes=[
            GenerationModeCapability(
                mode=cast(GenerationMode, mode),
                # Same single bridge as the balance gate and the debit — never a second price.
                creditCost=settings.chat_generation_credit_cost(mode),
                available=True,
            )
            for mode in settings.advertised_generation_modes()
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
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
    body: Annotated[ChatRunRequest, Body(openapi_examples=_RUN_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    log_id = await request_logs.start(
        user_id=current.user_id, endpoint=request.url.path, prompt=body.effective_user_text()
    )
    try:
        out = await orchestrator.run(
            user_id=current.user_id,
            project_id=body.projectId,
            session_id=body.sessionId,
            message=body.effective_user_text(),
            mode=body.mode,
            assistant_mode=body.assistantMode,
            attachments=body.attachments,
            model=body.model,
            workspace_project_id=body.workspaceProjectId,
            context=body.context,
            edit_message_step_id=body.editMessageStepId,
            generation_backend="legacy",
            temporary=body.temporary,
            memory_search=body.memorySearch,
        )
    except BaseException as exc:
        await request_logs.fail(
            log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
        )
        raise
    await request_logs.finish_chat(
        log_id,
        status_code=200,
        message_step_id=out.message_step_id,
        tokens_spent=out.credits_spent,
    )
    return _to_response(out)


@router.post(
    "/v2/run",
    response_model=ChatResponse,
    summary="Запустить шаг диалога через chat v2",
    description=(
        "Новая provider-neutral ручка для режимов `general`, `research`, `reasoning`, "
        "`study_learn`. `generationMode` выбирается на каждый ход и может меняться внутри одной "
        "сессии. `temporary: true` при создании сессии (без `sessionId`) делает чат временным: "
        "он не попадает в `GET /v1/chats`, но доступен по `sessionId` для multi-turn; клиент "
        "удаляет через `DELETE /v1/chats/{id}`. На resume поле игнорируется. OpenAI-ветка "
        "использует Responses API и `previous_response_id` там, где он сохранён; Anthropic-ветка "
        "использует Messages API с hosted web search или extended thinking для соответствующих "
        "режимов. В режиме `study_learn` ответ несёт пул вопросов в поле `quiz`, а "
        "`assistantMessage` = `null`. Стоимость в credits зависит от режима. "
        "Tool-loop продолжается через `/v1/chat/v2/tool-result`."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _V2_RUN_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_v2_run(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_v2_orchestrator)],
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
    body: Annotated[ChatV2RunRequest, Body(openapi_examples=_V2_RUN_REQUEST_EXAMPLES)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> ChatResponse:
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    media_selection = (
        body.mediaSelection.model_dump(by_alias=True, mode="json")
        if body.mediaSelection is not None
        else None
    )
    log_id = await request_logs.start(
        user_id=current.user_id, endpoint=request.url.path, prompt=body.effective_user_text()
    )
    try:
        out = await orchestrator.run(
            user_id=current.user_id,
            project_id=body.projectId,
            session_id=body.sessionId,
            message=body.effective_user_text(),
            mode=body.mode,
            assistant_mode=body.assistantMode,
            attachments=body.attachments,
            model=body.model,
            workspace_project_id=body.workspaceProjectId,
            context=body.context,
            edit_message_step_id=body.editMessageStepId,
            generation_mode=body.generationMode,
            generation_backend="v2",
            temporary=body.temporary,
            media_selection=media_selection,
            memory_search=body.memorySearch,
        )
    except BaseException as exc:
        await request_logs.fail(
            log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
        )
        raise
    await request_logs.finish_chat(
        log_id,
        status_code=200,
        message_step_id=out.message_step_id,
        tokens_spent=out.credits_spent,
    )
    return _to_response(out)


def _sse_frame(event: str, data: dict[str, Any]) -> bytes:
    """Format one SSE event frame (ADR-069)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _stream_event_frame(ev: ChatStreamEvent) -> bytes:
    if ev.kind == "delta":
        return _sse_frame("delta", {"text": ev.text})
    if ev.kind == "done" and ev.out is not None:
        resp = _to_response(ev.out)
        return _sse_frame(
            "done",
            resp.model_dump(by_alias=True, mode="json", exclude_none=False),
        )
    if ev.kind == "error":
        return _sse_frame(
            "error",
            {"code": ev.error_code or "error", "message": ev.error_message or ""},
        )
    raise RuntimeError(f"unknown ChatStreamEvent kind: {ev.kind}")


@router.post(
    "/v2/run/stream",
    summary="Запустить шаг диалога (SSE text stream)",
    description=(
        "Тот же body/auth/rate-limit, что у `/v1/chat/v2/run`, но ответ — "
        "`text/event-stream` ([ADR-069](../../docs/adr/ADR-069-sse-text-streaming.md)). "
        "События: `delta` (`{text}`), затем `done` (полный `ChatResponse`); при сбое после "
        "старта стрима — `error` (`{code,message}`). В `study_learn` дельт нет (анти-спойлер). "
        "JSON `/v2/run` без изменений."
    ),
    responses={
        200: {
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": (
                        'event: delta\ndata: {"text":"Hel"}\n\n'
                        'event: delta\ndata: {"text":"lo"}\n\n'
                        'event: done\ndata: {"status":"assistant_message",...}\n\n'
                    ),
                }
            }
        },
        **_CHAT_RESPONSES,
    },
)
async def chat_v2_run_stream(
    request: Request,
    current: CurrentUser,
    body: Annotated[ChatV2RunRequest, Body(openapi_examples=_V2_RUN_REQUEST_EXAMPLES)],
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
    x_device_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """SSE stream (ADR-069).

    The DB session lives only in a producer task (via ``get_db``), while this coroutine
    formats SSE frames. That keeps ``AsyncSession`` single-task and still lets text
    ``delta`` frames flush as the provider streams. Pre-stream errors are raised before
    ``StreamingResponse`` so FastAPI can return the usual 4xx.
    """
    require_owner(body.userId, current)
    device_id = x_device_id or current.device_id
    if not await enforce_chat_limits(
        user_id=current.user_id, device_id=device_id, ip=client_ip(request)
    ):
        raise RateLimitedError("rate limit exceeded")

    user_id = current.user_id
    log_id = await request_logs.start(
        user_id=user_id, endpoint=request.url.path, prompt=body.effective_user_text()
    )
    db_dep = request.app.dependency_overrides.get(get_db, get_db)
    queue: asyncio.Queue[ChatStreamEvent | BaseException | None] = asyncio.Queue()

    async def _produce() -> None:
        try:
            async for session in db_dep():
                orchestrator = get_v2_orchestrator(session)

                async def _on_delta(text: str) -> None:
                    await queue.put(ChatStreamEvent.delta(text))

                media_selection = (
                    body.mediaSelection.model_dump(by_alias=True, mode="json")
                    if body.mediaSelection is not None
                    else None
                )
                out = await orchestrator.run(
                    user_id=user_id,
                    project_id=body.projectId,
                    session_id=body.sessionId,
                    message=body.effective_user_text(),
                    mode=body.mode,
                    assistant_mode=body.assistantMode,
                    attachments=body.attachments,
                    model=body.model,
                    workspace_project_id=body.workspaceProjectId,
                    context=body.context,
                    edit_message_step_id=body.editMessageStepId,
                    generation_mode=body.generationMode,
                    generation_backend="v2",
                    temporary=body.temporary,
                    on_text_delta=_on_delta,
                    media_selection=media_selection,
                    memory_search=body.memorySearch,
                )
                await request_logs.finish_chat(
                    log_id,
                    status_code=200,
                    message_step_id=out.message_step_id,
                    tokens_spent=out.credits_spent,
                )
                await queue.put(ChatStreamEvent.done(out))
                break
        except BaseException as exc:
            await request_logs.fail(
                log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
            )
            await queue.put(exc)
        finally:
            await queue.put(None)

    produce_task = asyncio.create_task(_produce())
    first = await queue.get()
    if isinstance(first, BaseException):
        await produce_task
        raise first
    if first is None:
        await produce_task
        raise RuntimeError("chat stream ended without events")

    async def _events() -> AsyncIterator[bytes]:
        assert isinstance(first, ChatStreamEvent)
        yield _stream_event_frame(first)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    # Mid-stream failure: emit error frame (HTTP already 200).
                    yield _sse_frame(
                        "error",
                        {
                            "code": getattr(item, "code", None) or type(item).__name__,
                            "message": str(item) or type(item).__name__,
                        },
                    )
                    break
                yield _stream_event_frame(item)
        finally:
            if not produce_task.done():
                produce_task.cancel()
            with suppress(asyncio.CancelledError):
                await produce_task

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
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
    log_id = await request_logs.start(user_id=current.user_id, endpoint=request.url.path)
    try:
        out = await orchestrator.tool_result(
            user_id=current.user_id,
            session_id=body.sessionId,
            results=normalized,
            generation_backend="legacy",
        )
    except BaseException as exc:
        await request_logs.fail(
            log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
        )
        raise
    await request_logs.finish_chat(
        log_id,
        status_code=200,
        message_step_id=out.message_step_id,
        tokens_spent=out.credits_spent,
    )
    return _to_response(out)


@router.post(
    "/v2/tool-result",
    response_model=ChatResponse,
    summary="Передать результаты инструментов для chat v2",
    description=(
        "V2 continuation для tool-loop, начатого через `/v1/chat/v2/run`. Режим генерации и "
        "стоимость восстанавливаются из исходного user-step этого хода, поэтому body не содержит "
        "`generationMode`. Ответ несёт `quiz` того же ХОДА — и когда пул сформирован на этом "
        "витке, и когда он был выдан раньше в рамках того же `messageStepId`. "
        "Legacy tool-call продолжайте через `/v1/chat/tool-result`."
    ),
    responses={
        200: {"content": {"application/json": {"examples": _V2_TOOL_RESULT_RESPONSE_EXAMPLES}}},
        **_CHAT_RESPONSES,
    },
)
async def chat_v2_tool_result(
    request: Request,
    current: CurrentUser,
    orchestrator: Annotated[ChatOrchestrator, Depends(get_v2_orchestrator)],
    request_logs: Annotated[RequestLogWriter, Depends(get_request_log_writer)],
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
    log_id = await request_logs.start(user_id=current.user_id, endpoint=request.url.path)
    try:
        out = await orchestrator.tool_result(
            user_id=current.user_id,
            session_id=body.sessionId,
            results=normalized,
            generation_backend="v2",
        )
    except BaseException as exc:
        await request_logs.fail(
            log_id, status_code=exc.status_code if isinstance(exc, AppError) else 500
        )
        raise
    await request_logs.finish_chat(
        log_id,
        status_code=200,
        message_step_id=out.message_step_id,
        tokens_spent=out.credits_spent,
    )
    return _to_response(out)
