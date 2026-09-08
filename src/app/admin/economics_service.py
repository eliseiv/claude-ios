"""Пишущая половина поверхности экономики и настроек (ADR-099 §6, §7, §8, §10, §11).

Модуль ``admin`` **пишет** в слой ``instance_config`` и ничего не резолвит сам: читатели цены и
настроек — оркестратор чата, медиа-сабмит, вебхуки и каталоги — берут значения из снимка, а не
отсюда.

**Порядок «сначала факт, затем интерпретация» — не стилистика.** Правка коммитится и пишется в
аудит ДО сборки тела ответа; ни одна ветка сборки не имеет права поднять исключение раньше
аудита. Иначе правка состоялась, оператор видит ошибку, следа нет — и он нажимает «Сохранить»
второй раз.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.service import (
    EVENT_ADMIN_PRODUCT_ARCHIVED,
    EVENT_ADMIN_PRODUCT_CREATED,
    EVENT_ADMIN_PRODUCT_UPDATED,
    EVENT_ADMIN_SETTING_UPDATED,
    EVENT_ADMIN_TARIFF_UPDATED,
    AuditEvent,
    AuditService,
)
from app.config import Settings, get_settings
from app.instance_config import models as chat_models
from app.instance_config import products as product_catalog
from app.instance_config import tariffs as tariff_registry
from app.instance_config.settings_registry import (
    PRODUCT_TOKENS_MAX,
    SETTING_CHAT_ADVERTISED_MODES,
    SETTING_CHAT_DEFAULT_MODEL,
    SETTING_CHAT_MODELS_OFFERED,
    TARIFF_DECIMAL_PLACES,
    TARIFF_TOKENS_MAX,
    SettingSpec,
    SettingValueError,
    coerce_stored_setting_value,
    declared_settings,
    find_setting,
    resolve_setting,
    validate_setting_value,
)
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    SettingOverlay,
    get_snapshot,
    refresh_snapshot,
)
from app.models import AdminProduct, AdminSetting, AdminTariff
from app.observability.logging import log_event
from app.observability.metrics import admin_override_rejected_total
from app.schemas.admin_economics import (
    AdminCapabilitiesResponse,
    AdminProductCreateRequest,
    AdminProductCreateResponse,
    AdminProductItem,
    AdminProductListResponse,
    AdminProductPatchRequest,
    AdminProductWriteResponse,
    AdminSettingItem,
    AdminSettingListResponse,
    AdminSettingOption,
    AdminSettingPatchRequest,
    AdminSettingWriteResponse,
    AdminTariffItem,
    AdminTariffListResponse,
    AdminTariffPatchRequest,
    AdminTariffWriteResponse,
)

SCOPE_PRODUCTS = "products"
SCOPE_TARIFFS = "tariffs"
SCOPE_SETTINGS = "settings"

# Причины отказа (замороженный набор метрики §10) и их коды. Перечень — РАЗБИЕНИЕ по одному
# измерению «ГДЕ лежит несоответствие» (§10.0): идентификатор → форма присланного → объявленная
# граница → НЕобъявленная граница → данные источника элемента → поддержка поля сервисом →
# соседние элементы и версия. СЕМЬ мест — семь значений, ровно одно значение на место.
# Разведение кодов подчинено предикату §11: `422` ⟺ нарушено то, что мы САМИ объявили (схема
# ручки, `limits`, `type`/`options`/`constraints`) — CRM могла отклонить это в форме; `400` ⟺
# правило, которого в нашем объявлении НЕТ и быть не может, либо код, предписанный самим
# контрактом. Код и лейбл отвечают на РАЗНЫЕ вопросы («могла ли CRM отклонить это в форме» и
# «что именно не сошлось»), поэтому одному коду законно соответствует несколько лейблов.
REASON_UNKNOWN_ID = "unknown_id"  # -> 400 (предписан контрактом)
REASON_OUT_OF_RANGE = "out_of_range"  # -> 422 (нарушены объявленные `limits`)
REASON_TYPE_MISMATCH = "type_mismatch"  # -> 422 (нарушены объявленные `type`/`options`)
# ⚠️ НЕобъявленная граница — ОТДЕЛЬНОЕ МЕСТО, а не «похожий случай» `out_of_range` (§10.0).
# Нижние границы `tokens` (тариф `>= 1`; продукт `>= 1` для `one_time`) в `limits` не выражены и
# выражены быть не могут: ключа под нижнюю границу в замороженном наборе нет, а у продукта она
# вдобавок зависит от ВТОРОГО поля того же тела (`purchase_kind`). Отнести их к `out_of_range`
# значило бы утверждать, что CRM могла отклонить правку в форме, — и обесценить единственный
# практический смысл той серии («клиентская проверка CRM не сработала»).
REASON_UNDECLARED_BOUND = "undeclared_bound"  # -> 400 (граница есть только у нас)
# Данные ИСТОЧНИКА элемента: запрос безупречен (несёт `tokens`), но класса покупки не хватило у
# строки каталога, а число без класса не прочитал бы ни один резолвер начисления (§6.1).
# ⚠️ Соседнего значения про НЕДОСТАЮЩЕЕ ЧИСЛО здесь больше НЕТ: после перевода колонок оверлея в
# nullable факта «числа неоткуда взять» не существует ни на одном пути — archived-правка числа
# не требует вовсе, а правка числа приносит его в теле. Объявленное значение без достижимой
# ветки-producer'а было бы МЁРТВЫМ ЛЕЙБЛОМ: серия, которая никогда не растёт, читается дежурным
# как «этого не случается», и первый же случай был бы отнесён к соседнему значению.
REASON_SOURCE_KIND_MISSING = "source_kind_missing"  # -> 400 (источник не несёт `purchase_kind`)
# ⚠️ Единственный законный producer — поле, которого сервис НЕ ВЕДЁТ вовсе (`avatar_tokens`).
# Отказ «источник элемента не донёс величину» сюда НЕ относится: там запрос безупречен, и
# смешение сделало бы серию непригодной как измерение цены TD-043.
REASON_UNSUPPORTED_FIELD = "unsupported_field"  # -> 400 (поля контракта сервис не поддерживает)
REASON_CONFLICT = "conflict"  # -> 409 (версия) и 400 (дубликат / межэлементный)

# Объявляются ТОЛЬКО реализованные пути: `features` — единственный источник права записи для
# CRM, и он fail-closed.
FEATURES = (
    "products.read",
    "products.write_tokens",
    "products.write_archived",
    "products.create",
    "pricing.read",
    "pricing.write_tokens",
    "settings.write",
    "requests.costs",
)
CONTRACT_VERSION = 1

logger = logging.getLogger("app.admin.economics")

_AUDIT_DELTA_MAX_CHARS = 200

# Один дом у обоих носителей смысла «значение изменил другой оператор»: явная проверка версии
# (`if_updated_at`) и гонка на первичном ключе — одна и та же ситуация для оператора, и текст у
# неё обязан быть один, иначе две формулировки разойдутся.
_VERSION_CONFLICT_DETAIL = "значение изменил другой оператор: обновите страницу и повторите"
_DUPLICATE_PRODUCT_DETAIL = (
    "продукт «{product_id}» уже есть в каталоге инстанса: выберите другой идентификатор"
)


def _iso_z(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(value: Any) -> str:
    """Компактная сериализация стороны дельты. Аудит фиксирует факт и направление, не копию."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text[:_AUDIT_DELTA_MAX_CHARS]


