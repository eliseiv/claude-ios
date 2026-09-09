# Token Purchase — API Contracts

JWT, владелец = `sub`. Статус: **Реализован (MVP); требует доработки policy-guard** ([Q-015-1](../../99-open-questions.md) Closed = вариант B). Заголовок `Authorization: Bearer <JWT>`, тег `Tokens`.

> ✅ **[Q-015-1](../../99-open-questions.md) Closed (2026-06-02, вариант B):** покупка токенов **требует активной подписки** (докупка сверх месячного пакета). Без активной подписки → `403 subscription_required` **до** начисления. [ADR-002](../../adr/ADR-002-access-policy-state-machine.md) без изменений. Backend-доработка: добавить policy-guard перед `WalletService.grant` (см. [03-architecture.md](03-architecture.md), [07-implementation-phases.md](07-implementation-phases.md)).

## POST /v1/tokens/purchase
Обработка consumable-покупки пакета токенов.

### Request
```json
{
  "userId": "uuid",
  "transaction": { "...StoreKit consumable transaction payload (signed)..." }
}
```
- `transaction` — подписанный StoreKit payload consumable-покупки (JWS / App Store Server API). Не логируется (redaction, как subscription).

### Поведение ([ADR-015](../../adr/ADR-015-consumable-token-iap.md))
1. **Policy-guard (обязателен, [Q-015-1](../../99-open-questions.md) = вариант B):** проверить активную подписку (`subscription.status == active`). Нет активной подписки → **`403`** `{ "code": "subscription_required", "message": "..." }`. Кредиты **не** начисляются, ledger не пишется. Проверка — **до** verify/grant (fail-fast, не тратим вызов App Store API на неподписанных).
2. Верификация транзакции общим verifier'ом (реальная Apple JWS / `STOREKIT_TEST_MODE` для e2e). Невалидная → `422`/`400`.
3. Извлечь `transactionId`, `productId`.
4. Маппинг `productId → credits` через server-side `TOKEN_PRODUCTS`. Неизвестный `productId` → `422`.
5. `Wallet.grant(credits, idempotency_key=transactionId, type=credit, meta={source:"token_purchase", productId})`. Идемпотентно: повтор той же транзакции не начисляет повторно.

### Response (200)
```json
{
  "creditsAdded": 1500,
  "newBalance": 2730,
  "transactionId": "string"
}
```
- При повторной (уже обработанной) транзакции: `creditsAdded=0`, `newBalance` = текущий (идемпотентный ответ).

