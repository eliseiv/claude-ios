# Chat Orchestrator — RBAC

## Роль
- `user` — работает только со своими сессиями.

## Правила
- `userId` запроса == `sub` JWT (enforced на Gateway).
- Сессия должна принадлежать `userId`: `chat_sessions.user_id == userId`, иначе `404` (не раскрываем существование чужой сессии).
- `toolCallId` должен принадлежать сессии пользователя (`tool_calls.session_id` → `chat_sessions.user_id == userId`).
- `stepId` в `POST /v1/chat/speech` ([ADR-100](../../adr/ADR-100-assistant-speech-output.md)) ищется **внутри** сессии, уже проверенной на принадлежность `sub`: чужая сессия → `404 session_not_found`, шаг не из этой сессии → `404 step_not_found`. Знание чужого UUID шага доступа не даёт.
- WebSocket `/v1/chat/voice` ([ADR-104](../../adr/ADR-104-voice-mode-websocket.md)) авторизуется **тем же пользовательским JWT в заголовке `Authorization` рукопожатия**; токен в query-строке **не принимается** (query попадает в access-логи прокси). Одно соединение обслуживает **одну** сессию, резолвнутую `WHERE user_id = :sub` тем же путём, что у HTTP-ходов, — чужой `sessionId` недостижим при знании UUID. Отказы **до** апгрейда отдаются обычным HTTP в едином конверте ошибки (`401` / `422 voice_mode_disabled` / `503 voice_mode_not_configured` / `429`), после `accept` — кадром `error` и прикладным close-кодом. Ни публичного роута без JWT, ни signed URL здесь не заводится — **контраст с [ADR-085](../../adr/ADR-085-media-asset-download-proxy.md) помечен с обеих сторон**: там подпись нужна потому, что `AVPlayer` не умеет слать заголовок, а WebSocket-клиент умеет.
- BYOK plaintext ключ не возвращается клиенту никогда.
