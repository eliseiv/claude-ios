#!/usr/bin/env bash
# Наполнение инстанса на прикладном сервере (docs/MIGRATION-3-SERVERS.md §Порядок работ).
#
#   provision.sh adapt <инстанс> <A|B>   — привести УЖЕ существующий .env к новой схеме
#   provision.sh new   <инстанс> <домен> <A|B>  — создать .env с нуля из .env.prod.example
#
# Режим adapt — основной при переезде: настоящий .env приезжает со старого сервера вместе с
# ключами провайдеров и секретами, и трогать в нём можно ТОЛЬКО адресацию новой схемы.
# Переписывать его целиком нельзя: в нём лежат значения, которых нет больше нигде.
set -uo pipefail

MODE="${1:?режим: adapt | new}"
INST="${2:?имя инстанса}"
DIR="/opt/$INST"
PORTS="/opt/fleet/ports.txt"

wg_of() { [ "$1" = "A" ] && echo 10.10.0.1 || echo 10.10.0.2; }

# Каталог своей копии результатов генерации (ADR-109 §1, §5, §10, §Порядок выката п.2):
# /opt/<инстанс>/media-assets, владелец 10001:10001 (USER образа, Dockerfile), каталог 0750, и в
# его корне пустой маркер `.media-assets-root` (10001:10001, 0640). Без маркера приложение считает
# хранилище неподготовленным (deferred_unavailable) — так каталог, созданный Docker'ом от root на
# неподготовленном сервере, не принимается за «файлы пропали». Вызывается и в new, и в adapt:
# adapt запускают на ОБОИХ серверах (в том числе на резерве после копирования .env), а маркер
# обязан быть на обоих. Идемпотентно: существующий каталог и его содержимое не трогаются; маркер
# пересобирается с тем же содержимым, меняются только владелец и права каталога и маркера. Сбой — предупреждение, а не отказ провижининга:
# без каталога хранение просто не работает, инстанс работает (ADR-109 §4).
# Строку `pg_system_identifier=<ID>` маркера (ADR-109 §6.3) здесь НЕ пишем: её значение обязано
# сниматься на основном и сверяться с резервом, а provision.sh работает на ОДНОМ сервере (в new
# базы к тому же ещё нет). Пишет её infra/fleet/media-assets-rollout.sh с маршрутизатора; маркер,
# уже несущий строку, здесь не перезаписывается. Пустой маркер: корень доступен, блокируется только
# очистка сирот (media_asset_cleanup_blocked = 1, видно в verify.sh).
ensure_media_assets() {
  # Каталог media-assets ПИШЕТ контейнер (uid 10001), а этот код — root на хосте. Поэтому root
  # НИКОГДА не выполняет chown/chmod/запись по пути ВНУТРИ каталога: подложенная контейнером ссылка
  # `.media-assets-root -> <файл хоста>` иначе сменила бы владельца/права файла хоста, а FIFO
  # подвесил бы скрипт. Маркер собирается во временном файле в /opt/<инстанс> (root, контейнеру не
  # виден), там же получает владельца и права, и атомарно подменяет запись каталога через
  # `mv -T` (rename заменяет саму запись и ссылку не разыменовывает). Существующий маркер
  # принимается, только если это обычный файл, а не ссылка; читается с таймаутом.
  local d="$DIR/media-assets" m="$DIR/media-assets/.media-assets-root" line="" t
  if [ -L "$d" ] || { [ -e "$d" ] && [ ! -d "$d" ]; }; then
    echo "[$INST] ВНИМАНИЕ: $d существует и не является каталогом — хранение не подготовлено" >&2
    return 0
  fi
  mkdir -p "$d" && chown -h 10001:10001 "$d" && chmod 0750 "$d" \
    || { echo "[$INST] ВНИМАНИЕ: каталог хранения $d не подготовлен (владелец/права)" >&2; return 0; }
  if [ -L "$m" ] || { [ -e "$m" ] && [ ! -f "$m" ]; }; then
    echo "[$INST] ВНИМАНИЕ: $m — ссылка или спецфайл, а не обычный файл; НЕ трогаю, разобрать руками" >&2
    return 0
  fi
  if [ "$(stat -c %d "$DIR")" != "$(stat -c %d "$d")" ]; then
    echo "[$INST] ВНИМАНИЕ: $d на другой файловой системе, атомарная замена маркера невозможна — маркер не тронут" >&2
    return 0
  fi
  # Строку идентификатора, если она уже есть, сохраняем (её пишет media-assets-rollout.sh).
  [ -f "$m" ] && line="$(timeout 5 grep -m1 -E '^pg_system_identifier=[0-9]+$' -- "$m" 2>/dev/null)"
  t="$(mktemp "$DIR/.media-assets-root.tmp.XXXXXX")" || { echo "[$INST] ВНИМАНИЕ: mktemp в $DIR не удался — маркер не создан" >&2; return 0; }
  if { [ -z "$line" ] || printf '%s\n' "$line" > "$t"; } && chown 10001:10001 "$t" && chmod 0640 "$t" \
     && mv -fT -- "$t" "$m"; then
    echo "[$INST] каталог хранения media-assets готов (10001:10001, 0750, маркер)"
  else
    rm -f -- "$t"
    echo "[$INST] ВНИМАНИЕ: маркер $m не записан" >&2
    return 0
  fi
  if [ -z "$line" ]; then
    echo "[$INST] ВНИМАНИЕ: в маркере нет строки pg_system_identifier (ADR-109 §6.3) — очистка сирот будет" >&2
    echo "[$INST]   заблокирована. Допиши её ПОСЛЕ первого запуска postgres и подъёма резерва, на маршрутизаторе:" >&2
    echo "[$INST]   media-assets-rollout.sh $INST  (сухой прогон), затем  media-assets-rollout.sh $INST --apply" >&2
  fi
}

