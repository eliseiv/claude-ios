# Chat Orchestrator — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| CO-1 | Tool-схемы (Pydantic, 8 tools, args/result, `extra=forbid`). | — |
| CO-2 | Anthropic client wrapper (messages API, prompt caching, tools definition, usage parsing). | CO-1, GW config |
| CO-3 | Session/steps repository, реконструкция контекста из chat_steps. | DB schema |
| CO-4 | `/chat/run`: Policy call → generate → status mapping → chat_steps + audit. | CO-2, CO-3, Policy Engine |
| CO-4b | Генерация и персист `messageStepId` (chat_steps/tool_calls); восстановление при re-entry из tool-result. [ADR-005](../../adr/ADR-005-idempotency-ledger.md). | CO-3, CO-4 |
| CO-5 | tool_calls lifecycle + `/chat/tool-result` + идемпотентность (ADR-005). | CO-4, CO-4b |
| CO-6 | mode=byok routing (получение ключа от BYOK Service). | CO-4, BYOK module |
| CO-7 | mode=credits debit (Wallet `consume`, `amount=1` на финальный assistant_message; tool-раунды не списывают; идемпотентность по `messageStepId`, единому на message-шаг и переиспользуемому при re-entry; передаётся в поле `requestId` consume). [ADR-005](../../adr/ADR-005-idempotency-ledger.md), [ADR-006](../../adr/ADR-006-credit-billing-and-subscription-grant.md). | CO-4b, CO-5, Wallet module |

> Q-004-1 закрыт (ADR-006): CO-7 разблокирован. Правило — 1 кредит = 1 сообщение, 1 списание на пользовательский message-шаг.
