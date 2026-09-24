# ADR-113 — Платёжная страница broadapps открывается с домена инстанса (прокси `/cp/pay/*`)

- **Статус:** Accepted. **Состояние реализации, снято 2026-09-24T22:30Z (HEAD `d7a4295`), поэлементно:** (1) **код написан, не закоммичен** — `git status --porcelain -- src tests` даёт ` M` у `src/app/api_gateway/rate_limit.py`, `src/app/billing_cloudpayments/checkout.py`, `src/app/config.py`, `src/app/main.py`, `src/app/observability/logging.py`, `tests/integration/test_billing_cloudpayments_checkout.py` и `??` у `src/app/api_gateway/routers/cloudpayments_pay_page.py`, `src/app/billing_cloudpayments/pay_page.py` и трёх тест-файлов ниже; (2) **в `main` не слито** — коммита нет (та же команда); (3) **не выкачено** — следует из (2); (4) **автотесты** — `grep -c 'def test_'`: `tests/integration/test_cloudpayments_pay_url_rewrite_adr113.py` — 11, `tests/integration/test_cloudpayments_pay_page_proxy_adr113.py` — 30 (снято 2026-09-24T22:57Z), `tests/unit/test_cloudpayments_pay_page_access_log_adr113.py` — 5; покрытие не измерено; (5) **флаг `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` не включён ни на одном инстансе** (сообщение main chat, не измерено мной); (6) [Q-113-1](../99-open-questions.md), [Q-113-2](../99-open-questions.md), [Q-113-4](../99-open-questions.md) — открыты; (7) ревью — не измеряется, статус не утверждается.
- **Дата:** 2026-09-25
- **Связано:** [ADR-051](ADR-051-cloudpayments-checkout-payment-link.md) (контракт checkout; **§4 уточняется** этим решением — см. §2), [ADR-110](ADR-110-ru-payment-neutral-path-aliases.md) (дубликат `POST /v1/web/session` — тот же обработчик), [ADR-031](ADR-031-absolute-preview-url.md) (прецедент: абсолютный URL на домене инстанса из `SERVICE_DOMAIN`), [ADR-017](ADR-017-shared-server-traefik-deploy.md) (Traefik Host-роутер на `SERVICE_DOMAIN`), [ADR-054](ADR-054-cloudpayments-webhook-payment-verification.md) (per-IP лимитер публичного пути — образец), модуль [billing-cloudpayments](../modules/billing-cloudpayments/README.md).

## Контекст

**Проблема (со слов владельца):** «бекенд присылает именно pay.broadapps.dev как payment_url с любого под-домена … юзер видит pay.broadapps.dev когда платит». По домену платёжной страницы пользователь видит, что разные приложения — одни и те же. Требование: домен платёжной страницы не должен выдавать общность приложений.

**РЕШЕНИЕ владельца (2026-09-25, пересмотру не подлежит), дословно:** выбран вариант «проксировать страницу через свой домен»; домен — «**Домен API инстанса (Recommended)** — Бэкенд сам проксирует /cp/pay/* на своём домене. У каждого инстанса домен свой, поэтому связь между приложениями не видна. Фронтенд ничего не меняет.»

**Измерения (main chat, 2026-09-25; воспроизводятся командами, приведёнными в задании; здесь — результат):**
1. Домен `payment_url` зависит от провайдера, настроенного у приложения в broadapps. `POST /payments/link` по каждому продукту каждого приложения: elvarixa — 8 из 8 → `yoomoney.ru/checkout/payments/v2/contract`; lumirexa — 7 из 7 → `pay.tbank-online.com/<код>`; newweraar — 7 из 7 → `pay.broadapps.dev/cp/pay/<uuid>`; reluniqo — 7 из 7 → `pay.broadapps.dev/cp/pay/<uuid>`. Прочие инстансы не измерялись.
2. `GET https://pay.broadapps.dev/cp/pay/<uuid>` → `200 text/html`; ставит cookie `XSRF-TOKEN` и `pay-session` (`path=/`, без `Domain`, `secure; samesite=lax`); единственный внешний скрипт — `https://widget.cloudpayments.ru/bundles/cloudpayments.js`; в inline-скрипте `var returnUrl = "https:\/\/pay.broadapps.dev\/payment\/return";` (JSON-экранированные слеши) уходит в `successRedirectUrl`/`failRedirectUrl` виджета. Отсутствие ПРОЧИХ вхождений `pay.broadapps.dev` в HTML **не измерено** (поиск не видел экранированную форму).
3. `GET https://pay.broadapps.dev/payment/return` → `200 text/html`, подгружает `https://pay.broadapps.dev/main.css` и `https://pay.broadapps.dev/main.js`.
4. **Не проверено никем:** принимает ли виджет CloudPayments оплату со страницы, открытой с чужого домена (у CloudPayments бывает список разрешённых сайтов) — [Q-113-1](../99-open-questions.md).

**Факты кода, на которые опирается решение (HEAD `d7a4295`):** `SERVICE_DOMAIN` уже читается приложением (`Settings.service_domain`, `src/app/config.py`, и `Settings.normalized_service_domain()` — снимает схему и слеши; прецедент [ADR-031](ADR-031-absolute-preview-url.md) и [ADR-108 §1](ADR-108-media-generation-via-proxy.md)); Traefik-роутер инстанса — `Host(\`${SERVICE_DOMAIN}\`)` без `PathPrefix` (`docker-compose.prod.yml`), то есть любой путь домена инстанса доходит до приложения; `SecurityHeadersMiddleware` ставит `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Strict-Transport-Security` и **не** ставит CSP (`src/app/api_gateway/middleware.py`) — inline-скрипты страницы и скрипт виджета не блокируются; приложение не выдаёт собственных cookie (`grep -rni 'set_cookie' src/app` пуст).

**Предикат проблемы (позиция main chat, принята здесь как норма):** «ссылка выдаёт общность» ⇔ хост `payment_url` — хост платёжных страниц broadapps. Хосты `yoomoney.ru` и `pay.tbank-online.com` — общие для множества магазинов и общности приложений не выдают; они **не переписываются**.

## Решение

### 1. Хост платёжных страниц broadapps (upstream-хост) — один источник

Upstream-хост = хост `CLOUDPAYMENTS_API_BASE` (`urlsplit(settings.cloudpayments_api_base).hostname`; дефолт кода — `pay.broadapps.dev`). **Измерение флота (main chat, 2026-09-24, `grep -E '^CLOUDPAYMENTS_API_BASE='` по всем `/opt/*/.env` на appA и appB):** переменная задана на lumirexa, newweraar, probotit (на обоих серверах), qoravena, velunixa (appB) — везде ровно `https://pay.broadapps.dev/api/v1`; у прочих не задана, то есть действует тот же дефолт кода. Значит, на всём измеренном флоте upstream-хост — `pay.broadapps.dev`. Отдельной настройки не заводится: измерение 1 показало страницы на том же хосте, что и API, а вторая настройка того же хоста — второй источник истины, расходящийся молча. Схема к upstream — всегда `https`. Хост берётся ТОЛЬКО из конфигурации, никогда из запроса, из тела ответа broadapps или из `payment_url` (защита от SSRF, §4).

