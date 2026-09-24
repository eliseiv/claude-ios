# 04 — Модель данных

Одна таблица — `media_jobs` (миграция `0018_media_jobs`, down_revision `0017_subscription_will_renew`; миграция `0019_media_edit_chain` добавляет цепочку правок, single head). Существующие таблицы не изменяются: списание и возврат кредитов идут через существующий `WalletService` и ложатся в `ledger_transactions`.

Колонка `moderation` добавляется **отдельной expand-only миграцией** ([ADR-086](../../adr/ADR-086-ugc-moderation.md)); номер ревизии определяется на момент реализации (следующий свободный, single head сохраняется). Backfill не выполняется: у старых строк `moderation IS NULL`, и это честно означает «не проверялось».

Колонки `provider`, `vendor_price`, `pending_result` добавляются **одной** expand-only миграцией [ADR-108 §9](../../adr/ADR-108-media-generation-via-proxy.md) (номер ревизии — следующий после головы на момент реализации, single head). **DML по существующим строкам нет:** константный дефолт `''` у `provider` верен для всех прежних строк (они — прямой fal); констрейнты и индексы существующих колонок не меняются. Токен вебхука не хранится — он вычисляется из `jobId`. У proxy-задачи `status_url` и `response_url` — пустые строки (колонки остаются `NOT NULL`), `fal_endpoint` — endpoint варианта каталога, `fal_request_id` — id задачи прокси, а если прокси id не вернул — пустая строка ([ADR-108 §3.3](../../adr/ADR-108-media-generation-via-proxy.md)).

## `media_jobs`

| Колонка | Тип | Описание |
|---|---|---|
| `id` | `uuid` PK, default `gen_random_uuid()` | **на практике задаётся приложением**: `jobId` нужен как ключ идемпотентности списания раньше, чем появится строка. Дефолт оставлен для ручных вставок |
| `user_id` | `uuid` NOT NULL → `users(id) ON DELETE CASCADE` | владелец |
| `model_id` | `text` NOT NULL | публичный id из реестра (`veo-3.1`), **не** endpoint fal |
| `kind` | `text` NOT NULL | `image` \| `video` |
| `fal_endpoint` | `text` NOT NULL | endpoint провайдера, которым выполнен запуск (диагностика + контекст логов) |
| `fal_request_id` | `text` NOT NULL | id запроса в очереди провайдера |
| `status_url` | `text` NOT NULL | URL опроса статуса, **как его вернул провайдер** |
| `response_url` | `text` NOT NULL | URL результата, как его вернул провайдер |
| `status` | `text` NOT NULL, CHECK | `queued` \| `running` \| `completed` \| `failed` |
| `prompt` | `text` NOT NULL | промт запуска (нужен для листинга и повторного показа в UI) |
| `credits_charged` | `integer` NOT NULL default `0` | сколько списано при постановке |
| `credits_refunded` | `boolean` NOT NULL default `false` | вернулись ли кредиты (только у `failed`) |
| `parent_job_id` | `uuid` NULL FK → `media_jobs(id)` ON DELETE SET NULL | из результата какой задачи сделана эта ([ADR-063 §2](../../adr/ADR-063-media-feed-edit-chains-and-job-deletion.md)). `SET NULL`, а не `CASCADE`: удаление исходника убирает его из ленты, но не стирает выросшие из него правки |
| `input_image_urls` | `jsonb` NULL | ссылки, реально ушедшие на вход. Хранится, а не выводится из родителя: родителя могут удалить, а «из чего сделано» лента показывать обязана |
| `result` | `jsonb` NULL | **нормализованный** результат `{assets: [{url, contentType, fileName}], description?, seed?}` — не сырое тело провайдера. При блокировке пост-модерацией пишется `{"assets": []}` (ассеты отбрасываются, [ADR-086 §5](../../adr/ADR-086-ugc-moderation.md)) |
| `moderation` | `jsonb` NULL | вердикт модерации ([ADR-086 §10](../../adr/ADR-086-ugc-moderation.md)): `{status, stage, categories, checkedAt, provider, model}`. `NULL` = **не проверялось** (строка создана до ADR-086 либо `MODERATION_ENABLED=false`) и отдаётся клиенту как `status: "unchecked"` — никогда как `passed` |
| `error` | `text` NULL | причина провала, ≤ 500 символов |
| `provider` | `text` NOT NULL default `''` | **[ADR-108 §9](../../adr/ADR-108-media-generation-via-proxy.md).** Сервис прокси, принявший запуск: `fal` \| `kie` \| `sosana`; `''` — задача прямого fal (legacy), в том числе все строки, существовавшие до миграции. Классификатор транспорта: `<> ''` — колбэк без опроса, `''` — опрос `status_url`. `CHECK` на значения нет — набор сервисов прокси внешний |
| `vendor_price` | `numeric(18,6)` NULL | **[ADR-108 §8](../../adr/ADR-108-media-generation-via-proxy.md).** Фактическая цена запуска у вендора из колбэка; `NULL` — не сообщена. Клиенту не отдаётся. Если непуст и единицы сервиса подтверждены как USD (`fal`, `sosana`; `kie` — нет, [Q-108-12](../../99-open-questions.md)) — тот же приём колбэка заменяет им `provider_cost_usd` (реальная цена вместо оценки [ADR-079](../../adr/ADR-079-crm-provider-cost-duration-payments.md); CRM читает `provider_cost_usd`) |
| `pending_result` | `jsonb` NULL | **[ADR-108 §5](../../adr/ADR-108-media-generation-via-proxy.md).** Нормализованный результат колбэка `completed`, ещё не применённый общим путём завершения (пост-модерация / handler отказали транзиентно); после терминала — **SQL `NULL`**, не JSON `null` ([TD-061](../../100-known-tech-debt.md)). Download-роут его **не** читает — ассет достижим только из `result` |
| `created_at` | `timestamptz` NOT NULL default `now()` | постановка в очередь |
| `updated_at` | `timestamptz` NOT NULL default `now()` | последний переход состояния |

