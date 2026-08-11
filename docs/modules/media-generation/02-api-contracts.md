# 02 — API-контракты

Все эндпоинты требуют `Authorization: Bearer <accessToken>` и работают в скоупе владельца: чужая или несуществующая задача — `404`. Тег OpenAPI — `Media`. Тела запросов — `StrictModel` (`extra=forbid`): лишнее поле → `422`.

**Сценарий целиком:**

```
GET  /v1/media/templates/images|videos → плитки шаблонов (обложка + промпт + модель + params)
GET  /v1/media/templates/{id}/cover    → байты обложки (без JWT)
GET  /v1/media/models                  → модель, режимы и допустимые значения параметров
POST /v1/media/uploads                 → 201 { url }   (только если генерируем по своей картинке)
POST /v1/media/images | /videos        → 202 { jobId, status: "queued", creditsCharged }
GET  /v1/media/jobs/{jobId}            → опрашивать до status = completed | failed
                                          completed → assets[].url
                                          failed    → error, кредиты возвращены
POST /v1/media/images { sourceJobId }  → правка результата предыдущей задачи
GET  /v1/media/jobs?cursor=…           → лента, новые сверху
DELETE /v1/media/jobs/{jobId}          → убрать завершённую задачу из ленты
```

---

## `GET /v1/media/templates/images` / `GET /v1/media/templates/videos`

Каталог шаблонов галереи ([ADR-066](../../adr/ADR-066-media-templates-catalog.md)). JWT + per-user rate-limit. **Не зависит от `FAL_API_KEY`** — отвечает даже когда генерация на инстансе выключена. Кредитов не тратит.

**Ответ `200`:**

```json
{
  "templates": [
    {
      "id": "profile_picture",
      "title": "Profile Picture",
      "coverUrl": "https://velunixa.shop/v1/media/templates/profile_picture/cover",
      "prompt": "Studio portrait of the person in the photo, soft light, clean background",
      "model": "nano-banana-2",
      "requiredInputImages": 1,
      "parameters": { "aspectRatio": "1:1", "resolution": "1K", "numImages": 1 }
    }
  ]
}
```

| Поле | Смысл |
|---|---|
| `id` | стабильный slug; также путь обложки |
| `title` | подпись плитки |
| `coverUrl` | абсолютный URL обложки (`SERVICE_DOMAIN`); при пустом домене — относительный `/v1/media/templates/{id}/cover` |
| `prompt` / `model` / `parameters` | копируются в `POST /v1/media/images` или `/videos` |
| `requiredInputImages` | сколько фото попросить у юзера (`0` = text-only; image ≤ 14; video ≤ 1) |

Порядок — `sort_order` в БД (порядок seed / create).

## `GET /v1/media/templates/{id}/cover`

Байты обложки. **Без JWT.** `Content-Type` из записи, `Cache-Control: public, max-age=86400`. Чужой/несуществующий `id` → `404`.

## Admin: `POST /v1/admin/media/templates` / `DELETE /v1/admin/media/templates/{id}`

Авторизация `X-Admin-Key` / `X-Admin-Token` ([ADR-009](../../adr/ADR-009-admin-token-auth.md)). Create принимает base64-обложку (`cover.mediaType` ∈ jpeg/png/webp); конфликт `id` → `409`. Delete → `200 { "deleted": true }` или `404`.

---

## `GET /v1/media/models`

Каталог доступных моделей. Справочный, кредитов не тратит.

**Ответ `200`:**

```json
{
  "models": [
    {
      "id": "nano-banana-2",
      "title": "Nano Banana 2 (Gemini 3.1 Flash Image)",
      "kind": "image",
      "credits": 4,
      "baseDurationSeconds": null,
      "supportsImageInput": true,
      "maxInputImages": 14,
      "supportsAudio": false,
      "modes": [
        {
          "mode": "textToImage",
          "params": ["aspectRatio", "numImages", "outputFormat", "prompt", "resolution", "seed"],
          "aspectRatios": ["auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16", "4:1", "1:4", "8:1", "1:8"],
          "resolutions": ["0.5K", "1K", "2K", "4K"],
          "durations": [],
          "defaults": { "resolution": "1K", "numImages": 1 }
        },
        {
          "mode": "imageToImage",
          "params": ["aspectRatio", "numImages", "outputFormat", "prompt", "resolution", "seed"],
          "aspectRatios": ["auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16", "4:1", "1:4", "8:1", "1:8"],
          "resolutions": ["0.5K", "1K", "2K", "4K"],
          "durations": [],
          "defaults": { "resolution": "1K", "numImages": 1 }
        }
      ]
    }
  ]
}
```