### 2. Переписывание `paymentUrl` в ответе checkout (уточнение [ADR-051 §4](ADR-051-cloudpayments-checkout-payment-link.md))

[ADR-051 §4](ADR-051-cloudpayments-checkout-payment-link.md) предписывал `paymentUrl ← payment_url` «прямым пробросом». **Уточнение (не супессия):** проброс сохраняется для всех ссылок, КРОМЕ ссылок на платёжную страницу broadapps, которые переписываются на домен инстанса. Прочие поля ответа (`paymentId`, `status`, `expiresAt`), тип `paymentUrl` (`str`), коды ошибок и HTTP-статусы ADR-051 **не меняются**.

Предикат переписывания — конъюнкция, вычисляемая на КАЖДОМ успешном ответе checkout:
- (а) `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` = `true` (§6);
- (б) `urlsplit(payment_url).hostname` совпадает с upstream-хостом §1 (сравнение без учёта регистра);
- (в) путь `payment_url` начинается с `/cp/pay/` — единственный измеренный путь платёжной страницы;
- (г) `normalized_service_domain()` непуст.

Все четыре истинны → `paymentUrl = "https://" + normalized_service_domain() + <путь> + ("?" + query, если есть) + ("#" + fragment, если есть)`; путь, query и fragment переносятся **байт-в-байт**. Иначе — `paymentUrl = payment_url` без изменений.

**Откуда домен — `SERVICE_DOMAIN`, а не заголовок запроса.** `Host`/`X-Forwarded-Host` задаёт клиент (или любой промежуточный прокси); на инстансе за Traefik `Host` и так равен `SERVICE_DOMAIN` (иначе роутер не совпал бы), поэтому заголовок ничего не добавляет, кроме поверхности подмены в тестах, в dev и за будущими прокси. `SERVICE_DOMAIN` — уже действующий источник публичного хоста инстанса ([ADR-031](ADR-031-absolute-preview-url.md), [ADR-108 §1](ADR-108-media-generation-via-proxy.md)) и обязателен для деплоя: без него `docker-compose.prod.yml` не рендерит Traefik-метку (`${SERVICE_DOMAIN:?...}`).

**Наблюдаемость ложного «не переписано».** Ссылка, которую предикат НЕ переписал, хотя её хост — upstream-хост (не выполнены (а), (в) или (г)), — это ровно тот исход, который решение призвано исключить; он обязан быть виден: `cloudpayments_pay_page_rewrite_skipped` с `reason` ∈ {`disabled`, `path_not_proxied`, `service_domain_unset`} (§7). Уровень: `path_not_proxied` и `service_domain_unset` — WARNING (неожиданный исход при включённой функции); `disabled` — INFO, потому что это штатное состояние всего флота сразу после деплоя, и WARNING на каждом checkout обесценил бы канал. Ссылка на чужой хост (YooMoney, T-Банк) логом не сопровождается — это штатный путь.

