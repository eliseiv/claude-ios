# Snippets — Implementation Phases

Спринт 2. Зависит от миграции `0004` (таблица `snippets`).

1. **Phase 1 — миграция:** таблица `snippets` + индексы (`ix_snippets_user_created`, `ix_snippets_user_language`).
2. **Phase 2 — CRUD/list:** `GET` (фильтр/поиск/пагинация), `POST`, `GET /{id}`, `PATCH`, `DELETE`.