| Поле | Смысл |
|---|---|
| `id` | значение для поля `model` в запросах генерации |
| `kind` | `image` → отправлять в `/v1/media/images`; `video` → в `/v1/media/videos` |
| `credits` | базовая цена: image — одно фото `1K`; video — одна пачка `baseDurationSeconds` без quality-множителей |
| `baseDurationSeconds` | длительность видео на одну пачку; `null` у image-моделей |
| `resolutionCredits` | image: цена одного фото по `resolution` (целые ступени); `null` у video |
| `resolutionMultipliers` | video: множитель пачки по `resolution` (Veo: `4k`→2); `null`/пусто — не влияет |
| `audioMultiplier` | video: множитель при `generateAudio: true` (Veo→2, Kling V3→1.5); итог округляется **вверх**. `null` — звук на цену не влияет |
| `supportsImageInput` / `maxInputImages` | принимает ли модель референсные изображения и сколько (на цену не влияет) |
| `supportsAudio` | имеет ли смысл показывать переключатель `generateAudio` |
| `modes` | режимы генерации: первый — без референсного изображения, второй — с ним |

**Режим** (`modes[]`) — это то, по чему строится UI:

| Поле режима | Смысл |
|---|---|
| `mode` | `textToImage`/`imageToImage`/`textToVideo`/`imageToVideo`. Выбирается автоматически по наличию `imageUrls`/`imageUrl` в запросе |
| `params` | какие параметры принимает **этот** режим. Параметра нет в списке → контрол не показывать, присланное значение будет проигнорировано |
| `aspectRatios` / `resolutions` / `durations` | допустимые значения одноимённых полей **в этом режиме**. **Пустой список = параметр в режиме не поддерживается**: присланное значение вернёт `422` |
| `defaults` | что сервер подставит, если поле не прислано. Только параметры, влияющие на цену. Пустой объект — подставлять нечего |

Наборы намеренно даны по режимам, а не по модели, потому что они действительно различаются: у Veo в text-to-video нет `aspectRatio: "auto"`, а в image-to-video есть; Kling в image-to-video `aspectRatio` не принимает вовсе (берёт из стартового кадра). Каталог — источник истины: модели, значения и цены меняются на сервере без релиза клиента.

**Дефолты режима — часть цены.** Поле, влияющее на стоимость и не пришедшее в запросе, сервер заполняет значением из `defaults` и **отправляет его провайдеру явно**. Поэтому расчёт на клиенте обязан подставлять ровно эти значения — иначе он разойдётся с `creditsCharged`. Действующие дефолты:

| Модель | `duration` | `resolution` | `generateAudio` | `numImages` |
|---|---|---|---|---|
| `nano-banana-pro` | — | `1K` | — | `1` |
| `nano-banana-2` | — | `1K` | — | `1` |
| `kling-video` | `5` | — | — | — |
| `kling-video-v3` | `5` | — | `false` | — |
| `veo-3.1` | `8s` | `720p` | `false` | — |

Звук по умолчанию **выключен**: включение — явное действие пользователя, а не молчаливое удвоение счёта. Параметры, не влияющие на цену (`aspectRatio`, `outputFormat`, `negativePrompt`, `cfgScale`, `seed`), дефолтов не имеют — их по-прежнему выбирает провайдер.

**Как считается цена** (mode text/image не влияет):

- изображения: `resolutionCredits[resolution] × numImages` — например `nano-banana-2` 4K × 2 = `8 × 2 = 16`; без `resolution` берётся цена `1K` (дефолт режима);
- видео: `ceil(credits × ceil(duration / baseDurationSeconds) × resolutionMultipliers[resolution] × (audioMultiplier если generateAudio))` — например 15 с Kling V3 без звука = `23 × 3 = 69`, со звуком = `ceil(69 × 1.5) = 104`; Veo `8s` + `1080p` + audio = `32 × 2 × 1 × 2 = 128`.

Округление итога **вверх**: множитель звука Kling V3 дробный (×1.5), а цена в кредитах — целая.

Фактически списанное всегда приходит в `creditsCharged`; баланс проверяется по итоговой цене (`409` до списания).

---

## `POST /v1/media/uploads`

Превращает локальный файл в https-ссылку, пригодную для `imageUrls`/`imageUrl`. Кредитов не стоит.