**Соседний путь.** `POST /v1/web/session` ([ADR-110](ADR-110-ru-payment-neutral-path-aliases.md)) — тот же обработчик из одной таблицы регистрации, поэтому переписывание действует на него автоматически; реализуется в ОДНОМ месте (обработчик checkout или клиент), а не по пути. Ответы пары остаются тождественными (инвариант [ADR-110 §2](ADR-110-ru-payment-neutral-path-aliases.md)).

### 3. Прокси платёжной страницы на домене инстанса

Новые маршруты приложения **вне** `/v1` (браузерные страницы, не API), `include_in_schema=False` (в OpenAPI не показываются — это не интерфейс клиента):

| Метод | Путь (на домене инстанса) | Upstream | Назначение |
|---|---|---|---|
| `GET`, `HEAD`, `POST` | `/cp/pay/{rest}` | `https://<upstream-хост>/cp/pay/{rest}` | платёжная страница и её возможные под-запросы |
| `GET`, `HEAD` | `/payment/return` | `https://<upstream-хост>/payment/return` | страница возврата после оплаты (`returnUrl` виджета) |
| `GET`, `HEAD` | `/main.css`, `/main.js` | `https://<upstream-хост>/main.css`, `.../main.js` | ассеты страницы возврата (измерение 3) |

Пути прокси совпадают с путями upstream намеренно: переписывается только хост, относительные ссылки страницы продолжают работать без переписывания путей (§Альтернативы).

**Белый список путей (защита от SSRF и от открытого прокси).** `{rest}` — одна или несколько непустых компонент из символов `[A-Za-z0-9._~-]`, разделённых `/`; компоненты `.` и `..`, пустая компонента (`//`), `%`-кодирование любого символа, `\` — отвергаются. Проверка — по сырому пути запроса (`scope["raw_path"]`), а не по декодированному, чтобы `%2e%2e`/`%2f` не прошли. Неподходящий путь → `404` без исходящего вызова. Query-строка передаётся upstream байт-в-байт. **Query, который не удаётся собрать в URL upstream** (не-ASCII байт в сырой query-строке, символ `#`), отвергается ДО исходящего вызова тем же ответом, что отказ белого списка путей — `404`, — и лога `cloudpayments_pay_page_proxy` не даёт (как и отказ белого списка): такой запрос не может быть легитимным запросом страницы, а собирать из него URL «как получится» значит отправить upstream не то, что прислал браузер.

**Гейт маршрутов.** Все четыре маршрута прокси обслуживают запросы, только если на инстансе ОДНОВРЕМЕННО (1) `cloudpayments_checkout_configured()` истинно (оба `CLOUDPAYMENTS_APP_ID` и `CLOUDPAYMENTS_API_TOKEN` заданы) и (2) флаг `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` = `true` (§6). Иначе — `404` до исходящего вызова, одним и тем же ответом для обоих условий (по ответу нельзя отличить, какое не выполнено). Обоснование (решение main chat, выведенное из цели владельца): ответы этих путей одинаковы на всех доменах, поэтому прокси, открытый на каждом инстансе с настроенным checkout, — сам по себе новый способ сопоставить инстансы (запросить `/main.js` на двух доменах и сравнить), в том числе инстансы классов YooMoney и T-Банк, которым прокси вовсе не нужен. **Цена, принятая явно:** при откате (флаг → `false`) уже выданные переписанные ссылки перестают открываться — пользователь, получивший ссылку до отката, увидит `404` и должен запросить оплату заново. Ссылки короткоживущие, откат — аварийная мера.

**Исходящий запрос.** `httpx.AsyncClient` per-call, `follow_redirects=False` (редирект обрабатывает браузер), таймаут **15 с** (connect+read, как у checkout), `Accept-Encoding: identity`.
- Заголовки, передаваемые upstream (allowlist; прочие отбрасываются): `Accept`, `Accept-Language`, `Content-Type`, `User-Agent`, `Cookie`, `X-XSRF-TOKEN`, `X-CSRF-TOKEN`, `X-Requested-With`; `Origin` и `Referer` — с заменой хоста инстанса на upstream-хост. `Host` выставляет httpx (upstream-хост).
- **ЗАПРЕЩЕНО передавать upstream:** `Authorization` (в т.ч. JWT пользователя), `CLOUDPAYMENTS_API_TOKEN` в любом виде, `X-Forwarded-*`, `X-Real-IP`, `Forwarded`. **Контраст с checkout ([ADR-051 §3](ADR-051-cloudpayments-checkout-payment-link.md)):** там серверный `Authorization: Bearer <CLOUDPAYMENTS_API_TOKEN>` ОБЯЗАТЕЛЕН, здесь — ЗАПРЕЩЁН: страница публична, а токен — ключ API, и в браузерном контуре ему делать нечего.
- `Cookie` передаётся целиком: приложение собственных cookie не выдаёт, поэтому всё, что браузер шлёт на эти пути, — cookie страницы broadapps. Если приложение когда-либо начнёт выдавать cookie на своём домене, allowlist `Cookie` обязан быть пересмотрен в том же изменении.
- Тело запроса (`POST`) — байт-в-байт; предел размера — общий `SizeLimitMiddleware`.

