# ADR-082 — Hosted web search on legacy `/v1/chat/run` (`CHAT_LEGACY_WEB_SEARCH_ENABLED`)

- **Статус:** Accepted
- **Дата:** 2026-08-19
- **Связано:** [ADR-064](ADR-064-study-learn-quiz-generation-mode.md), [ADR-072](ADR-072-chat-media-tools-instance-gate.md), [ADR-081](ADR-081-disabled-tool-families.md)

## Контекст

iOS на `orvianix.shop` и `ravionet.shop` ходит в `POST /v1/chat/run`, не в `/v1/chat/v2/run`. Hosted web search провайдера подмешивается только при эффективном режиме `research`, а legacy-путь всегда форсил `general` — модель не ищет в интернете.

Перевод этих клиентов на v2 — отдельный релиз iOS. Глобально включить поиск на всех legacy-инстансах нельзя: остальные приложения останутся на 1 кредите и не должны внезапно ходить в веб.

## Решение

Env **`CHAT_LEGACY_WEB_SEARCH_ENABLED`** (bool, default **`false`**, per-instance).

Когда `true`, legacy `/v1/chat/run` и `/v1/chat/tool-result`:

1. Эффективный режим хода = `research` (тот же hosted `web_search`, что у v2 research).
2. OpenAI-инстанс переключается с Chat Completions на Responses API (`OpenAIResponsesClient`). Completions **игнорирует** `generation_mode` — без этого шага режим `research` тарифицируется, а `web_search` модели не отдаётся (прод orvianix 2026-08-19: «Я не могу искать в интернете напрямую»).
3. Цена хода = `CHAT_CREDIT_COST_RESEARCH` (дефолт 3).
4. Контракт запроса не меняется: поле `generationMode` на legacy по-прежнему 422.
5. В ответе `usage.generationMode` / `usage.creditsCharged` **не** появляются (старые клиенты их не ждут).

`/v1/chat/v2/*` флаг не трогает: клиент как раньше шлёт `generationMode`.

Дефолт `false` → все остальные инстансы байт-в-байт как сейчас (1 кредит, без web search).

Имя инстанса в коде не хардкодится.

Это **поиск**, не fetch конкретной URL ([Q-016-2](../99-open-questions.md) остаётся открыт).

## Операции

Только orvianix и ravionet:

```bash
CHAT_LEGACY_WEB_SEARCH_ENABLED=true
```

На остальных инстансах переменную не задавать.

## Не меняется

- Схема `ChatRunRequest` (без `generationMode`).
- v2 capabilities / v2 run.
- Ось C: `quiz.generate` по-прежнему только `study_learn`.
