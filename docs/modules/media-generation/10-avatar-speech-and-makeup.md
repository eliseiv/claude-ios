# Avatar speech и virtual makeup

## Границы

Это backend-контракт; iOS в изменение не входит. Новые сценарии переиспользуют существующие
`media_jobs`, polling/reconciler, signed assets, модерацию, списание и идемпотентный возврат.
Старые `/v1/media/images`, `/videos`, `/jobs` и форма `MediaJobResponse` не меняются.

Выкат безопасен для нескольких инстансов:

1. применить аддитивную миграцию `0035_media_features` на весь парк;
2. выкатить код с `AVATAR_SPEECH_ENABLED=false` и `MAKEUP_ENABLED=false`;
3. загрузить контент через `POST /v1/admin/media/features/presets`;
4. включить нужный флаг только на выбранных инстансах.

Миграция ничего не засевает: PNG/JPEG/WebP из макета не являются исходными продуктовыми
ассетами. Пустой каталог при выключенном флаге — штатное состояние.

## Конфигурация

| Переменная | Дефолт | Назначение |
|---|---:|---|
| `AVATAR_SPEECH_ENABLED` | `false` | Каталог, сохранённые аватары и lip-sync |
| `MAKEUP_ENABLED` | `false` | Каталог и применение makeup |
| `AVATAR_SPEECH_CREDIT_COST` | `10` | Цена готового lip-sync video |
| `MAKEUP_CREDIT_COST` | `10` | Цена применения makeup |
| `USER_AVATAR_MAX_COUNT` | `20` | Потолок сохранённых аватаров пользователя |

Avatar speech требует одновременно `FAL_API_KEY` и `OPENAI_API_KEY`; makeup — `FAL_API_KEY`.
[ADR-108](../../adr/ADR-108-media-generation-via-proxy.md): на инстансе с `PROXY_API_KEY` задачи `rembg` / `sync-lipsync` / `makeup-application` ставятся через прокси, маршрут только `fal`, payload прежний; `FAL_API_KEY` по-прежнему обязателен — загрузки аватара, аудио и фото идут в хранилище fal. Completion handler подготовки аватара вызывается общим путём завершения — и из вебхука, и из опроса. [ADR-109](../../adr/ADR-109-media-asset-local-storage-30d.md) (код в `main` (`f90d871`), выкачен (CI `36015691825` на `48f9018`, джоб `ssh deploy` — `success`)): результаты features-задач (`completed` с ассетами) хранятся на диске инстанса наравне с генерациями; completion handler работает до сохранения и своей копией не пользуется.
Неположительная цена безопасно деградирует до 10.

## Каталоги и пользовательские аватары

- `GET /v1/media/avatars?gender=&style=` — системные аватары и фильтры male/female/style.
- `GET /v1/media/avatar-backgrounds` — серверные фоны.
- `GET /v1/media/makeup/presets` — makeup-пресеты и цена.
- `GET /v1/media/avatar-speech/options` — языки, moods, voices, цена и готовность провайдеров.
- `POST|GET /v1/media/user-avatars` — сохранить фото или копию системного аватара и получить
  каталог для повторного использования.
- `POST /v1/media/user-avatars/{id}/prepare` — удалить фон и опционально поставить цвет,
  серверный фон или загруженную картинку.
- `DELETE /v1/media/user-avatars/{id}` — удалить сохранённый аватар.

Подготовка фона — бесплатная скрытая `media_job` (`visible_in_history=false`). Клиент получает
`preparationJobId` и опрашивает обычный `GET /v1/media/jobs/{jobId}`. После `completed` байты
подготовленного PNG сохранены в `user_avatars`, поэтому повторное использование не зависит от
срока жизни fal URL. При отказе задача не попадает в историю и аватар возвращается к исходному
фото вместо вечного `preparing`.

## Avatar speech

`POST /v1/media/avatar-speech` принимает ровно один источник — `systemAvatarId` либо
`userAvatarId` — и `text` (до 300 символов), `language`, `voiceId`, `mood`.

`language` задаёт произношение синтезатору; сервер явно запрещает переводить или переписывать
текст. Полученный audio загружается в fal и вместе с изображением отправляется в
`fal-ai/sync-lipsync/v3/image-to-video`. Ответ — обычная video-задача за 10 кредитов; ошибка
провайдера возвращает их существующим механизмом `media-refund:{jobId}`.

## Makeup

`POST /v1/media/makeup` принимает фото в base64 и `presetId`, загружает проверенное изображение
и ставит `fal-ai/image-apps-v2/makeup-application` в очередь. Цена — 10 кредитов. Значение
`providerValue` хранится только в операторском пресете, поэтому клиент не управляет upstream
параметрами. Для плитки **No makeup** оператор должен создать makeup-пресет с
`providerValue: "remove_makeup"`: это удаление существующего макияжа, а не no-op.

## Admin API

`POST /v1/admin/media/features/presets` принимает `feature`:

- `avatar`: обязательны `gender` и `style`;
- `background`: картинка фона;
- `makeup`: обязателен `providerValue`.

`DELETE /v1/admin/media/features/presets/{id}` удаляет плитку. Все admin-маршруты защищены
существующим `X-Admin-Token`; изображения ограничены общим `MEDIA_UPLOAD_MAX_BYTES`.
