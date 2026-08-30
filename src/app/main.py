"""FastAPI app factory: middleware, routers, exception handlers (api-gateway/03)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api_gateway.middleware import (
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
    SizeLimitMiddleware,
)
from app.api_gateway.rate_limit import close_redis
from app.api_gateway.routers import (
    admin,
    auth,
    billing_adapty,
    billing_cloudpayments,
    byok,
    chat,
    chats,
    documents,
    health,
    media,
    media_templates,
    memory,
    models,
    notifications,
    policy,
    preferences,
    presets,
    preview,
    profile,
    subscription,
    token_purchase,
    tools,
    wallet,
    workspaces,
)
from app.config import get_settings
from app.db import dispose_engine
from app.errors import AppError
from app.media_generation.reconciler import reconciler_loop
from app.observability.context import get_request_id
from app.observability.logging import configure_logging, log_event

logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.storekit_test_mode and settings.storekit_test_secret:
        # test-mode: TD-007 (09-e2e-testing.md §2.4). Secret is never logged.
        logger.warning(
            "STOREKIT_TEST_MODE is ENABLED — accepting HS256 test transactions. "
            "MUST be false in production."
        )
    stop = asyncio.Event()
    reconciler_task: asyncio.Task[None] | None = None
    if settings.media_reconcile_interval_seconds > 0:
        reconciler_task = asyncio.create_task(
            reconciler_loop(stop, settings), name="media-reconciler"
        )
    try:
        yield
    finally:
        stop.set()
        if reconciler_task is not None:
            try:
                await asyncio.wait_for(reconciler_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                reconciler_task.cancel()
        await dispose_engine()
        await close_redis()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "requestId": get_request_id()}},
    )


_API_DESCRIPTION = """\
Backend-оркестратор Claude для iOS-приложения.

### Авторизация
Все `/v1/*` требуют заголовок `Authorization: Bearer <accessToken>` — access-токен в формате
JWT (RS256). В claim `sub` лежит
`userId`; поле `userId` в теле запроса обязано совпадать с `sub`, иначе `403`. Получите токен
через `POST /v1/auth/register`, нажмите **Authorize** и вставьте `accessToken` — он применится
ко всем вызовам. Endpoint `/health`, `/ready`, `/metrics` токен не требуют.

