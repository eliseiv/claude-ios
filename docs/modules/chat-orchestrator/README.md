# Module: Chat Orchestrator

- Статус: Реализован
- Ответственность: вызовы Claude (messages API + prompt caching), управление tool-loop state, формирование `status` ответа, реконструкция контекста сессии.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md) — endpoints + строго типизированные tool-схемы
- [03-architecture.md](03-architecture.md)
- [04-data-model.md](04-data-model.md)
- [05-events.md](05-events.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

## DoD
Policy Engine вызывается перед каждой генерацией; tool-loop стабилен на нескольких шагах; tool payload строго типизирован; status=blocked машиночитаем; usage учитывается; mode=byok использует ключ пользователя; mode=credits инициирует списание.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: ADR-006 — credits-debit правило (1 кредит = 1 сообщение, 1 списание на message-шаг; tool-раунды не списывают). Унифицированы usage-ключи `cacheReadTokens/cacheWriteTokens`. Закрыт Q-004-1.
- 2026-05-21: введён `messageStepId` — billing idempotency key, генерируется в `/chat/run`, персистится в `chat_steps.message_step_id`/`tool_calls.message_step_id`, переиспользуется при re-entry из `/chat/tool-result`. Разведён с gateway `requestId` (ADR-005).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/chat/` (orchestrator, anthropic_client, repository, tools). Trial-flip атомарен и идемпотентен (`mark_trial_used`); окно двойной бесплатной генерации при гонке — осознанный риск, ADR-002 §Trial concurrency / TD-006.
- 2026-05-25: BUG-3 (CRITICAL) — dotted tool-имена (`files.read`, …) отклонялись Anthropic API (`400`, шаблон `^[a-zA-Z0-9_-]{1,128}$`), весь Claude-путь нерабочий. Решение: двунаправленный статический маппинг `domain (точка) ↔ anthropic (подчёркивание)`, применяется только в Anthropic-клиенте (at request build + at tool_use parse). Публичный iOS-контракт §5 (доменные имена с точкой) НЕ меняется. См. [02-api-contracts.md §Имена tools](02-api-contracts.md#имена-tools-доменный-ios-vs-anthropic-формат), [03-architecture.md §Маппинг имён tools](03-architecture.md#маппинг-имён-tools-bug-3). Scope backend.
- 2026-06-01: введён класс server-side tools (`site.*`, website-builder) — исполняет backend в tool-loop без round-trip к iOS ([ADR-011](../../adr/ADR-011-server-side-tools.md)). Orchestrator различает client-side/server-side по реестру `SERVER_SIDE_TOOLS`; guard `MAX_SERVER_TOOL_ROUNDS`. domain↔anthropic mapping расширен `site.*`. Схемы — [website-builder/02-api-contracts.md](../website-builder/02-api-contracts.md). Scope backend.
- 2026-05-25: BUG-4 (CRITICAL) — continuation сломан с реальным Claude. Доменный `toolCallId` выводился из anthropic `tool_use.id` (`uuid4`, если id не-UUID), а реплеемая история содержала raw `toolu_...` → `tool_result.tool_use_id` ≠ `tool_use.id` истории → Anthropic `400` → `502`. Решение ([ADR-008](../../adr/ADR-008-provider-tool-use-id.md)): новая колонка `tool_calls.provider_tool_use_id (TEXT NOT NULL)` хранит raw `toolu_...`; доменный `id` = независимый `uuid4`; continuation строит `tool_result.tool_use_id` из `provider_tool_use_id`, `payload` реплеится дословно. Миграция Alembic expand/contract. См. [03-architecture.md §Согласованность tool_use.id](03-architecture.md#согласованность-tool_useid-в-истории-anthropic-bug-4), [04-data-model.md](04-data-model.md), [09-testing.md](09-testing.md) (fake обязан отдавать `toolu_...`). Scope backend + qa fake.