read_ports() {
  local line; line="$(grep -E "^$INST " "$PORTS" 2>/dev/null | head -1)"
  [ -n "$line" ] || { echo "нет строки для $INST в $PORTS"; exit 1; }
  API_PORT="$(echo "$line" | awk '{print $2}')"
  PG_PORT="$(echo "$line" | awk '{print $3}')"
}

setvar() {  # setvar КЛЮЧ ЗНАЧЕНИЕ — заменить или дописать, не трогая остальное
  local k="$1" v="$2"
  if grep -q "^$k=" "$DIR/.env"; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$k" -v v="$v" -F= 'BEGIN{OFS="="} $1==k{print k, v; next} {print}' "$DIR/.env" > "$tmp"
    mv "$tmp" "$DIR/.env"
  else
    printf '%s=%s\n' "$k" "$v" >> "$DIR/.env"
  fi
}

gen_hex32() {  # gen_hex32 — 32 случайных байта в hex; при сбое openssl — ошибка и выход, а не пусто
  local v
  v="$(openssl rand -hex 32 2>/dev/null)"
  case "$v" in
    *[!0-9a-f]*|"") echo "[$INST] openssl rand не дал секрет — ПРЕРЫВАЮ" >&2; exit 1;;
  esac
  [ "${#v}" = "64" ] || { echo "[$INST] openssl rand дал секрет неверной длины — ПРЕРЫВАЮ" >&2; exit 1; }
  printf '%s' "$v"
}

case "$MODE" in
adapt)
  SELF="${3:?третьим аргументом — на каком сервере наполняем: A или B}"
  [ -f "$DIR/.env" ] || { echo "$DIR/.env отсутствует — нечего приводить"; exit 1; }
  read_ports
  cp "$DIR/.env" "$DIR/.env.bak-adapt-$(date +%Y%m%d-%H%M%S)"
  setvar WG_BIND_IP   "$(wg_of "$SELF")"
  setvar API_HOST_PORT "$API_PORT"
  setvar PG_HOST_PORT  "$PG_PORT"
  # Замер на прежнем сервере: api с четырьмя воркерами занимал 458 МБ, на 27 инстансов это
  # 14 ГБ только под приложение. Нагрузка почти целиком в ожидании ответа провайдера, поэтому
  # два асинхронных воркера обслуживают тот же поток запросов вдвое дешевле по памяти.
  grep -q "^GUNICORN_WORKERS=" "$DIR/.env" || setvar GUNICORN_WORKERS 2
  # PROXY_WEBHOOK_SECRET и PROXY_API_KEY режим adapt НЕ трогает (ADR-108). adapt запускается и
  # на РЕЗЕРВЕ — migrate-all.sh и repair-replication.sh зовут его после копирования .env с
  # основного, — поэтому секрет, сгенерированный здесь, разошёлся бы между основным и резервом, и
  # после повышения резерва колбэки задач в полёте получили бы 401. Оба значения выставляются
  # сразу на ОБА сервера скриптом infra/fleet/proxy-rollout.sh.
  # MEDIA_ASSET_STORAGE_DIR (включение хранения, ADR-109) режим adapt тоже НЕ трогает — оно
  # включается per-instance на ОБА сервера скриптом infra/fleet/media-assets-rollout.sh; здесь
  # только каталог и маркер.
  ensure_media_assets
  echo "[$INST] .env приведён: туннель $(wg_of "$SELF"), порты api=$API_PORT pg=$PG_PORT"
  ;;

