"""Integration: у каждого канала начисления СВОЙ фолбэк — на реальных путях (ADR-099 §6).

**Зачем отдельный файл.** Резолвер `subscription_credits()` покрыт юнитом
(``tests/unit/test_instance_config_products_adr099.py``), но юнит сам передаёт имя канала — то
есть доказывает устройство резолвера, а НЕ то, что каждый рабочий путь передаёт ему СВОЙ канал.
Это ровно форма «объявлено ≠ подключено»: подмена `CHANNEL_ADAPTY` на `CHANNEL_CLOUDPAYMENTS`
внутри вебхука прошла бы мимо юнита.

⚠️ **Соседние наборы фикстур этого не ловят по построению.** Все действующие
webhook-тесты (`test_billing_adapty_webhook.py`, `test_billing_cloudpayments_*`) фиксируют свой
фолбэк числом **1000** — тем же, что и у соседнего канала и что у
`SUBSCRIPTION_CREDITS_PER_PERIOD`. На совпадающих числах подмена фолбэка **невидима**, и зелёный
прогон там ничего о ней не говорит. Здесь три величины заведомо РАЗНЫЕ.

Продукты подобраны так, чтобы каждый путь ушёл именно в ФОЛБЭК: их нет ни в одной карте канала.
"""

from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.service import AuditService
from app.config import Settings, get_settings
from app.wallet.service import WalletService
from tests.conftest import seed_user

# ТРИ РАЗНЫХ числа — единственное, что делает подмену фолбэка видимой.
_CP_FALLBACK = 111
_ADAPTY_FALLBACK = 222
_PERIOD_FALLBACK = 333

# Продукт, которого нет ни в `CLOUDPAYMENTS_PRODUCT_TOKENS`, ни в `ADAPTY_PRODUCT_TOKENS`.
_UNMAPPED_SUB = "plan.unmapped.month"
# Продукт из `TOKEN_PRODUCTS`: карта CloudPayments его не знает, и ручная выдача уходит в
# `SUBSCRIPTION_CREDITS_PER_PERIOD` — пара «карта + фолбэк» у неё НЕ совпадает ни с одним вебхуком.
_TOKEN_PRODUCT = "tokens_100"

_ENV: dict[str, Any] = {
    "CLOUDPAYMENTS_SUBSCRIPTION_TOKENS_GRANT": _CP_FALLBACK,
    "ADAPTY_SUBSCRIPTION_TOKENS_GRANT": _ADAPTY_FALLBACK,
    "SUBSCRIPTION_CREDITS_PER_PERIOD": _PERIOD_FALLBACK,
    "CLOUDPAYMENTS_PRODUCT_TOKENS": json.dumps({"cp.known.month": 4444}),
    "ADAPTY_PRODUCT_TOKENS": json.dumps({"adapty.known.month": 5555}),
    "TOKEN_PRODUCTS": json.dumps({_TOKEN_PRODUCT: 100}),
    "PRODUCTS_CATALOG": "",
    "CLOUDPAYMENTS_API_TOKEN": "verify-token-secret",
    "ADAPTY_WEBHOOK_SECRET": "adapty-secret",
}


def _settings(**over: Any) -> Settings:
    return Settings(**{**_ENV, **over})


async def _balance(maker: async_sessionmaker[AsyncSession], uid: uuid.UUID) -> int:
    async with maker() as s:
        return int(
            await s.scalar(text("SELECT balance FROM wallets WHERE user_id=:u"), {"u": str(uid)})
            or 0
        )


def test_the_three_fallbacks_are_distinct_in_this_fixture() -> None:
    """Предусловие файла — и одновременно объяснение, почему соседние наборы дефект не ловят."""
    settings = _settings()

    assert (
        len(
            {
                settings.cloudpayments_subscription_tokens_grant,
                settings.adapty_subscription_tokens_grant,
                settings.subscription_credits_per_period,
            }
        )
        == 3
    )


