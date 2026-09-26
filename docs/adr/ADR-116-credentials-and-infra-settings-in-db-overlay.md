# ADR-116 — Креденшлы и инфраструктурно-продуктовые настройки инстанса в БД-оверлее, управляемые из CRM без перезапуска

- **Статус:** Accepted. **Состояние реализации, поэлементно, снято 2026-09-26 на `f38c9e6`:** (1) **код** — не написан: таблицы `admin_credentials` нет (`grep -n 'admin_credentials' src/app/models/tables.py` пуст), реестр `src/app/instance_config/settings_registry.py` объявляет прежние 14 строк; (2) **слито в `main` / выкачено** — нечего; (3) **ревью** — не измеряется этим проходом, переносится без изменений. До реализации действует [ADR-099 §8](ADR-099-crm-admin-economics-and-instance-settings.md) в прежней редакции.
- **Дата:** 2026-09-26
- **Супессирует частично:** [ADR-099 §8](ADR-099-crm-admin-economics-and-instance-settings.md) — правило отбора поверхности (предикат `CRM ADR-110 §4`, применённый поимённо) и классы §8.2 **(а)** (часть креденшлов), **(д)** (часть параметров StoreKit/CloudPayments), **(ж)** (`LLM_PROVIDER`/`LLM_PROVIDERS`) — в объёме §1 ниже. Тело ADR-099 не переписывается; в его шапке стоит ссылка сюда. Остальное в ADR-099 (оверлеи, снимок, `reason`, 14 объявленных строк, классы (б), (в), (г), (е), (з)) действует без изменений.
- **Сторона CRM:** та же норма в `broad-crm` живёт в **broad-crm ADR-110 §4** («Что в эту поверхность НЕ входит и почему»); её супессия — обязанность ADR `broad-crm`, который проектируется после этого (нумерация репозиториев независима). Новая версия контракта CRM для `/v1/admin/credentials` получает номер там же — следующий после максимального занятого **по всему `broad-crm`** (v1.3 живёт в модуле `backend-costs`, а не рядом с v1.1/v1.2/v1.4/v1.5).
- **Парный ADR:** [ADR-115](ADR-115-crm-managed-instance-and-server-lifecycle.md) (жизненный цикл; контракт `.env`, класс E3 — отсюда).
- **Модуль:** [admin](../modules/admin/README.md); затронуты [chat-orchestrator](../modules/chat-orchestrator/README.md), [subscription](../modules/subscription/README.md), [billing-adapty](../modules/billing-adapty/README.md), [billing-cloudpayments](../modules/billing-cloudpayments/README.md), [media-generation](../modules/media-generation/README.md), [byok](../modules/byok/README.md) (переиспользуется шифрование).

## Контекст

### Решения владельца (2026-09-26), дословно; пересмотру не подлежат

- **(Р6)** StoreKit: «Sandbox - отключена вся проверка подписи транзакции, Production -bundle».
- **(Р7)** «Правило ADR-110 думаю можно исправить. Провайдера, ключи, CloudPayments, Adapty, fal можно будет менять из CRM правкой в БД без перезапуска инстанса».
- Из сценария создания ([ADR-115 §Контекст](ADR-115-crm-managed-instance-and-server-lifecycle.md)): форма создания несёт «режим dual, или определенный провайдер, какие ключи Open Ai, Anthropic, backup (выбор из ключей из Срм), какие тулзы включать, генерацию видео и ключ от прокси сервера, ключ от fal, поиск по rag, режим Sandbox … Prod (с загрузкой bundle). Какие продукты, настройка Cloudlayments», позже редактируется на странице «Продукты и тарифы».

⚠️ **«ADR-110» в (Р7) — это broad-crm ADR-110 §4**, а не `claude-ios` [ADR-110](ADR-110-ru-payment-neutral-path-aliases.md) (нейтральные пути RU-оплаты, к настройкам отношения не имеет). В `claude-ios` эта норма применена в [ADR-099 §8](ADR-099-crm-admin-economics-and-instance-settings.md) под именем «предикат `CRM ADR-110 §4`».

### Что разворачивается

Правило ADR-099 §8: в поверхность входит только то, что задаёт продуктовое поведение **И** безопасно при **любом** значении; не входит credential, адрес/параметр инфраструктуры, ресурсный лимит. Три обоснования (broad-crm ADR-110 §4 (а)(б)(в), воспроизведены в ADR-099 §8.2 (а)): **(а)** секрет в БД ложится рядом с пользовательскими данными — дамп базы уносит и ключи; **(б)** мастер-ключ BYOK рядом с шифротекстом обнуляет шифрование; **(в)** `X-Admin-Key` поднимался бы с «правит цены» до «выпускает токены за любого пользователя». Решение владельца (Р7) снимает запрет для провайдера и ключей провайдеров, CloudPayments, Adapty, fal, прокси; (Р6) вводит управляемый режим StoreKit. Ниже — какие обоснования и чем закрываются, а какие остаются в силе и оставляют часть величин вне поверхности.

