#!/bin/bash
# fleet-instance — операции над инстансом на прикладном сервере (ADR-115 §4, §7, §9, §8.7, §12, §13).
#
#   fleet-instance create                         (stdin, см. ниже)
#   fleet-instance rollback <slug>                (stdin: {"instance_uid"})
#   fleet-instance enable-archive <slug>          (stdin: {"instance_uid","walg_key_id","instance_generation"?})
#   fleet-instance image <slug> <sha>
#   fleet-instance redeploy <slug> <sha>
#   fleet-instance stop-for-erase <slug>
#   fleet-instance halt <slug> [--no-final-check] (stdin: {"instance_uid"})
#   fleet-instance purge <slug>                   (stdin: {"instance_uid"})
#   fleet-instance rekey <slug>                   (stdin: {"new_generation","walg_key_id"})
#   fleet-instance restore <slug>                 (ТОЛЬКО оператор, §12; stdin: {"source_generation",
#                                                  "source_key_id"?,"backup_name"?,"target_time"?,"sha"})
#
# stdin create (одна JSON-строка; (E2)-секреты — только здесь):
#   {"slug":"…","instance_uid":"<uuid>","instance_generation":1,"domain":"…","api_port":18001,
#    "wg_bind_ip":"10.10.0.N","walg_key_id":"<12 hex>","last_deployed_sha":"<40 hex>",
#    "env":{"POSTGRES_PASSWORD":"…","KMS_LOCAL_MASTER_KEY":"…","ADMIN_API_SECRET":"…",
#           "PREVIEW_URL_SECRET":"…","METRICS_SCRAPE_TOKEN":"…","PROXY_WEBHOOK_SECRET":"…",
#           …прочие (E1)/(E2) из ADR-115 §5, напр. LOG_LEVEL, MEDIA_ASSET_STORAGE_DIR}}
# Поверх .env.prod.example create пишет базу флота (DOCS_ENABLED=true, песочница StoreKit, продукты —
# как provision.sh); ключи из env её переопределяют.
# Вычисляемые инструментом (E1) в env передавать нельзя: COMPOSE_PROJECT_NAME, SERVICE_DOMAIN,
# JWT_ISSUER, POSTGRES_USER, POSTGRES_DB, DATABASE_URL, REDIS_URL, WG_BIND_IP, API_HOST_PORT,
# PG_HOST_PORT, GUNICORN_WORKERS, TRAEFIK_CERTRESOLVER, INSTANCE_UID, INSTANCE_GENERATION, WALG_KEY_ID.
set -uo pipefail
# shellcheck source=lib/common.sh
. "$(dirname "$(readlink -f "$0")")/lib/common.sh"

SUB="${1:-}"; shift || true
# shellcheck disable=SC2034  # STEP читает lib/common.sh (emit)
STEP="instance.$SUB"
BUNDLE="$FLEET_VERSION_DIR/compose"
COMPOSE_SET="docker-compose.prod.yml docker-compose.fleet.yml docker-compose.walg.yml .env.prod.example"
COMPUTED_KEYS=" COMPOSE_PROJECT_NAME SERVICE_DOMAIN JWT_ISSUER POSTGRES_USER POSTGRES_DB DATABASE_URL REDIS_URL WG_BIND_IP API_HOST_PORT PG_HOST_PORT GUNICORN_WORKERS TRAEFIK_CERTRESOLVER INSTANCE_UID INSTANCE_GENERATION WALG_KEY_ID "
REQUIRED_SECRETS="POSTGRES_PASSWORD KMS_LOCAL_MASTER_KEY ADMIN_API_SECRET PREVIEW_URL_SECRET METRICS_SCRAPE_TOKEN PROXY_WEBHOOK_SECRET"

bundle_version() { tr -d ' \r\n' < "$FLEET_VERSION_DIR/VERSION" 2>/dev/null; }

copy_compose() {  # копия файлов compose из бандла действующей версии в /opt/<slug> (ADR-115 §4.1)
  local d; d="$(inst_dir "$1")"; local f t
  for f in $COMPOSE_SET; do
    [ -f "$BUNDLE/$f" ] || return 1
    t="$(mktemp "$d/.$f.XXXXXX")" || return 1
    cp "$BUNDLE/$f" "$t" && chmod 0644 "$t" && mv -f "$t" "$d/$f" || return 1
  done
}

pull_and_tag() {  # pull_and_tag <slug> <sha> — образ приложения из GHCR + тег проекта
  local img; img="$(app_image "$2")"
  image_id "$img" >/dev/null || docker pull -q "$img" >/dev/null 2>&1 || return 1
  docker tag "$img" "$(img_proj_of "$1")-backend:prod"
}

