# 06 — Авторизация и изоляция

## Аутентификация

Все эндпоинты `/v1/media/*`, кроме обложек шаблонов и **download ассета**, требуют `Authorization: Bearer <accessToken>` (JWT RS256). Идентичность берётся исключительно из проверенного claim `sub` ([ADR-007](../../adr/ADR-007-lazy-user-provisioning.md)); поля `userId` в телах запросов **нет** — подделать владельца задачи нечем. Нет/невалидный токен → `401 unauthorized`.

`GET`/`HEAD /v1/media/jobs/{jobId}/assets/{index}/{token}` **без JWT** ([ADR-085](../../adr/ADR-085-media-asset-download-proxy.md)): авторизация — HMAC в пути (тот же `PREVIEW_URL_SECRET`, канон `media-asset|{jobId}|{ownerUserId}|{index}|{exp}`). Подпись привязана к владельцу строки `media_jobs`. Битый/просроченный токен → `401`; нет job / нет index / хост stored URL вне allowlist → `404`. Preview-токен на этот роут не принимается. **[ADR-109 §5, §10](../../adr/ADR-109-media-asset-local-storage-30d.md) (код в `main` (`f90d871`), выкачен (CI `36015691825` на `48f9018`, джоб `ssh deploy` — `success`)):** своя копия результата на диске инстанса отдаётся **только** этим роутом и после той же проверки строки и токена; статической раздачи каталога нет; путь файла строится из `jobId` и `index` строки БД, не из запроса. После `DELETE` задачи или удаления пользователя копия недостижима сразу (строки нет → `404`).

`POST /v1/media/webhooks/proxy/{jobId}` ([ADR-108 §4](../../adr/ADR-108-media-generation-via-proxy.md)) **без JWT**: авторизация — query `token` = HMAC-SHA256 по `jobId` на `PROXY_WEBHOOK_SECRET` (иначе `PROXY_API_KEY`). **Контраст (обе стороны помечены):** download-токен подписан `PREVIEW_URL_SECRET`, выдаётся клиенту и привязан к владельцу; токен колбэка подписан другим секретом, клиенту не выдаётся никогда и разрешает только применение исхода к одной задаче. Неверный токен → `401` до обращения к БД; задачи нет или она не принималась прокси → `404`. Скоупа по `user_id` у ручки нет — владелец задачи колбэком не выбирается и не меняется.

## Изоляция владельца

Все запросы к `media_jobs` скоупятся `WHERE user_id = :sub`. Поэтому чужая задача **неотличима** от несуществующей: и то и другое → `404 not_found`. Чужая задача не появляется и в `GET /v1/media/jobs`.

Существование чужой задачи не раскрывается ни статусом, ни сообщением — тот же принцип, что у workspaces ([workspaces/06-rbac.md](../workspaces/06-rbac.md)).

## Гейт доступа

| Условие | Результат |
|---|---|
| `FAL_API_KEY` не задан на инстансе ([ADR-108 §1](../../adr/ADR-108-media-generation-via-proxy.md): не задан ни `FAL_API_KEY`, ни `PROXY_API_KEY` + `SERVICE_DOMAIN`) | `503 media_generation_not_configured` |
| провайдер отклонил ключ (`401`/`403`; ADR-108 — fal или прокси) | `503 media_generation_not_configured` |
| баланс кредитов меньше **итоговой** цены запуска (с учётом `numImages`/`duration`) | `409 insufficient_credits`, списания нет |
| удаление задачи в статусе `queued`/`running` | `409 job_not_terminal`; сначала опрос до терминального статуса, иначе возврат кредитов при провале станет невозможен |
| превышен per-user rate limit | `429 rate_limited` |

**Активной подписки не требуется**: единственный биллинговый гейт — баланс кредитов. Это отличается от покупки токенов, где подписка обязательна ([ADR-015](../../adr/ADR-015-consumable-token-iap.md), Q-015-1=B): там подписка защищала от «мёртвого» баланса, здесь баланс расходуется сразу.

## Rate limit

Общий per-user лимит для не-chat эндпоинтов (`enforce_other_limits`, `RATE_LIMIT_OTHER_PER_USER`), включая опрос задач. Redis недоступен → fail open, как и у остальных эндпоинтов.

## Что не логируется и не отдаётся

- `FAL_API_KEY` — не в логах (redaction покрывает `*key*`), не в ответах, не в БД. Так же `PROXY_API_KEY` и `PROXY_WEBHOOK_SECRET` ([ADR-108 §10](../../adr/ADR-108-media-generation-via-proxy.md)); значение `token` колбэка и тело колбэка целиком в структурные логи не пишутся.
- Тело ответа провайдера наверх не проксируется; исключение — текст `422`, который содержит только имя проблемного параметра (обрезается до 500 символов).
- Промт пользователя хранится в `media_jobs.prompt` (нужен для листинга) и **не** попадает в структурные логи.
- Полный URL CDN fal и signed token download-роута не логируются. Исходящий fetch — только `https`, без follow-redirect: чтение результата задачи (download-роут, подготовка аватара, своя копия) — по хостам `FAL_UPLOAD_HOST_SUFFIXES ∪ MEDIA_RESULT_HOST_SUFFIXES` ([ADR-108 §7](../../adr/ADR-108-media-generation-via-proxy.md), [ADR-112](../../adr/ADR-112-result-read-allowlist-for-avatar-preparation.md); для подготовки аватара код в `main` (`a985f30`), выкат — шапка ADR-112), слот загрузки и перехост кадра — только `FAL_UPLOAD_HOST_SUFFIXES` (уточнение факта 2026-09-24, решение не меняется).
- **Скачивание своей копии ([ADR-109 §3](../../adr/ADR-109-media-asset-local-storage-30d.md), код в `main` (`f90d871`), выкачен (CI `36015691825` на `48f9018`, джоб `ssh deploy` — `success`)):** тот же allowlist, что у download-роута (`FAL_UPLOAD_HOST_SUFFIXES ∪ MEDIA_RESULT_HOST_SUFFIXES`, [ADR-108 §7](../../adr/ADR-108-media-generation-via-proxy.md)), только `https`, без follow-redirect, предел `MEDIA_ASSET_MAX_BYTES`; в логах — ни URL целиком, ни путь каталога хоста.

## Валидация референсных изображений

Картинки передаются URL'ами, которые загружает провайдер, поэтому схема — allowlist: принимается **только `https://`**. `http://`, `file://`, `data:` и protocol-relative `//host` отбиваются `422` до отправки наверх, чтобы через нас нельзя было адресовать внутренние ресурсы.
