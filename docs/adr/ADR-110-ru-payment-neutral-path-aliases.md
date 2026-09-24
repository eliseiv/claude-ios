# ADR-110 — Нейтральные пути-дубликаты для ручек RU-оплаты (`/v1/web/*`)

- **Статус:** Accepted. **Состояние реализации на 2026-09-24T15:15Z, поэлементно:** (1) **код §1 написан** — `git status --porcelain -- src tests` даёт ` M src/app/api_gateway/routers/billing_cloudpayments.py` и ` M src/app/main.py`; в роутере `web_router = APIRouter(prefix="/v1/web", include_in_schema=False)` (`:81`), таблица `_ROUTES` (`:219`), регистрация обоих роутеров циклом по ней (`:306-308`); в `main.py:417` — `app.include_router(billing_cloudpayments.web_router)`; `git diff --stat -- src/app/billing_cloudpayments/service.py` пуст (сервис не менялся, как требует §5); (2) **в `main` не слито** — изменения не закоммичены (та же команда); (3) **не выкачено** — следует из (2); (4) **автотесты** — пишутся, покрытие не измерено (`grep -rln "/v1/web" tests` пуст на момент записи). **Переснято 2026-09-24T18:00Z:** код **слит в `main`** — коммит `67bfd66`, `git branch --contains 67bfd66` → `main`; **выкачено** — прогон CI `36020854651` на `67bfd66`: джоб `ssh deploy (shared server + Traefik)` — `success` (`gh run view`); **автотесты** — `tests/integration/test_billing_web_aliases_adr110.py`, `grep -c 'def test_'` = 12, покрытие не измерено; (5) **ревью кода** — не измеряется, статус не утверждается.
- **Переснято 2026-09-24T18:16Z:** состояние — поэлементно в строке выше, блок «Переснято 2026-09-24T18:00Z» (`main` — `67bfd66`; выкачено — CI `36020854651`, `ssh deploy` — `success`; 12 функций `test_`; ревью — не измеряется).
- **Дата:** 2026-09-24
- **Тип:** feature-ADR модуля [billing-cloudpayments](../modules/billing-cloudpayments/README.md). Логика оплаты, цены, начисление, контракты тел и ответов **не меняются**; добавляется второй путь к тем же обработчикам.
- **Решение владельца (дословно, пересмотру не подлежит):** «иногда IOS приложения банят за ру оплату, так что все эндпоинты /v1/billing/cloudpayments нужно продублировать и назвать их по другому». Таблица соответствия, выбранная владельцем:

| Действующий путь (остаётся) | Дубликат |
|---|---|
| `POST /v1/billing/cloudpayments/checkout` | `POST /v1/web/session` |
| `POST /v1/billing/cloudpayments/cancel` | `POST /v1/web/cancel` |
| `POST /v1/billing/cloudpayments/webhook` | `POST /v1/web/events` |
| `POST /v1/billing/cloudpayments/experiments/assign` | `POST /v1/web/offers/assign` |
| `POST /v1/billing/cloudpayments/experiments/paywall-shown` | `POST /v1/web/offers/shown` |

