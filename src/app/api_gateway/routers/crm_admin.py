"""CRM admin routes under /v1/admin (broad-crm «Пользователи бэков», v1).

Read/write endpoints for the CRM user-management panel. Authorization: X-Admin-Key (or legacy
X-Admin-Token) via the shared ``require_admin`` dependency on the parent admin router.
"""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, Request

from app.admin.crm_service import CrmAdminService
from app.admin.economics_service import AdminEconomicsService
from app.api_gateway.admin_guards import enforce_admin_body_size
from app.api_gateway.rate_limit import enforce_admin_economics_limits, enforce_admin_limits
from app.deps import client_ip, get_admin_economics_service, get_crm_admin_service
from app.errors import RateLimitedError, UserNotFoundError
from app.schemas.admin_economics import (
    AdminCapabilitiesResponse,
    AdminProductCreateRequest,
    AdminProductCreateResponse,
    AdminProductListResponse,
    AdminProductPatchRequest,
    AdminProductWriteResponse,
    AdminSettingListResponse,
    AdminSettingPatchRequest,
    AdminSettingWriteResponse,
    AdminTariffListResponse,
    AdminTariffPatchRequest,
    AdminTariffWriteResponse,
)
from app.schemas.crm_admin import (
    CrmDailyCostListResponse,
    CrmPaymentListResponse,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmSubscriptionGrantRequest,
    CrmSubscriptionGrantResponse,
    CrmTokensAdjustRequest,
    CrmTokensAdjustResponse,
    CrmUserDetailResponse,
    CrmUserListResponse,
)

router = APIRouter(tags=["Admin (CRM)"])

# Заявление оператора для корреляции с журналом панели. ЗАЯВЛЕНИЕ, А НЕ АУТЕНТИФИКАЦИЯ:
# значение ничем не подтверждено, единственная аутентификация — admin-ключ, и строить на этом
# поле отчёт «кто менял» нельзя.
AdminActor = Annotated[str | None, Header(alias="X-Admin-Actor", max_length=255)]


async def _enforce_admin_rate_limit(request: Request) -> None:
    if not await enforce_admin_limits(ip=client_ip(request)):
        raise RateLimitedError("admin rate limit exceeded")


def _enforce_admin_economics_write_guards(request: Request) -> None:
    """Гейт размера тела на ПИШУЩЕЙ ручке поверхности (ADR-009 §6).

    ⚠️ Не middleware, как и лимит частоты: забытый вызов оставляет путь без предела молча.
    Читающие ручки тела не имеют, поэтому гейт стоит ровно на четырёх пишущих.
    """
    enforce_admin_body_size(request)


async def _enforce_admin_economics_rate_limit(request: Request) -> None:
    """Корзина страницы экономики и настроек.

    ⚠️ Лимит НЕ middleware: он вызывается в теле каждого хендлера, и забытый вызов оставляет
    путь без лимита МОЛЧА — ни ошибки, ни лога.
    """
    if not await enforce_admin_economics_limits(ip=client_ip(request)):
        raise RateLimitedError("admin rate limit exceeded")


def _parse_dt(value: str | None) -> datetime.datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid datetime") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


# Верхний пресет страницы «Расход API» — 90 дней; 92 — он же плюс запас на границы месяцев и
# часовые пояса. Предел задан контрактом v1.3: открытый период превратил бы запрос в
# неограниченный скан, а именно неограниченная нагрузка на источник и была причиной инцидента,
# ради которого разбивка вводилась.
_MAX_COSTS_PERIOD_DAYS = 92


