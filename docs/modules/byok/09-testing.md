# BYOK — Testing

## Unit
- AES-256-GCM encrypt→decrypt round-trip восстанавливает ключ.
- KmsClient (fake): encrypt_dek→decrypt_dek round-trip.
- Tampered ciphertext/tag → ошибка аутентификации GCM.

## Integration (respx для Anthropic, fake KMS, AC-5)

**`keyStatus` перечислен целиком, а не по трём исходным значениям.** Домен закрыт контрактом [02-api-contracts.md](02-api-contracts.md) и расширением [ADR-016](../../adr/ADR-016-extended-byok-statuses.md): **шесть** значений — `missing`, `validating`, `valid`, `invalid`, `offline`, `expired`. Прежняя редакция перечня называла три (`valid`/`invalid`/`missing`), поэтому прогон «перечень против тестов» показывал полноту при трёх непроверенных значениях и непроверенном поле `activeModel`.

- `set` валидным ключом → keyStatus=valid; в БД хранится только ciphertext (нет plaintext). Кейс: `tests/integration/test_byok_status_policy.py::test_set_valid_reports_valid_and_active_model`.
- `set` ключом, на который провайдер ответил `401` → keyStatus=invalid, не enabled, `activeModel=null`. Кейс: `…::test_set_401_reports_invalid_no_active_model`.
- `set` при сетевой ошибке валидации (**не** `401`) → keyStatus=**offline**, а не `invalid`: различение обеих сторон обязательно, иначе временная недоступность провайдера выдаётся за отозванный ключ. Кейс: `…::test_set_network_error_reports_offline`.
- **`401` в момент ИСПОЛЬЗОВАНИЯ** (`/chat/run` в режиме byok) → ключ помечается **expired**, ход блокируется `byok_invalid`. Кейс: `…::test_runtime_401_marks_expired_and_blocks_byok`.
- **все шесть значений `keyStatus` наблюдаемы наружу**, включая `validating`; для каждого не-`valid` значения `toggle enabled=true` **не включает** BYOK и `activeModel=null`. Кейс: `…::test_all_six_key_statuses_surface`.
- **`activeModel` — только при `valid`** ([ADR-016](../../adr/ADR-016-extended-byok-statuses.md)): при любом другом статусе `null`. Обе стороны предиката покрыты кейсами `…::test_set_valid_reports_valid_and_active_model` и `…::test_all_six_key_statuses_surface`.
- Логи/audit НЕ содержат plaintext ключ (assert по redaction).
- `toggle enabled=true` при invalid → не включается; `toggle enabled=true` при `valid` → включается (обе стороны гейта). Кейс: `…::test_toggle_enables_only_when_valid`.
- `delete` → строка удалена, keyStatus=missing.
- `get_plaintext_key` восстанавливает исходный ключ (через fake KMS).
- Ответы endpoint не содержат ключ.