IMG_RESULT=""
api_image_check() {  # IMG_RESULT=match|mismatch|absent; ev_* пишет evidence (вызывать НЕ через $(…))
  local s="$1" sha="$2" c want have
  c="$(ctr "$s" api)"
  want="$(image_id "$(app_image "$sha")")"
  have="$(docker inspect -f '{{.Image}}' "$c" 2>/dev/null)"
  ev_str api_container "$c"; ev_str api_image_id "${have:-}"; ev_str expected_image "$(app_image "$sha")"
  ev_str expected_image_id "${want:-}"
  if [ -z "$have" ] || [ -z "$want" ]; then ev_bool match false; IMG_RESULT=absent; return; fi
  if [ "$have" = "$want" ]; then ev_bool match true; IMG_RESULT=match; else ev_bool match false; IMG_RESULT=mismatch; fi
}

bring_up_app() {  # migrate + api, гейт готовности (как шаг деплоя CI, ADR-115 §10.2 п. 3)
  local s="$1" want cur recreate=()
  if ! ctr_running "$(ctr "$s" postgres)"; then
    dc "$s" up -d --no-build --no-recreate postgres redis >/dev/null 2>&1 || return 1
  fi
  wait_healthy "$(ctr "$s" postgres)" 45 || return 2
  dc "$s" run --rm --no-deps migrate >/dev/null 2>&1 || return 3
  # Тег <проект>-backend:prod переназначается без смены compose: `up` без --force-recreate
  # оставил бы контейнер на прежнем образе.
  want="$(image_id "$(img_proj_of "$s")-backend:prod")"
  cur="$(docker inspect -f '{{.Image}}' "$(ctr "$s" api)" 2>/dev/null || true)"
  [ -n "$want" ] && [ "$cur" = "$want" ] || recreate=(--force-recreate)
  dc "$s" up -d --no-build --no-deps "${recreate[@]}" api >/dev/null 2>&1
  wait_healthy "$(ctr "$s" api)" 45 || return 4
}

archiver_state() {  # "<archived_count>|<failed_count>|<last_archived_time_epoch>"
  psql_inst "$1" "SELECT archived_count, failed_count, COALESCE(extract(epoch FROM last_archived_time)::bigint, 0) FROM pg_stat_archiver" 2>/dev/null | tr -d ' '
}

walg_in_pg() {  # wal-g от пользователя postgres ВНУТРИ контейнера инстанса (путь archive_command)
  local s="$1"; shift
  local d u db; d="$(inst_dir "$s")"; u="$(env_get "$d/.env" POSTGRES_USER)"; db="$(env_get "$d/.env" POSTGRES_DB)"
  docker exec -u postgres -e PGHOST=/var/run/postgresql -e "PGUSER=${u:-postgres}" -e "PGDATABASE=${db:-postgres}" \
    "$(ctr "$s" postgres)" nice -n 10 fleet-walg "$@"
}

# verify_archiving <slug> — постусловие §13 для enable-archive/rekey (и restore): после
# pg_switch_wal() сегмент выгружен (last_archived_time > момента t0) и failed_count не вырос.
verify_archiving() {
  local s="$1" st0 f0 t0 st la fc okw=0
  st0="$(archiver_state "$s")"; f0="$(cut -d'|' -f2 <<<"$st0")"
  t0="$(psql_inst "$s" "SELECT extract(epoch FROM now())::bigint")"
  # На базе без WAL-записей после последнего переключения pg_switch_wal() ничего не делает
  # (свежий инстанс) — сегмент не выгружается. Точка восстановления даёт запись в WAL.
  psql_inst "$s" "SELECT pg_create_restore_point('fleet-verify-archiving')" >/dev/null
  psql_inst "$s" "SELECT pg_switch_wal()" >/dev/null
  for _ in $(seq 1 45); do
    st="$(archiver_state "$s")"
    la="$(cut -d'|' -f3 <<<"$st")"; fc="$(cut -d'|' -f2 <<<"$st")"
    if [ "${la:-0}" -gt "${t0:-0}" ] && [ "$fc" = "$f0" ]; then okw=1; break; fi
    sleep 2
  done
  ev_str archiver "$st"; ev_num failed_count_before "${f0:-0}"
  [ "$okw" = 1 ] || fail archive_not_working "pg_stat_archiver: сегмент не выгружен или failed_count вырос"
  ev_bool archived_after_change true
}

slug_arg() {
  SLUG="${1:-}"; [ -n "$SLUG" ] || fail missing_slug "не передан slug"
  check_slug "$SLUG"; D="$(inst_dir "$SLUG")"
}

