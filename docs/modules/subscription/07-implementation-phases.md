# Subscription — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| SB-1 | Модель + миграция subscriptions. | DB |
| SB-2 | StoreKit verification (JWS / App Store Server API client, httpx). | SB-1, Q-007-1 (дефолт) |
| SB-3 | `/v1/subscription/sync`: verify → normalize → upsert → response. | SB-2 |
| SB-4 | Grant при активации/продлении (Wallet.grant фикс. пакета `SUBSCRIPTION_CREDITS_PER_PERIOD`, дефолт 1000; идемпотентно по transactionId периода). [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md). | SB-3, Wallet |
| SB-5 | refund/revocation handling → expired. | SB-3 |
| SB-6 | audit subscription_change. | SB-3, Audit |
| SB-7 | [ADR-106](../../adr/ADR-106-apple-billing-single-grant.md): `isUpgraded` → неактивна (`storekit._normalize_payload`); предикат устаревания — строка не меняется, ответ из строки; ключ `sub-grant:` с проверкой `adapty-txn:{T}` через `has_idempotency_key`, занятый ключ с другой суммой → `200`; источник суммы и WARNING/audit `subscription_product_unmapped`; audit `stale`/`upgraded`. Сценарии — [09-testing.md](09-testing.md#одна-оплата--одно-начисление-adr-106). | SB-4, SB-5, billing-adapty Фаза 9 |

> Q-006-1 закрыт (ADR-006): SB-4 разблокирован. Начисление — фикс. пакет на период, идемпотентно по transactionId.