# ============================== канал `adapty` ==============================================
@pytest.mark.asyncio
async def test_the_adapty_webhook_grants_its_own_channel_fallback(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: `POST /v1/billing/adapty/webhook` → consumer: `subscription_credits(_, adapty)`.

    Кейс падает при подмене `CHANNEL_ADAPTY` на любой другой канал: числа фолбэков различны.
    """
    from app.billing_adapty.service import AdaptyWebhookService

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    settings = _settings()
    audit = AuditService(db_session)
    service = AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, settings)
    body = json.dumps(
        {
            "event_id": f"evt-{uuid.uuid4()}",
            "event_type": "subscription_started",
            "customer_user_id": str(uid),
            "event_properties": {
                "vendor_product_id": _UNMAPPED_SUB,
                "expires_at": "2027-07-12T00:00:00Z",
            },
        }
    ).encode()

    outcome = await service.handle(body)
    await db_session.commit()

    assert outcome.result == "applied", outcome
    assert await _balance(db_sessionmaker, uid) == _ADAPTY_FALLBACK
    assert await _balance(db_sessionmaker, uid) != _CP_FALLBACK
    assert await _balance(db_sessionmaker, uid) != _PERIOD_FALLBACK


@pytest.mark.asyncio
async def test_the_adapty_webhook_still_prefers_its_own_product_map(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Карта канала выигрывает у фолбэка — та половина пары, которую фолбэк не подменяет."""
    from app.billing_adapty.service import AdaptyWebhookService

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    settings = _settings()
    audit = AuditService(db_session)
    service = AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, settings)

    outcome = await service.handle(
        json.dumps(
            {
                "event_id": f"evt-{uuid.uuid4()}",
                "event_type": "subscription_started",
                "customer_user_id": str(uid),
                "event_properties": {
                    "vendor_product_id": "adapty.known.month",
                    "expires_at": "2027-07-12T00:00:00Z",
                },
            }
        ).encode()
    )
    await db_session.commit()

    assert outcome.result == "applied", outcome
    assert await _balance(db_sessionmaker, uid) == 5555


# ============================== канал `cloudpayments` =======================================
class _FakeVerifyClient:
    def __init__(self, payments: list[dict[str, Any]]) -> None:
        self._payments = payments

    async def list_payments(self, *, device_id: uuid.UUID) -> list[dict[str, Any]]:
        _ = device_id
        return [dict(p) for p in self._payments]


def _paid(code: str, payment_type: str = "subscription") -> dict[str, Any]:
    return {
        "payment_id": f"pay-{uuid.uuid4()}",
        "status": "succeeded",
        "paid_at": (
            datetime.datetime.now(tz=datetime.UTC) - datetime.timedelta(minutes=5)
        ).isoformat(),
        "product": {"code": code, "payment_type": payment_type},
    }