### Блокировки приходят с HTTP 200
Бизнес-блокировка генерации — это успешный ответ `200` с телом
`{status: "blocked", blockReason}`, а не ошибка. Технические ошибки — `4xx`/`5xx` с телом
`{error: {code, message, requestId}}`. Значения `blockReason` см. в описании одноимённого поля.
"""

_OPENAPI_TAGS = [
    {
        "name": "Auth",
        "description": (
            "Получение и обновление токена доступа. Точка входа для тестирования: регистрация "
            "устройства, выпуск и обновление токенов, публичный ключ (JWKS). Без JWT."
        ),
    },
    {
        "name": "Chat",
        "description": (
            "Диалог с ассистентом и tool-loop. Сценарий: `POST /v1/chat/run` → ответ "
            "`tool_call` → клиент исполняет инструмент → `POST /v1/chat/tool-result` → "
            "`assistant_message`. `toolCall.id` передаётся обратно в `toolCallId`. Блокировки "
            "по бизнес-правилам приходят с HTTP 200 и полем `blockReason`."
        ),
    },
    {
        "name": "Tools",
        "description": "Каталог инструментов tool-loop: имя, описание, mutating, место исполнения.",
    },
    {
        "name": "Models",
        "description": ("Доступные модели активного провайдера инстанса для селектора модели."),
    },
    {
        "name": "Presets",
        "description": "Пресеты промтов для чипов на главном экране чата.",
    },
    {
        "name": "Media",
        "description": (
            "Генерация фото и видео через fal.ai. Сценарий: `GET /v1/media/models` → "
            "`POST /v1/media/images`|`/videos` (ответ `202`, задача в статусе `queued`, кредиты "
            "списаны) → опрос `GET /v1/media/jobs/{jobId}` до `completed`/`failed`. Неудачная "
            "генерация возвращает кредиты. Готовый файл — signed URL на этот домен "
            "(`GET /v1/media/jobs/{jobId}/assets/{index}/{token}`, без JWT). При `completed` "
            "(и фоновом reconciler) — push APNs с `jobId`/`kind`/`mediaUrl`, если включены "
            "уведомления и зарегистрирован device token."
        ),
    },
    {
        "name": "Notifications",
        "description": (
            "Регистрация APNs device-токена (`POST`/`DELETE /v1/notifications/device-token`). "
            "Toggle доставки — `notificationsEnabled` в `/v1/preferences`. "
            "Deep link media-ready push: `jobId` + `kind` (чата у media нет)."
        ),
    },
    {
        "name": "Policy",
        "description": (
            "Эффективные права пользователя для UI: можно ли генерировать и почему нет "
            "(`reasons[]` с теми же значениями, что и `blockReason`)."
        ),
    },
    {
        "name": "Wallet",
        "description": "Баланс кредитов и списание (1 кредит = 1 сообщение).",
    },
    {
        "name": "Subscription",
        "description": "Синхронизация подписки StoreKit и начисление кредитов периода.",
    },
    {
        "name": "Tokens",
        "description": "Покупка пакетов токенов и каталог продуктов.",
    },
    {
        "name": "BYOK",
        "description": "Свой ключ Anthropic: сохранение, включение/выключение, удаление.",
    },
    {
        "name": "Admin",
        "description": (
            "Операторские действия под заголовком `X-Admin-Token`. Пользовательский JWT здесь "
            "не авторизует. Начисление кредитов и просмотр кошелька. Список пользователей — "
            "в теге Admin (CRM), `GET /v1/admin/users`."
        ),
    },
    {
        "name": "Admin (CRM)",
        "description": (
            "Панель пользователей broad-crm: список, карточка, оплаты, запросы, начисление "
            "токенов. Тот же секрет, что у Admin (`X-Admin-Token` или `X-Admin-Key`)."
        ),
    },
    {
        "name": "Preview",
        "description": (
            "Публичная отдача сгенерированных сайтов по подписанной ссылке. Без JWT: "
            "авторизация в подписи."
        ),
    },
    {
        "name": "Chats",
        "description": (
            "История чатов: список, поиск, шаги, переименование, закрепление, удаление. "
            "Доступ только владельца; чужой/несуществующий чат — 404."
        ),
    },
    {
        "name": "Workspaces",
        "description": (
            "Рабочие пространства (iOS «Projects»): имя, описание, кастомные инструкции и "
            "файлы-знания как контекст чатов проекта. Доступ только владельца; чужой/"
            "несуществующий workspace — 404."
        ),
    },
    {
        "name": "Profile",
        "description": "Профиль пользователя: отображаемое имя и `accountId`.",
    },
    {
        "name": "Preferences",
        "description": (
            "Пользовательские настройки: дефолтный тип ассистента (chat|code), уведомления и "
            "дефолты Code-контекста."
        ),
    },
    {
        "name": "Documents",
        "description": (
            "Текстовые документы чата: загрузка, список, чтение, замена содержимого и скачивание "
            "файлом. Те же документы создаёт и правит модель инструментами `document.*`."
        ),
    },
    {
        "name": "Memory",
        "description": (
            "Cross-chat RAG-память: семантический поиск по истории (`GET /v1/search`), "
            "явные факты (`/v1/memories`), toggle в `/v1/preferences`."
        ),
    },
    {
        "name": "Health",
        "description": "Служебные проверки и метрики (без JWT): liveness, readiness, Prometheus.",
    },
]


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="claude-ios-backend",
        version="0.1.0",
        description=_API_DESCRIPTION,
        openapi_tags=_OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Middleware (added in reverse execution order; outermost added last).
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(SizeLimitMiddleware)

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Ответ клиенту НЕ меняется: он намеренно безлик, чтобы не раскрывать форму схемы.
        # Но раньше подробности не попадали и в лог — `exc.errors()` выбрасывался целиком, и
        # какое поле не прошло, не видел НИКТО. Отсюда тупик поддержки: разработчик получает
        # «request validation failed» и не может двинуться дальше, а сервер знает ответ и молчит.
        #
        # Логируются ТОЛЬКО путь до поля (`loc`) и тип ошибки (`type`). Значения (`input`) и
        # текст (`msg`, он цитирует значение) отброшены намеренно: сюда приходят подписанные
        # StoreKit-транзакции и тела с вложениями, попадание которых в лог недопустимо.
        log_event(
            logger,
            logging.WARNING,
            "request_validation_failed",
            fields=[
                {"loc": ".".join(str(x) for x in e.get("loc", ())), "type": e.get("type")}
                for e in exc.errors()[:20]
            ],
        )
        return _error_response(422, "validation_error", "request validation failed")

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return _error_response(500, "internal_error", "internal error")

    for module in (
        auth,
        chat,
        tools,
        memory,
        models,
        presets,
        media,
        media_templates,
        policy,
        wallet,
        subscription,
        token_purchase,
        byok,
        admin,
        preview,
        chats,
        documents,
        workspaces,
        profile,
        preferences,
        notifications,
        billing_adapty,
        billing_cloudpayments,
    ):
        app.include_router(module.router)
    app.include_router(health.router)

    return app


app = create_app()
