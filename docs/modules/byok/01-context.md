# BYOK — Context

## Зависимости
| Зависит от | Зачем |
|---|---|
| KMS (или эквивалент) | encrypt/decrypt DEK (envelope) |
| `cryptography` (AES-GCM) | шифрование ключа |
| Anthropic API | валидация ключа при set |
| PostgreSQL | byok_keys |
| Audit | byok_change события |

## Кто зависит
- Chat Orchestrator (`get_plaintext_key` при mode=byok).
- Policy Engine (read enabled, key_status).
- API Gateway (`/v1/byok/*`).

## Открытые вопросы
- [Q-002-1](../../99-open-questions.md) — конкретный KMS-провайдер (интерфейс `KmsClient` стабилен).
