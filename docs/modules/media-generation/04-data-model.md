# 04 — Модель данных

Одна таблица — `media_jobs` (миграция `0018_media_jobs`, down_revision `0017_subscription_will_renew`; миграция `0019_media_edit_chain` добавляет цепочку правок, single head). Существующие таблицы не изменяются: списание и возврат кредитов идут через существующий `WalletService` и ложатся в `ledger_transactions`.

Колонка `moderation` добавляется **отдельной expand-only миграцией** ([ADR-086](../../adr/ADR-086-ugc-moderation.md)); номер ревизии определяется на момент реализации (следующий свободный, single head сохраняется). Backfill не выполняется: у старых строк `moderation IS NULL`, и это честно означает «не проверялось».

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
| возврат при провале **или при блокировке результата модерацией** | `media-refund:{jobId}` | `credit` | `media_generation_refund` |

Один `jobId` ⇒ не более одного списания и не более одного возврата, сколько бы раз клиент ни повторил запрос или опрос.

**Отдельного namespace под возврат по модерации нет намеренно** ([ADR-086 §5](../../adr/ADR-086-ugc-moderation.md)): у одной задачи возможна ровно одна причина возврата (она терминальна), и общий ключ гарантирует, что «провал у провайдера» и «блокировка результата» не сложатся в два начисления.