case "$SUB" in
create)
  require_root; read_stdin_json
  jset SLUG .slug; [ -n "${1:-}" ] && [ "$1" != "$SLUG" ] && fail slug_mismatch
  check_slug "$SLUG"; D="$(inst_dir "$SLUG")"
  jset UIDV .instance_uid; check_uid "$UIDV"
  jset GEN .instance_generation opt; GEN="${GEN:-1}"; check_gen "$GEN"
  jset DOMAIN .domain; [[ "$DOMAIN" =~ $DOMAIN_RE ]] || fail bad_domain
  jset PORT .api_port; [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge "$API_PORT_MIN" ] && [ "$PORT" -le "$API_PORT_MAX" ] || fail bad_api_port
  jset WGIP .wg_bind_ip; [[ "$WGIP" =~ $IPV4_RE ]] || fail bad_wg_ip
  jset KID .walg_key_id; check_keyid "$KID"
  jset SHA .last_deployed_sha; check_sha "$SHA"
  lock_instance "$SLUG"

  [ "$(bundle_version)" = "$SHA" ] || fail bundle_version_mismatch "VERSION бандла /opt/fleet ≠ last_deployed_sha (ADR-115 §7.1 шаг 2)"
  ip -4 -o addr show wg0 2>/dev/null | grep -qw "$WGIP" || fail wg_ip_not_local "wg_bind_ip не адрес wg0 этого сервера"
  [ -r "$WALG_DIR/keys/$KID" ] || fail key_missing "ключ шифрования $KID не установлен (walg-key-add)"
  [ -r "$WALG_DIR/walg.json" ] && [ -r "$WALG_DIR/bucket" ] || fail objstore_missing
  # Чужой или служебный каталог не занимается (ADR-115 §4.1, §7.2).
  if [ -e "$D" ]; then
    [ -d "$D" ] && [ ! -L "$D" ] || fail foreign_dir "$D существует и не каталог"
    if [ -f "$D/.env" ]; then
      [ "$(env_get "$D/.env" INSTANCE_UID)" = "$UIDV" ] || fail foreign_dir "$D/.env не несёт INSTANCE_UID создаваемого инстанса"
    elif [ -n "$(ls -A "$D" 2>/dev/null)" ]; then
      fail foreign_dir "$D не пуст и не несёт .env создаваемого инстанса"
    fi
  fi
  # Порт уникален в пределах сервера (ADR-115 §4.1).
  while read -r s; do
    [ "$s" = "$SLUG" ] && continue
    [ "$(env_get "/opt/$s/.env" API_HOST_PORT)" = "$PORT" ] && fail port_in_use "API_HOST_PORT $PORT занят инстансом $s"
  done < <(list_instance_dirs)

  install -d -m 0755 "$D"
  copy_compose "$SLUG" || fail bundle_copy_failed
  if [ ! -f "$D/.env" ]; then
    for k in $REQUIRED_SECRETS; do
      [ -n "$(jin ".env.$k")" ] || fail "missing_field:env.$k"
    done
    PW="$(jin .env.POSTGRES_PASSWORD)"
    [[ "$PW" =~ ^[A-Za-z0-9._~-]{16,}$ ]] || fail bad_postgres_password "пароль БД: >=16 символов из [A-Za-z0-9._~-] (входит в DATABASE_URL)"
    while read -r k; do
      [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || fail "bad_env_key" "имя переменной вне ^[A-Z][A-Z0-9_]*$"
      json_has_ctl ".env.$k" && fail "bad_env_value:$k" "значение содержит перевод строки или нулевой байт"
      case "$COMPUTED_KEYS" in *" $k "*) fail "computed_env_key:$k" "эту переменную вычисляет инструмент";; esac
    done < <(jq -r '.env | keys[]' <<<"$STDIN_JSON")
    T="$(mktemp "$D/.env.new.XXXXXX")"; tmp_track "$T"; chmod 0600 "$T"
    # Заглушки шаблона вида <…> обнуляются: пустышка в рабочем .env не безобидна (инцидент
    # 2026-09-22 — литерал в KMS_LOCAL_MASTER_KEY ронял /v1/chat/run; 2026-09-02 — литерал в
    # APPSTORE_BUNDLE_ID отвергал каждую покупку). (E3) приходят в оверлей, ADR-115 §5.
    awk '/^[A-Z][A-Z0-9_]*=<.*>[[:space:]]*$/ { sub(/=.*/, "="); } { print }' "$D/.env.prod.example" > "$T"
    DBU="app_${SLUG//-/_}"; DBN="db_${SLUG//-/_}"
    env_set "$T" COMPOSE_PROJECT_NAME "$SLUG"
    env_set "$T" SERVICE_DOMAIN "$DOMAIN"
    env_set "$T" JWT_ISSUER "https://$DOMAIN"
    env_set "$T" POSTGRES_USER "$DBU"
    env_set "$T" POSTGRES_DB "$DBN"
    env_set "$T" DATABASE_URL "postgresql+asyncpg://$DBU:$PW@postgres:5432/$DBN"
    unset PW
    env_set "$T" REDIS_URL "redis://redis:6379/0"
    env_set "$T" WG_BIND_IP "$WGIP"
    env_set "$T" API_HOST_PORT "$PORT"
    # docker-compose.fleet.yml до ADR-115 §11 фазы 9 требует PG_HOST_PORT (публикация Postgres
    # в туннель ради репликации). Новому инстансу реплики нет, но compose без переменной не
    # стартует; значение — по схеме ports.txt (api 180NN ↔ pg 150NN), уникально на сервере.
    env_set "$T" PG_HOST_PORT "$((PORT - 3000))"
    env_set "$T" GUNICORN_WORKERS 2
    env_set "$T" TRAEFIK_CERTRESOLVER le
    env_set "$T" INSTANCE_UID "$UIDV"
    env_set "$T" INSTANCE_GENERATION "$GEN"
    env_set "$T" WALG_KEY_ID "$KID"
    # База флота (как в provision.sh): .env.prod.example — производственный шаблон, а флот живёт
    # в песочнице с открытой документацией и фактическим набором продуктов. Не вычисляемые —
    # env из stdin ниже их переопределяет.
    env_set "$T" DOCS_ENABLED true
    env_set "$T" APPSTORE_ENVIRONMENT sandbox
    env_set "$T" APPSTORE_ROOT_CERT_DIR /run/secrets/appstore_root_certs
    env_set "$T" STOREKIT_TEST_MODE true
    env_set "$T" STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION true
    env_set "$T" PRESETS_DEFAULT_LOCALE en
    env_set "$T" TOKEN_PRODUCTS '{"100_tokens_9.99":100,"250_tokens_19.99":250,"500_tokens_34.99":500,"1000_tokens_59.99":1000,"2000_tokens_99.99":2000}'
    env_set "$T" ADAPTY_PRODUCT_TOKENS '{"weekly_9.99_nottrial":100,"year_49.99_nottrial":1000}'
    env_set "$T" ADAPTY_SUBSCRIPTION_TOKENS_GRANT 100
    env_set "$T" TOKEN_PRODUCTS_DEFAULT 'weekly_9.99_nottrial,year_49.99_nottrial,100_tokens_9.99,250_tokens_19.99,500_tokens_34.99,1000_tokens_59.99,2000_tokens_99.99'
    while read -r k; do
      env_set "$T" "$k" "$(jin ".env.$k")"
    done < <(jq -r '.env | keys[]' <<<"$STDIN_JSON")
    mv -f "$T" "$D/.env"
    ev_bool env_rendered true
  else
    ev_bool env_rendered false; ev_str env_note "существующий .env того же INSTANCE_UID сохранён (повтор шага)"
  fi
  chmod 0600 "$D/.env"
  install -d -m 0700 "$D/.secrets"
  if [ ! -f "$D/.secrets/jwt_private.pem" ]; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$D/.secrets/jwt_private.pem" 2>/dev/null || fail jwt_keygen_failed
    openssl rsa -pubout -in "$D/.secrets/jwt_private.pem" -out "$D/.secrets/jwt_public.pem" 2>/dev/null || fail jwt_keygen_failed
  fi
  chown -R 10001:10001 "$D/.secrets"; chmod 0640 "$D/.secrets/"*.pem
  install -d -m 0755 "$D/certs/appstore"
  install -m 0644 "$BUNDLE/certs/AppleRootCA-G3.cer" "$D/certs/appstore/AppleRootCA-G3.cer" || fail apple_cert_missing
  ensure_media_assets "$SLUG" || log "ВНИМАНИЕ: media-assets не подготовлен (хранение не будет работать)"

  docker pull -q "$(walg_image)" >/dev/null 2>&1 || fail walg_image_pull_failed
  pull_and_tag "$SLUG" "$SHA" || fail app_image_pull_failed
  fence_state "$SLUG"; ev_str fence_at_start "$FENCE"
  bring_up_app "$SLUG"; rc=$?
  case "$rc" in 0) ;; 1) fail postgres_up_failed;; 2) fail postgres_not_healthy;; 3) fail migrate_failed;; *) fail api_not_healthy;; esac
  ev_str api_health "$(ctr_health "$(ctr "$SLUG" api)")"
  api_image_check "$SLUG" "$SHA"; [ "$IMG_RESULT" = match ] || fail api_image_mismatch
  ok
  ;;