def _normalized_setting_value(spec: SettingSpec, value: Any) -> Any:
    """Нормализация присланного значения, выполняемая на ЗАПИСИ (ADR-099 §8.1).

    Сейчас правило одно: ``chat.advertised_generation_modes`` всегда несёт
    ``DEFAULT_GENERATION_MODE``. Оператор вправе прислать список без него, и отказ здесь был бы
    ТУПИКОМ, а не защитой: `defaultGenerationMode` — константа кода, настройкой не является и
    оператору недоступна, поэтому «сначала смени дефолт» не ведёт ни к какому выполнимому шагу
    (в отличие от межэлементного инварианта моделей, где действие у оператора есть, и там стоит
    `400`).

    ⚠️ **Нормализация выполняется ДО сохранения, а не при сборке ответа.** ``previous_value``,
    ``changed`` и дельта аудита считаются по ХРАНИМОМУ значению; нормализация только в ответе
    развела бы показанное с хранимым — оператор увидел бы `general` в `value` и его отсутствие
    в `previous_value` соседней правки. Хранение нормализованного даёт ОДИН источник.

    ⚠️ Пустой список сюда НЕ доходит: `[]` нарушает объявленный `min_items: 1` и отвергается
    `422` раньше. Пустой env означает «оператор ничего не сказал», пустой оверлей — «оператор
    явно выбрал ничего», и подменять второе первым запрещено.

    Read-time барьер в ``values.advertised_generation_modes()`` НЕ отменяется: у него другая
    зона действия — значения, пришедшие не через эту ручку (env, прямая запись в БД).
    """
    from app.schemas.chat import DEFAULT_GENERATION_MODE

    if spec.setting_id != SETTING_CHAT_ADVERTISED_MODES:
        return value
    if not isinstance(value, list) or DEFAULT_GENERATION_MODE in value:
        return value
    from app.schemas.chat import GENERATION_MODE_ORDER

    selected = {*value, DEFAULT_GENERATION_MODE}
    # Канонический порядок, а не порядок ввода: клиент рендерит список как есть.
    return [mode for mode in GENERATION_MODE_ORDER if mode in selected]


