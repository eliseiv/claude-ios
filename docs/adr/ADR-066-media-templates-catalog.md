# ADR-066 — Каталог шаблонов генерации (photo/video templates)

- **Статус:** Accepted
- **Дата:** 2026-08-10
- **Связано:** [ADR-060](ADR-060-media-generation-fal.md) (create image/video), [ADR-035](ADR-035-prompt-presets-endpoint.md) (паттерн каталога), [ADR-009](ADR-009-admin-token-auth.md) (admin auth), [ADR-031](ADR-031-absolute-preview-url.md) (абсолютные URL через `SERVICE_DOMAIN`), [media-generation/02-api-contracts.md](../modules/media-generation/02-api-contracts.md)

## Контекст

На экране галереи iOS нужны плитки шаблонов: обложка + готовый промпт + модель + параметры + сколько фото попросить у юзера. Часть шаблонов — image-to-image / image-to-video и без фото пользователя не работают. Клиент тянет фото- и видео-каталоги параллельно и собирает UI сам.

Каталог должен меняться **без релиза App Store**: оператор/iOS-разработчик добавляет и удаляет шаблоны через admin API. Обложки раздаёт наш API (не fal CDN с TTL).

## Решение

### 1. Клиентские эндпоинты

- `GET /v1/media/templates/images` — JWT, rate-limit `enforce_other_limits`
- `GET /v1/media/templates/videos` — то же
- `GET /v1/media/templates/{id}/cover` — **без JWT**, raw bytes + `Content-Type` + `Cache-Control: public, max-age=86400`

Элемент list: `id`, `title`, `coverUrl`, `prompt`, `model`, `requiredInputImages`, `parameters`.  
`coverUrl` = `https://{SERVICE_DOMAIN}/v1/media/templates/{id}/cover` (пусто `SERVICE_DOMAIN` → относительный путь, как preview ADR-031).

Поля `prompt`/`model`/`parameters` + N загрузок через `POST /v1/media/uploads` копируются в `POST /v1/media/images|/videos`. Каталог **не зависит от `FAL_API_KEY`**: list/cover/admin работают, даже если генерация на инстансе выключена.

### 2. Admin CRUD

Под `/v1/admin/media/templates`, `require_admin` (`X-Admin-Key` / `X-Admin-Token`):

- `POST` — создать шаблон + base64-обложку (`kind`, `id`, `title`, `prompt`, `model`, `requiredInputImages`, `parameters`, `cover`)
- `DELETE /{id}` — удалить

Create-путь выведен из admin 8 KB cap и имеет raised transport limit (как `/v1/media/uploads`). Конфликт `id` → `409`.

### 3. Хранение

Таблица `media_templates`: метаданные + `cover_bytes` BYTEA / `cover_media_type` (паттерн `site_files`). Seed 5 image + 5 video с placeholder-обложками в миграции.

### 4. Отклонённое

- Статический реестр в коде без admin write — не даёт iOS-разработчику менять набор без деплоя.
- fal CDN для обложек — чужой TTL и зависимость от `FAL_API_KEY`.
- Local disk / S3 — в проекте нет такого слоя.
- JWT на cover GET — ломает простой `AsyncImage`/кэш плиток.

## Последствия

- Новый ADR + контракт в media-generation docs; миграция `0021`.
- Admin surface расширяется catalog-CRUD (раньше только user mutations).
- Клиент больше не хардкодит плитки; обновление = admin POST/DELETE или seed+deploy.