@pytest.mark.asyncio
async def test_the_cloudpayments_webhook_grants_its_own_channel_fallback(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """producer: вебхук broadapps → consumer: `subscription_credits(_, cloudpayments)`.

    Кейс падает при подмене фолбэка на adapty-й или на `SUBSCRIPTION_CREDITS_PER_PERIOD`.
    """
    from app.billing_cloudpayments.service import CloudPaymentsWebhookService

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    audit = AuditService(db_session)
    service = CloudPaymentsWebhookService(
        db_session,
        WalletService(db_session, audit),
        audit,
        _settings(),
        _FakeVerifyClient([_paid(_UNMAPPED_SUB)]),
    )
    body = json.dumps(
        {
            "Status": "Completed",
            "OperationType": "Payment",
            "Amount": 100,
            "Currency": "RUB",
            "AccountId": str(uid),
        }
    ).encode()

    outcome = await service.handle(body)
    await db_session.commit()

    assert outcome.result == "applied", outcome
    assert await _balance(db_sessionmaker, uid) == _CP_FALLBACK
    assert await _balance(db_sessionmaker, uid) != _ADAPTY_FALLBACK
    assert await _balance(db_sessionmaker, uid) != _PERIOD_FALLBACK


# ============================== канал `manual` ==============================================
@pytest.mark.asyncio
async def test_manual_plan_grant_uses_the_period_fallback_not_the_cloudpayments_one(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """⚠️ Пара «карта + фолбэк» у ручной выдачи НЕ совпадает ни с одним вебхуком (§6).

    Форма принимает продукты из `TOKEN_PRODUCTS`, которых в карте CloudPayments нет, — значит
    именно они и уходят в фолбэк. Свести четыре пары к трём «по смыслу» запрещено: экономия одной
    строки здесь стоит неверного начисления, и кейс падает ровно на такой экономии.
    """
    from app.admin.crm_service import CrmAdminService
    from app.admin.service import AdminService

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    settings = _settings()
    audit = AuditService(db_session)
    wallet = WalletService(db_session, audit)
    service = CrmAdminService(
        db_session, wallet, audit, AdminService(db_session, wallet, audit), settings
    )

    response = await service.grant_subscription(
        uid, product_id=_TOKEN_PRODUCT, expires_in_days=30, grant_id=str(uuid.uuid4())
    )
    await db_session.commit()

    assert response.tokens == _PERIOD_FALLBACK
    assert response.tokens != _CP_FALLBACK
    assert response.subscription_active is True
    assert await _balance(db_sessionmaker, uid) == _PERIOD_FALLBACK


@pytest.mark.asyncio
async def test_manual_plan_grant_of_a_mapped_product_reads_the_cloudpayments_map(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Вторая половина пары: КАРТА у ручной выдачи именно CloudPayments-ская, и это дословно
    сегодняшнее поведение."""
    from app.admin.crm_service import CrmAdminService
    from app.admin.service import AdminService

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    audit = AuditService(db_session)
    wallet = WalletService(db_session, audit)
    service = CrmAdminService(
        db_session, wallet, audit, AdminService(db_session, wallet, audit), _settings()
    )

    response = await service.grant_subscription(
        uid, product_id="cp.known.month", expires_in_days=30, grant_id=str(uuid.uuid4())
    )
    await db_session.commit()

    assert response.tokens == 4444
    assert await _balance(db_sessionmaker, uid) == 4444


# ============================== канал `storekit` ============================================
@pytest.mark.asyncio
async def test_the_storekit_subscription_sync_keeps_the_fixed_period_grant(
    db_session: AsyncSession,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Путь продукта не знает, и грант остаётся фиксированным — аддитивно, как сегодня.

    Кейс падает, если этот путь начнёт читать фолбэк другого канала: числа различны.
    """
    from app.subscription.service import SubscriptionService
    from app.subscription.storekit import VerifiedTransaction

    for alias, value in _ENV.items():
        monkeypatch.setenv(alias, str(value))
    get_settings.cache_clear()
    try:
        uid = uuid.uuid4()
        await seed_user(db_session, user_id=uid)

        class _Verifier:
            def verify(self, _signed: str) -> VerifiedTransaction:
                return VerifiedTransaction(
                    transaction_id=f"txn-{uuid.uuid4()}",
                    original_transaction_id="orig-1",
                    product_id=_UNMAPPED_SUB,
                    expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=30),
                    revoked=False,
                    environment="Sandbox",
                )

        audit = AuditService(db_session)
        service = SubscriptionService(
            db_session,
            _Verifier(),  # type: ignore[arg-type]
            WalletService(db_session, audit),
            audit,
        )

        await service.sync(uid, "signed-jws")
        await db_session.commit()

        assert await _balance(db_sessionmaker, uid) == _PERIOD_FALLBACK
        assert await _balance(db_sessionmaker, uid) != _CP_FALLBACK
        assert await _balance(db_sessionmaker, uid) != _ADAPTY_FALLBACK
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_an_operator_overlay_overrides_the_fallback_on_every_channel(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Оверлей — серверный источник, и он выигрывает у карты и у фолбэка на РАБОЧЕМ пути.

    Анти-тампер сохраняется дословно: число кредитов приходит только из серверного источника,
    никогда из тела пользовательского запроса.
    """
    from app.billing_adapty.service import AdaptyWebhookService
    from app.instance_config.snapshot import (
        InstanceConfigSnapshot,
        ProductOverlay,
        install_snapshot,
    )

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    install_snapshot(
        InstanceConfigSnapshot(
            products={
                _UNMAPPED_SUB: ProductOverlay(
                    product_id=_UNMAPPED_SUB,
                    name="План оператора",
                    purchase_kind="subscription",
                    tokens=1234,
                    archived=False,
                    updated_at=datetime.datetime.now(tz=datetime.UTC),
                )
            }
        )
    )
    audit = AuditService(db_session)
    service = AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, _settings())

    outcome = await service.handle(
        json.dumps(
            {
                "event_id": f"evt-{uuid.uuid4()}",
                "event_type": "subscription_started",
                "customer_user_id": str(uid),
                "event_properties": {
                    "vendor_product_id": _UNMAPPED_SUB,
                    "expires_at": "2027-07-12T00:00:00Z",
                },
            }
        ).encode()
    )
    await db_session.commit()

    assert outcome.result == "applied", outcome
    assert await _balance(db_sessionmaker, uid) == 1234


@pytest.mark.asyncio
async def test_an_archived_product_still_grants_on_the_webhook_path(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Архив снимает продукт с ВИТРИНЫ и не влияет ни на одно начисление (§6, таблица архивации).

    Иначе архив ломал бы уже оплаченное и активные подписки — адресат правила витрины другой.
    """
    from app.billing_adapty.service import AdaptyWebhookService
    from app.instance_config.snapshot import (
        InstanceConfigSnapshot,
        ProductOverlay,
        install_snapshot,
    )

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    install_snapshot(
        InstanceConfigSnapshot(
            products={
                _UNMAPPED_SUB: ProductOverlay(
                    product_id=_UNMAPPED_SUB,
                    name="Снят с витрины",
                    purchase_kind="subscription",
                    tokens=777,
                    archived=True,
                    updated_at=datetime.datetime.now(tz=datetime.UTC),
                )
            }
        )
    )
    audit = AuditService(db_session)
    service = AdaptyWebhookService(db_session, WalletService(db_session, audit), audit, _settings())

    outcome = await service.handle(
        json.dumps(
            {
                "event_id": f"evt-{uuid.uuid4()}",
                "event_type": "subscription_started",
                "customer_user_id": str(uid),
                "event_properties": {
                    "vendor_product_id": _UNMAPPED_SUB,
                    "expires_at": "2027-07-12T00:00:00Z",
                },
            }
        ).encode()
    )
    await db_session.commit()

    assert outcome.result == "applied", outcome
    assert await _balance(db_sessionmaker, uid) == 777


# ============ продукт оператора: классификация и начисление читают ОДНО множество ============
# Разовый продукт, ЗАВЕДЁННЫЙ ОПЕРАТОРОМ, чьё имя выглядит подпиской для эвристики
# `classify_product` (ключевое слово `month`). В env-картах его нет ни в одной.
_OPERATOR_ONE_TIME_SUB_LIKE = "operator.pack.month"
# Он же, но с «голым» именем: на СЫРОЙ env-карте эвристика вернула бы `unknown`.
_OPERATOR_ONE_TIME_BARE = "operator.pack.bare"
# Число, заведомо отличное от ЛЮБОГО фолбэка канала: на совпадающих числах подмена невидима.
_OPERATOR_TOKENS = 1357


def _one_time_overlay(product_id: str) -> Any:
    from app.instance_config.snapshot import InstanceConfigSnapshot, ProductOverlay

    return InstanceConfigSnapshot(
        products={
            product_id: ProductOverlay(
                product_id=product_id,
                name="Пачка оператора",
                purchase_kind="one_time",
                tokens=_OPERATOR_TOKENS,
                archived=False,
                updated_at=datetime.datetime.now(tz=datetime.UTC),
            )
        }
    )


@pytest.mark.asyncio
async def test_an_operator_one_time_product_named_like_a_subscription_grants_its_own_tokens(
    db_session: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """ADR-099 §6: классификация обязана смотреть в ТО ЖЕ множество, из которого берётся сумма.

    Форма дефекта — «классификация по одному источнику, деньги по другому». Разовый продукт,
    заведённый оператором, на СЫРОЙ env-карте не находится; эвристика имени видит `month`,
    переклассифицирует платёж в подписку, и вместо `tokens` продукта начисляется ФОЛБЭК КАНАЛА.

    ⚠️ Фикстура задаёт фолбэк канала (`_CP_FALLBACK`) ОТЛИЧНЫМ от `tokens` продукта: на
    совпадающих числах подмена невидима, и зелёный прогон о ней ничего не говорит.
    """
    from app.billing_cloudpayments.service import CloudPaymentsWebhookService
    from app.instance_config.snapshot import install_snapshot

    uid = uuid.uuid4()
    await seed_user(db_session, user_id=uid)
    install_snapshot(_one_time_overlay(_OPERATOR_ONE_TIME_SUB_LIKE))
    audit = AuditService(db_session)
    service = CloudPaymentsWebhookService(
        db_session,
        WalletService(db_session, audit),
        audit,
        _settings(),
        _FakeVerifyClient([_paid(_OPERATOR_ONE_TIME_SUB_LIKE, payment_type="one_time")]),
    )

    outcome = await service.handle(
        json.dumps(
            {
                "Status": "Completed",
                "OperationType": "Payment",
                "Amount": 100,
                "Currency": "RUB",
                "AccountId": str(uid),
            }
        ).encode()
    )
    await db_session.commit()

    assert outcome.result == "applied", outcome
    assert await _balance(db_sessionmaker, uid) == _OPERATOR_TOKENS
    # …и это НЕ фолбэк канала, в который платёж уехал бы после ложной переклассификации.
    assert _OPERATOR_TOKENS != _CP_FALLBACK  # предусловие фикстуры, названное здесь же
    assert await _balance(db_sessionmaker, uid) != _CP_FALLBACK


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "product_id",
    [_OPERATOR_ONE_TIME_SUB_LIKE, _OPERATOR_ONE_TIME_BARE],
    ids=["subscription_like_name", "bare_name"],
)
async def test_the_checkout_gate_issues_a_link_for_the_same_operator_product(
    product_id: str,
) -> None:
    """СИММЕТРИЯ гейта ссылки на оплату с вебхуком (ADR-099 §6, ADR-051 §2).

    Гейт объявляет себя симметричным вебхуку, и симметрия обязана держаться на ОДНОМ множестве:
    иначе продукт, заведённый оператором и у нас, и в панели поставщика, вебхук зачёл бы, а
    ссылку на оплату мы бы не выдали — деньги приняли, купить не дали.

    Оба имени намеренно: «голое» падает при возврате к сырой env-карте (эвристика вернёт
    `unknown` → `422`), «похожее на подписку» держит именно тот продукт, что и соседний кейс
    начисления, — чтобы симметрия проверялась на ТОМ ЖЕ входе, а не на удобном.
    """
    from app.billing_cloudpayments.checkout import CloudPaymentsCheckoutClient
    from app.instance_config.snapshot import install_snapshot

    install_snapshot(_one_time_overlay(product_id))
    client = CloudPaymentsCheckoutClient(_settings())

    client.validate_product(product_id)  # не поднимает — продукт известен инстансу

    # Контраст: идентификатор, которого нет НИ в env-карте, НИ в оверлее, по-прежнему отвергается.
    from app.errors import ValidationFailedError

    with pytest.raises(ValidationFailedError):
        client.validate_product("operator.pack.never-created")
