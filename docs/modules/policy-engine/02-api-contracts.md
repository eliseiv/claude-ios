# Policy Engine — API Contracts

## GET /v1/policy/effective
Эффективные права для UI.

### Request
- Query: нет (userId берётся из JWT `sub`).

### Response (200)
```json
{
  "isSubscribed": true,
  "trialRemaining": 0,
  "creditsBalance": 0,
  "byokEnabled": false,
  "canGenerateCreditsMode": true,
  "canGenerateByokMode": false,
  "reasons": ["enum list"]
}
```
| Поле | Тип | Семантика |
|---|---|---|
| `isSubscribed` | bool | `subscription.status == active` |
| `trialRemaining` | int | 1 если нет подписки и `trial_used=false`, иначе 0 |
| `creditsBalance` | int | текущий баланс |
| `byokEnabled` | bool | `byok.enabled && key_status==valid` |
| `canGenerateCreditsMode` | bool | результат `evaluate(state, mode=credits).allow` |
| `canGenerateByokMode` | bool | результат `evaluate(state, mode=byok).allow` |
| `reasons` | array | blockReason для тех режимов, где `canGenerate*=false` |

### `reasons[]` значения
Подмножество blockReason enum, **которое умеет вычислять `evaluate` (ADR-002)**: `trial_used | subscription_required | subscription_expired | credits_empty | byok_disabled | byok_invalid | policy_denied`.

**`rate_limited` НЕ входит в `reasons[]`** (BLK-7b). Это gateway-concern: rate-limit выражается исключительно как HTTP `429` на уровне API Gateway. Policy Engine не знает о rate-limit состоянии (оно не часть `PolicyState`), поэтому `reasons[]` строится строго из `evaluate()` и не содержит `rate_limited`. Значение `rate_limited` остаётся в общем blockReason enum (8 значений) для HTTP-слоя и `/chat/run` — см. [ADR-004](../../adr/ADR-004-blocked-http-200.md).

### Правила
- Консистентность с `/chat/run`: `canGenerateCreditsMode`/`canGenerateByokMode` и `reasons[]` вычисляются той же `evaluate` (AC-6). `reasons[]` отражает только бизнес-policy причины (subscription/trial/credits/byok), не транспортный rate-limit.
- `reasons[]` не содержит `rate_limited`: код `loader.py` строит `reasons[]` из `evaluate()` (credits/byok) — это поведение корректно и не меняется (BLK-7b).

## Внутренний контракт evaluate (не HTTP)
```
evaluate(state: PolicyState, mode: Mode) -> Decision
PolicyState = { subscription_status, trial_used, credits_balance, byok_enabled, byok_status }
Decision    = Allow | Blocked(blockReason)
```
Алгоритм — [ADR-002](../../adr/ADR-002-access-policy-state-machine.md).
