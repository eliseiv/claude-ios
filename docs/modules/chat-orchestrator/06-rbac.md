# Chat Orchestrator — RBAC

## Роль
- `user` — работает только со своими сессиями.

## Правила
- `userId` запроса == `sub` JWT (enforced на Gateway).
- Сессия должна принадлежать `userId`: `chat_sessions.user_id == userId`, иначе `404` (не раскрываем существование чужой сессии).
- `toolCallId` должен принадлежать сессии пользователя (`tool_calls.session_id` → `chat_sessions.user_id == userId`).
- `stepId` в `POST /v1/chat/speech` ([ADR-100](../../adr/ADR-100-assistant-speech-output.md)) ищется **внутри** сессии, уже проверенной на принадлежность `sub`: чужая сессия → `404 session_not_found`, шаг не из этой сессии → `404 step_not_found`. Знание чужого UUID шага доступа не даёт.
- BYOK plaintext ключ не возвращается клиенту никогда.
