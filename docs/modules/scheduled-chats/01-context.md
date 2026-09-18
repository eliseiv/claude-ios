# Scheduled Chats — Context

## Зависимости
- **API Gateway** — JWT, lazy provisioning, rate-limit (`enforce_other_limits`), роуты `/v1/scheduled-chats`.
- **Chat Orchestrator** — `ChatOrchestrator.run` (v2): `mode`, `assistantMode`, `model`, `generationMode`, policy/wallet.
- **Chats / chat_sessions** — опциональный `sessionId` (owned); результат пишет обычные шаги сессии.
- **Notifications** — APNs; новый триггер `scheduled_chat_ready` ([ADR-107](../../adr/ADR-107-scheduled-chat-tasks.md)); toggle `notifications_enabled` ([ADR-032](../../adr/ADR-032-notifications-enabled-default-false.md)).
- **Preferences** — чтение `notifications_enabled` перед push.
- **Wallet / Policy** — на момент исполнения (не при POST).

## Образцы
- Media reconciler lifespan-loop + batch ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)).
- Внутренний вызов оркестратора без JWT — голосовой WS ([ADR-104](../../adr/ADR-104-voice-mode-websocket.md)).

## Границы
- Модуль **не** дублирует chat REST: клиент после push открывает `GET /v1/chats/{sessionId}`.
- Модуль **не** закрывает TD-010/TD-013 (только появляется poller-инфраструктура того же класса).
