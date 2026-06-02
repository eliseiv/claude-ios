# Module: BYOK

- Статус: Реализован
- Ответственность: envelope-шифрование пользовательского Anthropic ключа, toggle, delete, валидация, выдача plaintext ключа Orchestrator in-memory.

## Документы
- [00-overview.md](00-overview.md) · [01-context.md](01-context.md) · [02-api-contracts.md](02-api-contracts.md) · [03-architecture.md](03-architecture.md) · [04-data-model.md](04-data-model.md) · [06-rbac.md](06-rbac.md) · [07-implementation-phases.md](07-implementation-phases.md) · [09-testing.md](09-testing.md)

## DoD
Ключ шифруется at-rest (envelope, AES-256-GCM + KMS); никогда не логируется и не возвращается клиенту; toggle/delete работают; key_status корректен. `keyStatus` — 6 значений (`valid`/`invalid`/`missing`/`validating`/`offline`/`expired`, [ADR-016](../../adr/ADR-016-extended-byok-statuses.md)); при `keyStatus=valid` ответ содержит `activeModel`, иначе `null`.

## Changelog
- 2026-05-21: bootstrap (architect).
- 2026-05-21: реализован (backend), тесты зелёные, ревью пройдено. Код: `src/app/byok/service.py`, `src/app/byok/kms.py` (`KmsClient` + `LocalKmsClient`, дефолт для local/CI; облачный провайдер — Q-002-1).
- 2026-06-02 (Спринт 1, backend, [ADR-016](../../adr/ADR-016-extended-byok-statuses.md)): `keyStatus` расширен до 6 значений (`+validating`/`offline`/`expired`); добавлено поле `activeModel` (модель при `valid`, иначе `null`). Миграция `0004` расширяет enum `byok_key_status` (`ALTER TYPE ... ADD VALUE`). Старые клиенты трактуют новые статусы как «не valid». Тесты зелёные (offline-сьют 681/681).