**Ответ клиенту.**
- Статус upstream передаётся как есть (в т.ч. `3xx`, `4xx` и `5xx` — это UX самой страницы: истёкшая ссылка, ошибка CSRF и т.п.).
- Заголовки ответа (allowlist; прочие отбрасываются, включая `Content-Encoding`, `Content-Length`, hop-by-hop, `Server`, `X-Powered-By`, `Date`): `Content-Type`, `Cache-Control`, `Expires`, `Set-Cookie`, `Location` и группа заголовков безопасности ниже. `Content-Length` вычисляется заново по отданному телу; **на `HEAD` заголовок `Content-Length` не отдаётся вовсе** — тела нет, а длину переписанного тела без самого тела вычислить нельзя; `0` отдавать ЗАПРЕЩЕНО (это ложное утверждение о длине `GET`-ответа).
  - `Set-Cookie`: атрибут `Domain` (если есть) удаляется — cookie становится host-only на домене инстанса; прочие атрибуты не меняются.
  - `Location`: если хост абсолютного URL — upstream-хост, он заменяется хостом инстанса; относительный `Location` и `Location` на чужой хост — без изменений.
- **Заголовки безопасности.** Измерено main chat 2026-09-24T21:33:44Z командой `curl -s -D - -o /dev/null https://pay.broadapps.dev/cp/pay/<uuid> | cut -d: -f1 | sort` и той же по `/payment/return` (по одной ссылке каждой страницы): upstream отдаёт ровно `Cache-Control`, `Connection`, `Content-Type`, `Date`, `Server`, `Set-Cookie` ×2, `Strict-Transport-Security`, `Transfer-Encoding`, `X-Powered-By` — ни `Content-Security-Policy`, ни `Referrer-Policy`, ни `Permissions-Policy`, ни `Cross-Origin-*` нет. Правило:
  - `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options` upstream **отбрасываются**, действуют значения приложения (`SecurityHeadersMiddleware`: HSTS `max-age=63072000; includeSubDomains`, `X-Frame-Options: DENY`, `nosniff`). Обоснование: HSTS привязан к хосту, который его прислал, — HSTS `pay.broadapps.dev` о домене инстанса ничего не говорит, а собственный HSTS инстанса уже строже или равен; два значения одного заголовка в ответе — неопределённость для браузера.
  - `Content-Security-Policy`, `Content-Security-Policy-Report-Only`, `Referrer-Policy`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, `Cross-Origin-Embedder-Policy`, `Cross-Origin-Resource-Policy` — **сегодня не приходят**; если broadapps их добавит, они **передаются** браузеру, а в их значении upstream-хост заменяется хостом инстанса по правилу токена ниже. Обоснование: молча отбросить будущую защиту страницы оплаты значит ослабить её без чьего-либо решения; оставить upstream-хост в CSP — сломать страницу (`'self'`-источники стали бы чужими) или вернуть домен broadapps в видимый браузеру текст. Приложение своей CSP на эти пути не ставит (inline-скрипты страницы обязаны исполняться).
  - Любой иной заголовок ответа upstream вне allowlist отбрасывается, а его **имя** (не значение) попадает в поле `droppedHeaders` лога прокси (§7). Заголовки, которые этот раздел называет отбрасываемыми поимённо (`Server`, `X-Powered-By`, `Date`, `Content-Encoding`, `Content-Length`, hop-by-hop, `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options`), в `droppedHeaders` НЕ попадают — поле сигналит только о заголовках, судьба которых этим ADR не решена — появление нового заголовка у broadapps наблюдаемо без чтения значений.

**Переписывание тела.** Для ответов с `Content-Type` из набора `text/html`, `text/css`, `text/javascript`, `application/javascript`, `application/x-javascript`, `application/json` тело декодируется (charset из `Content-Type`, иначе UTF-8) и в нём заменяется **каждое вхождение upstream-хоста как токена** на `normalized_service_domain()`, без учёта регистра. Токен — вхождение, у которого:
- **левая граница** — начало текста, ЛИБО символ не из `[A-Za-z0-9.-]`, ЛИБО трёхсимвольная последовательность `%2F` (любой регистр: закодированный `/`);
- **правая граница** — конец текста, ЛИБО символ не из `[A-Za-z0-9-]`, причём если этот символ `.`, то следующий за ним символ НЕ из `[A-Za-z0-9-]` (точка в конце предложения — граница, точка перед следующей меткой домена — нет).