rollback)
  require_root; slug_arg "${1:-}"; read_stdin_json
  jset UIDV .instance_uid; check_uid "$UIDV"
  lock_instance "$SLUG"
  if [ -f "$D/.env" ]; then
    [ "$(env_get "$D/.env" INSTANCE_UID)" = "$UIDV" ] || fail foreign_dir "INSTANCE_UID каталога не совпадает — откат чужого инстанса запрещён"
    # Защита пути — ДО любого разрушения (down -v и rm): каталог, COMPOSE_PROJECT_NAME и UID.
    safe_instance_path "$SLUG" "$UIDV" || fail unsafe_path "защита пути не пройдена — ничего не удалено"
    # Инвариант §7.3: откат только инстанса без пользовательских данных. Проверяет инструмент.
    pgc="$(ctr "$SLUG" postgres)"
    if docker volume inspect "$(proj_of "$SLUG")_pgdata" >/dev/null 2>&1; then
      ctr_running "$pgc" || dc "$SLUG" up -d --no-build --no-deps postgres >/dev/null 2>&1
      wait_healthy "$pgc" 30 || fail cannot_verify_users "Postgres не поднялся — отсутствие пользователей не доказано"
      reg="$(psql_inst "$SLUG" "SELECT to_regclass('public.users') IS NOT NULL")" || fail cannot_verify_users
      users=0
      if [ "$reg" = "t" ]; then
        users="$(psql_inst "$SLUG" "SELECT count(*) FROM users")" || fail cannot_verify_users
      fi
      ev_num users "$users"
      [ "$users" = 0 ] || fail has_users "в БД инстанса есть пользователи — откат запрещён (ADR-115 §7.3)"
    fi
    dc "$SLUG" down -v >/dev/null 2>&1
    rm -rf --one-file-system -- "$D"
  else
    [ -e "$D" ] && [ -n "$(ls -A "$D" 2>/dev/null)" ] && fail foreign_dir "$D без .env и не пуст — не трогаю"
    [ -d "$D" ] && rmdir "$D" 2>/dev/null
    # Контейнеры и тома с меткой проекта при ОТСУТСТВУЮЩЕМ .env НЕ удаляются: метка compose — один
    # признак, принадлежность этому INSTANCE_UID по ней не проверить. create пишет .env ДО любого
    # docker up, поэтому ресурсы проекта без .env созданы не этим create — они уходят в leftovers.
  fi
  collect_leftovers "$SLUG"
  ev_num leftovers_count "${#LEFT[@]}"
  [ "${#LEFT[@]}" -eq 0 ] || fail leftovers_remain
  ok
  ;;