class AdminEconomicsService:
    """CRUD операторских оверлеев экономики и настроек инстанса."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._audit = AuditService(session)

    # --- отказы ---------------------------------------------------------------------------

    @staticmethod
    def _reject(scope: str, reason: str, status_code: int, detail: str) -> HTTPException:
        admin_override_rejected_total.labels(scope=scope, reason=reason).inc()
        return HTTPException(status_code=status_code, detail=detail)

    def _log_applied(
        self, scope: str, target_id: str, previous: Any, next_value: Any, actor_claim: str | None
    ) -> None:
        """Структурное событие правки — рядом с аудитом, но в журнале процесса.

        Секретов здесь нет по построению: в поверхности настроек нет величин, запись которых
        опасна (ADR-099 §8.2), а цена и число кредитов — числа.
        """
        log_event(
            logger,
            logging.INFO,
            "admin_override_applied",
            scope=scope,
            id=target_id,
            previous=_short(previous),
            next=_short(next_value),
            actorClaim=actor_claim,
        )

    def _effective_after(self) -> int:
        # Объявленное окно == фактическое: наружу уходит та же зажатая величина, по которой
        # тикает обновитель (см. `Settings.admin_overrides_refresh_window`).
        return self._settings.admin_overrides_refresh_window()

    def _check_version(
        self, scope: str, current: datetime.datetime | None, expected: datetime.datetime | None
    ) -> None:
        """Оптимистичный конфликт: значение изменил другой оператор.

        Сравнение — С ТОЙ ЖЕ ТОЧНОСТЬЮ, с какой отметка ушла наружу (целые секунды). Оператор
        присылает обратно ровно то, что прочитал, и сравнение с микросекундами БД отвергало бы
        каждую условную правку как конфликт — то есть ломало бы механизм, который защищает.
        """
        if expected is None:
            return
        if expected.tzinfo is None:
            expected = expected.replace(tzinfo=datetime.UTC)
        normalized = (
            None if current is None else current.astimezone(datetime.UTC).replace(microsecond=0)
        )
        if normalized != expected.astimezone(datetime.UTC).replace(microsecond=0):
            raise self._reject(scope, REASON_CONFLICT, 409, _VERSION_CONFLICT_DETAIL)

    async def _flush_or_conflict(self, scope: str, *, status_code: int, detail: str) -> None:
        """Отправить запись в БД СЕЙЧАС и превратить гонку на PK в предписанный код отказа.

        Первая правка ещё не заведённой строки идёт веткой ``session.add(...)``, а ``INSERT``
        при ``autoflush=False`` ушёл бы только внутри ``commit()``. Два одновременных запроса по
        одному идентификатору (двойной клик «Сохранить» по разным воркерам, два оператора) оба
        проходят проверку версии — оба видят «строки нет», — оба вставляют, и второй падает на
        первичном ключе. Без этого перехвата исключение дошло бы до общего обработчика и стало
        бы **`500`**, которого нет ни в одной строке таблицы кодов контракта.

        Вызывается ДО записей аудита: провалившаяся вставка не должна оставлять следа, а
        успешная — обязана. Данные не теряются: аудит лежит в той же транзакции и откатывается
        вместе со вставкой, повтор операции проходит штатно.

        Код отказа задаёт вызывающий, потому что он зависит от пути, а не от механики: на
        `POST /products` контракт предписывает **`400`** (нужно сменить идентификатор), на любом
        `PATCH` — **`409`**, потому что там это ровно «значение изменил другой оператор».
        """
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise self._reject(scope, REASON_CONFLICT, status_code, detail) from exc

    def _reject_avatar_tokens(self, value: int | None) -> None:
        """Вторая валюта: молча принять и проигнорировать нельзя — оператор считал бы, что
        величина сохранена. Отсутствие ключа `product_avatar_tokens_max` в `limits` уже сказало
        CRM, что величины нет; этот отказ — страховка, а не основной механизм."""
        if value is None:
            return
        raise self._reject(
            SCOPE_PRODUCTS,
            REASON_UNSUPPORTED_FIELD,
            400,
            "сервис не ведёт вторую валюту: avatar_tokens не поддерживается",
        )

    # --- capabilities ---------------------------------------------------------------------

    def capabilities(self) -> AdminCapabilitiesResponse:
        """`limits` — runtime-величины ЭТОГО инстанса; заморожены только имена ключей и типы.

        Ключей ТРИ. ``product_avatar_tokens_max`` не отдаётся вовсе: предикат `limits` — НАЛИЧИЕ
        ключа, поэтому ключ даже со значением `0` объявил бы вторую валюту существующей и
        заставил бы CRM нарисовать контрол, который не может предложить ни одного значения.
        """
        return AdminCapabilitiesResponse(
            contract_version=CONTRACT_VERSION,
            features=list(FEATURES),
            limits={
                "product_tokens_max": PRODUCT_TOKENS_MAX,
                "tariff_tokens_max": TARIFF_TOKENS_MAX,
                "tariff_decimal_places": TARIFF_DECIMAL_PLACES,
            },
            cache_effective_after_seconds=self._effective_after(),
        )

    # --- продукты -------------------------------------------------------------------------

    @staticmethod
    def _visible_tokens(tokens: int | None, purchase_kind: str | None) -> int | None:
        """`tokens` отдаётся ТОЛЬКО когда класс покупки известен, иначе `null` (§6.1, правило 4).

        Число кредитов принадлежит ПАРЕ «класс + число»: без класса его не читает ни один
        резолвер начисления. Отдать число значило бы ПРЕДЛОЖИТЬ CRM правку, которую мы обязаны
        отвергнуть, — она рендерит карандаш ровно при непустом `tokens`, и оператор получил бы
        `400` на контрол, который ему показали. Пустой `tokens` — единственная форма
        высказывания «одного числа у этой строки нет», и контрагент её уже нормирует (правка
        числа read-only, архивация по-прежнему доступна). Остаток — TD-043.
        """
        return tokens if purchase_kind is not None else None

    def _product_item_of(
        self,
        *,
        product_id: str,
        name: str,
        tokens: int | None,
        purchase_kind: str | None,
        archived: bool,
        updated_at: datetime.datetime | None,
    ) -> AdminProductItem:
        return AdminProductItem(
            product_id=product_id,
            name=name,
            price=None,
            period=None,
            tokens=self._visible_tokens(tokens, purchase_kind),
            avatar_tokens=None,
            grantable=True,
            purchase_kind=purchase_kind,
            archived=archived,
            updated_at=_iso_z(updated_at),
        )

    def _product_item(self, row: product_catalog.ProductRow) -> AdminProductItem:
        return self._product_item_of(
            product_id=row.product_id,
            name=row.name,
            tokens=row.tokens,
            purchase_kind=row.purchase_kind,
            archived=row.archived,
            updated_at=row.updated_at,
        )

    def list_products(self) -> AdminProductListResponse:
        rows = product_catalog.catalog_rows(settings=self._settings)
        return AdminProductListResponse(items=[self._product_item(row) for row in rows])

    def _validate_product_tokens(self, tokens: int, purchase_kind: str) -> None:
        """Границы ПРИСЛАННОГО числа кредитов. Вызывается ТОЛЬКО для значения из тела запроса.

        ⚠️ **Сохранённое значение сюда не попадает.** Валидировать чужое, неприсланное число
        значило бы отвергать archived-правку строки, чей источник несёт `credits: 0` или число
        выше `product_tokens_max`, — правку, которая этого числа не касается вовсе (§6.1).

        ⚠️ **Две границы — два РАЗНЫХ кода, и это не косметика (§11).** Верхняя объявлена нами в
        `limits`, поэтому CRM могла отклонить её в форме ⇒ `422`/`out_of_range`. Нижняя зависит
        от `purchase_kind` и в `limits` невыразима в принципе ⇒ `400`/`undeclared_bound`:
        клиент прислал законное по всему, что мы объявили, и знать о правиле было неоткуда.
        """
        floor = 1 if purchase_kind == product_catalog.PURCHASE_KIND_ONE_TIME else 0
        if tokens < floor:
            raise self._reject(
                SCOPE_PRODUCTS,
                REASON_UNDECLARED_BOUND,
                400,
                f"tokens: для класса {purchase_kind} допустимы значения не меньше {floor}",
            )
        if tokens > PRODUCT_TOKENS_MAX:
            raise self._reject(
                SCOPE_PRODUCTS,
                REASON_OUT_OF_RANGE,
                422,
                f"tokens: допустимы значения от {floor} до {PRODUCT_TOKENS_MAX}"
                f" для класса {purchase_kind}",
            )

    async def create_product(
        self, body: AdminProductCreateRequest, *, actor_claim: str | None
    ) -> AdminProductCreateResponse:
        self._reject_avatar_tokens(body.avatar_tokens)
        product_id = body.product_id.strip()
        if not product_id or any(ch.isspace() for ch in product_id):
            raise self._reject(
                SCOPE_PRODUCTS,
                REASON_TYPE_MISMATCH,
                422,
                "product_id: непустая строка без пробельных символов",
            )
        if product_catalog.find_product(product_id, settings=self._settings) is not None:
            # Дубликат — `400`, а НЕ `409`: на этом пути `409` занят смыслом «значение изменил
            # другой оператор», и оператор получил бы «обновите страницу» там, где нужно
            # сменить идентификатор.
            raise self._reject(
                SCOPE_PRODUCTS,
                REASON_CONFLICT,
                400,
                _DUPLICATE_PRODUCT_DETAIL.format(product_id=product_id),
            )
        self._validate_product_tokens(body.tokens, body.purchase_kind)

        created_at = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        self._session.add(
            AdminProduct(
                product_id=product_id,
                name=body.name,
                purchase_kind=body.purchase_kind,
                tokens=body.tokens,
                archived=False,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        # Проверка дубликата выше и вставка не атомарны: гонку ловит сама БД. На ЭТОМ пути
        # контракт предписывает `400` — для оператора это та же ситуация, что и обнаруженный
        # дубликат, и «обновите страницу» ему бы не помогло, нужен другой идентификатор.
        await self._flush_or_conflict(
            SCOPE_PRODUCTS,
            status_code=400,
            detail=_DUPLICATE_PRODUCT_DETAIL.format(product_id=product_id),
        )
        await self._audit.record(
            AuditEvent(
                user_id=None,
                event_type=EVENT_ADMIN_PRODUCT_CREATED,
                payload={
                    "scope": SCOPE_PRODUCTS,
                    "id": product_id,
                    "previous": None,
                    "next": _short(
                        {
                            "name": body.name,
                            "purchase_kind": body.purchase_kind,
                            "tokens": body.tokens,
                        }
                    ),
                    "actorClaim": actor_claim,
                },
            )
        )
        await self._commit_and_refresh()
        self._log_applied(
            SCOPE_PRODUCTS,
            product_id,
            None,
            {"purchase_kind": body.purchase_kind, "tokens": body.tokens},
            actor_claim,
        )
        # Тело ответа собирается из ЗАПИСАННЫХ значений, а не из снимка: снимок мог не
        # обновиться (отказ БД оставляет прежний), и тогда ответ либо упал бы `500` по уже
        # закоммиченной и уже зааудированной правке, либо показал бы старое значение при
        # `changed: true`. Оператор в обоих случаях нажимает «Сохранить» второй раз.
        return AdminProductCreateResponse(
            **self._product_item_of(
                product_id=product_id,
                name=body.name,
                tokens=body.tokens,
                purchase_kind=body.purchase_kind,
                archived=False,
                updated_at=created_at,
            ).model_dump(),
            effective_after_seconds=self._effective_after(),
        )

    async def patch_product(
        self, product_id: str, body: AdminProductPatchRequest, *, actor_claim: str | None
    ) -> AdminProductWriteResponse:
        self._reject_avatar_tokens(body.avatar_tokens)
        if body.tokens is None and body.archived is None:
            raise self._reject(
                SCOPE_PRODUCTS,
                REASON_TYPE_MISMATCH,
                422,
                "укажите хотя бы одно из полей: tokens или archived",
            )
        # ⚠️ Решение «продукт неизвестен» принимается по БД, а не по снимку. Снимок этого
        # процесса отстаёт на окно обновления, а созданный оператором продукт существует ТОЛЬКО
        # в оверлее — значит `POST`, а следом `PATCH` в другом процессе внутри окна получил бы
        # `400 «продукт неизвестен»` по продукту, который только что создан. Env-источники
        # каталога от снимка не зависят вовсе, поэтому их достаточно спросить у резолвера.
        overlay = await self._session.scalar(
            select(AdminProduct).where(AdminProduct.product_id == product_id)
        )
        row = product_catalog.find_product(product_id, settings=self._settings)
        if overlay is None and row is None:
            # `PATCH` НИКОГДА не создаёт продукт: неизвестный идентификатор — `400`, а не `404`
            # (`404` в этом контракте означает «расширение не реализовано»).
            raise self._reject(
                SCOPE_PRODUCTS,
                REASON_UNKNOWN_ID,
                400,
                f"продукт «{product_id}» на этом инстансе неизвестен",
            )
        # Действующие значения — ПОФИЛДОВО: значение оверлея берётся только там, где оно
        # ЗАДАНО, иначе остаётся значение источника (§6.1). Строка БД читается вперёд снимка по
        # той же причине, что и у проверки версии ниже: правка соседнего процесса внутри окна
        # ещё не видна снимку. `row` уже слит пофилдово резолвером, поэтому он и есть источник
        # для полей, которых оверлей не задаёт.
        stored_tokens = (
            overlay.tokens
            if overlay is not None and overlay.tokens is not None
            else (row.tokens if row else None)
        )
        stored_archived = overlay.archived if overlay is not None else bool(row and row.archived)
        self._check_version(
            SCOPE_PRODUCTS,
            overlay.updated_at if overlay is not None else None,
            body.if_updated_at,
        )

        purchase_kind = (
            overlay.purchase_kind
            if overlay is not None and overlay.purchase_kind is not None
            else (row.purchase_kind if row else None)
        )
        # ⚠️ ПОРЯДОК ВЕТОК НЕСЁТ НОРМУ (§6.1), а не оформление: класс требуется ТОЛЬКО когда
        # правка несёт число. Архивация ортогональна классу и числу — `archived` есть свойство
        # ВИТРИНЫ, а витрина фильтрует по `product_id` и знать резолвер начисления не обязана.
        # Требовать для архивации класс и число значило бы требовать данные, которых операция не
        # использует, и отвергать фичу `products.write_archived` ровно на том классе строк,
        # который контрагент нормативно ОБЯЗАН на неё присылать (CRM ADR-073 §4; у него это
        # основной сценарий — 30 позиций из 39). Ветки отказа у archived-правки нет НИ ПРИ КАКОЙ
        # полноте источника.
        #
        # ⚠️ Соседние правила с ПРОТИВОПОЛОЖНЫМ требованием, помечены оба: правка `archived` НЕ
        # имеет права требовать класс, а правка `tokens` его ОБЯЗАНА требовать. Перенести «по
        # аналогии» ни одно на другое нельзя: разные адресаты (витрина против резолвера
        # начисления) и разные последствия ошибки (заблокированная архивация против принятой и
        # неприменённой правки цены).
        if body.tokens is not None:
            if purchase_kind is None:
                # Класс решает, какой резолвер начисления прочитает строку. Записать догадку
                # значило бы назначить продукту путь начисления, которого оператор не выбирал, а
                # число БЕЗ класса не прочитал бы ни один резолвер — правка была бы принята и не
                # применена. Лейбл следует из НАБЛЮДАЕМОГО ФАКТА ветки: несоответствие лежит в
                # ДАННЫХ ИСТОЧНИКА, а не в запросе — поле контракта сервис поддерживает, и
                # прислано оно верно. `unsupported_field` был бы ложен в обе стороны сразу:
                # обвинял бы безупречный запрос и разбавлял серию, единственный законный
                # producer которой — `avatar_tokens`. Именно этот лейбл делает частоту TD-043
                # измеримой. ⚠️ Код — `400` (§11): правило, которого CRM знать было неоткуда.
                # ⚠️ Страховка, а не механизм: у строки с неизвестным классом мы отдаём
                # `tokens: null` (см. `_visible_tokens`), и CRM этот контрол не рисует вовсе.
                raise self._reject(
                    SCOPE_PRODUCTS,
                    REASON_SOURCE_KIND_MISSING,
                    400,
                    f"у продукта «{product_id}» источник не задаёт класс покупки,"
                    " поэтому число кредитов править нельзя",
                )
            # Валидируется ТОЛЬКО присланное число. Сохранённое (чужое, неприсланное) значение
            # сюда не попадает — иначе archived-правка строки с `credits: 0` или с числом выше
            # `product_tokens_max` падала бы `422` по величине, которой она не касается.
            self._validate_product_tokens(body.tokens, purchase_kind)
        archived = body.archived if body.archived is not None else stored_archived

        previous_tokens = self._visible_tokens(stored_tokens, purchase_kind)
        # Сравнение — с СОБСТВЕННЫМ полем строки оверлея, а не с эффективным значением: переход
        # поля из «не задано» в «задано» меняет СОСТАВ строки (величина уходит из-под `.env`),
        # даже если число совпало с источником. Считать такую правку холостой значило бы принять
        # запись оператора и не сохранить её.
        tokens_changed = body.tokens is not None and (
            overlay is None or overlay.tokens is None or body.tokens != overlay.tokens
        )
        archived_changed = body.archived is not None and (
            overlay is None or body.archived != stored_archived
        )

        # ⚠️ ЗАПИСЬ ВЫПОЛНЯЕТСЯ ТОЛЬКО ТОГДА, КОГДА ОНА ЧТО-ТО МЕНЯЕТ, И ТОГДА ЖЕ ПИШЕТСЯ
        # АУДИТ — запись и след парны в обе стороны.
        #
        # «Что-то меняет» — это ДВА разных изменения, и второе легко потерять: изменение
        # ЗНАЧЕНИЯ и изменение СОСТАВА оверлеев. Первая правка ещё не заведённой строки меняет
        # состав, даже если значение совпало с env: с этого момента `updated_at` перестаёт быть
        # пустым (а именно им оператор видит, что строку трогали), величина уходит из-под `.env`
        # и снять её можно только из панели. Считать такую правку холостой значило бы принять
        # запись оператора и не сохранить её.
        #
        # Холостым остаётся ровно повтор по УЖЕ существующей строке: он не трогает БД вовсе,
        # иначе двигал бы `updated_at` и выдавал соседу ложный `409` на строку, которой никто
        # не правил.
        # Отображаемое название — тоже пофилдово: оверлей его задаёт крайне редко (поля `name` в
        # теле `PATCH` нет вовсе), поэтому обычно оно приходит от источника и ПРОДОЛЖАЕТ за ним
        # следовать.
        effective_name = (
            overlay.name
            if overlay is not None and overlay.name is not None
            else (row.name if row else product_id)
        )
        effective_tokens = body.tokens if body.tokens is not None else stored_tokens
        if not tokens_changed and not archived_changed:
            return AdminProductWriteResponse(
                **self._product_item_of(
                    product_id=product_id,
                    name=effective_name,
                    tokens=stored_tokens,
                    purchase_kind=purchase_kind,
                    archived=stored_archived,
                    updated_at=overlay.updated_at if overlay is not None else None,
                ).model_dump(),
                previous_tokens=previous_tokens,
                changed=False,
                effective_after_seconds=self._effective_after(),
            )

        now = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        # ⚠️ ЗАПИСЫВАЮТСЯ ТОЛЬКО ТЕ КОЛОНКИ, ЧТО ЗАДАНЫ ПРАВКОЙ (§6.1). Строка оверлея хранит
        # РОВНО то, что задал оператор: `tokens` пишется ВМЕСТЕ с классом (инвариант «число
        # только вместе с классом», он же CHECK в БД), `archived` — в одиночку. `name` не
        # пишется никогда: поля `name` в теле `PATCH` нет, и скопированное в строку название
        # навсегда перестало бы следовать за источником, не правясь из CRM ни при каком праве.
        if overlay is None:
            self._session.add(
                AdminProduct(
                    product_id=product_id,
                    name=None,
                    purchase_kind=purchase_kind if body.tokens is not None else None,
                    tokens=body.tokens,
                    archived=archived,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            if body.tokens is not None:
                overlay.tokens = body.tokens
                overlay.purchase_kind = purchase_kind
            if body.archived is not None:
                overlay.archived = archived
            overlay.updated_at = now
        # Гонка на первичном ключе: строки ещё не было, и два одновременных `PATCH` вставляют
        # её оба. На `PATCH` это ровно «значение изменил другой оператор» → `409`, тот же код,
        # что и у явной проверки версии выше.
        await self._flush_or_conflict(
            SCOPE_PRODUCTS, status_code=409, detail=_VERSION_CONFLICT_DETAIL
        )
        if tokens_changed:
            await self._audit.record(
                AuditEvent(
                    user_id=None,
                    event_type=EVENT_ADMIN_PRODUCT_UPDATED,
                    payload={
                        "scope": SCOPE_PRODUCTS,
                        "id": product_id,
                        "previous": _short(previous_tokens),
                        "next": _short(body.tokens),
                        "actorClaim": actor_claim,
                    },
                )
            )
        if archived_changed:
            # Имя события называет ИЗМЕНЁННОЕ: смена флага витрины и смена цены — разные факты,
            # и записать одно действием другого запрещено.
            await self._audit.record(
                AuditEvent(
                    user_id=None,
                    event_type=EVENT_ADMIN_PRODUCT_ARCHIVED,
                    payload={
                        "scope": SCOPE_PRODUCTS,
                        "id": product_id,
                        "previous": _short(stored_archived),
                        "next": _short(archived),
                        "actorClaim": actor_claim,
                    },
                )
            )
        await self._commit_and_refresh()
        self._log_applied(
            SCOPE_PRODUCTS,
            product_id,
            {"tokens": previous_tokens, "archived": stored_archived},
            {"tokens": effective_tokens, "archived": archived},
            actor_claim,
        )
        # Из ЗАПИСАННЫХ значений, а не из снимка (см. `create_product`).
        return AdminProductWriteResponse(
            **self._product_item_of(
                product_id=product_id,
                name=effective_name,
                tokens=effective_tokens,
                purchase_kind=purchase_kind,
                archived=archived,
                updated_at=now,
            ).model_dump(),
            previous_tokens=previous_tokens,
            changed=tokens_changed or archived_changed,
            effective_after_seconds=self._effective_after(),
        )

    # --- тарифы ---------------------------------------------------------------------------

    def _tariff_item_of(
        self, row: tariff_registry.TariffRow, *, tokens: int, updated_at: datetime.datetime | None
    ) -> AdminTariffItem:
        """Строка тарифа с ЯВНО заданными ценой и отметкой версии.

        Метаданные варианта (`kind`/`name`/`provider`/`model`/`unit`/`options`) выводятся из
        реестра в КОДЕ и от снимка не зависят; из оверлея приходят только эти две величины,
        поэтому после записи их подставляет вызывающий, а не перечитанный снимок.
        """
        return AdminTariffItem(
            tariff_id=row.tariff_id,
            kind=row.kind,
            name=row.name,
            tokens=tokens,
            provider=row.provider,
            model=row.model,
            unit=row.unit,
            options=row.options,
            updated_at=_iso_z(updated_at),
        )

    def _tariff_item(self, row: tariff_registry.TariffRow) -> AdminTariffItem:
        return self._tariff_item_of(row, tokens=row.tokens, updated_at=row.updated_at)

    def list_pricing(self) -> AdminTariffListResponse:
        rows = tariff_registry.pricing_rows(settings=self._settings)
        return AdminTariffListResponse(items=[self._tariff_item(row) for row in rows])

    async def patch_tariff(
        self, tariff_id: str, body: AdminTariffPatchRequest, *, actor_claim: str | None
    ) -> AdminTariffWriteResponse:
        row = tariff_registry.find_tariff_row(tariff_id, settings=self._settings)
        if row is None:
            raise self._reject(
                SCOPE_TARIFFS,
                REASON_UNKNOWN_ID,
                400,
                f"тариф «{tariff_id}» на этом инстансе неизвестен",
            )
        overlay = await self._session.scalar(
            select(AdminTariff).where(AdminTariff.tariff_id == tariff_id)
        )
        # Версия — из БД, а не из снимка (см. `patch_product`).
        self._check_version(
            SCOPE_TARIFFS,
            overlay.updated_at if overlay is not None else None,
            body.if_updated_at,
        )
        if body.tokens < 1:
            # Ноль отвергается не «для строгости»: балансовый гейт его пропускает, а списание
            # берёт ноль — генерация тихо становится бесплатной. Второй барьер — CHECK в БД.
            # ⚠️ Код — `400`, а НЕ `422`, и лейбл — `undeclared_bound` (§11): замороженный
            # контракт объявляет тело как `tokens: number >= 0`, а наш `limits` несёт только
            # ВЕРХНЮЮ границу — ключа под нижнюю в замороженном наборе нет вовсе. Значит CRM
            # отклонить это в форме НЕ МОГЛА, и текст отказа — единственный носитель правила.
            raise self._reject(
                SCOPE_TARIFFS,
                REASON_UNDECLARED_BOUND,
                400,
                "tokens: цена в кредитах не может быть меньше 1",
            )
        if body.tokens > TARIFF_TOKENS_MAX:
            # Верхняя граница ОБЪЯВЛЕНА нами в `limits.tariff_tokens_max` ⇒ CRM могла отклонить
            # значение в своей форме ⇒ `422`/`out_of_range`. Соседняя ветка выше помечена
            # противоположно намеренно: две границы одного поля живут в разных мирах.
            raise self._reject(
                SCOPE_TARIFFS,
                REASON_OUT_OF_RANGE,
                422,
                f"tokens: целое число от 1 до {TARIFF_TOKENS_MAX}",
            )

        previous_tokens = overlay.tokens if overlay is not None else row.tokens
        changed = overlay is None or body.tokens != previous_tokens
        # ⚠️ ЗАПИСЬ ВЫПОЛНЯЕТСЯ ТОЛЬКО ТОГДА, КОГДА ОНА ЧТО-ТО МЕНЯЕТ, И ТОГДА ЖЕ ПИШЕТСЯ
        # АУДИТ — запись и след парны в обе стороны.
        #
        # «Что-то меняет» — это ДВА разных изменения, и второе легко потерять: изменение
        # ЗНАЧЕНИЯ и изменение СОСТАВА оверлеев. Первая правка ещё не заведённой строки меняет
        # состав, даже если значение совпало с env: с этого момента `updated_at` перестаёт быть
        # пустым (а именно им оператор видит, что строку трогали), величина уходит из-под `.env`
        # и снять её можно только из панели. Считать такую правку холостой значило бы принять
        # запись оператора и не сохранить её.
        #
        # Холостым остаётся ровно повтор по УЖЕ существующей строке: он не трогает БД вовсе,
        # иначе двигал бы `updated_at` и выдавал соседу ложный `409` на строку, которой никто
        # не правил.
        if not changed:
            return AdminTariffWriteResponse(
                **self._tariff_item_of(
                    row,
                    tokens=previous_tokens,
                    updated_at=overlay.updated_at if overlay is not None else None,
                ).model_dump(),
                previous_tokens=previous_tokens,
                changed=False,
                effective_after_seconds=self._effective_after(),
            )
        now = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        if overlay is None:
            self._session.add(AdminTariff(tariff_id=tariff_id, tokens=body.tokens, updated_at=now))
        else:
            overlay.tokens = body.tokens
            overlay.updated_at = now
        # Та же гонка и тот же код, что в `patch_product` (см. `_flush_or_conflict`).
        await self._flush_or_conflict(
            SCOPE_TARIFFS, status_code=409, detail=_VERSION_CONFLICT_DETAIL
        )
        await self._audit.record(
            AuditEvent(
                user_id=None,
                event_type=EVENT_ADMIN_TARIFF_UPDATED,
                payload={
                    "scope": SCOPE_TARIFFS,
                    "id": tariff_id,
                    "previous": _short(previous_tokens),
                    "next": _short(body.tokens),
                    "actorClaim": actor_claim,
                },
            )
        )
        await self._commit_and_refresh()
        self._log_applied(SCOPE_TARIFFS, tariff_id, previous_tokens, body.tokens, actor_claim)
        # Из ЗАПИСАННЫХ значений, а не из перечитанного снимка (см. `create_product`).
        return AdminTariffWriteResponse(
            **self._tariff_item_of(row, tokens=body.tokens, updated_at=now).model_dump(),
            previous_tokens=previous_tokens,
            changed=changed,
            effective_after_seconds=self._effective_after(),
        )

    # --- настройки ------------------------------------------------------------------------

    def _setting_item_of(
        self, spec: SettingSpec, *, value: Any, updated_at: datetime.datetime | None
    ) -> AdminSettingItem:
        """Элемент настройки с ЯВНО заданными значением и отметкой версии.

        Объявление строки (`type`/`label`/`options`/`constraints`) выводится из реестра в КОДЕ;
        из оверлея приходят только эти две величины.
        """
        options = spec.options(self._settings) if spec.options else None
        return AdminSettingItem(
            setting_id=spec.setting_id,
            type=spec.type,
            label=spec.label,
            value=value,
            group=spec.group,
            description=spec.description,
            options=(
                [AdminSettingOption(value=option, label=label) for option, label in options]
                if options is not None
                else None
            ),
            constraints=dict(spec.constraints) if spec.constraints else None,
            readonly=None,
            updated_at=_iso_z(updated_at),
        )

    def _setting_item(self, spec: SettingSpec) -> AdminSettingItem:
        snapshot = get_snapshot()
        overlay = snapshot.settings.get(spec.setting_id)
        return self._setting_item_of(
            spec,
            value=resolve_setting(spec.setting_id, settings=self._settings, snapshot=snapshot),
            updated_at=overlay.updated_at if overlay is not None else None,
        )

    def list_settings(self) -> AdminSettingListResponse:
        return AdminSettingListResponse(
            items=[self._setting_item(spec) for spec in declared_settings(self._settings)]
        )

    async def _check_model_invariant(self, spec: SettingSpec, value: Any) -> None:
        """`chat.default_model` обязана входить в `chat.models_offered`.

        Код отказа — `400`, а НЕ `422`, по предикату §11: это МЕЖЭЛЕМЕНТНЫЙ инвариант, он
        связывает две разные строки настроек, и выразить его ни в `options`, ни в `constraints`
        одной строки нечем — CRM не могла отклонить такую правку в форме.
        """
        # Снимок этого процесса отстаёт на окно обновления, поэтому межэлементный инвариант
        # сверяется по СТРОКАМ В БД — той же формы дефект, что и у проверки версии: соседняя
        # настройка, изменённая внутри окна, здесь ещё не видна, и барьер пропустил бы правку,
        # ради отклонения которой он и стоит.
        snapshot = await self._db_settings_snapshot(
            (SETTING_CHAT_DEFAULT_MODEL, SETTING_CHAT_MODELS_OFFERED)
        )
        if spec.setting_id == SETTING_CHAT_MODELS_OFFERED:
            current_default = chat_models.instance_default_model(
                settings=self._settings, snapshot=snapshot
            )
            if current_default not in value:
                raise self._reject(
                    SCOPE_SETTINGS,
                    REASON_CONFLICT,
                    400,
                    f"модель по умолчанию «{current_default}» обязана остаться"
                    " в списке предлагаемых: "
                    "сначала смените модель по умолчанию",
                )
        elif spec.setting_id == SETTING_CHAT_DEFAULT_MODEL:
            offered = resolve_setting(
                SETTING_CHAT_MODELS_OFFERED, settings=self._settings, snapshot=snapshot
            )
            if value not in offered:
                raise self._reject(
                    SCOPE_SETTINGS,
                    REASON_CONFLICT,
                    400,
                    f"модель «{value}» не входит в список предлагаемых:"
                    " сначала добавьте её туда",
                )

    async def _db_settings_snapshot(self, setting_ids: tuple[str, ...]) -> InstanceConfigSnapshot:
        """Снимок перечисленных настроек, собранный НАПРЯМУЮ из БД, минуя кэш процесса.

        Нужен там, где решение принимается о ЗАПИСИ: снимок процесса отстаёт на окно
        обновления, и проверка по нему пропустила бы правку, конфликтующую с чужой, уже
        закоммиченной. Разрешение значения при этом остаётся ЕДИНСТВЕННЫМ — тот же
        ``resolve_setting`` поверх подставленного снимка, а не вторая копия его правил.
        """
        overlays: dict[str, SettingOverlay] = {}
        for setting_id in setting_ids:
            row = await self._session.scalar(
                select(AdminSetting).where(AdminSetting.setting_id == setting_id)
            )
            if row is None:
                continue
            coerced = coerce_stored_setting_value(setting_id, row.value, self._settings)
            if coerced is None:
                continue
            overlays[setting_id] = SettingOverlay(
                setting_id=setting_id, value=coerced, updated_at=row.updated_at
            )
        return InstanceConfigSnapshot(settings=overlays)

    async def patch_setting(
        self, setting_id: str, body: AdminSettingPatchRequest, *, actor_claim: str | None
    ) -> AdminSettingWriteResponse:
        spec = find_setting(setting_id, self._settings)
        if spec is None:
            raise self._reject(
                SCOPE_SETTINGS,
                REASON_UNKNOWN_ID,
                400,
                f"настройка «{setting_id}» на этом инстансе неизвестна",
            )
        stored = await self._session.scalar(
            select(AdminSetting).where(AdminSetting.setting_id == setting_id)
        )
        # Версия — из БД, а не из снимка (см. `patch_product`).
        self._check_version(
            SCOPE_SETTINGS,
            stored.updated_at if stored is not None else None,
            body.if_updated_at,
        )
        try:
            value = validate_setting_value(spec, body.value, self._settings)
        except SettingValueError as exc:
            # Лейбл вычисляется из НАБЛЮДАЕМОГО ФАКТА ветки: нарушен объявленный ключ
            # `constraints` (размерная граница) — `out_of_range`; нарушен тип или перечень
            # `options` — `type_mismatch`. Отдавать `type_mismatch` на любой отказ значило бы
            # приучить дежурного не верить серии: пустой `chat.models_offered` и слишком
            # длинный CSV категорий приходили бы под лейблом «не тот тип».
            # ⚠️ Код ответа при этом ОДИН И ТОТ ЖЕ — `422`: он отвечает на «могла ли CRM
            # отклонить это в форме», а лейбл на «что именно оператор нарушил». Переписывать
            # код «для симметрии с лейблом» запрещено.
            reason = REASON_OUT_OF_RANGE if exc.constraint is not None else REASON_TYPE_MISMATCH
            raise self._reject(SCOPE_SETTINGS, reason, 422, str(exc)) from exc
        value = _normalized_setting_value(spec, value)
        await self._check_model_invariant(spec, value)

        # «Прежнее значение» — тоже из БД: строка, изменённая другим процессом внутри окна,
        # ещё не попала в снимок, и дельта аудита указала бы неверное направление.
        previous_value = (
            coerce_stored_setting_value(setting_id, stored.value, self._settings)
            if stored is not None
            else None
        )
        if previous_value is None:
            # Действующей строки в БД нет (или её значение не по объявленному типу) ⇒ прежнее
            # значение — ЕNV, и берётся оно поверх ПУСТОГО снимка, а не текущего: в снимке мог
            # остаться оверлей, которого в БД уже нет, и дельта аудита указала бы на величину,
            # которой не было. Разрешение при этом остаётся тем же единственным резолвером.
            previous_value = resolve_setting(
                setting_id, settings=self._settings, snapshot=EMPTY_SNAPSHOT
            )
        changed = stored is None or previous_value != value
        # ⚠️ ЗАПИСЬ ВЫПОЛНЯЕТСЯ ТОЛЬКО ТОГДА, КОГДА ОНА ЧТО-ТО МЕНЯЕТ, И ТОГДА ЖЕ ПИШЕТСЯ
        # АУДИТ — запись и след парны в обе стороны.
        #
        # «Что-то меняет» — это ДВА разных изменения, и второе легко потерять: изменение
        # ЗНАЧЕНИЯ и изменение СОСТАВА оверлеев. Первая правка ещё не заведённой строки меняет
        # состав, даже если значение совпало с env: с этого момента `updated_at` перестаёт быть
        # пустым (а именно им оператор видит, что строку трогали), величина уходит из-под `.env`
        # и снять её можно только из панели. Считать такую правку холостой значило бы принять
        # запись оператора и не сохранить её.
        #
        # Холостым остаётся ровно повтор по УЖЕ существующей строке: он не трогает БД вовсе,
        # иначе двигал бы `updated_at` и выдавал соседу ложный `409` на строку, которой никто
        # не правил.
        if not changed:
            return AdminSettingWriteResponse(
                **self._setting_item_of(
                    spec,
                    value=previous_value,
                    updated_at=stored.updated_at if stored is not None else None,
                ).model_dump(),
                previous_value=previous_value,
                changed=False,
                effective_after_seconds=self._effective_after(),
            )
        now = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        if stored is None:
            self._session.add(AdminSetting(setting_id=setting_id, value=value, updated_at=now))
        else:
            stored.value = value
            stored.updated_at = now
        # Та же гонка и тот же код, что в `patch_product` (см. `_flush_or_conflict`).
        await self._flush_or_conflict(
            SCOPE_SETTINGS, status_code=409, detail=_VERSION_CONFLICT_DETAIL
        )
        await self._audit.record(
            AuditEvent(
                user_id=None,
                event_type=EVENT_ADMIN_SETTING_UPDATED,
                payload={
                    "scope": SCOPE_SETTINGS,
                    "id": setting_id,
                    "previous": _short(previous_value),
                    "next": _short(value),
                    "actorClaim": actor_claim,
                },
            )
        )
        await self._commit_and_refresh()
        self._log_applied(SCOPE_SETTINGS, setting_id, previous_value, value, actor_claim)
        # Из ЗАПИСАННЫХ значений, а не из перечитанного снимка (см. `create_product`): та же
        # форма дефекта, тот же адресат — оператор, которому правка показалась непринятой.
        return AdminSettingWriteResponse(
            **self._setting_item_of(spec, value=value, updated_at=now).model_dump(),
            previous_value=previous_value,
            changed=changed,
            effective_after_seconds=self._effective_after(),
        )

    # --- общее ----------------------------------------------------------------------------

    async def _commit_and_refresh(self) -> None:
        """Зафиксировать факт и обновить снимок ЭТОГО процесса.

        Оператор, нажавший «Сохранить» и тут же перечитавший список, обязан увидеть новое
        значение; остальные процессы инстанса подхватят его в течение окна.

        ⚠️ **Отказ обновления снимка не имеет права превратить состоявшуюся правку в ошибку.**
        ``refresh_snapshot`` исключения не бросает — при ошибке БД он возвращает ``False`` и
        ОСТАВЛЯЕТ ПРЕЖНИЙ снимок; поэтому тело ответа собирается вызывающим из ЗАПИСАННЫХ
        значений, а не из перечитанного снимка. Ветки «если снимок не обновился» здесь нет
        намеренно: она выполнялась бы только при отказе БД, то есть почти никогда, и потому
        протухла бы незамеченной — один путь надёжнее двух, из которых один не ходят.

        Гонка на первичном ключе сюда НЕ доходит: её перехватывает ``_flush_or_conflict``,
        который каждый пишущий путь зовёт сразу после ``session.add(...)`` — до записей аудита
        и до этого коммита. Поэтому здесь ловить нечего, и никакого обработчика на ``commit()``
        нет намеренно.
        """
        await self._session.commit()
        try:
            await refresh_snapshot(self._session, self._settings)
        except Exception:  # noqa: BLE001 — факт уже зафиксирован и уже в аудите
            logger.exception("admin_override_snapshot_refresh_failed")