# `strptime` со `%Y-%m-%d` НЕ проверяет ширину компонент: `%m`/`%d` принимают запись без ведущих
# нулей, поэтому `2026-8-1` разбирается молча и период уезжает мимо контракта. Форму проверяем
# отдельно — ровно 4/2/2 ASCII-цифры; `[0-9]` вместо `\d` намеренно (`\d` матчит и не-ASCII цифры,
# которые `strptime` тоже принимает).
_ISO_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _parse_date(value: str, *, field: str) -> datetime.date:
    """`YYYY-MM-DD` строго по контракту; иначе — `400`, а не `404`.

    `404` на этом пути означает ровно одно — «расширение v1.3 не реализовано», — и отдать его в
    ответ на кривой параметр значило бы сообщить CRM, что эндпоинта нет; она перестала бы
    опрашивать этот бэк вовсе (`daily_costs_supported = false`).
    """
    raw = value.strip()
    if _ISO_DATE_RE.fullmatch(raw) is None:
        raise HTTPException(status_code=400, detail=f"invalid {field}, expected YYYY-MM-DD")
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"invalid {field}, expected YYYY-MM-DD"
        ) from exc


@router.get(
    "/users",
    response_model=CrmUserListResponse,
    summary="CRM: список пользователей",
)
async def crm_list_users(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    search: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    is_paid: bool | None = Query(default=None),
) -> CrmUserListResponse:
    await _enforce_admin_rate_limit(request)
    return await service.list_users(
        limit=limit,
        offset=offset,
        search=search,
        date_from=_parse_dt(date_from),
        date_to=_parse_dt(date_to),
        is_paid=is_paid,
    )


