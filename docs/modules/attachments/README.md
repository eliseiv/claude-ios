# Module: Attachments (мультимодальный ввод)

- Статус: Спроектирован (backend — Спринт 3)
- Ответственность: загрузка и хранение вложений (images/documents) для мультимодального ввода в `/chat/run` (Claude vision/document) и файлов-контекста workspace. Двухшаговая модель ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)): upload → ссылка.

## Документы
- [00-overview.md](00-overview.md)
- [01-context.md](01-context.md)
- [02-api-contracts.md](02-api-contracts.md)
- [03-architecture.md](03-architecture.md)
- [05-security.md](05-security.md)
- [06-rbac.md](06-rbac.md)
- [07-implementation-phases.md](07-implementation-phases.md)
- [09-testing.md](09-testing.md)

> Data model — `attachments` (таблица 16, миграция `0004`). Общее хранилище байтов для вложений сообщений и `workspace_files` ([ADR-013](../../adr/ADR-013-workspace-projects-vs-website-builder.md)).

## DoD
- `POST /v1/attachments` (multipart) — загрузка, валидация media_type/размера, извлечение текста для PDF/text, возврат `attachmentId`.
- `GET /v1/attachments/{id}` (метаданные), `DELETE /v1/attachments/{id}`.
- `/chat/run` `attachments[]` резолвится в Anthropic content-блоки (image → vision; document → document/extracted_text) ([ADR-014](../../adr/ADR-014-multimodal-attachments.md)).
- Лимиты/allowlist, изоляция владельца. Биллинг — обычный chat-шаг (ADR-006 без изменений).

## Changelog
- 2026-06-02: bootstrap модуля (architect, Figma-gap). [ADR-014](../../adr/ADR-014-multimodal-attachments.md). Таблица `attachments`. orphan-retention → [TD-010](../../100-known-tech-debt.md); хранение в БД → [TD-009](../../100-known-tech-debt.md). См. [figma-gap-analysis.md](../../figma-gap-analysis.md).
