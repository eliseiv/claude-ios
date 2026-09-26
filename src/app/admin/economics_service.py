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
    EVENT_ADMIN_CREDENTIAL_CLEARED,
    EVENT_ADMIN_CREDENTIAL_SET,
    EVENT_ADMIN_PRODUCT_ARCHIVED,
    EVENT_ADMIN_PRODUCT_CREATED,
    EVENT_ADMIN_PRODUCT_UPDATED,
    EVENT_ADMIN_SETTING_UPDATED,
    EVENT_ADMIN_TARIFF_UPDATED,
    AuditEvent,
    AuditService,
)
from app.byok.kms import KmsClient
from app.config import Settings
from app.instance_config import models as chat_models
from app.instance_config import products as product_catalog
from app.instance_config import tariffs as tariff_registry
from app.instance_config.credentials import (
    CREDENTIAL_ANTHROPIC_API_KEY,
    CREDENTIAL_MAX_LENGTH,
    CREDENTIAL_OPENAI_API_KEY,
    CREDENTIAL_PROXY_API_KEY,
    SOURCE_ENV,
    SOURCE_OVERLAY,
    SOURCE_UNSET,
    CredentialSpec,
    credential_fingerprint,
    declared_credentials,
    decrypt_credential,
    encrypt_credential,
    env_credential_value,
    find_credential,
    has_undeclared_character,
)
from app.instance_config.effective import (
    apply_overlay,
    base_settings_of,
    effective_settings,
    other_provider,
    providers_named,
)
from app.instance_config.settings_registry import (
    INFRA_SETTING_IDS,
    PRODUCT_TOKENS_MAX,
    SETTING_CHAT_ADVERTISED_MODES,
    SETTING_CHAT_DEFAULT_MODEL,
    SETTING_CHAT_MODELS_OFFERED,
    SETTING_LLM_DUAL_ENABLED,
    SETTING_LLM_PROVIDER,
    SETTING_STOREKIT_BUNDLE_ID,
    SETTING_STOREKIT_MODE,
    STOREKIT_MODE_PRODUCTION,
    STOREKIT_MODE_SANDBOX,
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
    CredentialOverlay,
    InstanceConfigSnapshot,
    SettingOverlay,
    get_snapshot,
    refresh_snapshot,
)
from app.models import AdminCredential, AdminProduct, AdminSetting, AdminTariff
from app.observability.logging import log_event
from app.observability.metrics import admin_override_rejected_total
from app.schemas.admin_economics import (
    AdminCapabilitiesResponse,
    AdminCredentialConstraints,
    AdminCredentialItem,
    AdminCredentialListResponse,
    AdminCredentialPatchRequest,
    AdminCredentialWriteResponse,
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
SCOPE_CREDENTIALS = "credentials"

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
# ⚠️ ОКРУЖЕНИЕ ИНСТАНСА — отдельное место (ADR-116 §4.3), а не «похожий случай» `conflict`:
# форма, границы, данные источника и соседние элементы в порядке, но на сервере нет того, без
# чего значение нерабочее или опасное (файл корневых сертификатов Apple; секрет подписи
# колбэков прокси). Лечится доступом к серверу, а не правкой другого элемента в CRM.
# Производители: `storekit.mode = production`, `PATCH proxy.api_key` и запись креденшла
# без мастер-ключа шифрования.
REASON_ENVIRONMENT_MISSING = "environment_missing"  # -> 400
# Третий производитель — решение main chat 2026-09-26: `PATCH /credentials` со строкой при
# незаданном мастер-ключе шифрования (без него значение не зашифровать).

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
    # ADR-116 §2.6: поинстансный признак поддержки `/v1/admin/credentials` — флот обновляется
    # порциями, и версия контракта CRM для флота не атомарна.
    "credentials.read",
    "credentials.write",
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
        # ADR-116 §5: действующие настройки — `options` моделей, доступность строк и
        # межэлементные инварианты считаются для провайдера, выбранного в CRM.
        self._settings = effective_settings(settings)
        self._base_settings = base_settings_of(self._settings)
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
        await self._check_infra_invariant(spec, value)

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
        if (
            setting_id == SETTING_STOREKIT_MODE
            and value == STOREKIT_MODE_SANDBOX
            and previous_value != STOREKIT_MODE_SANDBOX
        ):
            # ADR-116 §6 (в): в песочнице подлинность транзакции не проверяется, и покупку можно
            # подделать самодельным сертификатом — денежный риск, поэтому WARNING, а не INFO.
            log_event(
                logger,
                logging.WARNING,
                "admin_storekit_mode_changed",
                previous=_short(previous_value),
                next=_short(value),
                actorClaim=actor_claim,
            )
        # Из ЗАПИСАННЫХ значений, а не из перечитанного снимка (см. `create_product`): та же
        # форма дефекта, тот же адресат — оператор, которому правка показалась непринятой.
        return AdminSettingWriteResponse(
            **self._setting_item_of(spec, value=value, updated_at=now).model_dump(),
            previous_value=previous_value,
            changed=changed,
            effective_after_seconds=self._effective_after(),
        )

    # --- инварианты провайдера и StoreKit (ADR-116 §4.3) ------------------------------------

    async def _db_overlay_snapshot(self) -> InstanceConfigSnapshot:
        """Оверлей ADR-116 (строки провайдера/инфраструктуры и креденшлы), прочитанный из БД.

        Решение о ЗАПИСИ принимается по состоянию БД, а не по снимку процесса: снимок отстаёт
        на окно обновления, и соседняя правка внутри окна была бы не видна барьеру (та же форма
        дефекта, что у ``_check_model_invariant``). Строка креденшла, которую не удалось
        расшифровать, в снимок не входит — действует `.env`, как и при сборке снимка процесса.
        """
        overlays: dict[str, SettingOverlay] = {}
        for row in (
            await self._session.scalars(
                select(AdminSetting).where(AdminSetting.setting_id.in_(sorted(INFRA_SETTING_IDS)))
            )
        ).all():
            coerced = coerce_stored_setting_value(row.setting_id, row.value, self._base_settings)
            if coerced is None:
                continue
            overlays[row.setting_id] = SettingOverlay(
                setting_id=row.setting_id, value=coerced, updated_at=row.updated_at
            )
        credentials: dict[str, CredentialOverlay] = {}
        rows = list((await self._session.scalars(select(AdminCredential))).all())
        if rows:
            from app.byok.kms import get_kms_client

            try:
                kms = get_kms_client()
            except (RuntimeError, ValueError):
                kms = None
            for cred_row in rows:
                if kms is None or find_credential(cred_row.credential_id) is None:
                    continue
                try:
                    value = decrypt_credential(
                        kms,
                        cred_row.credential_id,
                        cred_row.encrypted_value,
                        cred_row.encrypted_dek,
                    )
                except Exception:  # noqa: BLE001 — нерасшифруемая строка = действует `.env`
                    continue
                credentials[cred_row.credential_id] = CredentialOverlay(
                    credential_id=cred_row.credential_id,
                    value=value,
                    fingerprint=cred_row.fingerprint,
                    updated_at=cred_row.updated_at,
                )
        return InstanceConfigSnapshot(settings=overlays, credentials=credentials)

    @staticmethod
    def _with_setting(
        snapshot: InstanceConfigSnapshot, setting_id: str, value: Any
    ) -> InstanceConfigSnapshot:
        now = datetime.datetime.now(tz=datetime.UTC)
        settings = dict(snapshot.settings)
        settings[setting_id] = SettingOverlay(setting_id=setting_id, value=value, updated_at=now)
        return InstanceConfigSnapshot(settings=settings, credentials=snapshot.credentials)

    @staticmethod
    def _with_credential(
        snapshot: InstanceConfigSnapshot, credential_id: str, value: str | None
    ) -> InstanceConfigSnapshot:
        credentials = dict(snapshot.credentials)
        if value is None:
            credentials.pop(credential_id, None)
        else:
            credentials[credential_id] = CredentialOverlay(
                credential_id=credential_id,
                value=value,
                fingerprint=credential_fingerprint(value),
                updated_at=datetime.datetime.now(tz=datetime.UTC),
            )
        return InstanceConfigSnapshot(settings=snapshot.settings, credentials=credentials)

    @staticmethod
    def _dual_enabled(cfg: Settings) -> bool:
        """`llm.dual_enabled` действующих настроек: CSV провайдеров называет второй провайдер."""
        second = other_provider(cfg._normalized_llm_provider())
        return second in providers_named(cfg.llm_providers_raw)

    def _reject_no_key(self, scope: str, provider: str) -> HTTPException:
        return self._reject(
            scope,
            REASON_CONFLICT,
            400,
            f"у провайдера «{provider}» нет основного ключа: сначала запишите ключ",
        )

    @staticmethod
    def _apple_roots_loaded(cfg: Settings) -> bool:
        from app.subscription.storekit import apple_root_certificates_loaded

        return apple_root_certificates_loaded(cfg.appstore_root_cert_dir)

    async def _check_infra_invariant(self, spec: SettingSpec, value: Any) -> None:
        """Межэлементные инварианты строк провайдера и StoreKit (ADR-116 §4.3).

        Правки, после которых инстанс перестал бы работать, отвергаются ДО записи. «Действующий
        ключ» — непустой ОСНОВНОЙ ключ провайдера после наложения оверлея, ровно предикат
        ``Settings._credits_api_key_configured``; резервный ключ действующим не считается.
        """
        if spec.setting_id not in INFRA_SETTING_IDS:
            return
        snapshot = self._with_setting(await self._db_overlay_snapshot(), spec.setting_id, value)
        cfg = apply_overlay(self._base_settings, snapshot)
        if spec.setting_id == SETTING_LLM_PROVIDER:
            provider = cfg._normalized_llm_provider()
            if not cfg._credits_api_key_configured(provider):
                raise self._reject_no_key(SCOPE_SETTINGS, provider)
        elif spec.setting_id == SETTING_LLM_DUAL_ENABLED:
            if value is True:
                second = other_provider(cfg._normalized_llm_provider())
                if not cfg._credits_api_key_configured(second):
                    raise self._reject_no_key(SCOPE_SETTINGS, second)
        elif spec.setting_id == SETTING_STOREKIT_MODE:
            if value == STOREKIT_MODE_PRODUCTION:
                if not cfg.appstore_bundle_id.strip():
                    raise self._reject(
                        SCOPE_SETTINGS,
                        REASON_CONFLICT,
                        400,
                        "режим Production требует идентификатор приложения: сначала задайте его",
                    )
                if not self._apple_roots_loaded(cfg):
                    raise self._reject(
                        SCOPE_SETTINGS,
                        REASON_ENVIRONMENT_MISSING,
                        400,
                        "на сервере не загружены корневые сертификаты Apple: режим Production "
                        "недоступен до их установки",
                    )
        elif spec.setting_id == SETTING_STOREKIT_BUNDLE_ID:
            mode = resolve_setting(SETTING_STOREKIT_MODE, settings=cfg, snapshot=snapshot)
            if isinstance(value, str) and not value.strip() and mode == STOREKIT_MODE_PRODUCTION:
                raise self._reject(
                    SCOPE_SETTINGS,
                    REASON_CONFLICT,
                    400,
                    "в режиме Production идентификатор приложения обязателен: сначала "
                    "переключите режим",
                )

    # --- креденшлы (ADR-116 §2) ---------------------------------------------------------

    @staticmethod
    def _credential_constraints() -> AdminCredentialConstraints:
        return AdminCredentialConstraints(max_length=CREDENTIAL_MAX_LENGTH)

    def _env_credential_state(self, spec: CredentialSpec) -> tuple[str, str | None, bool]:
        """``(source, fingerprint, configured)`` значения из конфигурации сервера."""
        value = env_credential_value(spec, self._base_settings)
        if not value.strip():
            return SOURCE_UNSET, None, False
        return SOURCE_ENV, credential_fingerprint(value), True

    def _credential_item_of(
        self,
        spec: CredentialSpec,
        *,
        source: str,
        fingerprint: str | None,
        configured: bool,
        updated_at: datetime.datetime | None,
    ) -> AdminCredentialItem:
        return AdminCredentialItem(
            credential_id=spec.credential_id,
            label=spec.label,
            group=spec.group,
            description=spec.description,
            constraints=self._credential_constraints(),
            configured=configured,
            source=source,
            fingerprint=fingerprint,
            updated_at=_iso_z(updated_at),
        )

    def _credential_item(self, spec: CredentialSpec) -> AdminCredentialItem:
        overlay = get_snapshot().credentials.get(spec.credential_id)
        if overlay is not None:
            return self._credential_item_of(
                spec,
                source=SOURCE_OVERLAY,
                fingerprint=overlay.fingerprint,
                configured=bool(overlay.value.strip()),
                updated_at=overlay.updated_at,
            )
        source, fingerprint, configured = self._env_credential_state(spec)
        return self._credential_item_of(
            spec, source=source, fingerprint=fingerprint, configured=configured, updated_at=None
        )

    def list_credentials(self) -> AdminCredentialListResponse:
        """Только метаданные: значение не отдаётся никогда (ADR-116 §2.4)."""
        return AdminCredentialListResponse(
            items=[self._credential_item(spec) for spec in declared_credentials()]
        )

    def _validate_credential_value(self, value: Any) -> str | None:
        """Одна ветка — один `reason` и один код (ADR-116 §2.3). Текст отказа значения не несёт."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise self._reject(
                SCOPE_CREDENTIALS, REASON_TYPE_MISMATCH, 422, "ожидается строка или null"
            )
        if len(value) > CREDENTIAL_MAX_LENGTH:
            raise self._reject(
                SCOPE_CREDENTIALS,
                REASON_OUT_OF_RANGE,
                422,
                f"длиннее {CREDENTIAL_MAX_LENGTH} символов",
            )
        if has_undeclared_character(value):
            raise self._reject(
                SCOPE_CREDENTIALS,
                REASON_UNDECLARED_BOUND,
                400,
                "значение содержит пробельный или управляющий символ",
            )
        return value

    async def _check_credential_invariant(self, spec: CredentialSpec, value: str | None) -> None:
        """Инварианты записи креденшла (ADR-116 §4.3), в порядке таблицы §2.3.

        Основной ключ выбранного провайдера (а при двух провайдерах — и второго) не может стать
        пустым; резервный ключ инвариант не нарушает никогда. Ключ прокси при пустом секрете
        подписи колбэков служит и ключом подписи — его смена сломала бы задачи в полёте.
        """
        provider_of = {
            CREDENTIAL_OPENAI_API_KEY: "openai",
            CREDENTIAL_ANTHROPIC_API_KEY: "anthropic",
        }
        provider = provider_of.get(spec.credential_id)
        if provider is not None:
            snapshot = self._with_credential(
                await self._db_overlay_snapshot(), spec.credential_id, value
            )
            cfg = apply_overlay(self._base_settings, snapshot)
            active = cfg._normalized_llm_provider()
            required = provider == active or (
                self._dual_enabled(cfg) and provider == other_provider(active)
            )
            if required and not cfg._credits_api_key_configured(provider):
                raise self._reject(
                    SCOPE_CREDENTIALS,
                    REASON_CONFLICT,
                    400,
                    f"провайдер «{provider}» выбран на инстансе, и без основного ключа он "
                    "перестанет работать: сначала смените провайдера",
                )
        if (
            spec.credential_id == CREDENTIAL_PROXY_API_KEY
            and not self._base_settings.proxy_webhook_secret.strip()
        ):
            raise self._reject(
                SCOPE_CREDENTIALS,
                REASON_ENVIRONMENT_MISSING,
                400,
                "на сервере не задан отдельный секрет подписи колбэков прокси: ключ прокси "
                "подписывает колбэки задач, и его смена сломала бы задачи в полёте",
            )

    async def patch_credential(
        self,
        credential_id: str,
        body: AdminCredentialPatchRequest,
        *,
        actor_claim: str | None,
    ) -> AdminCredentialWriteResponse:
        """Записать (строка) или удалить (``null``) строку оверлея креденшла (ADR-116 §2.3).

        Значение не попадает ни в ответ, ни в аудит, ни в лог, ни в текст отказа (§2.5).
        """
        spec = find_credential(credential_id)
        if spec is None:
            raise self._reject(
                SCOPE_CREDENTIALS,
                REASON_UNKNOWN_ID,
                400,
                f"креденшл «{credential_id}» на этом инстансе неизвестен",
            )
        value = self._validate_credential_value(body.value)
        await self._check_credential_invariant(spec, value)
        # Шифрование требует мастер-ключа на сервере: без него запись невозможна, и это место
        # несоответствия — ОКРУЖЕНИЕ инстанса (лечится доступом к серверу), а не правка в CRM.
        # Проверяется ДО любой записи; удаление строки (`null`) мастер-ключа не требует.
        kms = self._kms_or_reject() if value is not None else None

        stored = await self._session.scalar(
            select(AdminCredential).where(AdminCredential.credential_id == credential_id)
        )
        env_source, env_fingerprint, env_configured = self._env_credential_state(spec)
        previous_source: str = env_source
        previous_fingerprint: str | None = env_fingerprint
        if stored is not None:
            previous_source, previous_fingerprint = SOURCE_OVERLAY, stored.fingerprint

        if value is None:
            if stored is None:
                return AdminCredentialWriteResponse(
                    **self._credential_item_of(
                        spec,
                        source=env_source,
                        fingerprint=env_fingerprint,
                        configured=env_configured,
                        updated_at=None,
                    ).model_dump(),
                    changed=False,
                    effective_after_seconds=self._effective_after(),
                )
            await self._session.delete(stored)
            await self._session.flush()
            await self._audit_credential(
                EVENT_ADMIN_CREDENTIAL_CLEARED,
                credential_id,
                previous=(previous_source, previous_fingerprint),
                next_state=(env_source, env_fingerprint),
                actor_claim=actor_claim,
            )
            await self._commit_and_refresh()
            self._log_applied(
                SCOPE_CREDENTIALS,
                credential_id,
                f"{previous_source}:{previous_fingerprint}",
                f"{env_source}:{env_fingerprint}",
                actor_claim,
            )
            return AdminCredentialWriteResponse(
                **self._credential_item_of(
                    spec,
                    source=env_source,
                    fingerprint=env_fingerprint,
                    configured=env_configured,
                    updated_at=None,
                ).model_dump(),
                changed=True,
                effective_after_seconds=self._effective_after(),
            )

        assert kms is not None  # value is a string here, so the KMS client was resolved above
        fingerprint = credential_fingerprint(value)
        if stored is not None and self._stored_equals(kms, stored, value):
            # Холостой повтор по существующей строке БД не трогает: иначе сдвинулся бы
            # `updated_at` без изменения значения.
            return AdminCredentialWriteResponse(
                **self._credential_item_of(
                    spec,
                    source=SOURCE_OVERLAY,
                    fingerprint=stored.fingerprint,
                    configured=bool(value.strip()),
                    updated_at=stored.updated_at,
                ).model_dump(),
                changed=False,
                effective_after_seconds=self._effective_after(),
            )
        encrypted_value, encrypted_dek = encrypt_credential(kms, credential_id, value)
        now = datetime.datetime.now(tz=datetime.UTC).replace(microsecond=0)
        if stored is None:
            self._session.add(
                AdminCredential(
                    credential_id=credential_id,
                    encrypted_value=encrypted_value,
                    encrypted_dek=encrypted_dek,
                    fingerprint=fingerprint,
                    updated_at=now,
                )
            )
        else:
            stored.encrypted_value = encrypted_value
            stored.encrypted_dek = encrypted_dek
            stored.fingerprint = fingerprint
            stored.updated_at = now
        await self._flush_or_conflict(
            SCOPE_CREDENTIALS, status_code=409, detail=_VERSION_CONFLICT_DETAIL
        )
        await self._audit_credential(
            EVENT_ADMIN_CREDENTIAL_SET,
            credential_id,
            previous=(previous_source, previous_fingerprint),
            next_state=(SOURCE_OVERLAY, fingerprint),
            actor_claim=actor_claim,
        )
        await self._commit_and_refresh()
        self._log_applied(
            SCOPE_CREDENTIALS,
            credential_id,
            f"{previous_source}:{previous_fingerprint}",
            f"{SOURCE_OVERLAY}:{fingerprint}",
            actor_claim,
        )
        return AdminCredentialWriteResponse(
            **self._credential_item_of(
                spec,
                source=SOURCE_OVERLAY,
                fingerprint=fingerprint,
                configured=bool(value.strip()),
                updated_at=now,
            ).model_dump(),
            changed=True,
            effective_after_seconds=self._effective_after(),
        )

    def _kms_or_reject(self) -> KmsClient:
        """KMS-клиент либо `400 environment_missing`, если мастер-ключ на сервере не задан."""
        from app.byok.kms import get_kms_client

        try:
            return get_kms_client()
        except (RuntimeError, ValueError) as exc:
            raise self._reject(
                SCOPE_CREDENTIALS,
                REASON_ENVIRONMENT_MISSING,
                400,
                "на сервере не задан ключ шифрования: записать значение невозможно",
            ) from exc

    @staticmethod
    def _stored_equals(kms: KmsClient, stored: AdminCredential, value: str) -> bool:
        """Совпадает ли записанное значение с присланным. Нерасшифруемая строка — «нет»."""
        if stored.fingerprint != credential_fingerprint(value):
            return False
        try:
            current = decrypt_credential(
                kms, stored.credential_id, stored.encrypted_value, stored.encrypted_dek
            )
        except Exception:  # noqa: BLE001 — не расшифровалась: перезаписываем
            return False
        return current == value

    async def _audit_credential(
        self,
        event_type: str,
        credential_id: str,
        *,
        previous: tuple[str, str | None],
        next_state: tuple[str, str | None],
        actor_claim: str | None,
    ) -> None:
        """Аудит правки креденшла: идентификатор, источник и отпечаток до→после (§2.5).

        ⚠️ Имена полей деталей подобраны вне денилиста редакции (``*key*``/``*token*``/
        ``*secret*``/``*credential*``): иначе редакция стёрла бы саму деталь. Значение здесь не
        появляется ни в каком виде.
        """
        await self._audit.record(
            AuditEvent(
                user_id=None,
                event_type=event_type,
                payload={
                    "scope": SCOPE_CREDENTIALS,
                    "id": credential_id,
                    "source": f"{previous[0]}->{next_state[0]}",
                    "fingerprint": f"{previous[1]}->{next_state[1]}",
                    "actorClaim": actor_claim,
                },
            )
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