@router.get(
    "/users/{id}",
    response_model=CrmUserDetailResponse,
    summary="CRM: карточка пользователя",
)
async def crm_get_user(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
) -> CrmUserDetailResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.get_user(user_id)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.get(
    "/users/{id}/payments",
    response_model=CrmPaymentListResponse,
    summary="CRM: история оплат пользователя",
)
async def crm_user_payments(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CrmPaymentListResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.list_payments(user_id, limit=limit, offset=offset)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.get(
    "/users/{id}/requests",
    response_model=CrmRequestListResponse,
    summary="CRM: история запросов пользователя",
)
async def crm_user_requests(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CrmRequestListResponse:
    await _enforce_admin_rate_limit(request)
    try:
        return await service.list_requests(user_id, limit=limit, offset=offset)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.get(
    "/stats",
    response_model=CrmStatsResponse,
    summary="CRM: сводная статистика",
)
async def crm_stats(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> CrmStatsResponse:
    await _enforce_admin_rate_limit(request)
    return await service.stats(
        date_from=_parse_dt(date_from),
        date_to=_parse_dt(date_to),
    )


@router.get(
    "/costs/daily",
    response_model=CrmDailyCostListResponse,
    summary="CRM: расходы на провайдеров по дням",
    description=(
        "Периодная разбивка расходов на AI-провайдеров — день × провайдер (расширение "
        "контракта CRM v1.3). Период `date_from`/`date_to` — `YYYY-MM-DD`, UTC, включительно "
        "с обеих сторон, не длиннее 92 дней; иначе `400`. Порядок — `date ASC, provider ASC`. "
        "Ключ провайдера отдаётся СЫРЫМ, нормализует его потребитель. Отсутствие строки за "
        "(день, провайдер) означает «расхода не было»; `null` в поле — «величина не измерена», "
        "и это не ноль."
    ),
)
async def crm_daily_costs(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    date_from: Annotated[str, Query()],
    date_to: Annotated[str, Query()],
    limit: int = Query(default=1000, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> CrmDailyCostListResponse:
    await _enforce_admin_rate_limit(request)
    start = _parse_date(date_from, field="date_from")
    end = _parse_date(date_to, field="date_to")
    if start > end:
        raise HTTPException(status_code=400, detail="date_from is after date_to")
    if (end - start).days + 1 > _MAX_COSTS_PERIOD_DAYS:
        raise HTTPException(
            status_code=400, detail=f"period longer than {_MAX_COSTS_PERIOD_DAYS} days"
        )
    return await service.daily_costs(date_from=start, date_to=end, limit=limit, offset=offset)


@router.post(
    "/users/{id}/tokens",
    response_model=CrmTokensAdjustResponse,
    summary="CRM: начислить/списать токены",
)
async def crm_adjust_tokens(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    body: Annotated[CrmTokensAdjustRequest, Body()],
) -> CrmTokensAdjustResponse:
    enforce_admin_body_size(request)
    await _enforce_admin_rate_limit(request)
    try:
        return await service.adjust_tokens(user_id, body.amount)
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


@router.post(
    "/users/{id}/subscription",
    response_model=CrmSubscriptionGrantResponse,
    summary="CRM: выдать/продлить подписку",
)
async def crm_grant_subscription(
    request: Request,
    service: Annotated[CrmAdminService, Depends(get_crm_admin_service)],
    user_id: Annotated[uuid.UUID, Path(alias="id")],
    body: Annotated[CrmSubscriptionGrantRequest, Body()],
) -> CrmSubscriptionGrantResponse:
    enforce_admin_body_size(request)
    await _enforce_admin_rate_limit(request)
    try:
        return await service.grant_subscription(
            user_id,
            product_id=body.product_id,
            expires_in_days=body.expires_in_days,
            grant_id=body.grant_id,
        )
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc


# --- Экономика и настройки инстанса (контракт CRM v1.1 + v1.2 + v1.4 + v1.5) --------------
#
# ⚠️ Гейтов на каждой из восьми ручек ДВА, и оба обязательны: зависимость `require_admin`
# (объявлена на родительском admin-роутере) И явный вызов лимита корзины `rl:admin_econ`.
# `404` НИ НА ОДНОМ из этих путей не возникает: он означает «расширение не реализовано», и
# отдать его в ответ на неизвестный идентификатор значило бы сообщить панели, что ручки нет.


@router.get(
    "/capabilities",
    response_model=AdminCapabilitiesResponse,
    summary="CRM: возможности и границы инстанса",
    description=(
        "Что этот инстанс умеет и в каких границах. Список возможностей — единственный "
        "источник, из которого панель выводит право записи: отсутствие возможности означает "
        "запрет. Отсутствующий ключ границ означает, что величины на этом сервисе нет."
    ),
)
async def admin_capabilities(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
) -> AdminCapabilitiesResponse:
    await _enforce_admin_economics_rate_limit(request)
    return service.capabilities()


@router.get(
    "/products",
    response_model=AdminProductListResponse,
    summary="CRM: каталог продуктов",
    description=(
        "Продукты, за которые инстанс начисляет кредиты. Пустая отметка изменения означает, "
        "что строку ни разу не правили из панели. Архивный продукт остаётся в списке: архив "
        "снимает продукт с витрины приложения и не влияет ни на одно начисление."
    ),
)
async def admin_list_products(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
    scope: Literal["grantable", "all"] = Query(default="grantable"),
) -> AdminProductListResponse:
    await _enforce_admin_economics_rate_limit(request)
    # На этом сервисе обе выборки совпадают: каждый продукт каталога может быть выдан
    # оператором вручную, поэтому признак выдаваемости у всех строк истинный.
    _ = scope
    return service.list_products()


@router.post(
    "/products",
    response_model=AdminProductCreateResponse,
    status_code=201,
    summary="CRM: создать продукт",
    description=(
        "Заводит продукт в каталоге инстанса. Идентификатор вводит оператор — это ключ связи с "
        "платёжным провайдером. Заведён ли он в сторе, сервис проверить не может: «продукт "
        "работает» здесь означает серверную сторону — инстанс отдаёт его в каталоге и начисляет "
        "по нему. Уже существующий идентификатор отвергается с пояснением."
    ),
)
async def admin_create_product(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
    body: Annotated[AdminProductCreateRequest, Body()],
    x_admin_actor: AdminActor = None,
) -> AdminProductCreateResponse:
    _enforce_admin_economics_write_guards(request)
    await _enforce_admin_economics_rate_limit(request)
    return await service.create_product(body, actor_claim=x_admin_actor)


@router.patch(
    "/products/{product_id}",
    response_model=AdminProductWriteResponse,
    summary="CRM: изменить продукт",
    description=(
        "Меняет число кредитов продукта и его присутствие на витрине. Повтор той же правки "
        "безопасен и ничего не меняет. Неизвестный продукт отвергается: правка продукта не "
        "создаёт. Правка применяется не мгновенно — величина окна в ответе."
    ),
)
async def admin_patch_product(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
    product_id: Annotated[str, Path(max_length=128)],
    body: Annotated[AdminProductPatchRequest, Body()],
    x_admin_actor: AdminActor = None,
) -> AdminProductWriteResponse:
    _enforce_admin_economics_write_guards(request)
    await _enforce_admin_economics_rate_limit(request)
    return await service.patch_product(product_id, body, actor_claim=x_admin_actor)


@router.get(
    "/pricing",
    response_model=AdminTariffListResponse,
    summary="CRM: тарифы списания",
    description=(
        "Сколько кредитов стоит одно обращение. Инстанс отдаёт строку на каждый поддерживаемый "
        "вариант, включая те, чья цена ещё не настраивалась — тогда показано действующее "
        "значение. Единица объявляется честно: у фото это одно изображение, у видео — полный "
        "запуск с этими параметрами, у чата — один завершённый ход на этой модели."
    ),
)
async def admin_list_pricing(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
) -> AdminTariffListResponse:
    await _enforce_admin_economics_rate_limit(request)
    return service.list_pricing()


@router.patch(
    "/pricing/{tariff_id}",
    response_model=AdminTariffWriteResponse,
    summary="CRM: изменить тариф",
    description=(
        "Назначает цену варианта в кредитах. Значение — целое число не меньше единицы: нулевая "
        "цена сделала бы генерацию бесплатной, а дробную кошелёк не выражает. Неизвестный "
        "вариант отвергается: правка строк не создаёт."
    ),
)
async def admin_patch_tariff(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
    tariff_id: Annotated[str, Path(max_length=200)],
    body: Annotated[AdminTariffPatchRequest, Body()],
    x_admin_actor: AdminActor = None,
) -> AdminTariffWriteResponse:
    _enforce_admin_economics_write_guards(request)
    await _enforce_admin_economics_rate_limit(request)
    return await service.patch_tariff(tariff_id, body, actor_claim=x_admin_actor)


@router.get(
    "/settings",
    response_model=AdminSettingListResponse,
    summary="CRM: продуктовые настройки инстанса",
    description=(
        "Настройки, задающие поведение приложения для его пользователей. Поверхность "
        "самоописываема: состав, типы, подписи и допустимые значения приходят в ответе — их не "
        "нужно знать заранее. Состав зависит от инстанса. Секретов, адресов инфраструктуры, "
        "ресурсных лимитов и денежных величин эта поверхность не несёт."
    ),
)
async def admin_list_settings(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
) -> AdminSettingListResponse:
    await _enforce_admin_economics_rate_limit(request)
    return service.list_settings()


@router.patch(
    "/settings/{setting_id}",
    response_model=AdminSettingWriteResponse,
    summary="CRM: изменить настройку инстанса",
    description=(
        "Меняет одну настройку. Значение проверяет сервис: допустимость зависит от конфигурации "
        "инстанса. Пустой список никогда не означает «вернуть значение по умолчанию» — он "
        "означает явный выбор оператора и отвергается там, где объявлен минимум элементов. "
        "Неизвестная настройка отвергается: правка настроек не создаёт."
    ),
)
async def admin_patch_setting(
    request: Request,
    service: Annotated[AdminEconomicsService, Depends(get_admin_economics_service)],
    setting_id: Annotated[str, Path(max_length=128)],
    body: Annotated[AdminSettingPatchRequest, Body()],
    x_admin_actor: AdminActor = None,
) -> AdminSettingWriteResponse:
    _enforce_admin_economics_write_guards(request)
    await _enforce_admin_economics_rate_limit(request)
    return await service.patch_setting(setting_id, body, actor_claim=x_admin_actor)
