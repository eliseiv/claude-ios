# ADR-085 — Прокси ассетов генерации: signed URL вместо голого fal.media

- **Статус:** Accepted
- **Дата:** 2026-08-21
- **Тип:** feature-ADR, **пересматривает границу [ADR-060](ADR-060-media-generation-fal.md)** «ассеты не проксируются и не перекладываются в наше хранилище» (тело ADR-060 не переписано — immutability)
- **Связано:** [ADR-010](ADR-010-backend-hosted-preview.md) (HMAC+TTL в пути), [ADR-031](ADR-031-absolute-preview-url.md) (`SERVICE_DOMAIN`), [ADR-062](ADR-062-media-upload-via-fal-storage.md) (`FAL_UPLOAD_HOST_SUFFIXES`), [ADR-067](ADR-067-media-ready-push-and-reconciler.md) (`mediaUrl` в APNs)
- **Реализуется в:** [modules/media-generation](../modules/media-generation/README.md)

## Контекст

Готовый job отдаёт `assets[].url` прямой ссылкой CDN fal (`*.fal.media`). С телефона (и части сетей) загрузка/playback рвётся по `-1001`. Датацентр до fal доходит. Своего object storage нет; складывать видео на диск 19 инстансов нельзя.

`AVPlayer` не умеет слать `Authorization: Bearer`, поэтому JWT на download-роуте бесполезен — как у обложек шаблонов и preview.

## Решение

**Прокси без своего хранилища.** Байты не пишутся на диск и не копируются в S3. Клиенту не отдаём голые `*.fal.media` для playback/download.

1. **В БД** `media_jobs.result.assets[].url` остаётся сырым fal CDN. Edit-chain, reconciler и rehost читают БД, не клиентский URL.
2. **На границе API** (`GET /v1/media/jobs/{jobId}`, лента) и в push `mediaUrl` поле `url` переписывается в signed URL на `SERVICE_DOMAIN` (пустой домен — относительный путь, как cover).
3. **Публичный** `GET`/`HEAD` `/v1/media/jobs/{jobId}/assets/{index}/{token}` **без JWT**. Авторизация — HMAC в пути (не query: меньше шансов утечь в referrer).
4. Роут грузит job, сверяет подпись с `job.user_id` + `index`, проверяет хост stored URL против `FAL_UPLOAD_HOST_SUFFIXES`, стримит байты с fal (`StreamingResponse`, без буфера файла). `Range` / `If-Range` пробрасываются — иначе видео не seek'ается и снова рвётся.
5. Токен: `b64url(exp).b64url(HMAC_SHA256(PREVIEW_URL_SECRET, "media-asset|{jobId}|{ownerUserId}|{index}|{exp}"))`. Тот же секрет, что у preview — новый env на инстансы не катим. Префикс `media-asset|` не даёт обменять preview-токен на видео. TTL — `MEDIA_DOWNLOAD_TTL_SECONDS` (дефолт 86400). Просрочка → клиент снова поллит job и получает свежий URL.
6. Пустой `PREVIEW_URL_SECRET` (локалка) — rewrite не падает: отдаём stored URL и пишем WARNING.
7. `inputImageUrls` не переписываем: их забирает сервер для следующей генерации.

### Карта ошибок download-роута

- `401 unauthorized` — битый или просроченный токен.
- `404 not_found` — нет job / нет index / хост не из allowlist / fal 404 (истёкший ассет). Чужое существование не раскрываем отдельным кодом.
- `502 upstream_error` — fal недоступен.
- `504 gateway_timeout` — таймаут исходящего чтения.
- `503 media_generation_not_configured` — нет `FAL_API_KEY` (общий гейт префикса `/v1/media`).

Исходящий fetch: `https` only, `follow_redirects=False` (редирект на внутренний хост = SSRF). Connect-timeout короткий, read — длинный. Полный fal URL и токен не логируются.

Схема `MediaAssetSchema` не меняется: то же поле `url`. iOS менять не обязан — играет ту же ссылку.

## Что НЕ меняется

- Списание/возврат кредитов, очередь fal, каталог моделей.
- Хранение ассетов у fal (Q-060-1 / своё object storage по-прежнему открыты).
- Upload референсов (ADR-062): байты по-прежнему транзитом в fal, не к нам.
- Цепочки правок: `sourceJobId` читает fal URL из БД.