Нужен потому, что оба поля с картинкой принимают **только `https://`**: файл скачивает сам провайдер, поэтому inline-base64 в запросе генерации не принимается вовсе. У мобильного клиента публичной ссылки на фото из галереи нет — этот эндпоинт её и выдаёт.

**Тело:**

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `type` | `image` | да | только изображения — референс генерации |
| `mediaType` | `image/jpeg` \| `image/png` \| `image/gif` \| `image/webp` | да | сверяется с реальной сигнатурой файла |
| `filename` | string | да | 1–512 символов |
| `data` | string | да | содержимое в base64 |

```json
{ "type": "image", "mediaType": "image/jpeg", "filename": "photo.jpg", "data": "/9j/4AAQ…" }
```

**Ответ `201`:**

```json
{
  "url": "https://v3b.fal.media/files/b/…/photo.jpg",
  "mediaType": "image/jpeg",
  "size": 1048576,
  "expiresAt": null
}
```

`expiresAt` — когда ссылка перестанет работать, если срок задан на инстансе; `null` — срок не ограничен либо определяется политикой провайдера. На бессрочность полагаться не стоит: нужный файл клиенту лучше хранить у себя.

**Лимиты.** Файл — до **10 МБ** после декодирования (`MEDIA_UPLOAD_MAX_BYTES`), тело запроса — до **16 МБ** (`MEDIA_UPLOAD_REQUEST_BODY_LIMIT`; base64 раздувает файл в ⁴⁄₃ раза, плюс JSON-обвязка). Превышение — `413 payload_too_large`, причём размер оценивается **до** декодирования.

**Ошибки:** `413` — файл или тело больше лимита; `422` — `mediaType` вне allowlist, не-`image` в `type`, битый base64 или подделанная сигнатура файла; `502` — провайдер недоступен; `503` — генерация не настроена на инстансе.

---

## `POST /v1/media/images`

**Тело:**

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `model` | string | да | id image-модели из каталога |
| `prompt` | string | да | описание желаемого изображения, ≤ 5000 символов |
| `imageUrls` | string[] \| null | нет | референсные изображения (**только `https://`**, ≤ 14) → включает режим `imageToImage`. Локальный файл сначала через `POST /v1/media/uploads` |
| `sourceJobId` | uuid \| null | нет | «отредактируй результат вон той задачи» — сервер сам подставит её изображения. **Взаимоисключимо с `imageUrls`** |
| `aspectRatio` | string \| null | нет | из `aspectRatios` режима |
| `resolution` | string \| null | нет | из `resolutions` режима (`0.5K`/`1K`/`2K`/`4K`) |
| `numImages` | int \| null | нет | 1–4. **Умножает цену** |
| `outputFormat` | `jpeg`\|`png`\|`webp` \| null | нет | формат файла |
| `seed` | int \| null | нет | 0…2147483647. Тот же `seed` с теми же параметрами даёт похожий результат |

```json
{ "model": "nano-banana-2", "prompt": "уютная кофейня, вывеска «OPEN»", "aspectRatio": "16:9", "resolution": "2K" }
```

**Ответ `202`** — объект задачи (см. ниже), `status: "queued"`, `assets: []`.

Поля цены в теле **нет**: стоимость определяет сервер.

---

## `POST /v1/media/videos`

**Тело:**

| Поле | Тип | Обяз. | Описание |
|---|---|---|---|
| `model` | string | да | id video-модели из каталога |
| `prompt` | string | да | описание сцены, ≤ 5000 символов. Для моделей со звуком может содержать реплики |
| `imageUrl` | string \| null | нет | стартовый кадр (**только `https://`**) → включает режим `imageToVideo`. Локальный файл сначала через `POST /v1/media/uploads` |
| `sourceJobId` | uuid \| null | нет | взять стартовый кадр из результата вон той задачи. **Взаимоисключимо с `imageUrl`** |
| `negativePrompt` | string \| null | нет | что не должно попасть в кадр, ≤ 2000 символов |
| `aspectRatio` | string \| null | нет | из `aspectRatios` **режима**. У Kling в image-to-video параметра нет — берётся из стартового кадра |
| `resolution` | string \| null | нет | из `resolutions` режима (`720p`/`1080p`/`4k` у Veo; у Kling параметра нет) |
| `duration` | string \| null | нет | из `durations` режима (`4s`/`6s`/`8s` у Veo; `5`/`10` у Kling 2.5; `3`…`15` у Kling V3). **Умножает цену** |
| `generateAudio` | bool \| null | нет | генерировать ли звук; только у моделей с `supportsAudio` |
| `cfgScale` | float \| null | нет | 0–1, насколько строго следовать промту (у провайдера по умолчанию 0.5). Только у моделей Kling |
| `seed` | int \| null | нет | 0…2147483647, воспроизводимость. Только у Veo |

