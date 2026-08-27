"""Auth-issuer routes: /v1/auth/register|token|refresh|jwks (auth/02, ADR-018).

Public (no user JWT — this is where the token is obtained); throttled per source IP. Issuer
endpoints return 503 when no private signing key is configured. Tokens are never logged.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api_gateway.rate_limit import enforce_auth_limits
from app.auth.issuer import build_jwks
from app.auth.service import AuthService, IssuedTokens
from app.billing_cloudpayments.service import CloudPaymentsWebhookService
from app.config import get_settings
from app.deps import client_ip, get_auth_service, get_cloudpayments_webhook_service
from app.errors import NotFoundError, RateLimitedError
from app.observability.logging import log_event
from app.schemas.auth import (
    AppleSignInRequest,
    JwksResponse,
    RefreshRequest,
    RegisterRequest,
    TokenRequest,
    TokenResponse,
)

# No bearer_scheme / get_current_user here: these endpoints are public (R2.3, ADR-018 §2).
router = APIRouter(prefix="/v1/auth", tags=["Auth"])


async def _rate_limit(request: Request) -> None:
    if not await enforce_auth_limits(ip=client_ip(request)):
        raise RateLimitedError("rate limit exceeded")


async def _reconcile_ru_payments(
    cloudpayments: CloudPaymentsWebhookService, tokens: IssuedTokens
) -> None:
    """Дозачислить РФ-оплаты, пришедшие до появления этого устройства в базе.

    **Дыра, которую это закрывает.** Колбэк с неизвестным устройством отбрасывается как
    ``user_not_found`` и отвечает провайдеру кодом 200 — тот больше не повторяет. Деньги
    списаны, начисления нет. Реальный случай (veltriohub, 2026-08-23): две завершённые оплаты
    на 47.89 ₽ пропали именно так; всего таких пользователей 18 из 2 864 плативших.

    Вызывается ТОЛЬКО на ``/register`` — при первом открытии приложения или переустановке.
    На ``/token`` не вызывается: он идёт при каждом обновлении сессии, и лишний исходящий
    запрос к провайдеру на этом пути был бы платой за то, что уже сделано.

    **Никогда не роняет вход.** Провайдер недоступен, медленный или ответил ерундой — человек
    всё равно должен войти в приложение. Ошибка уходит в журнал предупреждением, а не наружу.
    """
    if not get_settings().cloudpayments_api_token:
        return
    # Провайдер адресует устройство UUID'ом. `deviceId` у нас — свободная строка, и не всякая
    # ею является: у такого устройства оплат в broadapps быть не может, спрашивать не о чем.
    try:
        device_uuid = uuid.UUID(tokens.device_id)
    except (ValueError, AttributeError, TypeError):
        return
    try:
        await cloudpayments.reconcile_device(device_id=device_uuid, user_id=tokens.user_id)
    except Exception:  # noqa: BLE001 — вход важнее сверки; причина уходит в журнал
        log_event(
            logging.getLogger("app.api_gateway.auth"),
            logging.WARNING,
            "cloudpayments_reconcile_on_register_failed",
            event="cloudpayments_reconcile_on_register_failed",
            userId=str(tokens.user_id),
        )


def _to_response(tokens: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        userId=tokens.user_id,
        deviceId=tokens.device_id,
        accessToken=tokens.access_token,
        tokenType="Bearer",
        expiresIn=tokens.expires_in,
        refreshToken=tokens.refresh_token,
        refreshExpiresIn=tokens.refresh_expires_in,
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    summary="Регистрация устройства",
    description=(
        "Создаёт или находит идентичность устройства и выдаёт пару токенов. `deviceId` "
        "опционален — без него сервер сгенерирует и вернёт его. Известное устройство возвращает "
        "тот же `userId`. `503`, если выпуск токенов не настроен."
    ),
)
async def auth_register(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    cloudpayments: Annotated[
        CloudPaymentsWebhookService, Depends(get_cloudpayments_webhook_service)
    ],
    body: RegisterRequest,
) -> TokenResponse:
    await _rate_limit(request)
    tokens = await auth.register_or_token(body.deviceId)
    await _reconcile_ru_payments(cloudpayments, tokens)
    return _to_response(tokens)


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Токены для устройства",
    description=(
        "Выдаёт пару токенов для уже известного устройства (тот же `userId`). `deviceId` "
        "обязателен. `503`, если выпуск токенов не настроен."
    ),
)
async def auth_token(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: TokenRequest,
) -> TokenResponse:
    await _rate_limit(request)
    tokens = await auth.register_or_token(body.deviceId)
    return _to_response(tokens)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Обновить токены",
    description=(
        "Обменивает refresh-токен на новую пару. Refresh-токен одноразовый: после обмена "
        "становится недействительным. Повторное использование, невалидный или истёкший "
        "токен — `401`."
    ),
)
async def auth_refresh(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: RefreshRequest,
) -> TokenResponse:
    await _rate_limit(request)
    tokens = await auth.refresh(body.refreshToken)
    return _to_response(tokens)


@router.post(
    "/apple",
    response_model=TokenResponse,
    summary="Вход через Apple",
    description=(
        "Принимает Apple identity token (нативный Sign in with Apple), верифицирует его и "
        "выдаёт нашу пару токенов — кросс-девайс аккаунт (один Apple-аккаунт = один `userId`). "
        "`deviceId` опционален. Невалидный/просроченный токен — `401`. `503`, если выпуск "
        "токенов не настроен или Apple-аудитория не задана."
    ),
)
async def auth_apple(
    request: Request,
    auth: Annotated[AuthService, Depends(get_auth_service)],
    body: AppleSignInRequest,
) -> TokenResponse:
    # "not configured" (503) and verification failures (401) are raised inside the service /
    # verifier and mapped by the global error handler (ServiceUnavailableError / UnauthorizedError).
    await _rate_limit(request)
    tokens = await auth.sign_in_with_apple(
        identity_token=body.identityToken, device_id=body.deviceId, nonce=body.nonce
    )
    return _to_response(tokens)


@router.get(
    "/jwks",
    response_model=JwksResponse,
    summary="Публичный ключ (JWKS)",
    description=(
        "Публичный ключ подписи в формате JWKS для самопроверки токенов. Приватный ключ не "
        "отдаётся. `404`, если JWKS отключён или публичный ключ не настроен."
    ),
)
async def auth_jwks(request: Request) -> JwksResponse:
    await _rate_limit(request)
    settings = get_settings()
    if not settings.auth_jwks_enabled:
        raise NotFoundError("jwks disabled")
    public_key = settings.resolve_public_key()
    if not public_key:
        raise NotFoundError("public key not configured")
    return JwksResponse.model_validate(build_jwks(public_key, settings.jwt_kid))
