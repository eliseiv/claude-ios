# 04 — Модель данных

Одна новая таблица — `media_jobs` (миграция `0018_media_jobs`, down_revision `0017_subscription_will_renew`, single head). Существующие таблицы не изменяются: списание и возврат кредитов идут через существующий `WalletService` и ложатся в `ledger_transactions`.

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
| `result` | `jsonb` NULL | **нормализованный** результат `{assets: [{url, contentType, fileName}], description?, seed?}` — не сырое тело провайдера |
| `error` | `text` NULL | причина провала, ≤ 500 символов |
| `created_at` | `timestamptz` NOT NULL default `now()` | постановка в очередь |
| `updated_at` | `timestamptz` NOT NULL default `now()` | последний переход состояния |

**Индекс** `ix_media_jobs_user_created (user_id, created_at)` — под owner-scoped листинг newest-first.

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
| возврат при провале | `media-refund:{jobId}` | `credit` | `media_generation_refund` |

Один `jobId` ⇒ не более одного списания и не более одного возврата, сколько бы раз клиент ни повторил запрос или опрос.