enable-archive)
  require_root; slug_arg "${1:-}"; read_stdin_json
  jset UIDV .instance_uid; check_uid "$UIDV"
  jset KID .walg_key_id; check_keyid "$KID"
  jset GEN .instance_generation opt; GEN="${GEN:-1}"; check_gen "$GEN"
  lock_instance "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  cur="$(env_get "$D/.env" INSTANCE_UID)"
  [ -z "$cur" ] || [ "$cur" = "$UIDV" ] || fail foreign_uid "в .env уже другой INSTANCE_UID"
  # Только основной экземпляр (ADR-115 §8.2, §11 фаза 4): на резервах INSTANCE_UID не пишется.
  [ "$(tr -d ' \r\n' 2>/dev/null < "$D/.role")" = "standby" ] && fail not_primary ".role = standby"
  pgc="$(ctr "$SLUG" postgres)"
  ctr_running "$pgc" || fail postgres_not_running
  [ "$(psql_inst "$SLUG" 'SELECT pg_is_in_recovery()')" = "f" ] || fail not_primary "Postgres в режиме восстановления — это резерв"
  for f in walg.json bucket server_id "keys/$KID"; do [ -r "$WALG_DIR/$f" ] || fail "objstore_missing:$f"; done
  # meta.json обязан уже называть этот инстанс/поколение/сервер: иначе первый же сегмент WAL
  # будет отвергнут ограждением, и pg_wal начнёт расти (ADR-115 §8.4, §11 фаза 2).
  meta_read "$UIDV" || fail meta_unavailable
  [ "$(jq -r '.instance_uid' <<<"$META")" = "$UIDV" ] && [ "$(jq -r '.current_generation|tostring' <<<"$META")" = "$GEN" ] \
    && [ "$(jq -r '.server_id|tostring' <<<"$META")" = "$(server_id)" ] || fail meta_mismatch "meta.json не называет этот инстанс/поколение/сервер"
  WIMG="$(walg_image)"; docker pull -q "$WIMG" >/dev/null 2>&1 || fail walg_image_pull_failed
  already=0; [ "$cur" = "$UIDV" ] && ctr_running "$pgc" && [ "$(docker inspect -f '{{.Config.Image}}' "$pgc")" = "$WIMG" ] && already=1
  if [ "$already" = 0 ]; then
    BK="$D/.env.bak-enable-archive-$(date -u +%Y%m%dT%H%M%SZ)"; cp -p "$D/.env" "$BK"; tmp_track "$BK"
    t="$(mktemp "$D/.walg.XXXXXX")"; cp "$BUNDLE/docker-compose.walg.yml" "$t" && chmod 0644 "$t" && mv -f "$t" "$D/docker-compose.walg.yml"
    env_set "$D/.env" INSTANCE_UID "$UIDV"; env_set "$D/.env" INSTANCE_GENERATION "$GEN"; env_set "$D/.env" WALG_KEY_ID "$KID"
    if ! dc "$SLUG" up -d --no-build --no-deps postgres >/dev/null 2>&1 || ! wait_healthy "$pgc" 60; then
      # Возврат к прежнему Postgres: .env из копии, пересоздание без оверлея.
      cp -p "$BK" "$D/.env"; dc "$SLUG" up -d --no-build --no-deps postgres >/dev/null 2>&1
      fail postgres_restart_failed "Postgres с оверлеем архива не поднялся — .env возвращён из копии"
    fi
  fi
  docker exec -u postgres "$pgc" test -r /etc/fleet/walg/walg.json || fail walg_json_unreadable_by_postgres
  ev_bool walg_json_readable_by_postgres true
  verify_archiving "$SLUG"
  ok
  ;;