```json
{ "model": "veo-3.1", "prompt": "город в сумерках, камера летит над крышами", "duration": "8s", "resolution": "1080p", "generateAudio": true }
```

Параметр, которого у выбранного режима нет в `params`, **отбрасывается** и наверх не уходит (провайдер отбивает неизвестные ключи). Значение вне набора режима — `422` **до** списания.

**Ответ `202`** — объект задачи, `status: "queued"`.

---

## `GET /v1/media/jobs/{jobId}`

Актуальное состояние задачи. Опрашивает провайдера, пока задача не терминальна; после `completed`/`failed` ответ идёт из БД. Параллельно фоновый reconciler ([ADR-067](../../adr/ADR-067-media-ready-push-and-reconciler.md)) продвигает non-terminal jobs без клиентского poll — нужен для media-ready push, когда iOS заморозил приложение.

При переходе в `completed` (poll или reconciler) бэкенд один раз шлёт APNs (если `notificationsEnabled` + device token + `APNS_*`): custom keys `jobId`, `kind`, `mediaUrl` (= `assets[0].url`), `aps.mutable-content=1`. Deep link — по `jobId` (чата у media нет).

**Ответ `200`:**

```json
{
  "jobId": "e1f0c8a2-3b4d-4e5f-8a9b-0c1d2e3f4a5b",
  "status": "completed",
  "kind": "image",
  "model": "nano-banana-2",
  "prompt": "уютная кофейня, вывеска «OPEN»",
  "creditsCharged": 4,
  "creditsRefunded": false,
  "assets": [
    { "url": "https://v3.fal.media/files/…/out.png", "contentType": "image/png", "fileName": "out.png" }
  ],
  "error": null,
  "parentJobId": null,
  "inputImageUrls": [],
  "createdAt": "2026-08-04T11:20:31.482Z",
  "updatedAt": "2026-08-04T11:20:48.117Z"
}
```

| `status` | Что делать клиенту |
|---|---|
| `queued` | задача в очереди провайдера — опрашивать дальше |
| `running` | генерация идёт — опрашивать дальше |
| `completed` | результат в `assets` (непустой), `error: null` |
| `failed` | причина в `error`; `creditsRefunded: true` — кредиты уже вернулись на баланс |

Часть входных данных провайдер проверяет только в момент исполнения — например, недостижимую ссылку на референсную картинку. Такой запуск приходит как `failed` с текстом провайдера в `error` (например, `body.image_url: Failed to download the file…`) и с возвратом кредитов. `422` на **опрос** не приходит никогда: сам `GET` корректен, а вердикт по запуску живёт в поле `status`.

`contentType` и `fileName` заполняются, только если провайдер их вернул (у видео обычно `null`). `assets` непуст **только** при `completed`.

**Интервал опроса.** Разумно: изображения — раз в 2 с, видео — раз в 5–10 с. Опрос лимитируется общим per-user rate limit (`RATE_LIMIT_OTHER_PER_USER`), превышение → `429`.

---

## Цепочки правок

Чтобы отредактировать только что сгенерированное фото, не нужно нигде хранить его URL: пришлите `sourceJobId` — идентификатор задачи, результат которой берётся за основу. Сервер сам подставит её `assets[].url` (не больше `maxInputImages` штук).

Исходная задача должна принадлежать вам, быть `completed`, иметь `kind: image` и непустой `assets`. Чужая или несуществующая — `404`; остальные несоответствия — `422` **до** списания.

В ответе задачи это видно двумя полями:

| Поле | Смысл |
|---|---|
| `parentJobId` | из результата какой задачи сделана эта. `null` — начало цепочки либо исходную задачу удалили |
| `inputImageUrls` | что реально ушло на вход: присланное в `imageUrls`/`imageUrl` либо взятое из `sourceJobId`. Пустой список — генерация из текста |

`inputImageUrls` хранится вместе с задачей, поэтому «из чего это сделано» видно и после удаления исходной задачи.

