# 06 — Testing Strategy

## Пирамида
| Уровень | Доля | Что покрывает | Инструменты |
|---|---|---|---|
| Unit | ~60% | Чистая логика: Policy Engine (state machine), биллинг-правило (1 кредит = 1 сообщение / 1 списание на message-шаг, ADR-006), валидация tool-схем, encryption helpers. | pytest, без I/O |
| Integration | ~30% | Endpoint + реальные PostgreSQL/Redis (testcontainers), миграции, идемпотентность, атомарность ledger. Внешние HTTP (Anthropic/Apple) — мок через respx. | pytest-asyncio, testcontainers, respx |
| E2E | ~10% | Полные сценарии: trial-once, blocked при истёкшей подписке, tool-loop в несколько шагов, BYOK routing. | pytest против поднятого app + контейнеры |

## Coverage gate
- Глобальный минимум: **80%** (`--cov-fail-under=80`, см. [02-tech-stack.md](02-tech-stack.md)).
- Критические пакеты (`policy`, `wallet`, `byok`) — целевое покрытие **≥ 95%**, проверяется per-package в CI.

## Обязательные тест-кейсы (привязка к AC из 00-vision)
| Тест | AC | Уровень |
|---|---|---|
| Trial доступен ровно 1 раз, второй → `trial_used` | AC-1 | integration |
| `/chat/run` blocked при `subscription=expired`, mode=credits и mode=byok | AC-2 | integration |
| Конкурентные `consume` с одним idempotency key (для chat-debit — один `messageStepId`) → одно списание | AC-3 | integration |
| Re-entry message-шага (`/chat/run` + N×`/chat/tool-result`) → ровно один debit по `messageStepId` | AC-3, AC-4 | e2e |
| `consume` при balance < amount → отказ, баланс не отрицателен | AC-3 | unit+integration |
| Tool-loop: run → tool_call → tool-result → tool_call → ... → assistant_message | AC-4 | e2e |
| Повторный tool-result с тем же `toolCallId` → идемпотентно | AC-4 | integration |
| BYOK ключ зашифрован в БД; логи не содержат plaintext | AC-5 | integration |
| `/policy/effective` совпадает с фактическим решением `/chat/run` для всех состояний | AC-6 | integration |
| Audit-запись на каждое мутирующее tool-действие и каждое списание | AC-7 | integration |

## Политика моков
- **PostgreSQL и Redis — реальные** (testcontainers). Не мокать БД.
- **Anthropic API, App Store Server API, KMS** — мокаются (respx / fakes). Реальные вызовы только в отдельном `@pytest.mark.external` наборе (вне CI по умолчанию).

## State-machine тестирование Policy Engine
Полная таблица переходов из [ADR-002](adr/ADR-002-access-policy-state-machine.md) покрывается параметризованными unit-тестами: декартово произведение {subscription: none/active/expired} × {trial_used: T/F} × {credits: 0/>0} × {byok: disabled/invalid/valid} × {mode: credits/byok} → ожидаемый `allow|blockReason`.

## Структура
```
tests/
  unit/         # policy, conversion, schemas, crypto
  integration/  # endpoints + db + redis, respx для внешних
  e2e/          # сквозные сценарии
  conftest.py   # фикстуры: app, db container, redis container, jwt factory
```

## CI gate (см. 07-deployment.md)
PR не проходит, если: `ruff format --check` fail, `ruff check` fail, `mypy` fail, `pytest` fail, coverage < 80%.