**Индекс** `ix_media_jobs_user_created (user_id, created_at)` — под owner-scoped листинг newest-first.

> **Ведущего индекса по `created_at` у таблицы НЕТ.** `GET /v1/admin/costs/daily` ([ADR-092](../../adr/ADR-092-crm-daily-costs-endpoint.md)) отбирает media-строки единственным предикатом `created_at >= :from AND created_at < :to`, и ни `ix_media_jobs_user_created` (начинается с `user_id`), ни частичный `ix_media_jobs_non_terminal` (по статусу) этот отбор не обслуживают ⇒ **seq scan**. Chat-половина того же запроса индекс получила (`ix_steps_created_at`, миграция `0029`), media-половина оставлена незакрытой осознанно: эффект на порядки меньше — строка за генерацию, а не за каждый вызов LLM. Долг — [TD-033](../../100-known-tech-debt.md).

## Почему так

**`status` — `TEXT` + `CHECK`, а не PostgreSQL enum.** Набор значений повторяет lifecycle очереди провайдера, то есть внешний контракт; его расширение не должно требовать `CREATE TYPE`/`ALTER TYPE` на каждом инстансе. Остальные enum'ы схемы ([03-data-model.md](../../03-data-model.md)) описывают наши собственные домены и остаются enum'ами.

**URL'ы опроса персистятся, а не вычисляются.** Для вложенных endpoint'ов (`fal-ai/kling-video/v3/pro/text-to-video`) путь в очереди не выводится из одного идентификатора модели. Провайдер возвращает готовые URL — их и храним; префикс проверяется при каждом использовании (SSRF-guard, см. [03-architecture.md](03-architecture.md)).

**`result` хранит нормализованную форму.** Сырое тело провайдера в БД не попадает: смена провайдера не должна требовать миграции данных, а вендорные имена полей не должны просачиваться в чтения.

**Ассеты не хранятся.** В `result` только ссылки CDN провайдера; байты через нас не проходят (в отличие от файлов сайта, которые лежат в `site_files`). Срок жизни ссылок — на стороне провайдера, [Q-060-1](../../99-open-questions.md).

## Связь с ledger

Кредиты живут в существующих `wallets`/`ledger_transactions` ([ADR-005](../../adr/ADR-005-idempotency-ledger.md)); своей учётной сущности у модуля нет. Ключи идемпотентности изолированы в своём namespace:

| Операция | Ключ | Тип записи | `meta.source` |
|---|---|---|---|
| списание при постановке | `media-gen:{jobId}` | `debit` | `media_generation` |
| возврат при провале **или при блокировке результата модерацией** | `media-refund:{jobId}` | `credit` | `media_generation_refund` |

Один `jobId` ⇒ не более одного списания и не более одного возврата, сколько бы раз клиент ни повторил запрос или опрос.

**Отдельного namespace под возврат по модерации нет намеренно** ([ADR-086 §5](../../adr/ADR-086-ugc-moderation.md)): у одной задачи возможна ровно одна причина возврата (она терминальна), и общий ключ гарантирует, что «провал у провайдера» и «блокировка результата» не сложатся в два начисления.
