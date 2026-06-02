# BYOK — Implementation Phases

| Phase | Задача | Зависит от |
|---|---|---|
| BY-1 | Модель + миграция byok_keys. | DB |
| BY-2 | `KmsClient` интерфейс + дефолт-реализация (Q-002-1) + AES-GCM helpers. | — |
| BY-3 | `set` (envelope encrypt + валидация ключа через Anthropic). | BY-1, BY-2 |
| BY-4 | `toggle` + `delete`. | BY-1 |
| BY-5 | `get_plaintext_key` (internal, decrypt) для Orchestrator. | BY-2, BY-3 |
| BY-6 | audit byok_change + redaction проверки. | BY-3, Audit |

> Интерфейс KMS стабилен; конкретный провайдер (Q-002-1) нужен до prod, не до начала кода.
