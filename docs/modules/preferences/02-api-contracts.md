# Preferences — API Contracts

JWT, владелец = `sub`.

## GET /v1/preferences
### Response (200)
```json
{
  "defaultAssistantMode": "chat | code",
  "notificationsEnabled": true,
  "codeDefaults": { }
}
```
- Если строки `user_preferences` нет — возвращаются дефолты (`chat` / `true` / `{}`).

## PATCH /v1/preferences
Частичное обновление (любое подмножество полей).

### Request
```json
{
  "defaultAssistantMode": "chat | code",
  "notificationsEnabled": true,
  "codeDefaults": { }
}
```
- `extra='forbid'`. Хотя бы одно поле. `defaultAssistantMode` ∈ {chat, code}, иначе `422`. `codeDefaults` ≤ 8KB сериализованного JSON.
- Upsert: создаёт строку при отсутствии, обновляет заданные поля.

### Response (200)
Полный текущий объект preferences (как GET).
