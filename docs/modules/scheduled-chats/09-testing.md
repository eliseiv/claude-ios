# Scheduled Chats — Testing

> Статус покрытия (2026-09-18): автотесты **в дереве** — `tests/unit/test_scheduled_chats_adr107.py`, `tests/integration/test_scheduled_chats_adr107.py` (qa 21 passed — по сообщению orchestrator'а); OpenAPI tag `ScheduledChats` — `tests/integration/test_api_documentation.py`. Выкат на инстансы не утверждается.

## Unit
- Валидация `runAt`: not_in_future / too_soon / too_far / **naive → `run_at_timezone_required`**.
- Валидация `assistantMode` вне `chat`\|`code` → `unsupported_assistant_mode`.
- Create/PATCH с `sessionId`: `mode` из тела игнорируется; в строке — `mode` сессии.
- Лимит активных → `active_limit_exceeded`.
- PATCH/DELETE только из `scheduled`; `running` → 409.
- **Claim:** две параллельные выборки не отдают одну строку (мок session / SQL); повторный claim `running` не возвращает строку.
- Stuck TTL: `running` с `started_at` старше TTL → `failed` + `worker_interrupted` + push once; **не** возвращается в `scheduled`.
- Push skip: `notifications_enabled=false` / нет токена / нет APNs.
- `push_sent_at` claim — второй вызов не шлёт.
- Payload: `type=scheduled_chat_ready`; `sessionId` = `resultSessionId ?? planned ?? null`.

## Integration
- CRUD happy-path + изоляция чужого id (`404`).
- `sessionId` чужой → `404 session_not_found` на create.
- Worker: due row → `completed` + `resultSessionId` / `resultMessageStepId` (оркестратор с fake LLM, `status=assistant_message`).
- Worker: `ChatResponse.status=blocked` + policy → `failed` + `errorCode=blockReason` + push once.
- Worker: `blocked` + `max_tokens` → `failed` + `errorCode=max_tokens`.
- Worker: `status=tool_call` → `failed` + `tool_loop_unsupported`.
- Worker: планируемый `session_id` есть в задаче, строки сессии нет → `failed`/`session_not_found` **без** создания новой сессии.
- Worker: crash mid-run симулировать стартом с протухшим `started_at` → recovery `worker_interrupted`.
- `POLL_SECONDS=0` → воркер не стартует (как media reconciler в conftest).

## Out of scope
- Реальная доставка APNs (fake client).
- Recurring / Celery.
- E2E на живом устройстве — ручная приёмка после выката.