## Решение

### §1. Новое правило отбора (заменяет правило ADR-099 §8 в объёме этого ADR)

**Из CRM управляется то, что владелец выбирает для конкретного инстанса при его создании и эксплуатации** — продукт, провайдер и его ключи, платёжные интеграции, режим проверки покупок, набор инструментов. **Не управляется из CRM никогда:**

1. **материал подписи и шифрования**, от которого зависит защита данных этой же БД или личность пользователей — класс (E2) [ADR-115 §5](ADR-115-crm-managed-instance-and-server-lifecycle.md): `KMS_LOCAL_MASTER_KEY`/`KMS_KEY_ID`, `JWT_PRIVATE_KEY`/`JWT_PRIVATE_KEY_PATH`, `ADMIN_API_SECRET`/`ADMIN_API_SECRET_PREV`/`ADMIN_API_KEY`, `PREVIEW_URL_SECRET`, `METRICS_SCRAPE_TOKEN`, `PROXY_WEBHOOK_SECRET`, `APNS_AUTH_KEY`/`APNS_AUTH_KEY_PATH`, `APPLE_TEST_SECRET`, `STOREKIT_TEST_SECRET`. Обоснования (б) и (в) broad-crm ADR-110 §4 для них **остаются в силе дословно**. Прочие `JWT_*` и `APNS_*` — не материал подписи, а параметры проверки личности (ADR-099 §8.2 (д)), их судьба — в §7;
2. **адресация и инфраструктура** — класс (E1) ADR-115 §5 и ADR-099 §8.2 (б);
3. **ресурсные лимиты процесса** — ADR-099 §8.2 (в).

**Названное исключение — `ADAPTY_WEBHOOK_SECRET`.** Утечка этого секрета позволяет подделать вебхук начисления — по последствию он соседствует с `PROXY_WEBHOOK_SECRET` из п. 1. В поверхность он входит **по решению владельца (Р7: «Adapty»)**, и граница с соседом проводится по признаку, а не по удобству. `PROXY_WEBHOOK_SECRET` — ключ, которым инстанс **сам** подписывает `callbackUrl` (HMAC, [ADR-108](ADR-108-media-generation-via-proxy.md) §4.1): он не покидает инстанс, и знать его больше никому не нужно. `ADAPTY_WEBHOOK_SECRET` — **общий bearer-секрет**: владелец вручную вписывает его в консоль Adapty и видит в карточке CRM (сценарий создания), то есть секрет уже живёт вне инстанса. Признак п. 1 в формулировке [ADR-115 §5](ADR-115-crm-managed-instance-and-server-lifecycle.md) (E2) уточнён так же: E2 — только материал, которым сервис подписывает или шифрует сам и который инстанс не покидает.

**Креденшлы, попавшие в поверхность, — только на запись и только зашифрованными** (§2): так закрывается обоснование (а) и суживается (в) — см. §6.

**Предикат в обе стороны.** Против недооценки: величина из пп. 1–3 в поверхность не входит, как бы её ни хотелось менять из CRM. Против переоценки: величина, которую владелец меняет вручную в `.env` при настройке инстанса и которая не подпадает под пп. 1–3, — кандидат, и её отсутствие в поверхности должно быть объяснено в §5.

### §2. Креденшлы: хранение, запись, чтение обратно

#### §2.1. Перечень (8)

| `credential_id` | Переменная (и алиасы, `src/app/config.py`) | Точки применения (снимок; перечень — не граница, свип по имени поля) |
|---|---|---|
| `openai.api_key` | `OPENAI_API_KEY` | `chat/openai_client.py` (`_service_key`), `chat/speech.py`, `chat/transcription.py`, `memory/embedding.py`, `api_gateway/routers/chat_voice.py`, `Settings._credits_api_key_configured` / `credits_providers()` |
| `openai.api_key_backup` | `OPENAI_API_KEY_BACKUP` (алиас `OPEN_AI_BACK_UP_API_KEY`) | ротация ключей [ADR-074](ADR-074-provider-key-failover.md) |
| `anthropic.api_key` | `ANTHROPIC_API_KEY` | `chat/anthropic_client.py` (`_service_key`), `credits_providers()` |
| `anthropic.api_key_backup` | `ANTHROPIC_API_KEY_BACKUP` (алиас `ANTHROPIC_FALLBACK_API_KEY`) | [ADR-074](ADR-074-provider-key-failover.md) |
| `fal.api_key` | `FAL_API_KEY` | `media_generation/fal_client.py`; гейт генерации [ADR-108 §1](ADR-108-media-generation-via-proxy.md) |
| `proxy.api_key` | `PROXY_API_KEY` | `media_generation/proxy_client.py`, `media_generation/webhook.py`. ⚠️ При пустом `PROXY_WEBHOOK_SECRET` этот ключ служит **и ключом подписи** `callbackUrl` ([ADR-108](ADR-108-media-generation-via-proxy.md): «пусто → `PROXY_API_KEY`»), и его смена из CRM сломала бы колбэки задач в полёте. Поэтому `PATCH proxy.api_key` при пустом действующем `PROXY_WEBHOOK_SECRET` отвергается: `400`, `reason=environment_missing` (§4.3). На инстансах [ADR-115](ADR-115-crm-managed-instance-and-server-lifecycle.md) `PROXY_WEBHOOK_SECRET` генерируется всегда (E2) |
| `cloudpayments.api_token` | `CLOUDPAYMENTS_API_TOKEN` | `billing_cloudpayments/{checkout,verify,service,experiments}.py`, `api_gateway/routers/auth.py` |
| `adapty.webhook_secret` | `ADAPTY_WEBHOOK_SECRET` | `billing_adapty/auth.py` |