- **Не пересматривает** ни одного принятого ADR: [ADR-050](ADR-050-cloudpayments-webhook.md)/[ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) (вебхук), [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) (checkout), [ADR-098](ADR-098-broadapps-paywall-experiments-and-default-product.md) (эксперименты) действуют дословно и распространяются на дубликаты. Нормы «единственный публичный путь вебхука» в них нет (проверено `grep -n -i "единствен"` по телам ADR-050/051/054/098 и `modules/billing-cloudpayments/*`: «единственный» там относится к ТРИГГЕРУ начисления, не к URL), поэтому супессии не требуется.
- **Уточнение факта (решение не меняется):** `POST /v1/billing/cloudpayments/cancel` существует в коде с коммита `71b12bf` (`src/app/api_gateway/routers/billing_cloudpayments.py`, функция `cloudpayments_cancel`), но до этого ADR не был описан в `docs/`, а [00-overview.md](../modules/billing-cloudpayments/00-overview.md) числил «subscription cancel» вне scope. Контракт `/cancel` зафиксирован по коду (document-as-built) в [02-api-contracts.md §cancel](../modules/billing-cloudpayments/02-api-contracts.md#post-v1billingcloudpaymentscancel); сомнительный эффект (безусловный `will_renew=false`) нормой не объявлен — [TD-064](../100-known-tech-debt.md).
- **Миграций, новых env, новых таблиц, новых кодов ошибок — нет.**

## Контекст

**Состав предмета — по ПРИЗНАКУ «все маршруты под префиксом `/v1/billing/cloudpayments`», измерено по коду в этом ходу.** Роутер `src/app/api_gateway/routers/billing_cloudpayments.py` объявлен как `APIRouter(prefix="/v1/billing/cloudpayments", tags=["Billing (CloudPayments)"])` и несёт ровно пять декораторов `@router.post`: `/webhook`, `/checkout`, `/experiments/assign`, `/experiments/paywall-shown`, `/cancel` (прочитан файл целиком). Других роутеров с этим префиксом нет: `grep -rn "billing/cloudpayments" src` даёт, кроме этого роутера, только комментарии/докстроки (`config.py`, `billing_cloudpayments/__init__.py`). Состав совпадает с таблицей владельца — **сверх пяти маршрутов нет**, новых имён не вводится.

**Соседний потребитель того же клиента, НЕ под префиксом.** `CloudPaymentsCheckoutClient` используется ещё в `src/app/api_gateway/routers/token_purchase.py` — и не в `POST /v1/tokens/purchase` (тот верифицирует StoreKit-транзакцию и клиента оплаты не вызывает), а в `GET /v1/tokens/products` (`client.list_products()` — живой каталог поставщика). Путь `/v1/tokens/products` слов о RU-оплате не содержит; нужен ли ему дубликат — вопрос владельцу ([Q-110-1](../99-open-questions.md)), в норму не входит.

**Что из поверхности зависит от ПУТИ (измерено по коду).**
- Маршрутизация edge — только по `Host` (правила `infra/fleet/dynamic.yml` — `Host(...)`, `PathPrefix` в `infra/` нет): новые пути доходят до приложения без правки инфраструктуры.
- `SizeLimitMiddleware` (`src/app/api_gateway/middleware.py`) повышает лимит тела только для перечисленных в нём путей; ручек RU-оплаты среди них нет — обе пары получают общий лимит.
- Rate-limit ключуется НЕ путём: `rl:other:{user_id}` (`rate_limit.py:169`), `rl:experiments:{user_id}` (`:192`), `rl:cpwebhook:{ip}` (`:154`).
- Метрик с лейблом пути/маршрута в `src/app/observability/metrics.py` нет; outcome-логи (`cloudpayments_webhook_outcome`, `cloudpayments_checkout_outcome`, `cloudpayments_cancel_outcome`, логи экспериментов) пути не несут; audit `cloudpayments_payment` и чтения CRM (`src/app/admin/crm_service.py` → `cloudpayments_webhook_events`) пути не касаются.
- Access-логи: edge (Traefik) пишет только `400-599` (`infra/fleet/router/traefik.yml`, `accessLog.filters.statusCodes`), а access-лог приложения (логгер `uvicorn.access` в stdout контейнера api: gunicorn с `UvicornWorker` и `--access-logfile -`, `Dockerfile:93`) пишет метод, путь и статус КАЖДОГО запроса, включая `200`. Фильтр `AccessLogQueryRedactionFilter` (`src/app/observability/logging.py`) срезает query только у путей вебхука прокси медиа; у ручек RU-оплаты query нет, строка вида `POST /v1/web/events ... 200` секретов не содержит.
- OpenAPI: `/docs`/`/openapi.json` включаются `DOCS_ENABLED` (`src/app/main.py:339-341`), а провижининг флота ставит `setvar DOCS_ENABLED true` (`infra/fleet/provision.sh:159`) — схема на инстансах флота публична.
- Коды ошибок со словом `cloudpayments`: `cloudpayments_webhook_misconfigured` (`src/app/errors.py:345`), `cloudpayments_verification_unavailable` (`:360`), `cloudpayments_checkout_not_configured` (`:430`).

**Гипотеза мотива, не проверенная.** Какой именно признак приводит к бану приложения (строки в бинаре, URL в трафике, тексты ошибок, публичная схема сервера), не установлено ни владельцем, ни измерением. Поэтому решение ниже исходит из того, что проверяемо: оно убирает слово из ПУТЕЙ, которые приложение вызывает, и не делает утверждений о том, что этого достаточно. В частности, тело ответа checkout по-прежнему несёт `paymentUrl` платёжной страницы YooKassa (`02-api-contracts.md`, пример `https://yoomoney.ru/...`) — путь-дубликат это не скрывает и скрыть не может.

## Решение

### §1. Механизм — один обработчик, два пути, одна таблица регистрации

- Каждый дубликат регистрирует **ту же функцию-обработчик** (`cloudpayments_checkout`, `cloudpayments_cancel`, `cloudpayments_webhook`, `experiments_assign`, `experiments_paywall_shown`) вторым маршрутом на отдельном роутере с префиксом `/v1/web`. Копирования логики, обёрток с собственным телом и отдельных сервисов нет.
- **Инвариант «одна декларация»:** оба роутера строятся из ОДНОЙ таблицы (суффикс действующего пути, суффикс дубликата, функция, параметры маршрута), а не двумя независимыми наборами декораторов — два списка об одном факте расходятся молча. Параметры маршрута, обязанные совпадать у пары побуквенно: HTTP-метод, `response_model`, `status_code`, `dependencies` (у вебхука — `Depends(require_cloudpayments_webhook)`), параметры обработчика (JWT-зависимость `CurrentUser`, `Header` `Accept-Language` у экспериментов, сырое тело у вебхука). `response_model` в паре одинаков, потому что он определяет сериализацию: иной `response_model` у дубликата изменил бы форму ответа.
- Действующие пути, их роутер, теги, `summary`/`description` и схема OpenAPI **не меняются ни на байт**.

### §2. Что видно снаружи

- **Форма ответа — тождественна.** На одинаковый вход (тело, заголовки, JWT, состояние БД/поставщика) пара отвечает одинаковым HTTP-статусом и одинаковым телом, включая тела ошибок, `error.code` и `message`. Это следствие §1, а не отдельная реализация.
- **Коды ошибок на дубликатах — те же** (`cloudpayments_checkout_not_configured`, `cloudpayments_webhook_misconfigured`, `cloudpayments_verification_unavailable`, `upstream_error`, `rate_limited`, `unauthorized`, `validation_error`). Обоснование: (1) клиент, переключающийся со старого пути на новый, обрабатывает прежний набор кодов — ветвление клиентской логики по пути было бы новым контрактом; (2) выбор кода по пути — это логика, зависящая от маршрута, то есть нарушение §1; (3) коды со словом `cloudpayments` появляются только в отказах: `503 cloudpayments_checkout_not_configured` — на инстансе, где RU-оплата не настроена и приложение ручку вызывать не должно, `500`-коды вебхука видит только поставщик (сервер-сервер). Нейтральные коды для дубликатов — альтернатива, вынесенная владельцу ([Q-110-3](../99-open-questions.md)).
- **OpenAPI — дубликаты в схему НЕ входят** (`include_in_schema=False` на роутере `/v1/web`; прецедент — роутер `media_webhooks`, `src/app/api_gateway/routers/media_webhooks.py:39`). Обоснование: схема на флоте публична (см. Контекст); дубликат, показанный в ней рядом с оригиналом, с теми же моделями (`CloudPaymentsCheckoutRequest` и т. д.) и тегом `Billing (CloudPayments)`, сам раскрывает связь, ради сокрытия которой введён; нейтрализовать это можно только вторым набором имён схем и тегом, то есть второй поверхностью документации, расходящейся с первой. Контракт дубликатов для интеграторов живёт в `docs/` ([API-REFERENCE.md §7b](../API-REFERENCE.md), [02-api-contracts.md](../modules/billing-cloudpayments/02-api-contracts.md)). Подтверждение выбора — [Q-110-2](../99-open-questions.md). Требования [08-api-documentation.md §R2/§R4](../08-api-documentation.md) («каждый endpoint схемы — ровно один тег и одна security-привязка») на дубликаты не распространяются, потому что в схеме их нет; для оригиналов они действуют как прежде.

### §3. Защита вебхука на дубликате — без ослабления

`POST /v1/web/events` — тот же обработчик `cloudpayments_webhook`, поэтому совпадает всё, что задают [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) и [06-rbac.md](../modules/billing-cloudpayments/06-rbac.md):

| Свойство | Значение на обоих путях |
|---|---|
| Публичность | публичный, `401` не выдаётся; `require_cloudpayments_webhook` — наблюдательная зависимость (подписи/HMAC у поставщика нет — [05-security.md](../05-security.md)) |
| Trust-anchor начисления | только верификация `GET /users/{deviceId}/payments` нашим `CLOUDPAYMENTS_API_TOKEN` |
| Гейт инстанса | `CLOUDPAYMENTS_API_TOKEN` пуст → `500 cloudpayments_webhook_misconfigured` |
| Rate-limit | `enforce_cloudpayments_webhook_limits`, ключ `rl:cpwebhook:{ip}` — **ОДНА корзина на оба пути**: чередование путей бюджет не удваивает |
| Тело | сырое, без Pydantic-модели; кривое тело → `200 {"code":0}`, не `422` |
| Ответы | `200 {"code":0}` / `429` / `500` — как у оригинала |
| Идемпотентность | по broadapps `payment_id` (`cp-txn:{payment_id}`, дедуп `cloudpayments_webhook_events.transaction_id`) — один колбэк, доставленный на ОБА пути, начисляет один раз |
| Лимит размера тела | общий `SizeLimitMiddleware` |

> **Контраст с клиентскими ручками (§4):** у вебхука общая корзина `rl:cpwebhook` — а у клиентских ручек тоже общие, но СВОИ корзины (`rl:other`, `rl:experiments`). Правило одно: корзина принадлежит обработчику, а не пути, и в паре никогда не удваивается.

### §4. Авторизация и rate-limit клиентских ручек

| Пара | Auth | Корзина | Гейт инстанса |
|---|---|---|---|
| `/checkout` ↔ `/v1/web/session` | JWT (`CurrentUser`), `userId` = `sub` | `rl:other:{user_id}` | `cloudpayments_checkout_configured()` → `503` |
| `/cancel` ↔ `/v1/web/cancel` | JWT | `rl:other:{user_id}` | то же |
| `/experiments/assign` ↔ `/v1/web/offers/assign` | JWT | `rl:experiments:{user_id}` | то же |
| `/experiments/paywall-shown` ↔ `/v1/web/offers/shown` | JWT | `rl:experiments:{user_id}` | то же |

Корзина общая для пары: вызовы по старому и по новому пути расходуют один бюджет пользователя. Инвариант исходящего контура [ADR-098 §1](ADR-098-broadapps-paywall-experiments-and-default-product.md) (`user_id` к поставщику = JWT `sub`) действует на дубликатах без изменений.

### §5. Наблюдаемость

- **Метрики:** новых нет, лейблов пути не вводится (кардинальность не меняется).
- **Лог вебхука: новых полей НЕТ** (редакция после ревью, круг 1; в первой редакции здесь вводилось поле `pathFamily` — снято, см. §Альтернативы). Путь, на который пришёл колбэк, наблюдаем по access-логу приложения (см. Контекст), `cloudpayments_webhook_outcome` и сигнатура `CloudPaymentsWebhookService.handle(raw)` не меняются.
- **Клиентские ручки:** полей тоже не вводится; какой путь вызывает iOS-сборка, видно в том же access-логе приложения.
- **Audit / CRM:** не меняются — они пути не касаются.

### §6. Обратная совместимость

Действующие пути, их поведение, схема OpenAPI и корзины лимитов не меняются; ни один существующий клиент ничего не замечает. Этот ADR не снимает и не объявляет устаревшими старые пути; их снятие — только отдельным ADR.

**Необратимость, появляющаяся после выката:** как только iOS-сборка начнёт вызывать `/v1/web/*`, эти пути становятся публичным контрактом — откат коммита, вводящего дубликаты, после этого ломает такие сборки. Откат одним коммитом безопасен только до выпуска сборки, использующей новые пути.

### §7. Порядок выката

1. `backend` — реализация §1 (пятью вторыми маршрутами из одной таблицы, `include_in_schema=False`) → `backend-reviewer` → `qa` (§8) → Pre-push CI gate → выкат на весь флот штатным деплоем (маршруты есть на всех инстансах, работают там же, где оригиналы: гейты §3/§4 те же).
2. Смоук по классам инстансов — ожидание у дубликата то же, что у оригинала на том же инстансе:
   - **задан `CLOUDPAYMENTS_API_TOKEN`:** `POST /v1/web/events` с пустым телом → `200 {"code":0}`;
   - **`CLOUDPAYMENTS_API_TOKEN` пуст:** `POST /v1/web/events` → `500 cloudpayments_webhook_misconfigured` (гейт сервиса срабатывает до разбора тела, ADR-054);
   - **заданы оба `CLOUDPAYMENTS_APP_ID`+`CLOUDPAYMENTS_API_TOKEN`:** `POST /v1/web/session` с JWT идёт к поставщику; **хотя бы один пуст:** `503 cloudpayments_checkout_not_configured`;
   - на любом инстансе: `POST /v1/web/session` без JWT → `401`; `/openapi.json` не содержит `/v1/web/`.
3. iOS-команда переключает пути в новой сборке (вне этого репозитория).
4. **Операторский шаг (необязательный, решение — [Q-110-4](../99-open-questions.md)):** в панели broadapps сменить Callback URL приложения с `https://<домен>/v1/billing/cloudpayments/webhook` на `https://<домен>/v1/web/events`; проверить по access-логу приложения (`docker logs` контейнера api) строку `POST /v1/web/events` со статусом `200` на ближайшем колбэке. **Если поставщик продолжает слать на старый URL** — ничего не теряется: старый путь работает; если на оба — дедуп по `payment_id` (§3) не даёт двойного начисления. Вебхук вызывает поставщик, не приложение, поэтому для мотива владельца этот шаг не обязателен.

### §8. Тесты (обязательное покрытие, зона `qa`)

- **Паритет каждой из пяти пар** — отдельный параметризованный кейс на пару: одинаковый вход → одинаковые статус и тело, минимум по ветвям успеха и по одному отказу каждой пары (`401` без JWT у клиентских ручек; `503` на ненастроенном инстансе; `422` у checkout/экспериментов; `502` у `/assign`/`/checkout`/`/cancel`; `200 {"logged": false}` у `/shown`).
- **Вебхук на `/v1/web/events`:** без `Authorization` → не `401`; поддельный колбэк без подтверждённого платежа → начислений нет; `CLOUDPAYMENTS_API_TOKEN` пуст → `500 cloudpayments_webhook_misconfigured`; кривое тело → `200 {"code":0}`.
- **Общая корзина:** запросы, чередующие старый и новый путь, исчерпывают ОДИН лимит (`429` на обоих) — отдельно для `rl:cpwebhook`, `rl:other`, `rl:experiments`.
- **Дедуп через пару путей:** один и тот же платёж, доставленный сначала на старый, затем на новый путь, начисляется один раз.
- **OpenAPI:** в `/openapi.json` нет ни одного пути `/v1/web/`; операции оригиналов не изменились.
- **`/cancel` (до этого ADR без автотестов):** нет активной подписки у поставщика → `canceled=false`; отказ поставщика → `502 upstream_error`; успех → локально `will_renew=false`, `status`/`expires_at` не трогаются. Кейс `canceled=false` при существующей локальной строке `subscriptions` — исход **НЕ закрепляется** тестом, пока не решено [TD-064](../100-known-tech-debt.md) (сегодня код ставит `will_renew=false` и здесь); проверяется только паритет пары. **Уточнение 2026-09-24, решение ADR-110 не меняется:** TD-064 решён [ADR-111](ADR-111-ru-cancel-will-renew-only-on-found.md) — кейс закрепляет «`will_renew` не меняется, `willRenew` = текущее значение».
- Diff-стойкость: снятие регистрации дубликата (любого из пяти) обязано уронить хотя бы один кейс.

## Альтернативы

- **Префикс дубликатов из env на каждый инстанс** — предложен владельцу, НЕ выбран. Разные пути на разных инстансах = разная сборка клиента на инстанс и ещё одна настройка на провижининге.
- **Путь из клиентского конфига (сервер отдаёт адреса ручек)** — предложен владельцу, НЕ выбран. Добавляет конфиг-эндпоинт и зависимость клиента от него.
- **Скопировать обработчики под новыми именами** — отвергнуто: две реализации одного контракта расходятся.
- **Показать дубликаты в OpenAPI с нейтральными тегом, текстами и именами схем** — отвергнуто в пользу §2 (вторая поверхность документации, вторые имена моделей); вынесено на подтверждение ([Q-110-2](../99-open-questions.md)).
- **Скрыть из схемы оригиналы, а показывать дубликаты** — отвергнуто: меняет Swagger для существующих интеграторов.
- **Нейтральные коды ошибок на дубликатах** — отвергнуто в §2, вынесено владельцу ([Q-110-3](../99-open-questions.md)).
- **Переписывание путей на edge (Traefik rewrite `/v1/web/*` → старые пути)** — отвергнуто: правило живёт в инфраструктуре, а не рядом с обработчиком; автономные инстансы вне общего роутера правило не получат.

- **Поле `pathFamily` в логе исхода вебхука** (первая редакция этого ADR) — снято: необходимость обосновывалась отсутствием другого носителя пути для успешных колбэков, а он есть — access-лог приложения. Новое поле расширяло бы allowlist PII-чувствительного лога и меняло сигнатуру `handle` без потребителя, которого access-лог не обслуживает.

## Последствия

- Пять новых публичных путей на каждом инстансе; поведение и защита — как у оригиналов.
- Появление в коде слова `web` в префиксе не скрывает RU-оплату в ответе checkout (`paymentUrl`) — это за рамками решения.
- После выпуска iOS-сборки на новых путях они — часть публичного контракта (§6).