## GET /v1/tokens/products
Каталог продуктов: пакеты токенов **и** подписки. JWT (`bearerAuth`). Полное описание для интеграторов — [API-REFERENCE §GET /v1/tokens/products](../../API-REFERENCE.md#get-v1tokensproducts).

### Источник ответа (первый непустой выигрывает)
1. **Живой рублёвый каталог broadapps** — `GET {CLOUDPAYMENTS_API_BASE}/apps/{CLOUDPAYMENTS_APP_ID}/products` (`Bearer CLOUDPAYMENTS_API_TOKEN`, тот же клиент, что у checkout, [ADR-051](../../adr/ADR-051-cloudpayments-checkout-payment-link.md)). Оттуда — `title`, `kind`, `period`, `price`, `currency`, `isSpecialOffer`, `isDefault`; `credits` подставляются из нашей карты `TOKEN_PRODUCTS` (поставщик про кредиты не знает), у подписок — `null`. Любой сбой/неконфигурированный инстанс → следующий источник (ошибка наружу не поднимается).
2. **Статический `PRODUCTS_CATALOG`** (JSON-массив в env), если задан; элементы, не прошедшие схему, пропускаются.
3. **Карта `TOKEN_PRODUCTS`** — только `productId` + `credits`, без цен ([07-deployment.md](../../07-deployment.md)).

**Операторский оверлей каталога** ([ADR-099 §6](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)) —
правило по **владельцу витрины**, а не по номеру ветки:

- **архивный продукт исключается из ответа во ВСЕХ трёх ветках** — «снят с витрины» есть свойство
  продукта, а не свойство источника. На начисления архив не влияет: уже совершённые покупки и
  активные подписки продолжают начислять кредиты;
- **созданные оператором продукты добавляются в ветки 2 и 3** (каталогом там владеем мы):
  `credits` = `tokens` строки, `title` = `name`, `kind` = `tokens`\|`subscription`,
  `price`/`currency` = `null` — пока оператор не завёл продукт в панели поставщика, достоверной
  цены у нас нет, и придумывать её нельзя;
- **в ветке 1 своих строк мы не добавляем** — каталог принадлежит поставщику, и позиция без
  платёжной ссылки была бы некликабельной на пейволле; там оверлей уточняет только `credits` и
  `title` уже перечисленных продуктов.

### Response (200)
```json
{ "products": [
    { "productId": "week_6.99_nottrial", "title": "Неделя", "kind": "subscription",
      "period": "week", "price": 599, "currency": "RUB", "credits": null,
      "isSpecialOffer": false, "isDefault": false },
    { "productId": "1000_Tokens_59.99", "title": "1000 токенов", "kind": "tokens",
      "period": null, "price": 5990, "currency": "RUB", "credits": 1000,
      "isSpecialOffer": true, "isDefault": true }
] }
```
На инстансе без рублёвого каталога — `{ "products": [ { "productId": "100_tokens_9.99", "credits": 100, "isSpecialOffer": false, "isDefault": false } ] }` (цены клиент берёт из StoreKit; это не отказ).

| Поле | Тип | Прим. |
|---|---|---|
| `productId` | str | код продукта |
| `title`/`kind`/`period`/`price`/`currency` | str\|int\|null | из рублёвого каталога; `null`, если источник их не даёт. Единицы `price` — `TOKEN_PRODUCTS_PRICE_MINOR_UNITS` |
| `credits` | int\|null | **только** из server-side `TOKEN_PRODUCTS`; `null` у подписок и у пакета, не заведённого у нас (покупка такого будет отвергнута) |
| `isSpecialOffer` | bool | флаг `is_special_offer` рублёвого каталога; присутствует всегда (`false` там, где каталога нет); отображательный |
| `isDefault` | bool | **[ADR-098 §11](../../adr/ADR-098-broadapps-paywall-experiments-and-default-product.md)** — «продукт по умолчанию» (предвыбор на пейволле). Источник — **наша** сторона: `TOKEN_PRODUCTS_DEFAULT`, список идентификаторов на инстанс; поднимается в `_catalog_response` для **всех** веток источника; присутствует всегда (`false`, если продукта нет в списке); отображательный |

### Признак «по умолчанию» ([ADR-098 §11](../../adr/ADR-098-broadapps-paywall-experiments-and-default-product.md), пересмотр 2026-09-08)
- Живёт **у нас**, а не в каталоге поставщика: `TOKEN_PRODUCTS_DEFAULT` — список идентификаторов
  (JSON-массив либо перечисление через запятую), `src/app/config.py` (`token_products_default_raw`).
  ⚠️ **Прежняя редакция ([ADR-098 §9](../../adr/ADR-098-broadapps-paywall-experiments-and-default-product.md)) объявляла источником поле `is_default` каталога broadapps —
  это отменено:** сверка живого каталога (`novirell`, 2026-09-08) показала, что поля с таким смыслом
  у поставщика **нет ни одного**. Чтение `is_default` из кода убрано; ждать его появления нельзя.
- Признак поднимается **в единой точке** `_catalog_response`
  (`src/app/api_gateway/routers/token_purchase.py:165-189`) — одинаково для всех трёх веток
  источника: ветки различаются тем, ОТКУДА взят перечень продуктов, а «предвыбран» — свойство
  продукта. Новой таблицы и миграции нет.
- **На деньги не влияет никак**: цена, число кредитов и allowlist покупки по-прежнему берутся из
  серверных карт (anti-tamper, [ADR-015](../../adr/ADR-015-consumable-token-iap.md)).
- **Несколько помеченных продуктов — законное решение оператора, а не ошибка, и предупреждения
  больше нет.** Пока признак приходил из чужой панели, несколько помеченных означали описку;
  теперь список задаёт оператор ЭТОГО сервиса явной строкой конфигурации. WARNING
  `token_products_multiple_defaults` **снят вместе с источником**: предупреждение на каждом запросе
  каталога приучало бы игнорировать предупреждения. Порядок витрины задаёт каталог, а не список, —
  список только помечает.
- **Ни одного помеченного продукта** — штатное состояние: предвыбора нет, клиент ведёт себя как до
  появления поля. ⚠️ Пустой дефолт кода (`""`) при этом на живых инстансах **не действует**:
  значение задаётся при провижининге (`infra/fleet/provision.sh:126`).
- Величина классифицирована как принадлежащая домену продуктов
  ([ADR-099 §8.2 (г)](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md)); поднимать ли
  её в admin-поверхность `/products` — [Q-099-6](../../99-open-questions.md).

### Коды
`200`; `401` (нет/невалидный JWT); `429`; `5xx`.

## Ошибки
- **Нет активной подписки → `403` `{code: "subscription_required"}`** ([Q-015-1](../../99-open-questions.md) вариант B; код консистентен с enum [ADR-004](../../adr/ADR-004-blocked-http-200.md), здесь — как `code` в error-теле `4xx`, не `blockReason`+`200`, т.к. это не endpoint генерации).
- Неизвестный `productId` → `422`. Невалидная транзакция → `422`/`400`. `userId` ≠ `sub` → `403` (`code=forbidden`).
- `401` (нет/невалидный JWT), `429` (rate limit), `5xx` (App Store API / внутренняя ошибка).