new)
  DOMAIN="${3:?третьим аргументом — домен}"
  SELF="${4:?четвёртым — сервер A или B}"
  read_ports
  cd "$DIR" || { echo "нет каталога $DIR"; exit 1; }
  [ -f .env ] && { echo "$DIR/.env уже существует — используйте adapt"; exit 1; }
  cp .env.prod.example .env
  PW="$(openssl rand -hex 20)"
  setvar COMPOSE_PROJECT_NAME "$INST"
  setvar SERVICE_DOMAIN "$DOMAIN"
  setvar POSTGRES_USER "app_$INST"
  setvar POSTGRES_PASSWORD "$PW"
  setvar POSTGRES_DB "db_$INST"
  setvar DATABASE_URL "postgresql+asyncpg://app_$INST:$PW@postgres:5432/db_$INST"
  setvar REDIS_URL "redis://redis:6379/0"
  setvar TRAEFIK_CERTRESOLVER "le"
  setvar JWT_ISSUER "https://$DOMAIN"
  setvar ADMIN_API_SECRET "$(openssl rand -base64 32)"
  setvar WG_BIND_IP "$(wg_of "$SELF")"
  setvar API_HOST_PORT "$API_PORT"
  setvar PG_HOST_PORT "$PG_PORT"
  setvar GUNICORN_WORKERS 2
  # --- Эксплуатационная база флота -------------------------------------------------------
  # `.env.prod.example` описывает ПРОИЗВОДСТВЕННУЮ конфигурацию, и правильно делает: в ней
  # StoreKit боевой, документация закрыта. Флот же пока живёт в другом режиме — приложения на
  # ревью, поэтому песочница и открытая документация. Раньше эта разница нигде не была записана,
  # и каждый новый инстанс рождался с умолчаниями шаблона, молча отличаясь от всех живых.
  # Так и вышло 2026-09-01: три новых инстанса отдавали 404 на /docs, потому что DOCS_ENABLED
  # остался false. Инстанс при этом «работал» — health отвечал, — и расхождение было не видно,
  # пока в него не ткнулись руками.
  # Значения ниже — то, что ФАКТИЧЕСКИ работает на флоте. Возвращать к производственным нужно
  # осознанно и вместе с корневым сертификатом Apple (07-deployment.md §Prod-readiness).
  setvar DOCS_ENABLED true
  setvar APPSTORE_ENVIRONMENT sandbox
  setvar APPSTORE_ROOT_CERT_DIR /run/secrets/appstore_root_certs
  setvar STOREKIT_TEST_MODE true
  setvar STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION true
  setvar PRESETS_DEFAULT_LOCALE en
  # --- Заглушки шаблона, которые ЛОМАЮТ покупки, если их оставить ------------------------
  # `.env.prod.example` — образец для заполнения человеком, и в нём стоят наглядные пустышки.
  # Провижининг же создаёт РАБОЧИЙ инстанс, и пустышка в нём не безобидна:
  #
  #   APPSTORE_BUNDLE_ID=<com.example.app> — проверка bundle активна, пока значение НЕПУСТОЕ.
  #   Сервер честно сравнивает идентификатор из транзакции с литералом «<com.example.app>» и
  #   отвергает КАЖДУЮ покупку с «bundleId mismatch». Прод 2026-09-02, joraliqo: разработчик не
  #   мог купить ничего, а по тексту ошибки казалось, что дело в подписи.
  #   Пусто — проверка отключена; это рабочее состояние для песочницы. Реальный bundle
  #   выставляется ПЕРЕД выходом в производство (07-deployment.md §Prod-readiness).
  #
  setvar APPSTORE_BUNDLE_ID ""
  # --- Секреты, которые `.env.prod.example` оставляет плейсхолдером-заглушкой ------------
  # Инцидент 2026-09-22 (ittechnewapps/appscoolnew/backrewio): `new` копировал шаблон, но не
  # генерировал `KMS_LOCAL_MASTER_KEY` — на клоне оставался буквальный литерал
  # `<base64-32-random-bytes>`. Он не ломает ни health, ни /docs — инстанс выглядит рабочим —
  # но КАЖДЫЙ `/v1/chat/run` падал `500` на `base64.b64decode()` в `get_kms_client()`
  # (`binascii.Error: Incorrect padding`), потому что зависимость собирается на первом же
  # обращении к оркестратору. Тот же класс дефекта — ещё у пяти ключей: `PREVIEW_URL_SECRET`,
  # `METRICS_SCRAPE_TOKEN` (свежие секреты, `.env.prod.example` только подсказывает команду
  # `openssl rand ...`, а не генерирует значение) и `FAL_API_KEY`/`APNS_KEY_ID`/`APNS_TEAM_ID`/
  # `APNS_TOPIC` (их легитимное «выключено» — ПУСТАЯ строка, а не текст подсказки в `<...>`:
  # непустой мусор в `FAL_API_KEY` ушёл бы к fal.ai как `Authorization: Key <...>` вместо
  # чистого документированного `503`).
  setvar KMS_LOCAL_MASTER_KEY "$(openssl rand -base64 32)"
  setvar PREVIEW_URL_SECRET "$(openssl rand -base64 32)"
  setvar METRICS_SCRAPE_TOKEN "$(openssl rand -base64 32)"
  setvar FAL_API_KEY ""
  # Генерация через прокси (ADR-108 §1, §Порядок выката п.1). Ключ прокси выдаёт владелец и
  # вписывает позже, вручную, и только там, где задан FAL_API_KEY: непустое значение сразу
  # переключает инстанс на прокси, поэтому здесь — пусто, а не текст подсказки. Секрет подписи
  # колбэка — свежий на инстанс; пока ключа нет, он ни на что не влияет. Значение не печатается.
  setvar PROXY_API_KEY ""
  # Секрет — через переменную, а не прямой подстановкой: exit внутри $(...) завершил бы только
  # подоболочку, и в .env записалось бы пусто.
  PWS="$(gen_hex32)" || exit 1
  setvar PROXY_WEBHOOK_SECRET "$PWS"
  unset PWS
  setvar APNS_KEY_ID ""
  setvar APNS_TEAM_ID ""
  setvar APNS_TOPIC ""
  # --- Продукты по умолчанию (решение владельца 2026-09-08) ---------------------------------
  # Раньше здесь стояли пустые карты — как защита от ВЫМЫШЛЕННЫХ продуктов шаблона
  # (`tokens_1500`, `weekly_xxx`), из-за которых покупка настоящего продукта отвергалась как
  # «unknown token product», а подписка начисляла резервную величину.
  #
  # Пустая карта эту беду лечила, но заводила свою: каждый новый инстанс до настройки оплаты
  # отвергал ЛЮБУЮ покупку, и разработчик упирался в это на первом же тесте.
  #
  # Значения ниже — не выдумка шаблона, а фактический набор продуктов, одинаковый у приложений
  # флота. Идентификаторы записаны ДОСЛОВНО как в App Store: начисление ищет продукт точным
  # совпадением ключа (token_purchase/service.py), поэтому `weekly_9.99_nottrial` и
  # `weekly_9.99_not_trial` — РАЗНЫЕ продукты. Регистр и подчёркивания менять нельзя.
  #
  # Инстанс с продуктом, которого нет в его приложении, ведёт себя ровно так же, как с пустой
  # картой: покупка такого продукта просто не придёт. Цена ошибки в другую сторону — покупка,
  # которую некуда начислить, — выше.
  setvar TOKEN_PRODUCTS '{"100_tokens_9.99":100,"250_tokens_19.99":250,"500_tokens_34.99":500,"1000_tokens_59.99":1000,"2000_tokens_99.99":2000}'
  setvar ADAPTY_PRODUCT_TOKENS '{"weekly_9.99_nottrial":100,"year_49.99_nottrial":1000}'
  # Резерв для подписки вне карты — величина МЛАДШЕГО тарифа, а не старшего: опечатка в
  # идентификаторе не должна раздавать годовой пакет.
  setvar ADAPTY_SUBSCRIPTION_TOKENS_GRANT 100
  # Предвыбранные на пейволле продукты (ADR-098 §11). Признак наш, не поставщика: у broadapps
  # поля с таким смыслом нет вовсе. Список — те же продукты, что заведены выше.
  setvar TOKEN_PRODUCTS_DEFAULT 'weekly_9.99_nottrial,year_49.99_nottrial,100_tokens_9.99,250_tokens_19.99,500_tokens_34.99,1000_tokens_59.99,2000_tokens_99.99'
  mkdir -p .secrets && chmod 700 .secrets
  if [ ! -f .secrets/jwt_private.pem ]; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .secrets/jwt_private.pem 2>/dev/null
    openssl rsa -pubout -in .secrets/jwt_private.pem -out .secrets/jwt_public.pem 2>/dev/null
  fi
  chown -R 10001:10001 .secrets 2>/dev/null || true
  chmod 640 .secrets/*.pem 2>/dev/null || true
  mkdir -p certs/appstore
  # Хранение (ADR-109) в new остаётся ВЫКЛЮЧЕННЫМ: MEDIA_ASSET_STORAGE_DIR в шаблоне не задан.
  # Каталог и маркер готовятся заранее, чтобы включение потом было одной записью в .env.
  ensure_media_assets
  echo "[$INST] создан: домен $DOMAIN, порты api=$API_PORT pg=$PG_PORT"
  ;;
*) echo "неизвестный режим"; exit 2;;
esac