**Не входят (с причиной):** `MODERATION_API_KEY` — владелец его не называл; при пустом значении модерация и так использует `OPENAI_API_KEY` ([05-security.md §Секреты и ключи](../05-security.md#секреты-и-ключи)), то есть следует за оверлеем. `CLOUDPAYMENTS_WEBHOOK_TOKEN` — легаси, вебхук больше не гейтит ([ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md)). Материал (E2) — §1 п. 1.

#### §2.2. Хранение — та же схема, что BYOK

Новая таблица `admin_credentials` (миграция expand-only, без backfill; номер ревизии — следующий после максимального в `migrations/versions/`, выделяет исполнитель):

| Колонка | Тип | Смысл |
|---|---|---|
| `credential_id` | `text` PK | из §2.1; неизвестный — не пишется |
| `encrypted_value` | `bytea NOT NULL` | значение, AES-256-GCM под DEK |
| `encrypted_dek` | `bytea NOT NULL` | DEK, зашифрованный `KmsClient` (`src/app/byok/kms.py`, `LocalKmsClient` под `KMS_LOCAL_MASTER_KEY`) |
| `fingerprint` | `text NOT NULL` | §2.4 |
| `updated_at` | `timestamptz NOT NULL default now()` | |

Схема — envelope encryption [ADR-003](ADR-003-byok-envelope-encryption.md), та же, что у `byok_keys` (`encrypted_key`/`encrypted_dek`). **Мастер-ключ остаётся в `.env`** и в БД не попадает: дамп базы или бэкап ([ADR-115 §8](ADR-115-crm-managed-instance-and-server-lifecycle.md), вдобавок зашифрованный ключом бэкапов) без `.env` значений не раскрывает. Пустое значение (§2.3) хранится как зашифрованная пустая строка — отличие «явно пусто» от «не задано» держится наличием строки.

#### §2.3. Запись

**`PATCH /v1/admin/credentials/{credential_id}`**, тело `{"value": <string | null>}`:

- строка (в т.ч. пустая `""`) — записать в оверлей. Пустая строка = **явно выключено** и перекрывает `.env` (`FAL_API_KEY=""` — легитимное «генерации нет», `provision.sh`);
- `null` — **удалить строку оверлея**: величина снова берётся из `.env` (или дефолта);
- ответ `200`: элемент §2.4 + `changed` (bool) + `effective_after_seconds` (int, из `ADMIN_OVERRIDES_REFRESH_SECONDS`, как у `PATCH /settings` [ADR-099](ADR-099-crm-admin-economics-and-instance-settings.md));
- исходы отказа — **одна ветка = один `reason` и один код**, по разбиению [ADR-099 §10.0](ADR-099-crm-admin-economics-and-instance-settings.md) и правилу кода [ADR-099 §11](ADR-099-crm-admin-economics-and-instance-settings.md) (`422` — нарушено наше объявление, `400` — правило, которого в объявлении нет и быть не может):

  | Ветка | Код | `reason` |
  |---|---|---|
  | неизвестный `credential_id` | `400` | `unknown_id` |
  | `value` не строка и не `null` | `422` | `type_mismatch` |
  | длина больше объявленного `constraints.max_length` (512) | `422` | `out_of_range` |
  | в значении есть пробельный или управляющий символ — ограничение **не объявлено** и объявить его нечем: замороженный набор ключей `constraints` — `max_length`/`min_items`/`max_items` (broad-crm ADR-110 §5) | `400` | `undeclared_bound` |
  | межэлементный инвариант §4.3 | `400` | `conflict` |
  | в окружении инстанса нет величины, без которой запись опасна (§4.3, `proxy.api_key`) | `400` | `environment_missing` |

- проверка ключа сетевым вызовом провайдера **не делается** (ключ может быть заведён до пополнения счёта) — Q-116-2;
- авторизация, корзина лимита `rl:admin_econ`, правило «сначала факт, потом ответ» — как у ручек ADR-099.

#### §2.4. Чтение обратно — только метаданные, значения — никогда

**`GET /v1/admin/credentials`** → `{"items": [...]}`, элемент: `credential_id`, `label`, `group`, `description`, `constraints` (`{max_length}`), `configured` (bool — действующее значение непусто), `source` (`overlay` \| `env` \| `unset`), `fingerprint` (str \| null), `updated_at` (ISO \| null). Самоописываемо, как `/settings`.

**`fingerprint`** = первые 12 шестнадцатеричных символов SHA-256 от UTF-8 значения (для `source` `overlay` и `env`; `null` при `unset`).

**Почему значения не отдаются:**
1. **Источник ключей — CRM** («выбор из ключей из Срм»): ей незачем читать то, что она сама записала; чтобы знать, **какой** из её ключей стоит на инстансе, ей достаточно сравнить `fingerprint` с хэшем своего экземпляра.
2. **Компрометация `X-Admin-Key` остаётся «может заменить», а не «может похитить»** (§6). Ручка, отдающая значения, превратила бы один ключ CRM в доступ к ключам провайдеров всего флота.
3. Ответ admin API проходит через логи прокси и CRM; значения в нём — утечка по третьему каналу.

12 символов хэша (48 бит) от ключа высокой энтропии не дают восстановить значение; величин низкой энтропии в §2.1 нет (`adapty.webhook_secret` генерирует CRM, [ADR-115 §5](ADR-115-crm-managed-instance-and-server-lifecycle.md)).

#### §2.5. Аудит и логи

Действия `admin_credential_set` и `admin_credential_cleared` (в стиле действий ADR-099); деталь — `credential_id`, `source` до→после, `fingerprint` до→после. **Значение — ни в аудит, ни в лог, ни в метрику, ни в текст отказа.** Все имена полей подпадают под денилист redaction (`key`/`secret`/`token`).

#### §2.6. Как CRM узнаёт, что инстанс поддерживает `/credentials`

Инстансы обновляются не одновременно: деплой идёт порциями по серверам ([ADR-115 §10.2](ADR-115-crm-managed-instance-and-server-lifecycle.md) п. 3), и в окне прогона часть флота уже отдаёт `/v1/admin/credentials`, а часть — нет. Поэтому версия контракта CRM, вводящая эту поверхность, **не атомарна для флота**, и признак поддержки определяется **поинстансно** — тем же механизмом, что остальные возможности: в `features` ответа `GET /v1/admin/capabilities` (перечень `FEATURES`, `src/app/admin/economics_service.py:121-130`) добавляются `credentials.read` и `credentials.write`. Правило fail-closed то же: значение объявляется только там, где путь реализован. `contract_version` не меняется — поля добавляются, ни одно не меняет смысла.

- Нет `credentials.read` → CRM не показывает раздел креденшлов и не зовёт ручки. Ключи инстанса остаются в `.env` до следующего деплоя, который доставит поддержку — CI доводит до неё весь флот (ADR-115 §10.2 п. 6).
- Нет `credentials.write` на шаге 6 создания ([ADR-115 §7.1](ADR-115-crm-managed-instance-and-server-lifecycle.md)) → шаг падает с причиной «инстанс не поддерживает запись креденшлов», дальше откат §7.3. Практически недостижимо: новый инстанс создаётся на `last_deployed_sha`.
- Семь новых строк `/v1/admin/settings` (§4) признака не требуют: поверхность самоописываема, и строки, которых инстанс не знает, в его ответе просто отсутствуют.

### §3. Adapty: URL и секрет

`adapty.webhook_secret` генерирует CRM при создании инстанса (32 случайных байта) и пишет через §2.3 шагом 6 [ADR-115 §7.1](ADR-115-crm-managed-instance-and-server-lifecycle.md); CRM хранит свой экземпляр, чтобы показать его в примечаниях карточки вместе с ADAPTY URL `https://<домен>/v1/billing/adapty/webhook`. Смена секрета — та же запись; вебхук, пришедший со старым секретом в окне `effective_after_seconds`, получает `401` и повторяется Adapty.

### §4. Настройки, добавляемые в `GET /v1/admin/settings`

Механизм — существующий ([ADR-099](ADR-099-crm-admin-economics-and-instance-settings.md) §8.1, `admin_settings`, самоописание, `PATCH /v1/admin/settings/{setting_id}`). Реестр растёт с 14 до **21**.

#### §4.1. Перечень

| `setting_id` | `type` | `options` / `constraints` | Источник дефолта (env) | Точка применения |
|---|---|---|---|---|
| `llm.provider` | `enum` | `openai`, `anthropic` | `LLM_PROVIDER` | `Settings._normalized_llm_provider()` / `credits_providers()`, `chat/llm_client.py` (`get_llm_client`, `get_generation_llm_client`), `byok/service.py`, `api_gateway/routers/chat.py` |
| `llm.dual_enabled` | `bool` | — | `true`, если `LLM_PROVIDERS` называет второй провайдер | `credits_providers()` ([ADR-073](ADR-073-dual-credits-llm-providers.md)): `true` ⇔ второй провайдер = тот из `openai`/`anthropic`, что не `llm.provider` |
| `storekit.mode` | `enum` | `sandbox`, `production` | `production`, если `APPSTORE_ENVIRONMENT=production`, иначе `sandbox` | `subscription/storekit.py` (`StoreKitVerifier`), §4.2 |
| `storekit.bundle_id` | `string` | `max_length: 255` | `APPSTORE_BUNDLE_ID` | проверка `bundleId` транзакции (§4.2); `apple_audience_resolved()` (Sign in with Apple, фолбэк при пустом `APPLE_AUDIENCE`) |
| `cloudpayments.app_id` | `string` | `max_length: 128` | `CLOUDPAYMENTS_APP_ID` | `billing_cloudpayments/{checkout,experiments}.py`; гейт RU-пути (`config.py`: `app_id ∧ api_token`) |
| `cloudpayments.pay_page_proxy_enabled` | `bool` | — | `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` | `billing_cloudpayments/pay_page.py`, `api_gateway/routers/cloudpayments_pay_page.py` ([ADR-113](ADR-113-ru-payment-page-proxy-on-instance-domain.md)) |
| `chat.maps_tools_enabled` | `bool` | — | `MAPS_TOOLS_ENABLED` | `chat/orchestrator.py` (гейт оси E [ADR-102](ADR-102-mapkit-client-tools.md)) |

«Генерация видео» и «поиск по RAG» из сценария владельца **уже** в поверхности: `chat.media_tools_enabled` (+ гейт `/v1/media/*` = `proxy.api_key` ∨ `fal.api_key`, §2.1) и `chat.memory_enabled`. «Какие продукты» — `/products` ADR-099. `chat.maps_tools_enabled` принадлежит тому же классу, что уже объявленный `chat.code_tools_enabled` (клиентские инструменты, которые включают только там, где приложение их реализовало), — по правилу §1 он кандидат и включается.

#### §4.2. StoreKit-режим (Р6)

| Действующее значение | `sandbox` | `production` |
|---|---|---|
| `APPSTORE_ENVIRONMENT` | `sandbox` | `production` |
| Привязка цепочки x5c к корню Apple | **нет** (`STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION=true`) | **да** (корни из `APPSTORE_ROOT_CERT_DIR`) |
| Тестовая ветка HS256 | вкл. (`STOREKIT_TEST_MODE=true`; действует, только если задан `STOREKIT_TEST_SECRET`) | **выкл.** |
| Проверка `bundleId` транзакции | **нет** (независимо от `storekit.bundle_id`) | **да**, против `storekit.bundle_id` |

- **`sandbox` = нынешняя база флота** (`infra/fleet/provision.sh` режим `new`: `APPSTORE_ENVIRONMENT=sandbox`, `STOREKIT_TEST_MODE=true`, `STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION=true`, `APPSTORE_BUNDLE_ID=""`). **Почему это и есть «отключена вся проверка подписи»:** без привязки к корню Apple подпись проверяется ключом листового сертификата, лежащего **в самой транзакции** (`_verify_real_transaction`, `src/app/subscription/storekit.py`), — транзакция, подписанная любым самодельным сертификатом, проходит. Проверки подлинности нет; остаётся только проверка внутренней целостности JWS. Буквальный режим «не разбирать подпись вовсе» не вводится: он не снимает ни одной проверки подлинности сверх уже снятых и добавил бы третью ветку кода. Трактовка подтверждена владельцем 2026-09-26: «ДА, все верно архитектор понял» ([Q-116-1](../99-open-questions.md) закрыт).
- **`storekit.bundle_id` в `sandbox` сохраняется**, хоть и не проверяется у транзакций: он же — фолбэк аудитории Sign in with Apple. Сегодняшнее обнуление `APPSTORE_BUNDLE_ID` в песочнице отключало и этот фолбэк; с разделением проверки и значения фолбэк работает в обоих режимах.
- **Код:** `StoreKitVerifier` читает режим из действующих настроек (§5) и **пересоздаётся** при их смене (сегодня — синглтон, собираемый один раз: `get_storekit_verifier()`, `storekit.py`). Существующая защита «флаг пропуска цепочки игнорируется при `production`» (`storekit.py`, `_skip_chain_verification`) сохраняется как второй барьер.

#### §4.3. Межэлементные инварианты (отказ `400`, `reason=conflict`)

Правки, после которых инстанс перестал бы работать, отвергаются **до** записи:

| Правка | Отказ, если |
|---|---|
| `llm.provider = X` | у `X` нет действующего ключа |
| `llm.dual_enabled = true` | у второго провайдера нет действующего ключа |
| `PATCH credentials/{X.api_key}` в `""` либо `null` (для `null` — если `.env` тоже пуст) | `X` — `llm.provider` либо второй провайдер при `llm.dual_enabled = true`, и после правки у него нет действующего ключа |
| `storekit.mode = production` | `storekit.bundle_id` пуст |
| `storekit.bundle_id = ""` | `storekit.mode = production` |
| `PATCH credentials/proxy.api_key` | действующий `PROXY_WEBHOOK_SECRET` пуст (ключ прокси служит ключом подписи колбэков, §2.1) — **`400`, `reason=environment_missing`** |
| `storekit.mode = production` | корневые сертификаты Apple не загружены (`APPSTORE_ROOT_CERT_DIR` пуст) — **`400`, `reason=environment_missing`** (новое значение, см. ниже) |

**«Действующий ключ» определён ОДИН раз — так, как его уже определяет код:** непустой **основной** ключ провайдера (`X.api_key`) после наложения оверлея, ровно предикат `Settings._credits_api_key_configured` (`src/app/config.py:1404-1407`), по которому `credits_providers()` (`:1421-1439`) решает, включать ли второй провайдер. Резервный ключ (`X.api_key_backup`) действующим **не считается**: код строит второй провайдер только при непустом основном, а резервный работает лишь как второе звено цепочки ротации (`openai_api_key_chain()`, `anthropic_api_key_chain()`, [ADR-074](ADR-074-provider-key-failover.md)). Меняется сторона документа, код остаётся прежним; правка резервного ключа инварианта не нарушает никогда.

**`environment_missing` — новое место несоответствия, а не похожий случай** (правило [ADR-099 §10.0](ADR-099-crm-admin-economics-and-instance-settings.md): новое место расширяет перечень одним значением). Предикат: форма, объявленные и необъявленные границы, данные источника и соседние элементы — в порядке, но в **окружении инстанса** (файл на сервере или величина `.env`, которую CRM не правит) нет того, без чего значение нерабочее или опасное. Производителей два: корневые сертификаты Apple для `storekit.mode = production` и `PROXY_WEBHOOK_SECRET` для `PATCH proxy.api_key`. Отнести к `conflict` нельзя: лечится не правкой другого элемента в CRM, а доступом к серверу. [ADR-115 §5](ADR-115-crm-managed-instance-and-server-lifecycle.md) требует класть сертификат и генерировать `PROXY_WEBHOOK_SECRET` всегда, поэтому на инстансах ADR-115 обе ветки недостижимы — они защищают унаследованные инстансы.

**Смена провайдера и модельные строки.** После `llm.provider = X` сохранённые `chat.default_model`/`chat.models_offered`, не входящие в новый `allowed_models_union()`, **игнорируются** существующим разбором снимка (`coerce_stored_setting_value`, лог `admin_override_value_ignored` (`_log_ignored_setting`)) — действует дефолт кода нового провайдера, пока оператор не выберет модели заново. Удаления строк и отказа нет: отказ запер бы переключение (модель нового провайдера нельзя выбрать до переключения). ⚠️ **Порядок сборки снимка обязателен:** строки `llm.*` и креденшлы накладываются **до** проверки модельных строк, и `coerce_stored_setting_value` получает уже действующие настройки — иначе модели проверялись бы против провайдера из `.env`, а не выбранного в CRM.

### §5. Приоритет и применение без перезапуска

- **Порядок разрешения — оверлей → env → дефолт кода**, тот же, что [ADR-099 §2](ADR-099-crm-admin-economics-and-instance-settings.md). ⚠️ Следствие, названное явно: после первой записи величины из CRM правка той же переменной в `.env` перестаёт действовать, пока строка оверлея не удалена (`null` в §2.3 / удаление настройки).
- **Единая точка резолва:** «действующие настройки» = `get_settings()` с наложенными значениями оверлея **для величин этого ADR** (копия объекта `Settings`, пересобираемая при смене снимка). Все потребители величин §2.1 и §4.1 читают **её**, а не `get_settings()`: методы `Settings` (`credits_providers()`, `apple_audience_resolved()`, гейт RU-пути) тогда работают на действующих значениях без дублирования логики. Прямое чтение `get_settings().<поле>` для величины этого ADR после реализации — дефект того же класса, что «объявлено ≠ подключено».
- **Снимок** ([ADR-099 §2](ADR-099-crm-admin-economics-and-instance-settings.md), `src/app/instance_config/snapshot.py`) дополнительно читает `admin_credentials` и расшифровывает значения в памяти процесса — ровно там, где сегодня живут значения из `.env`. Окно применения — до `ADMIN_OVERRIDES_REFRESH_SECONDS` (дефолт 30 с) на каждый воркер; наружу — `effective_after_seconds`.
- **Клиенты-синглтоны пересоздаются по отпечатку входов:** клиенты, захватывающие ключ при создании (`_anthropic_singleton`, `_openai_singleton` в `chat/llm_client.py`/`chat/anthropic_client.py`, `get_speech_client()`, `get_embedding_client()`, `get_moderation_service()` в `deps.py`/`memory/embedding.py`, `StoreKitVerifier`), при смене отпечатка своих входов создаются заново при следующем обращении; вызов, уже идущий на старом клиенте, завершается на нём. Клиенты, создаваемые на вызов (`FalClient`, `ProxyClient` в `deps.py`, `TranscriptionClient`), получают действующие настройки без дополнительной меры.
- **Отказ расшифровки строки** (мастер-ключ сменён, строка повреждена): строка игнорируется, лог `admin_credential_undecryptable` (ERROR) + счётчик; действует **прежнее** значение снимка, если оно было (правило ADR-099 «отказ обновления не откатывает»), иначе — env. Переход на env при холодном старте назван: он может переключить инстанс на старый ключ из `.env` — поэтому тревога по счётчику обязательна.

### §6. Что остаётся от обоснований broad-crm ADR-110 §4 — честный баланс

- **(а) секреты рядом с пользовательскими данными** — закрыто шифрованием: в БД и бэкапах только шифротекст, ключ — в `.env`. Остаточный риск: похищение **и** БД, **и** `.env` одного инстанса раскрывает его креденшлы — ровно как сегодня похищение одного `.env`.
- **(б) мастер-ключ рядом с шифротекстом** — **не затронуто**: мастер-ключ в поверхность не входит (§1 п. 1).
- **(в) расширение возможностей `X-Admin-Key`** — **сужено, но не снято, и это последствие решения владельца.** Ключ CRM теперь может: заменить ключ провайдера (трафик и оплата уходят на чужой аккаунт; ключи **не** читаются, §2.4); переключить провайдера; **перевести StoreKit в `sandbox`, после чего покупки подделываются любым самодельным сертификатом и начисляют кредиты** — это денежный риск; сменить секрет вебхука Adapty. Материал подписи JWT и мастер-ключ по-прежнему недостижимы. Меры: аудит каждой смены (§2.5), лог-событие `admin_storekit_mode_changed` уровня WARNING на переход в `sandbox`. Отдельного admin-ключа для этой поверхности **нет** — решение владельца 2026-09-26 ([Q-116-3](../99-open-questions.md) закрыт: «Нет»); риск принят владельцем и записан в [05-security.md](../05-security.md#секреты-и-ключи).

### §7. Что НЕ входит в поверхность и после этого решения

**Сплошной свип по классам ADR-099 §8.2, которые этот ADR затрагивает** (перечни классов — дословно из ADR-099 §8.2; по каждому члену указано, переходит он в поверхность или остаётся в `.env`):

| Класс ADR-099 §8.2 | Переходит в поверхность (§2.1, §4.1) | Остаётся в `.env` — почему |
|---|---|---|
| **(а)** креденшлы | `OPENAI_API_KEY`, `OPENAI_API_KEY_BACKUP` (+ алиас `OPEN_AI_BACK_UP_API_KEY`), `ANTHROPIC_API_KEY`, `ANTHROPIC_API_KEY_BACKUP` (+ алиас `ANTHROPIC_FALLBACK_API_KEY`), `FAL_API_KEY`, `PROXY_API_KEY`, `CLOUDPAYMENTS_API_TOKEN`, `ADAPTY_WEBHOOK_SECRET` (исключение §1) | материал подписи и шифрования §1 п. 1 — `PROXY_WEBHOOK_SECRET`, `JWT_PRIVATE_KEY`/`JWT_PRIVATE_KEY_PATH`, `KMS_LOCAL_MASTER_KEY`/`KMS_KEY_ID`, `ADMIN_API_SECRET`/`ADMIN_API_SECRET_PREV`/`ADMIN_API_KEY`, `PREVIEW_URL_SECRET`, `METRICS_SCRAPE_TOKEN`, `APNS_AUTH_KEY`/`APNS_AUTH_KEY_PATH`, `APPLE_TEST_SECRET`, `STOREKIT_TEST_SECRET`; `MODERATION_API_KEY` — владелец не называл, при пустом значении следует за `OPENAI_API_KEY` (§2.1); `CLOUDPAYMENTS_WEBHOOK_TOKEN` — легаси (§2.1) |
| **(д)** параметры проверки личности и платежей | `APPSTORE_ENVIRONMENT`, `STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION`, `STOREKIT_TEST_MODE` (все три — производные `storekit.mode`, §4.2), `APPSTORE_BUNDLE_ID` (`storekit.bundle_id`), `CLOUDPAYMENTS_APP_ID` (`cloudpayments.app_id`) | `APPSTORE_ROOT_CERT_DIR` — путь внутри контейнера, задаётся compose (`docker-compose.prod.yml`), инфраструктура; `APPLE_AUDIENCE` — типовой случай закрыт фолбэком на `storekit.bundle_id`; `APPLE_TEST_MODE` — тестовый бэкдор Sign in with Apple, владелец не называл; `JWT_ISSUER`, `JWT_AUDIENCE`, `JWT_KID`, `JWT_PUBLIC_KEY`/`JWT_PUBLIC_KEY_PATH`, `JWT_JWKS_CACHE_TTL`, `AUTH_JWKS_ENABLED` — параметры собственного issuer токенов, связаны с ключами §1 п. 1 и доменом (E1); `CLOUDPAYMENTS_PAID_STATUSES`, `CLOUDPAYMENTS_PAYMENT_FRESHNESS_HOURS` — решают, что считать оплатой, владелец не называл; `APNS_ENVIRONMENT`, `APNS_TOPIC`, `APNS_TEAM_ID`, `APNS_KEY_ID` — push-уведомления, владелец не называл |
| **(ж)** небезопасные при некоторых значениях | `LLM_PROVIDER`, `LLM_PROVIDERS` (`llm.provider`, `llm.dual_enabled`; опасное значение «провайдер без ключа» теперь отвергается инвариантом §4.3) | `CHAT_LEGACY_WEB_SEARCH_ENABLED` — ломает Anthropic-класс, владелец не называл; `TOKEN_PRODUCTS_PRICE_MINOR_UNITS` — зависит от сборки клиента |

Остальное — без изменений: (E1) и классы ADR-099 §8.2 (б), (в), (е); голосовые флаги — [Q-099-5](../99-open-questions.md); `TOKEN_PRODUCTS_DEFAULT` — [Q-099-6](../99-open-questions.md). `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` и `MAPS_TOOLS_ENABLED` в перечнях классов §8.2 не значились и добавлены в поверхность по признаку §1 (§4.1).

## Альтернативы

- **Креденшлы в `.env`, запись из CRM по SSH + перезапуск.** Нарушает (Р7) «без перезапуска»; перезапуск рвёт идущие ходы и голосовые сессии.
- **Отдавать значения креденшлов CRM на чтение.** Отвергнуто §2.4.
- **Хранить креденшлы в `admin_settings` (JSONB) открытым текстом.** Отвергнуто: оживляет обоснование (а) целиком.
- **Буквальное «без проверки подписи» в `sandbox`.** Отвергнуто §4.2; трактовка подтверждена владельцем (Q-116-1 закрыт 2026-09-26).
- **Отдельная ручка «запиши переменную по имени».** Отвергнута ещё ADR-099 (§Альтернативы): снимает границу поверхности; здесь граница — закрытый перечень §2.1 и §4.1.

## Последствия

- (+) Настройка и смена провайдера, ключей и платёжных интеграций — из CRM, без SSH и без перезапуска; ключи в БД и бэкапах только зашифрованными.
- (+) Sign in with Apple перестаёт зависеть от обнуления bundle в песочнице (§4.2).
- (−) Компрометация `X-Admin-Key` даёт денежный вектор через `storekit.mode` (§6 (в)) — риск принят владельцем 2026-09-26 (Q-116-3 закрыт: «Нет»).
- (−) Правка `.env` по величине, тронутой из CRM, молча не действует (§5) — названо в описаниях строк.
- (−) Второй источник значения ключа (оверлей и `.env`) — `source` в `GET /v1/admin/credentials` делает его наблюдаемым.

## Открытые вопросы

- **Q-116-2** — проверять ли ключ провайдера сетевым вызовом при записи.

Закрыты решениями владельца 2026-09-26: Q-116-1 (`sandbox` = нынешняя база флота), Q-116-3 (отдельного admin-ключа нет). Формулировки и дефолты — [99-open-questions.md](../99-open-questions.md#открытые-вопросы-жизненного-цикла-инстансов-и-серверов-2026-09-26-adr-115-adr-116).

## Фронт работ

1. **`backend`:** миграция `admin_credentials`; `credentials.read`/`credentials.write` в `FEATURES` (§2.6); расшифровка в снимке; «действующие настройки» и перевод всех потребителей величин §2.1/§4.1 на них (свип по имени поля, в т.ч. алиасов); пересоздание синглтонов по отпечатку; ручки `GET`/`PATCH /v1/admin/credentials`; 7 строк реестра настроек; инварианты §4.3 и значение `reason=environment_missing`; режим StoreKit §4.2; аудит и лог-события §2.5/§6.
2. **`qa`:** по каждому инварианту §4.3 — кейс отказа и кейс успеха; применение без рестарта (смена ключа видна новому вызову в окне `effective_after_seconds`); отсутствие значения в ответе, аудите и логах; `sandbox`/`production` — по четыре строки §4.2.
3. **`broad-crm`:** супессия broad-crm ADR-110 §4; определение поддержки по `features` (§2.6); новая версия контракта для `/v1/admin/credentials`; хранение ключей и сравнение по `fingerprint`.
