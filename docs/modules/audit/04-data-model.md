# Audit — Data Model

Владеет: `audit_logs`. Полный DDL — [03-data-model.md](../../03-data-model.md).

## audit_logs
| Поле | Тип | Назначение |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID FK **nullable** | владелец события; **`NULL` = у события нет субъекта-пользователя** — операторские правки каталога и настроек инстанса ([ADR-099 §9](../../adr/ADR-099-crm-admin-economics-and-instance-settings.md), миграция `0033_admin_economics`). Обратно `NOT NULL` не ужесточается: таблица append-only |
| `session_id` | UUID FK nullable | связь с сессией (если есть) |
| `event_type` | TEXT | каталог из 02-api-contracts |
| `payload` | JSONB | детали, без секретов |
| `created_at` | timestamptz | |

## Индексы
- `ix_audit_user_created (user_id, created_at DESC)`.
- `ix_audit_event_type (event_type, created_at DESC)`.

## Инварианты
- Append-only (app-level).
- `payload` без API-ключей/секретов/raw StoreKit.
- Только Audit-модуль пишет сюда.
