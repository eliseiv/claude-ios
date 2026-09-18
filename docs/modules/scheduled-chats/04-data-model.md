# Scheduled Chats — Data Model

Таблица `scheduled_chat_tasks` ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)). Миграция в дереве: `migrations/versions/20260918_0036_scheduled_chat_tasks.py` (revision `0036_scheduled_chat_tasks`, `down_revision=0035_media_features`; single head). Существующие таблицы не ломаются: ход пишет в `chat_sessions` / `chat_steps` как обычный `/v1/chat/v2/run`.

## `scheduled_chat_tasks`

```sql
CREATE TABLE scheduled_chat_tasks (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- planned session UUID: НЕТ FK / НЕТ ON DELETE SET NULL
    -- (SET NULL = молчаливая подмена resume→new — запрещена, ADR-107 §1)
    session_id              UUID NULL,
    prompt                  TEXT NOT NULL,
    mode                    TEXT NOT NULL,              -- credits | byok
    assistant_mode          TEXT NULL,                  -- chat | code
    model                   TEXT NULL,
    generation_mode         TEXT NULL,                  -- general | research | reasoning | study_learn
    run_at                  TIMESTAMPTZ NOT NULL,
    status                  TEXT NOT NULL,              -- scheduled | running | completed | failed | cancelled
    claimed_at              TIMESTAMPTZ NULL,
    started_at              TIMESTAMPTZ NULL,
    finished_at             TIMESTAMPTZ NULL,
    -- фактическая сессия после run; без FK — UUID сохраняется для GET/push даже если чат позже удалён
    result_session_id       UUID NULL,
    result_message_step_id  UUID NULL,
    error_code              TEXT NULL,
    error_message           TEXT NULL,                  -- ≤ 500 chars
    push_sent_at            TIMESTAMPTZ NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_scheduled_chat_status CHECK (
        status IN ('scheduled', 'running', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT ck_scheduled_chat_mode CHECK (mode IN ('credits', 'byok')),
    CONSTRAINT ck_scheduled_chat_assistant_mode CHECK (
        assistant_mode IS NULL OR assistant_mode IN ('chat', 'code')
    )
);

CREATE INDEX ix_scheduled_chat_status_run_at
    ON scheduled_chat_tasks (status, run_at);

CREATE INDEX ix_scheduled_chat_user_created
    ON scheduled_chat_tasks (user_id, created_at DESC);

-- recovery stuck running: partial index опционален
-- CREATE INDEX ix_scheduled_chat_running_started
--     ON scheduled_chat_tasks (started_at) WHERE status = 'running';
```

| Колонка | Смысл |
|---|---|
| `session_id` | планируемый UUID сессии; `NULL` = создать новую при запуске. **Без FK:** UUID не затирается при DELETE чата; на run отсутствие строки → `failed`/`session_not_found` |
| `result_session_id` | фактическая сессия после run (может совпасть с `session_id` или быть новой); без FK |
| `result_message_step_id` | доменный `message_step_id` хода (не FK на `chat_steps.id`) |
| `push_sent_at` | идемпотентность APNs |
| `claimed_at` / `started_at` | момент claim `scheduled→running` (в MVP совпадают); отсчёт stuck-TTL — `coalesce(started_at, claimed_at)` |

**`status` — TEXT + CHECK**, не PG enum (расширение без `ALTER TYPE`, как `media_jobs`).

## Индексы

- `(status, run_at)` — due-poll воркера.
- `(user_id, created_at DESC)` — список владельца + подсчёт активных.

Частичный индекс только на `scheduled` / `running` допустим как оптимизация реализации, но не обязателен нормой.

## Согласование с репо

- `attachments.session_id` / `audit_logs.session_id` используют `ON DELETE SET NULL` — там orphan/журнал переживают удаление чата **без** смены семантики операции.
- Здесь SET NULL **неприемлем**: NULL читался бы как «новая сессия». Норма — хранить UUID + явный fail ([ADR-107 §1](../../adr/ADR-107-scheduled-chat-tasks.md)).