image)
  slug_arg "${1:-}"; SHA="${2:-}"; check_sha "$SHA"
  api_image_check "$SLUG" "$SHA"; r="$IMG_RESULT"
  ev_str result "$r"
  [ "$r" = absent ] && fail api_or_image_absent "контейнера api или образа SHA на сервере нет"
  ok
  ;;

redeploy)
  require_root; slug_arg "${1:-}"; SHA="${2:-}"; check_sha "$SHA"
  lock_instance "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  [ "$(bundle_version)" = "$SHA" ] || fail bundle_version_mismatch
  copy_compose "$SLUG" || fail bundle_copy_failed
  pull_and_tag "$SLUG" "$SHA" || fail app_image_pull_failed
  bring_up_app "$SLUG"; rc=$?
  case "$rc" in 0) ;; 1) fail postgres_up_failed;; 2) fail postgres_not_healthy;; 3) fail migrate_failed;; *) fail api_not_healthy;; esac
  ev_str api_health "$(ctr_health "$(ctr "$SLUG" api)")"
  api_image_check "$SLUG" "$SHA"; [ "$IMG_RESULT" = match ] || fail api_image_mismatch
  ok
  ;;

stop-for-erase)
  require_root; slug_arg "${1:-}"
  lock_instance "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  dc "$SLUG" stop api >/dev/null 2>&1
  ctr_running "$(ctr "$SLUG" api)" && fail api_still_running
  ctr_running "$(ctr "$SLUG" postgres)" || fail postgres_not_running "Postgres нужен для финального бэкапа (§9 шаг 3)"
  ev_bool api_running false; ev_bool postgres_running true
  ok
  ;;

halt)
  require_root; slug_arg "${1:-}"; nofinal=0; [ "${2:-}" = "--no-final-check" ] && nofinal=1
  lock_instance "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  UIDV="$(env_get "$D/.env" INSTANCE_UID)"
  if [ "$nofinal" = 0 ]; then
    read_stdin_json; jset U2 .instance_uid; [ "$U2" = "$UIDV" ] || fail foreign_uid
    # Порядок §9 шагов 3 → 4 держит сам инструмент: без СВЕРЕННОГО final/ остановки нет.
    [ -f "$D/.final-verified" ] || fail no_verified_final "fleet-backup final не завершился сверкой"
    key="$(awk '{print $1; exit}' "$D/.final-verified")"
    case "$key" in "instances/$UIDV/final/"*) ;; *) fail no_verified_final;; esac
    walg_host -- --root st ls "instances/$UIDV/final/" 2>/dev/null | grep -qF "$(basename "$key")" || fail final_not_in_bucket
    ev_str final_object "$key"
  else
    ev_bool final_check_skipped true
  fi
  dc "$SLUG" down >/dev/null 2>&1
  running="$(docker ps -q --filter "label=com.docker.compose.project=$(proj_of "$SLUG")" | wc -l)"
  vols="$(docker volume ls -q --filter "label=com.docker.compose.project=$(proj_of "$SLUG")" | wc -l)"
  ev_num running_containers "$running"; ev_num volumes "$vols"
  [ "$running" -eq 0 ] || fail containers_still_running
  [ "$vols" -gt 0 ] || fail volumes_missing "тома проекта не найдены — данные не на месте"
  ok
  ;;

