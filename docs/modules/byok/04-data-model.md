# BYOK — Data Model

Владеет: `byok_keys`. Полный DDL — [03-data-model.md](../../03-data-model.md).

## byok_keys
| Поле | Тип | Назначение |
|---|---|---|
| `user_id` | UUID PK | владелец |
| `encrypted_key` | BYTEA | AES-256-GCM ciphertext (+tag) пользовательского ключа |
| `encrypted_dek` | BYTEA | DEK, зашифрованный KMS |
| `nonce` | BYTEA | AES-GCM nonce |
| `key_status` | enum | valid / invalid / missing |
| `enabled` | bool | использовать ли byok |
| `updated_at` | timestamptz | |

## Инварианты
- Plaintext ключ и plaintext DEK НИКОГДА не сохраняются.
- Только этот модуль читает/расшифровывает `byok_keys`.
- delete физически удаляет строку (зашифрованные материалы исчезают).