Правило задано по ХОСТУ, а не по формам записи URL, поэтому покрывает `https://pay.broadapps.dev/…`, JSON-экранированное `https:\/\/pay.broadapps.dev\/…` (измерение 2), протокол-относительное `//pay.broadapps.dev`, голый хост и URL-кодированное `https%3A%2F%2Fpay.broadapps.dev` (левая граница — `%2F`). Не заменяются хосты, в которые upstream-хост входит частью: `xpay.broadapps.dev` (слева буква), `pay.broadapps.dev.evil.test` (справа `.` и метка), `pay.broadapps.devx` (справа буква). **Асимметрия границ намеренна:** слева `.` исключён безусловно (`a.pay.broadapps.dev` — другой хост), справа — только перед меткой (иначе не заменился бы хост в конце предложения). Иные кодировки (`%252F`, HTML-сущности `&#47;`) правилом не покрываются: не измерены и проявятся по WARNING остаточных признаков ниже ([Q-113-2](../99-open-questions.md)). Тело, которое не декодируется, отдаётся без изменений с WARNING (§7). Прочие типы содержимого не переписываются. **Пустой `SERVICE_DOMAIN`:** прокси продолжает работать, но подмену хоста (в теле, `Location`, значениях заголовков безопасности) не выполняет — замены на пустую строку не бывает; upstream-хост остаётся в ответе и виден по WARNING `cloudpayments_pay_page_residual_brand`. Выдача переписанных ссылок при пустом `SERVICE_DOMAIN` и так невозможна (§2 (г)).

**Остаточные признаки.** После замены тело текстового ответа проверяется на вхождения подстроки `broadapps` (без учёта регистра); их число > 0 → WARNING `cloudpayments_pay_page_residual_brand` (§7). Автоматически такие вхождения не заменяются: их форма и смысл не измерены ([Q-113-2](../99-open-questions.md), [Q-113-3](../99-open-questions.md)).

### 4. Безопасность

- **SSRF:** хост upstream — только из конфигурации (§1), схема — только `https`, путь — только белый список §3 по сырому пути; тело, заголовки и query запроса хост не определяют.
- **Не открытый прокси:** четыре семейства путей, фиксированный хост, лимит per-IP (§5).
- **Секреты и PII:** `CLOUDPAYMENTS_API_TOKEN` в прокси не участвует; тела запросов и ответов, cookie, значения заголовков — не логируются нигде. uuid в пути `/cp/pay/<uuid>` — ключ доступа к странице оплаты; его носители в логах разобраны поимённо:
  - **структурные логи прокси (§7)** — только класс пути, uuid и query не пишутся;
  - **access-лог приложения** (логгер `uvicorn.access`: gunicorn с `UvicornWorker` и `--access-logfile -`, `Dockerfile:93`) пишет путь с query КАЖДОГО запроса. Существующий фильтр `AccessLogQueryRedactionFilter` (`src/app/observability/logging.py`, ставится `install_access_log_redaction()`) сегодня срезает только query у пути вебхука прокси медиа. **Решение:** маскировка расширяется на пути прокси — у путей под `/cp/pay/` в access-строке остаётся `/cp/pay/*` (хвост пути и query заменяются) **независимо от того, есть ли в пути `?`**: действующий фильтр выходит рано, если в пути нет `?` (проверка `"?" not in path` в `AccessLogQueryRedactionFilter.filter`), и на пути под `/cp/pay/` этот ранний выход распространяться НЕ должен — штатная ссылка `/cp/pay/<uuid>` query не несёт, и именно её uuid подлежит маскировке; у `/payment/return` срезается query; `/main.css`, `/main.js` не трогаются. Запись не удаляется: факт вызова, метод и статус остаются наблюдаемы (тот же принцип, что у действующего фильтра);
  - **access-лог edge-Traefik** (`infra/fleet/router/traefik.yml`, `accessLog.filters.statusCodes: ["400-599"]`) пишет полный путь только ответов `4xx`/`5xx`. Маскировки пути по префиксу Traefik не имеет (только отбрасывание поля для всех роутеров), а менять формат общего edge-лога ради одного пути — несоразмерно. **Принятый остаточный риск:** uuid попадает в edge-лог только на ошибочных ответах (истёкшая или несуществующая ссылка, ошибка CSRF, сбой upstream); на успешной загрузке страницы (`200`) его там нет. Сам uuid broadapps выдаёт в открытом `payment_url`, и у поставщика он лежит в его собственных логах; пересмотр — по решению владельца, если он сочтёт риск неприемлемым.
- **Нет перехвата JWT:** `Authorization` не передаётся upstream; браузер на эти пути JWT не шлёт.

### 5. Недоступность upstream, таймауты, лимиты

| Ситуация | Ответ клиенту | `reason` (лог) |
|---|---|---|
| `httpx.TimeoutException` | `502`, `text/html; charset=utf-8`, `Cache-Control: no-store`, фиксированная нейтральная страница «Страница оплаты временно недоступна. Повторите попытку позже.» (без упоминания поставщика и его домена) | `timeout` |
| `httpx.RequestError` (connect/TLS/network) | то же | `connect_error` |
| тело ответа upstream > **5 MiB** | то же | `too_large` |
| upstream ответил любым статусом | статус и тело upstream (после переписывания) | — |

Формат ошибки — HTML, а не JSON-конверт API: адресат — браузер пользователя. Лимит — per-source-IP, отдельная корзина `rl:cppage:{ip}` (IP — `client_ip(request)`, как у вебхука [ADR-054 §1](ADR-054-cloudpayments-webhook-payment-verification.md)), **120 запросов в окно** `rate_limit_window_seconds`, fail-open при недоступности Redis; превышение → `429` c тем же нейтральным HTML. Значения таймаута, предела тела и лимита — константы модуля, отдельных env не заводится (как `_CHECKOUT_TIMEOUT_SECONDS` [ADR-051 §3](ADR-051-cloudpayments-checkout-payment-link.md)).

