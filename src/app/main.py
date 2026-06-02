"""FastAPI app factory: middleware, routers, exception handlers (api-gateway/03)."""

from __future__ import annotations

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
    byok,
    chat,
    chats,
    health,
    policy,
    preferences,
    preview,
    profile,
    subscription,
    token_purchase,
    wallet,
)
from app.config import get_settings
from app.db import dispose_engine
from app.errors import AppError
from app.observability.context import get_request_id
from app.observability.logging import configure_logging

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
    yield
    await dispose_engine()
    await close_redis()


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "requestId": get_request_id()}},
    )


_API_DESCRIPTION = """\
Backend-оркестратор Claude для iOS-приложения: принимает запросы от приложения, проверяет
права доступа, ходит в Anthropic Messages API и ведёт биллинг.

### Как авторизоваться
Все `/v1/*` endpoint требуют заголовок `Authorization: Bearer <JWT>` (RS256). В claim `sub`
лежит `userId`; поле `userId` в теле запроса обязано совпадать с `sub`, иначе вернётся `403`.
Нажмите **Authorize** и введите `Bearer <JWT>` один раз — он применится ко всем вызовам.
Служебные endpoint (`/health`, `/ready`, `/metrics`) JWT не требуют.

### Правила доступа (бизнес)
Сначала одна бесплатная пробная генерация (trial). Дальше — либо активная подписка с
кредитами (1 кредит = 1 сообщение), либо собственный ключ Anthropic (BYOK). Эффективные
права для UI отдаёт `GET /v1/policy/effective`.

### Важно: блокировки приходят с HTTP 200
Бизнес-блокировка генерации — это **успешный** ответ `200 OK` с телом
`{status: "blocked", blockReason}`, а **не** ошибка 4xx (см. ADR-004). Технические ошибки —
4xx/5xx со стандартным `{error: {code, message, requestId}}`. Перечень значений `blockReason`
и их трактовку для UI см. в описании поля `blockReason` (endpoint Chat).
"""

_OPENAPI_TAGS = [
    {
        "name": "Chat",
        "description": (
            "Диалог с ассистентом и tool-loop (вызовы инструментов на устройстве). "
            "Сценарий end-to-end: `POST /v1/chat/run` → ответ `tool_call` → клиент исполняет "
            "инструмент локально → `POST /v1/chat/tool-result` → финальный `assistant_message`. "
            "id из `toolCall.id` передаётся обратно в `toolCallId`. Блокировки по бизнес-"
            "правилам приходят с HTTP 200 и полем `blockReason` (см. ADR-004)."
        ),
    },
    {
        "name": "Policy",
        "description": (
            "Эффективные права пользователя для UI: можно ли генерировать в режимах "
            "credits/byok и почему нет (`reasons[]` с теми же значениями, что и `blockReason`)."
        ),
    },
    {
        "name": "Wallet",
        "description": (
            "Баланс кредитов и списание (1 кредит = 1 сообщение, идемпотентно по requestId)."
        ),
    },
    {
        "name": "Subscription",
        "description": (
            "Синхронизация подписки StoreKit (подписанная JWS-транзакция) и начисление "
            "кредитов периода."
        ),
    },
    {
        "name": "Tokens",
        "description": (
            "Разовая покупка пакетов токенов (consumable StoreKit IAP) и каталог пакетов. "
            "Подписанная consumable-транзакция верифицируется и идемпотентно начисляет "
            "кредиты по серверному маппингу `productId → credits`; отдельный путь от "
            "подписки (ADR-015)."
        ),
    },
    {
        "name": "BYOK",
        "description": (
            "Свой ключ Anthropic (Bring Your Own Key): сохранение, включение/выключение, "
            "удаление. Ключ хранится зашифрованным и не логируется."
        ),
    },
    {
        "name": "Health",
        "description": "Служебные проверки и метрики (без JWT): liveness, readiness, Prometheus.",
    },
    {
        "name": "Admin",
        "description": (
            "Операторские действия под изолированным заголовком `X-Admin-Token` (ADR-009). "
            "Пользовательский JWT здесь не авторизует. Начисление кредитов и просмотр кошелька."
        ),
    },
    {
        "name": "Preview",
        "description": (
            "Публичная отдача сгенерированных сайтов по подписанной ссылке (HMAC+TTL, ADR-010). "
            "Без JWT: авторизация заключена в подписи; sandbox-заголовки безопасности."
        ),
    },
    {
        "name": "Chats",
        "description": (
            "История чатов: список (закреплённые сверху, затем по свежести), поиск, "
            "просмотр истории шагов и steps-view, переименование, закрепление и удаление. "
            "Доступ строго владельца; чужой/несуществующий чат — 404."
        ),
    },
    {
        "name": "Profile",
        "description": (
            "Профиль пользователя: отображаемое имя (`displayName`) и человекочитаемый "
            "`accountId` (детерминированная производная от userId)."
        ),
    },
    {
        "name": "Preferences",
        "description": (
            "Пользовательские настройки: дефолтный тип ассистента (chat|code), toggle "
            "уведомлений и дефолты Code-контекста. Тип ассистента ортогонален режиму оплаты "
            "(ADR-012)."
        ),
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
        return _error_response(422, "validation_error", "request validation failed")

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return _error_response(500, "internal_error", "internal error")

    for module in (
        chat,
        policy,
        wallet,
        subscription,
        token_purchase,
        byok,
        admin,
        preview,
        chats,
        profile,
        preferences,
    ):
        app.include_router(module.router)
    app.include_router(health.router)

    return app


app = create_app()