purge)
  require_root; slug_arg "${1:-}"; read_stdin_json
  jset UIDV .instance_uid; check_uid "$UIDV"
  lock_instance "$SLUG"
  safe_instance_path "$SLUG" "$UIDV" || fail unsafe_path "защита пути (ADR-115 §9 шаг 5): каталог, COMPOSE_PROJECT_NAME или INSTANCE_UID не совпали — ничего не удалено"
  dc "$SLUG" down -v >/dev/null 2>&1
  rm -rf --one-file-system -- "$D"
  collect_leftovers "$SLUG"
  ev_num leftovers_count "${#LEFT[@]}"
  [ "${#LEFT[@]}" -eq 0 ] || fail leftovers_remain
  ok
  ;;

rekey)
  require_root; slug_arg "${1:-}"; read_stdin_json
  jset NG .new_generation; check_gen "$NG"; jset KID .walg_key_id; check_keyid "$KID"
  lock_instance "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  UIDV="$(env_get "$D/.env" INSTANCE_UID)"; check_uid "$UIDV"
  CG="$(env_get "$D/.env" INSTANCE_GENERATION)"; check_gen "$CG"
  [ -r "$WALG_DIR/keys/$KID" ] || fail key_missing
  if [ "$CG" != "$NG" ]; then
    [ "$NG" = "$((CG + 1))" ] || fail bad_generation "new_generation обязан быть текущее + 1"
    meta_read "$UIDV" || fail meta_unavailable
    [ "$(jq -r '.current_generation|tostring' <<<"$META")" = "$NG" ] && [ "$(jq -r '.server_id|tostring' <<<"$META")" = "$(server_id)" ] \
      || fail meta_mismatch "meta.json ещё не переведён на новое поколение этого сервера (§8.7 шаг 2)"
    env_set "$D/.env" INSTANCE_GENERATION "$NG"; env_set "$D/.env" WALG_KEY_ID "$KID"
    dc "$SLUG" up -d --no-build --no-deps postgres >/dev/null 2>&1
  fi
  wait_healthy "$(ctr "$SLUG" postgres)" 60 || fail postgres_not_healthy
  require_fence_ok "$SLUG"
  [ "$(env_get "$D/.env" INSTANCE_GENERATION)" = "$NG" ] && [ "$(env_get "$D/.env" WALG_KEY_ID)" = "$KID" ] || fail env_not_updated
  walg_in_pg "$SLUG" backup-push /var/lib/postgresql/data >/dev/null 2>&1 || fail backup_push_failed
  last="$(walg_in_pg "$SLUG" backup-list --json 2>/dev/null | jq -r 'last | .backup_name // empty')"
  [ -n "$last" ] || fail backup_not_listed
  ev_num generation "$NG"; ev_str walg_key_id "$KID"; ev_str backup_name "$last"
  verify_archiving "$SLUG"
  ok
  ;;