### 6. Настройка и выкат

**Новая настройка** `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` (`bool`, дефолт кода `false`) — управляет И переписыванием `paymentUrl` (§2), И доступностью маршрутов прокси (§3, гейт маршрутов). Цепочка разрешения: переменной нет ни в `.env.example`, ни в `.env.prod.example` (`grep -n CLOUDPAYMENTS .env.example .env.prod.example` на `d7a4295` пуст), ни в провижининге — значит, на всех живых инстансах после выката действует дефолт кода `false`, и поведение checkout не меняется, пока оператор явно не включит флаг. Это исход (б) нормы о смене дефолта: новая настройка, а не правка существующей.

**Почему выключатель, а не безусловное поведение:** риск [Q-113-1](../99-open-questions.md) не проверен; если виджет отвергает чужой домен, переписанная ссылка ломает оплату — и откат должен быть правкой `.env` одного инстанса, а не деплоем.

**Порядок выката (по классам конфигурации, а не списком инстансов):**
1. Деплой кода (флаг `false` везде; ссылки не переписываются, все четыре маршрута прокси отвечают `404` на каждом инстансе — ни один домен не отдаёт страницу broadapps).
2. Класс «страница broadapps» (newweraar или reluniqo — один инстанс): включить флаг, провести **реальный тестовый платёж** минимальным продуктом по ссылке с домена инстанса; в инструментах разработчика браузера убедиться, что (i) адресная строка на всех шагах — домен инстанса, (ii) нет запросов на upstream-хост, (iii) нет `404` на домене инстанса, (iv) оплата прошла и колбэк начислил (журнал `cloudpayments_webhook_outcome` = `applied`), (v) **ни один запрос на домен инстанса не несёт полей карты и криптограммы** (номер, срок, CVC, `cryptogram`/`CardCryptogramPacket` — в теле, query и заголовках; проверяется просмотром тела каждого `POST` на домен инстанса). Результат (i)–(iv) закрывает [Q-113-1](../99-open-questions.md) и [Q-113-2](../99-open-questions.md), результат (v) — [Q-113-4](../99-open-questions.md). **Если (v) опровергнуто — флаг не включается ни на одном инстансе (на проверочном — выключается) до решения владельца:** тогда через сервер инстанса идут данные карты, и это другой класс ответственности, чем проксирование страницы.
3. Только после п. 2 — включение на остальных инстансах класса. На классах «YooMoney» и «T-Банк» флаг НЕ включается: переписывать там нечего (предикат §2(б) ложен), а включение открыло бы на их доменах маршруты прокси — тот самый признак общности, от которого защищает гейт §3.
4. Откат — `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED=false` + перезапуск `api`; новые ссылки снова идут на broadapps, маршруты прокси отвечают `404`, **уже выданные переписанные ссылки перестают открываться** (цена, принятая в §3).

Если в п. 2 выяснится, что страница делает запросы на пути вне белого списка §3, — белый список расширяется правкой этого ADR (новая запись в §Ревизии), а не молча в коде.

### 7. Наблюдаемость

- `cloudpayments_checkout_outcome` ([ADR-051 §6](ADR-051-cloudpayments-checkout-payment-link.md)): allowlist дополняется полем `paymentUrlRewritten` (`bool`, только при `result=created`). Прочие поля не меняются; `paymentUrl` целиком не логируется.
- `cloudpayments_pay_page_rewrite_skipped` — по одному на ответ checkout, чья ссылка ведёт на upstream-хост и не переписана; уровень — INFO при `reason=disabled` (штатное состояние после деплоя), WARNING при `path_not_proxied` и `service_domain_unset`; поля: `reason` (`disabled` | `path_not_proxied` | `service_domain_unset`), `userId`, `productId`.
- `cloudpayments_pay_page_proxy` — по одному на запрос прокси; поля: `result` (`ok` | `error`), `reason` (на ошибке: `timeout` | `connect_error` | `too_large` | `rate_limited`), `pathClass` (`pay` | `return` | `asset`), `method`, `upstreamStatus` (при `ok`), `durationMs`, `droppedHeaders` (имена отброшенных заголовков ответа upstream, отсортированные, без значений; при `ok`; без заголовков, отбрасываемых §3 поимённо — `Server`, `X-Powered-By`, `Date`, `Content-Encoding`, `Content-Length`, hop-by-hop, HSTS/XFO/XCTO). Уровни: `ok` с `upstreamStatus < 500` — DEBUG; `ok` с `upstreamStatus ≥ 500` — INFO; `error` — WARNING (кроме `rate_limited` — INFO).
- `cloudpayments_pay_page_residual_brand` — WARNING с полями `pathClass`, `count` (§3).
- `cloudpayments_pay_page_rewrite_failed` — WARNING с полями `pathClass`, `contentType`, если текстовое тело не декодировалось.
- ЗАПРЕЩЕНО писать в структурные логи: путь `/cp/pay/<uuid>` целиком, query, cookie, тела, значения заголовков. Access-лог приложения маскируется по §4; edge-лог Traefik — принятый остаточный риск §4.