Идентификатор, а не URL, потому что правка — отношение между **вашими** задачами, а ссылка провайдера живёт по его политике хранения и однажды перестанет открываться. Связь в ленте при этом останется верной; но если ассет исходной задачи уже истёк, повторная правка вернётся как `failed` от провайдера.

---

## `GET /v1/media/jobs`

Лента генераций, новые сверху. **Read-only: провайдера не опрашивает** — у незавершённых задач отдаётся последнее известное состояние. Чтобы обновить конкретную задачу, используйте `GET /v1/media/jobs/{jobId}`.

**Query:** `limit` (1–100, дефолт 20), `kind` (`image`\|`video`, опционально), `cursor` (опционально).

**Ответ `200`:** `{ "jobs": [ …объекты задачи… ], "nextCursor": "…" | null }`

**Пролистывание.** Передайте `nextCursor` из предыдущего ответа в `cursor`; `nextCursor: null` означает, что страниц больше нет. `kind` при пролистывании должен оставаться тем же. Битый курсор — `422`.

Курсор, а не `offset`: лента растёт с головы, и при `offset` задача, созданная между двумя запросами, сдвигает окно — пользователь увидит дубли и пропуски.

---

## `DELETE /v1/media/jobs/{jobId}`

Убирает завершённую задачу из ленты.

**Ответ `200`:** `{ "deleted": true }`

- `404` — чужая, несуществующая или уже удалённая задача.
- `409 job_not_terminal` — задача в статусе `queued`/`running`. Возврат кредитов при провале у провайдера привязан к этой записи и срабатывает при опросе, поэтому сначала доведите задачу опросом до `completed`/`failed`.

Удаляется **только запись у нас**. Файл остаётся у провайдера до истечения его срока хранения — мы им не владеем. Удаление исходной задачи не удаляет сделанные из неё правки: у них просто обнуляется `parentJobId`.

---

## Ошибки

Формат общий: `{"error": {"code", "message", "requestId"}}`.

| Код | HTTP | Когда |
|---|---|---|
| `unauthorized` | 401 | нет/невалидный Bearer |
| `not_found` | 404 | чужая или несуществующая задача (в том числе в `sourceJobId`) |
| `job_not_terminal` | 409 | попытка удалить задачу в статусе `queued`/`running` |
| `insufficient_credits` | 409 | на балансе меньше **итоговой** цены (с учётом `numImages`/`duration`). **Списания не произошло**, задача не создана |
| `validation_error` | 422 | **только на POST:** неизвестная модель; модель не того типа для маршрута; значение вне набора **режима** (`aspectRatio`/`resolution`/`duration`); параметр, которого у режима нет вовсе; не-`https` URL картинки; больше `maxInputImages` картинок; лишнее поле в теле; `sourceJobId` вместе с `imageUrls`/`imageUrl`; `sourceJobId` на незавершённую задачу, на видео или на задачу без результата; битый `cursor`. Также — если параметры отклонил сам провайдер при приёме (в `message` будет имя проблемного параметра). Отклонение уже принятого запуска приходит не этим кодом, а как `status: "failed"` |
| `payload_too_large` | 413 | **только на `POST /v1/media/uploads`:** файл или тело запроса больше лимита |
| `rate_limited` | 429 | превышен per-user лимит или лимит провайдера |
| `upstream_error` | 502 | провайдер недоступен (таймаут, connect, 5xx, битый ответ). **Кредиты не списаны** — списание откатывается вместе с задачей |
| `media_generation_not_configured` | 503 | генерация не настроена на инстансе (`FAL_API_KEY` не задан) либо провайдер отклонил ключ. Проблема оператора, не клиента. Так отвечают маршруты постановки/опроса/uploads/`GET /v1/media/models`. **Исключение:** `GET /v1/media/templates/*` и admin CRUD шаблонов не зависят от fal и при пустом ключе остаются доступны ([ADR-066](../../adr/ADR-066-media-templates-catalog.md)) |

**Инварианты биллинга, на которые можно опираться:**

- `202` получен ⇒ кредиты списаны (`creditsCharged` — уже с учётом `numImages`/`duration`), задача существует.
- `4xx`/`5xx` на POST ⇒ кредиты **не** списаны.
- `status: "failed"` ⇒ `creditsRefunded: true`, кредиты вернулись. Повторный опрос не возвращает их дважды. Это верно и когда запуск отклонён провайдером уже после приёма.
- `502` на **опрос** ничего не меняет: задача остаётся незавершённой и неоплаченной повторно, следующий опрос доберёт исход.
