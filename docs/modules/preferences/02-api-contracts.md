# Preferences — API Contracts

JWT, владелец = `sub`.

## GET /v1/preferences
### Response (200)
```json
{
  "defaultAssistantMode": "chat | code",
  "notificationsEnabled": false,
  "defaultVoiceId": null,
  "codeDefaults": { }
}
```
- Если строки `user_preferences` нет — возвращаются дефолты (`chat` / `false` / `null` / `{}`). Дефолт `notificationsEnabled=false` ([ADR-032](../../adr/ADR-032-notifications-enabled-default-false.md)): privacy-by-default, iOS включает push через `PATCH` после системного разрешения. Существующие строки сохраняют сохранённое значение.
- **`defaultVoiceId` (nullable, [ADR-100](../../adr/ADR-100-assistant-speech-output.md))** — голос озвучки по умолчанию: `id` записи реестра голосов с `selectable: true` (каталог — [`GET /v1/voices`](../chat-orchestrator/02-api-contracts.md#get-v1voices--каталог-голосов-adr-100)). `null` = голос инстанса (`TTS_DEFAULT_VOICE_ID`). **Сохранённое значение отдаётся и при `VOICE_OUTPUT_ENABLED=false`** — как сохраняется `characterId` в списке чатов при снятом флаге персонажей ([ADR-097 §7](../../adr/ADR-097-character-personas.md)): выключатель гасит поведение, но не стирает выбор пользователя.
- **Голоса персонажей этой настройкой не переопределяются** — голос персонажа стоит выше в порядке резолва ([chat-orchestrator/03-architecture §Резолв голоса](../chat-orchestrator/03-architecture.md#резолв-голоса-adr-100)), и это решение владельца, а не деталь реализации.

## PATCH /v1/preferences
Частичное обновление (любое подмножество полей).

### Request
```json
{
  "defaultAssistantMode": "chat | code",
  "notificationsEnabled": true,
  "defaultVoiceId": "default_male",
  "codeDefaults": { }
}
```
- `extra='forbid'`. Хотя бы одно поле. `defaultAssistantMode` ∈ {chat, code}, иначе `422`. `codeDefaults` ≤ 8KB сериализованного JSON.
- **`defaultVoiceId`**: значение вне `selectable`-записей реестра → `422 unknown_voice`; `null` сбрасывает к голосу инстанса; при `VOICE_OUTPUT_ENABLED=false` любое непустое значение → `422 voice_output_disabled` (симметрия с `characters_disabled`, [ADR-097 §7](../../adr/ADR-097-character-personas.md)). **Отказ, а не молчаливое игнорирование:** выбор голоса виден пользователю в настройках, и молча отброшенное значение дало бы экран, который показывает один голос, а звучит другим — тот отказ, который снаружи не диагностируется.
- Upsert: создаёт строку при отсутствии, обновляет заданные поля.

### Response (200)
Полный текущий объект preferences (как GET).