### 8. Тестовая стратегия (что обязано быть покрыто)

Тесты герметичны: upstream подменяется транспортом httpx, реальной сети нет. Каждый кейс ниже — отдельный тест; обязательны оба критерия (переписано, когда должно; не тронуто, когда не должно).

**Переписывание `paymentUrl` (оба пути пары — `/v1/billing/cloudpayments/checkout` и `/v1/web/session`):**
1. флаг `true`, ссылка `https://pay.broadapps.dev/cp/pay/<uuid>?a=b` → `https://<SERVICE_DOMAIN>/cp/pay/<uuid>?a=b` (путь и query байт-в-байт); `paymentUrlRewritten=true`;
2. тот же вход с хостом в другом регистре (`PAY.BroadApps.dev`) → переписан;
3. `yoomoney.ru/...` и `pay.tbank-online.com/...` → не тронуты, WARNING нет;
4. флаг `false` → не тронута, `rewrite_skipped/disabled` уровнем INFO (не WARNING);
5. upstream-хост, путь не `/cp/pay/` → не тронута, WARNING `path_not_proxied` (уровень проверяется);
6. пустой `SERVICE_DOMAIN` → не тронута, WARNING `service_domain_unset`;
7. `SERVICE_DOMAIN` вида `https://example.shop/` → в ссылке ровно один `https://` и нет `//` перед путём;
8. хост вида `xpay.broadapps.dev` или `pay.broadapps.dev.evil.test` → не тронут (сравнение хоста, а не подстроки);
9. ответы пары тождественны на каждом из кейсов 1–6;
10. diff-стойкость: удаление вызова переписывания роняет кейс 1 на обоих путях.

**Прокси:**
11. белый список: `/cp/pay/x/../../api`, `/cp/pay/%2e%2e/x`, `/cp/pay//x`, `/cp/pay/a%2Fb`, `/cp/pay/` → `404`, исходящего вызова нет;
12. исходящий URL — всегда `https://<upstream-хост из CLOUDPAYMENTS_API_BASE>/<тот же путь>?<та же query>`; подмена `Host`/`X-Forwarded-Host` во входящем запросе хост upstream не меняет;
13. upstream не получает `Authorization`, `X-Forwarded-*`, `X-Real-IP`, `Forwarded`; получает `Cookie` и `X-XSRF-TOKEN`; `Origin`/`Referer` — с upstream-хостом;
14. тело HTML с `https:\/\/pay.broadapps.dev\/payment\/return`, `https://pay.broadapps.dev/main.css`, `//pay.broadapps.dev/x`, `https%3A%2F%2Fpay.broadapps.dev` и `https%3a%2f%2fpay.broadapps.dev` → все пять форм указывают на `SERVICE_DOMAIN`; `widget.cloudpayments.ru` не тронут; фраза «…на pay.broadapps.dev.» (точка в конце предложения) → заменена; `xpay.broadapps.dev`, `pay.broadapps.dev.evil.test`, `pay.broadapps.devx`, `a.pay.broadapps.dev` → НЕ заменены;
15. `Set-Cookie` с `Domain=pay.broadapps.dev` → без `Domain`; без `Domain` → без изменений;
16. `Location: https://pay.broadapps.dev/payment/return` → хост инстанса; `Location` на чужой хост → без изменений; редирект сервером не следуется;
17. статусы upstream `404`/`419`/`500` → переданы как есть;
18. таймаут, ошибка соединения, тело > 5 MiB → `502` нейтральный HTML без `broadapps` в теле;
19. превышение лимита per-IP → `429`; корзина `rl:cppage` не расходует `rl:cpwebhook` и наоборот;
20. гейт маршрутов, на КАЖДОМ из четырёх маршрутов: флаг `false` при настроенном checkout → `404`, исходящего вызова нет; `cloudpayments_checkout_configured()` ложно при флаге `true` → тот же `404`, исходящего вызова нет; ответы двух отказов побайтно одинаковы; флаг `true` и checkout настроен → прокси работает; снятие проверки флага роняет кейс;
21. остаток `broadapps` в теле после замены → WARNING `residual_brand` c верным `count`;
22. лог прокси не содержит uuid пути, query, cookie;
23. OpenAPI не содержит путей прокси;
24. access-лог (`uvicorn.access`, запись в форме, которую пишет сервер): `GET /cp/pay/<uuid>` БЕЗ query → в строке `/cp/pay/*`, uuid нет (diff-стойкость обязательна именно на этом входе: возврат раннего выхода по отсутствию `?` для путей `/cp/pay/` обязан ронять кейс); `GET /cp/pay/<uuid>?x=1` → `/cp/pay/*`, ни uuid, ни query; `GET /payment/return?x=1` → `/payment/return` без query; `/main.js` и прочие пути приложения — байт-в-байт; запись не удаляется; снятие новой маскировки роняет кейс;
25. заголовки ответа: `Strict-Transport-Security`, `X-Frame-Options`, `X-Content-Type-Options` upstream не доходят до клиента, в ответе — значения приложения, каждый ровно один раз;
26. `Content-Security-Policy` upstream со ссылкой на upstream-хост → передан, хост в значении заменён; `Referrer-Policy` и `Cross-Origin-Opener-Policy` → переданы как есть;
27. неизвестный заголовок upstream (`X-Test: v`) не передан; в логе `droppedHeaders` содержит `x-test` и не содержит `v`; `Server`, `X-Powered-By` не переданы;
28. `HEAD /cp/pay/<uuid>` → статус upstream, пустое тело, заголовка `Content-Length` нет (в т.ч. не `0`);
29. query, не собираемый в URL upstream (не-ASCII байт в сырой query, `#`), → `404` тем же ответом, что отказ белого списка; исходящего вызова нет; записи `cloudpayments_pay_page_proxy` нет.