restore)
  # Runbook ADR-115 §12, шаги 4–6 — ТОЛЬКО оператор (Р4: кнопки в CRM нет). До вызова оператор
  # выполнил шаги 0–3: meta.json переведён на поколение N+1 и этот сервер; каталог /opt/<slug>
  # подготовлен (бандл версии last_deployed_sha), .env и .secrets из config/-архива с WG_BIND_IP,
  # API_HOST_PORT и INSTANCE_GENERATION = N+1 целевого сервера.
  require_root; slug_arg "${1:-}"; read_stdin_json
  jset SRC_GEN .source_generation; check_gen "$SRC_GEN"
  jset SRC_KID .source_key_id opt; jset BNAME .backup_name opt; BNAME="${BNAME:-LATEST}"
  jset TT .target_time opt; jset SHA .sha; check_sha "$SHA"
  [[ "$BNAME" =~ ^(LATEST|base_[0-9A-F]{24}(_D_[0-9A-F]{24})?)$ ]] || fail bad_backup_name
  [ -z "$TT" ] || [[ "$TT" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}[T\ ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}(:[0-9]{2})?)$ ]] || fail bad_target_time
  lock_instance "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  UIDV="$(env_get "$D/.env" INSTANCE_UID)"; check_uid "$UIDV"
  NG="$(env_get "$D/.env" INSTANCE_GENERATION)"; check_gen "$NG"
  [ "$NG" = "$((SRC_GEN + 1))" ] || fail bad_generation ".env обязан нести поколение source_generation + 1"
  KID="$(env_get "$D/.env" WALG_KEY_ID)"; SRC_KID="${SRC_KID:-$KID}"; check_keyid "$SRC_KID"
  fence_state "$SLUG"; [ "$FENCE" = ok ] || fail meta_mismatch "meta.json не называет поколение $NG и этот сервер (§12 шаг 0)"
  P="$(proj_of "$SLUG")"; VOL="${P}_pgdata"
  # Пустота тома проверяется ДО остановки: непустой том = живые данные, их нельзя даже гасить.
  if docker volume inspect "$VOL" >/dev/null 2>&1; then
    n="$(docker run --rm -v "$VOL:/d:ro" --entrypoint sh "$(walg_image)" -c 'ls -A /d | wc -l')"
    [ "$n" = 0 ] || fail pgdata_not_empty "том $VOL не пуст — восстановление поверх данных запрещено"
  fi
  dc "$SLUG" down >/dev/null 2>&1
  pull_and_tag "$SLUG" "$SHA" || fail app_image_pull_failed
  dc "$SLUG" create --no-build postgres >/dev/null 2>&1 || fail compose_create_failed
  RC="fleet-restore-$SLUG-$(date -u +%Y%m%d%H%M%S)"
  on_exit 'docker rm -f "$RC" >/dev/null 2>&1'
  extra=(-c archive_mode=off -c "restore_command=fleet-walg wal-fetch %f %p" -c recovery_target_action=promote)
  [ -n "$TT" ] && extra+=(-c "recovery_target_time=$TT")
  docker run -d --name "$RC" -u postgres -v "$VOL:/var/lib/postgresql/data" -v "$WALG_DIR:/etc/fleet/walg:ro" \
    -e FLEET_INSTANCE_UID="$UIDV" -e FLEET_INSTANCE_GENERATION="$SRC_GEN" -e FLEET_WALG_KEY_ID="$SRC_KID" \
    --entrypoint bash "$(walg_image)" -c \
    "fleet-walg backup-fetch /var/lib/postgresql/data '$BNAME' && chmod 0700 /var/lib/postgresql/data && touch /var/lib/postgresql/data/recovery.signal && exec postgres $(printf '%q ' "${extra[@]}")" \
    >/dev/null || fail restore_container_failed
  done_=0
  for _ in $(seq 1 1800); do
    ctr_running "$RC" || break
    r="$(docker exec "$RC" psql -X -U "$(env_get "$D/.env" POSTGRES_USER)" -d "$(env_get "$D/.env" POSTGRES_DB)" -tAc 'SELECT pg_is_in_recovery()' 2>/dev/null)"
    [ "$r" = "f" ] && { done_=1; break; }
    sleep 2
  done
  if [ "$done_" != 1 ]; then
    ev_str recovery_log_tail "$(docker logs --tail 20 "$RC" 2>&1 | tr '\n' ' ' | cut -c1-2000)"
    fail recovery_not_finished "база не вышла из восстановления"
  fi
  docker exec -u postgres "$RC" pg_ctl stop -m fast -D /var/lib/postgresql/data >/dev/null 2>&1
  docker rm -f "$RC" >/dev/null 2>&1
  # Новая линия архива — в префикс g<N+1>; первая полная копия ОБЯЗАТЕЛЬНА (§12 шаг 5).
  dc "$SLUG" up -d --no-build --no-deps postgres redis >/dev/null 2>&1
  wait_healthy "$(ctr "$SLUG" postgres)" 90 || fail postgres_not_healthy
  walg_in_pg "$SLUG" backup-push /var/lib/postgresql/data >/dev/null 2>&1 || fail first_backup_failed
  ev_str first_backup "$(walg_in_pg "$SLUG" backup-list --json 2>/dev/null | jq -r 'last | .backup_name // empty')"
  verify_archiving "$SLUG"
  bring_up_app "$SLUG"; rc=$?
  [ "$rc" = 0 ] || fail "app_up_failed:$rc"
  api_image_check "$SLUG" "$SHA"; [ "$IMG_RESULT" = match ] || fail api_image_mismatch
  ev_num generation "$NG"
  ok
  ;;

*) unknown_subcommand "instance.$SUB";;
esac