Регресс: прочие поля ответа checkout, коды ошибок ADR-051 и вебхук не меняются. Ручная проверка после выката — §6 п. 2 (автотест её не заменяет).

## Последствия

- Для приложений, чьи ссылки ведут на страницу broadapps, пользователь видит домен своего инстанса; у каждого инстанса домен свой.
- Публичный контракт: значение `paymentUrl` меняет домен там, где сработал предикат §2; форма поля и прочие поля не меняются, клиенту менять ничего не нужно.
- Новые публичные браузерные пути на домене каждого инстанса, где включён флаг `CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED` и настроен checkout (при выключенном флаге эти пути отвечают `404`, §3); через сервер инстанса проходит трафик страницы оплаты. **Идут ли через него данные карты — НЕ измерено:** ожидание, что их принимает виджет CloudPayments в своём фрейме (`widget.cloudpayments.ru`), ничем не подтверждено, а прокси пропускает `POST` под `/cp/pay/*` байт-в-байт. Проверка — §6 п. 2 (v), вопрос — [Q-113-4](../99-open-questions.md); до её результата флаг не раскатывается дальше проверочного инстанса.
- broadapps видит запросы страницы с IP сервера инстанса, а не пользователя (заголовки IP не передаются); если их антифрод на это опирается — всплывёт на §6 п. 2.
- Домен upstream не исчезает из виджета CloudPayments: сам виджет грузится с `widget.cloudpayments.ru` — это общий домен платёжного сервиса, как YooMoney.
- Прочие признаки общности (тексты, логотипы, названия на странице broadapps; общее имя получателя платежа на страницах YooMoney и T-Банк) решением не скрываются — [Q-113-3](../99-open-questions.md), решает владелец.

## Альтернативы (отвергнуты)

- **Брать домен из `Host`/`X-Forwarded-Host` запроса** — заголовок задаёт клиент; `SERVICE_DOMAIN` уже авторитетен для публичного хоста (§2).
- **Отдельная настройка хоста страниц broadapps** — второй источник истины о том же хосте; измерение показало совпадение с хостом API (§1).
- **Нейтральный префикс вместо `/cp/pay/*`** (напр. `/pay/*`) — потребовал бы переписывать и пути в теле страницы (относительные ссылки, `returnUrl`) по неизмеренному набору; владелец назвал `/cp/pay/*`.
- **Проксировать на edge (Traefik) вместо приложения** — Traefik не переписывает тело (экранированный `returnUrl`, cookie `Domain`); правило жило бы в инфраструктуре вне кода и тестов (тот же довод, что в [ADR-110 §Альтернативы](ADR-110-ru-payment-neutral-path-aliases.md)).
- **Переписывать все ссылки, включая YooMoney и T-Банк** — эти домены общие для множества магазинов и общности не выдают; проксирование чужой платёжной формы банка добавляло бы риск без пользы.
- **Безусловное поведение без флага** — риск [Q-113-1](../99-open-questions.md) не проверен; откат требовал бы деплоя.
- **Маршруты прокси без гейта флага (открыты везде, где настроен checkout)** — первая редакция этого ADR; отвергнута: одинаковые ответы `/main.js`, `/main.css`, `/payment/return`, `/cp/pay/*` на всех доменах флота сразу после деплоя дают новый способ сопоставить инстансы, прямо против цели владельца.
- **(б) Отдельный флаг для маршрутов** (один — переписывание, другой — прокси) — отвергнуто: единственное осмысленное сочетание — оба включены или оба выключены (переписанная ссылка без прокси даёт `404`, прокси без переписывания — лишняя открытая поверхность); два флага — две настройки об одном факте, расходящиеся при ручной правке `.env`.
- **(в) Принять риск сопоставления ради отката без потери выданных ссылок** — отвергнуто: цель решения — неразличимость инстансов по домену, и открытый прокси на каждом домене нарушал бы её постоянно, тогда как потеря уже выданных ссылок случается только при аварийном откате и затрагивает короткоживущие ссылки.
- **Следовать редиректам на сервере** — браузер ушёл бы на чужой URL без ведома пользователя страницы и потерял бы cookie; редирект отдаётся браузеру.

## Ревизии

(нет)
