# shellcheck shell=bash
# Общая часть инструментов флота (ADR-115 §13). Подключается через `source`, сама не запускается.
#
# Контракт вывода (ADR-115 §13): последняя строка stdout — `FLEET-RESULT <json>`
#   {"ok":true|false,"step":"…","reason":"…","leftovers":[…],"evidence":{…},"tools_version":"…"}
# код возврата 0 — успех (в т.ч. идемпотентный повтор), ≠0 — отказ, 2 — unknown_subcommand.
# `ok:true` допустим ТОЛЬКО вместе с evidence — наблюдаемым постусловием (ok() отказывает,
# если evidence пуст). Диагностика — в stderr. Значения секретов не печатаются НИГДЕ, в т.ч.
# частично: секреты приходят одной JSON-строкой в stdin и живут только в переменных процесса.
#
# JSON собирается без jq: `fleet-server version` и начало `bootstrap` исполняются на сервере,
# где jq ещё не установлен. Разбор stdin — через jq (bootstrap сначала читает stdin, потом ставит jq).

set -uo pipefail
umask 077

FLEET_VERSION_DIR="${FLEET_VERSION_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
# shellcheck source=../fleet.conf
. "$FLEET_VERSION_DIR/fleet.conf"
TOOLS_VERSION="$(tr -d ' \r\n' 2>/dev/null < "$FLEET_VERSION_DIR/VERSION")"
[ -n "$TOOLS_VERSION" ] || TOOLS_VERSION="unknown"

STEP="${STEP:-}"
REASON=""
EV=()
LEFT=()
RESULT_TO_STDERR="${RESULT_TO_STDERR:-0}"
STDIN_JSON=""

# --- JSON ------------------------------------------------------------------------------------
json_str() {
  local s="$1"
  s="$(printf '%s' "$s" | tr -d '\000-\010\013\014\016-\037')"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"
  s="${s//$'\n'/\\n}"; s="${s//$'\r'/\\r}"; s="${s//$'\t'/\\t}"
  printf '"%s"' "$s"
}
ev_str()  { EV+=("$(json_str "$1"):$(json_str "$2")"); }
ev_num()  { if [[ "$2" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then EV+=("$(json_str "$1"):$2"); else ev_str "$1" "$2"; fi; }
ev_bool() { if [ "$2" = "true" ]; then EV+=("$(json_str "$1"):true"); else EV+=("$(json_str "$1"):false"); fi; }
ev_json() { EV+=("$(json_str "$1"):$2"); }       # $2 — уже валидный JSON (строится jq'ом)
left_add() { LEFT+=("$(json_str "$1")"); }

emit() {
  local ok="$1" ev left line
  ev="$(IFS=,; printf '%s' "${EV[*]:-}")"
  left="$(IFS=,; printf '%s' "${LEFT[*]:-}")"
  line="FLEET-RESULT {\"ok\":$ok,\"step\":$(json_str "$STEP"),\"reason\":$(json_str "$REASON"),\"leftovers\":[$left],\"evidence\":{$ev},\"tools_version\":$(json_str "$TOOLS_VERSION")}"
  if [ "$RESULT_TO_STDERR" = 1 ]; then printf '%s\n' "$line" >&2; else printf '%s\n' "$line"; fi
}
log()  { printf '[%s] %s\n' "${STEP:-fleet}" "$*" >&2; }
ok()   {
  if [ "${#EV[@]}" -eq 0 ]; then REASON="no_evidence"; emit false; exit 1; fi
  REASON="${1:-}"; emit true; exit 0
}
fail() { REASON="$1"; shift; [ "$#" -gt 0 ] && log "$*"; emit false; exit "${FAIL_CODE:-1}"; }
unknown_subcommand() { STEP="${1:-}"; REASON="unknown_subcommand"; emit false; exit 2; }

# --- окружение -------------------------------------------------------------------------------
require_root() { [ "$(id -u)" = 0 ] || fail not_root "инструменты флота исполняются от root"; }
need() { local c; for c in "$@"; do command -v "$c" >/dev/null 2>&1 || fail "missing_tool:$c" "не найдена утилита $c"; done; }

# Временные файлы с секретами (креды, .env.new, ключи, незашифрованные дампы) регистрируются
# здесь и удаляются на ЛЮБОМ выходе, включая fail(): иначе отказ посреди подкоманды оставлял бы
# их на диске. Уже переименованный (mv) временный путь не существует — rm -f его просто пропускает.
TMP_TRACK=()
EXIT_HOOKS=()
tmp_track() { TMP_TRACK+=("$1"); }
# on_exit '<команда>' — уборка одноразовых контейнеров/сетей/томов на любом выходе. Подкоманды
# НЕ ставят свой trap EXIT: он затёр бы уборку временных файлов с секретами.
on_exit() { EXIT_HOOKS+=("$1"); }
_tmp_cleanup() {
  local f h
  for h in "${EXIT_HOOKS[@]:-}"; do [ -n "$h" ] && eval "$h"; done
  for f in "${TMP_TRACK[@]:-}"; do [ -n "$f" ] && rm -rf -- "$f"; done
}
trap _tmp_cleanup EXIT

# Значение из stdin, которое пишется в ПОСТРОЧНЫЙ файл (.env, objstore.env, wg0.conf,
# authorized_keys), обязано быть одной строкой: перевод строки внёс бы в файл лишнюю запись
# (чужую переменную окружения, лишний ключ SSH, лишний пир). Нулевой байт bash и так отбрасывает,
# поэтому он проверяется по JSON (jq), а не по переменной.
one_line() { case "$1" in *$'\n'*|*$'\r'*) return 1;; esac; return 0; }
json_has_ctl() {  # json_has_ctl '<jq-путь>' — 0, если строка по пути содержит \n, \r или \u0000
  jq -e "($1) | tostring | test(\"[\\n\\r\\u0000]\")" >/dev/null 2>&1 <<<"$STDIN_JSON"
}
# Ровно ОДНА строка открытого ключа SSH известного типа (ADR-115 §8.7): без переводов строк,
# без опций authorized_keys перед типом, разбирается ssh-keygen.
PUBKEY_TYPES="ssh-ed25519 ssh-rsa ecdsa-sha2-nistp256 ecdsa-sha2-nistp384 ecdsa-sha2-nistp521 sk-ssh-ed25519@openssh.com sk-ecdsa-sha2-nistp256@openssh.com"
valid_pubkey_line() {
  local k="$1" t
  one_line "$k" || return 1
  t="${k%% *}"
  case " $PUBKEY_TYPES " in *" $t "*) ;; *) return 1;; esac
  printf '%s\n' "$k" | ssh-keygen -lf - >/dev/null 2>&1
}

read_stdin_json() {
  need jq
  STDIN_JSON="$(cat)"
  [ -n "$STDIN_JSON" ] || fail stdin_empty "ожидалась одна JSON-строка в stdin"
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$STDIN_JSON" || fail stdin_not_json "stdin не JSON-объект"
}
# jin '<jq-путь>' — значение или пусто; секреты НЕ печатаются, только присваиваются.
jin() { jq -r "($1) // empty | if type == \"string\" then . else tojson end" <<<"$STDIN_JSON" 2>/dev/null; }
# jset <ПЕРЕМЕННАЯ> '<jq-путь>' [opt] — присвоить значение поля; без opt пустое поле = отказ.
# Не через $(…): fail внутри подстановки завершил бы только подоболочку, и FLEET-RESULT ушёл бы
# в переменную, а не наружу.
jset() {
  local __v; __v="$(jin "$2")"
  if [ -z "$__v" ] && [ "${3:-}" != "opt" ]; then fail "missing_field:${2#.}" "в stdin нет поля ${2#.}"; fi
  printf -v "$1" '%s' "$__v"
}

# --- валидаторы ------------------------------------------------------------------------------
SLUG_RE='^[a-z][a-z0-9-]{1,30}$'
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
GEN_RE='^[1-9][0-9]{0,8}$'
KEYID_RE='^[0-9a-f]{12}$'
SHA_RE='^[0-9a-f]{40}$'
# shellcheck disable=SC2034  # используют подключающие скрипты (fleet-server/instance/route)
IPV4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
# shellcheck disable=SC2034  # используют подключающие скрипты (fleet-server/instance/route)
DOMAIN_RE='^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'
# Зарезервированные имена (ADR-115 §4.1, снимок). Имя служебного каталога /opt, здесь не
# названное, защищается второй проверкой: create отказывается, если каталог уже есть и его .env
# не несёт INSTANCE_UID создаваемого инстанса.
RESERVED_SLUGS="fleet router edge backup backups tmp containerd"

is_reserved() { local r; for r in $RESERVED_SLUGS; do [ "$1" = "$r" ] && return 0; done; return 1; }
check_slug() {
  [[ "$1" =~ $SLUG_RE ]] || fail bad_slug "slug не проходит ^[a-z][a-z0-9-]{1,30}\$"
  is_reserved "$1" && fail reserved_slug "slug зарезервирован"
  return 0
}
check_uid()   { [[ "$1" =~ $UUID_RE ]] || fail bad_instance_uid "INSTANCE_UID не UUID"; }
check_gen()   { [[ "$1" =~ $GEN_RE ]]  || fail bad_generation "INSTANCE_GENERATION не целое >= 1"; }
check_keyid() { [[ "$1" =~ $KEYID_RE ]] || fail bad_key_id "key_id не 12 hex"; }
check_sha()   { [[ "$1" =~ $SHA_RE ]]  || fail bad_sha "SHA не 40 hex"; }

# --- .env ------------------------------------------------------------------------------------
env_get() {  # env_get <файл> <КЛЮЧ> — значение первой строки КЛЮЧ=…, кавычки по краям снимаются
  [ -f "$1" ] || return 0
  awk -v k="$2" 'index($0, k "=") == 1 { v = substr($0, length(k) + 2); sub(/\r$/, "", v);
    if (v ~ /^".*"$/ || v ~ /^'"'"'.*'"'"'$/) v = substr(v, 2, length(v) - 2); print v; exit }' "$1"
}
env_set() {  # env_set <файл> <КЛЮЧ> <ЗНАЧЕНИЕ> — заменить или дописать атомарно, права сохраняются
  local f="$1" k="$2" v="$3" t
  t="$(mktemp "$(dirname "$f")/.env.tmp.XXXXXX")" || return 1
  if [ -f "$f" ]; then chmod --reference="$f" "$t" 2>/dev/null; chown --reference="$f" "$t" 2>/dev/null; fi
  if [ -f "$f" ] && grep -q "^$k=" "$f"; then
    K="$k" V="$v" awk 'BEGIN { k = ENVIRON["K"]; v = ENVIRON["V"] }
      index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next } { print }' "$f" > "$t"
  else
    { [ -f "$f" ] && cat "$f"; printf '%s=%s\n' "$k" "$v"; } > "$t"
  fi
  mv -f "$t" "$f"
}

# --- инстанс ---------------------------------------------------------------------------------
inst_dir() { printf '/opt/%s' "$1"; }
proj_of() {  # имя compose-проекта: COMPOSE_PROJECT_NAME из .env, иначе имя каталога
  local p; p="$(env_get "$(inst_dir "$1")/.env" COMPOSE_PROJECT_NAME)"; printf '%s' "${p:-$1}"
}
img_proj_of() {  # префикс тега образа: как в docker-compose.prod.yml — ${COMPOSE_PROJECT_NAME:-claude-ios}
  local p; p="$(env_get "$(inst_dir "$1")/.env" COMPOSE_PROJECT_NAME)"; printf '%s' "${p:-claude-ios}"
}
# Набор файлов compose собирается ПО ПРИЗНАКАМ, одинаково у всех вызывающих (ADR-115 §8.2,
# §10.2 п. 3): fleet — при наличии API_HOST_PORT в .env; walg — при НЕПУСТОМ INSTANCE_UID.
compose_files() {
  local d; d="$(inst_dir "$1")"
  printf '%s' "-f docker-compose.prod.yml"
  grep -q '^API_HOST_PORT=' "$d/.env" 2>/dev/null && printf ' %s' "-f docker-compose.fleet.yml"
  [ -n "$(env_get "$d/.env" INSTANCE_UID)" ] && printf ' %s' "-f docker-compose.walg.yml"
  return 0
}
dc() {  # dc <slug> <аргументы docker compose…>
  local s="$1"; shift
  local d cf p; d="$(inst_dir "$s")"; p="$(proj_of "$s")"; cf="$(compose_files "$s")"
  # shellcheck disable=SC2086  # cf — набор флагов -f, разбиение по пробелам намеренное
  (cd "$d" && docker compose -p "$p" $cf --env-file .env "$@")
}
ctr() { printf '%s-%s-1' "$(proj_of "$1")" "$2"; }
ctr_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]; }
ctr_health() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null || echo absent; }

psql_inst() {  # psql_inst <slug> <SQL> — вывод -tA; секреты не участвуют (локальный сокет, trust)
  local s="$1" d u db; d="$(inst_dir "$s")"
  u="$(env_get "$d/.env" POSTGRES_USER)"; db="$(env_get "$d/.env" POSTGRES_DB)"
  docker exec -i "$(ctr "$s" postgres)" psql -X -v ON_ERROR_STOP=1 -U "${u:-postgres}" -d "${db:-postgres}" -tAc "$2"
}

wait_healthy() {  # wait_healthy <контейнер> <попыток по 2 с>
  local c="$1" n="${2:-45}" h
  for _ in $(seq 1 "$n"); do
    h="$(ctr_health "$c")"
    [ "$h" = "healthy" ] && return 0
    sleep 2
  done
  return 1
}

lock_instance() {  # общий лок инстанса для CRM-операций и шага деплоя CI (ADR-115 §13)
  mkdir -p "$LOCK_DIR"
  exec {LOCK_FD}>"$LOCK_DIR/$1.lock" || fail lock_open_failed
  flock -w "${FLEET_LOCK_WAIT:-900}" "$LOCK_FD" || fail lock_timeout "лок инстанса $1 занят дольше ${FLEET_LOCK_WAIT:-900}s"
}

server_id() { tr -d ' \r\n' < "$FLEET_ETC/server_id" 2>/dev/null; }

# --- образы ----------------------------------------------------------------------------------
walg_image() {  # тег образа Postgres с wal-g — из оверлея бандла ЭТОЙ версии инструментов
  awk '/^[[:space:]]*image:[[:space:]]*/ { print $2; exit }' "$FLEET_VERSION_DIR/compose/docker-compose.walg.yml" 2>/dev/null
}
app_image() { printf '%s:%s' "$APP_IMAGE_REPO" "$1"; }
image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null; }

# --- хранилище с хоста -----------------------------------------------------------------------
# Задания хоста идут через ту же обёртку fleet-walg в образе Postgres (один бинарник, одно
# шифрование): `docker run --rm --env-file objstore.env`. Креды передаются файлом окружения, а
# не аргументами — argv виден в `ps` и журналах. На R контейнеров Postgres нет, но образ тот же.
#   walg_host [-i UID GEN] [-k KEYID] [-v ХОСТ:КОНТЕЙНЕР]… -- <аргументы fleet-walg…>
walg_host() {
  local uid="" gen="" kid="" mounts=() img
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -i) uid="$2"; gen="$3"; shift 3;;
      -k) kid="$2"; shift 2;;
      -v) mounts+=(-v "$2"); shift 2;;
      --) shift; break;;
      *) break;;
    esac
  done
  img="$(walg_image)"; [ -n "$img" ] || return 70
  [ -r "$OBJSTORE_DIR/objstore.env" ] || return 71
  local envs=(-e "FLEET_WALG_DIR=/etc/fleet/walg")
  [ -n "$uid" ] && envs+=(-e "FLEET_INSTANCE_UID=$uid" -e "FLEET_INSTANCE_GENERATION=$gen")
  [ -n "$kid" ] && envs+=(-e "FLEET_WALG_KEY_ID=$kid")
  [ -d "$WALG_DIR" ] && mounts+=(-v "$WALG_DIR:/etc/fleet/walg:ro")
  docker run --rm -i --network host --env-file "$OBJSTORE_DIR/objstore.env" "${envs[@]}" "${mounts[@]}" \
    --entrypoint fleet-walg "$img" "$@"
}

# meta_read <UID> — пишет meta.json в переменную META; код 0 — прочитан, иначе недоступен.
META=""
meta_read() {
  META="$(walg_host -- --root st cat "registry/$1/meta.json" 2>/dev/null)" || { META=""; return 1; }
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$META" || { META=""; return 1; }
}

# fence_state <slug> — ограждение по поколению (ADR-115 §8.4). Пишет в глобальную FENCE одно из
# значений ниже и оставляет прочитанный meta.json в META. Вызывать НЕ через $(…).
#   disabled     — архив не включён (INSTANCE_UID пуст): ограждать нечего;
#   ok           — meta.json называет этот инстанс, это поколение и этот сервер;
#   other_server — server_id в meta.json другой: экземпляр устарел;
#   transition   — сервер тот же, current_generation = поколение .env + 1 (смена ключа);
#   mismatch     — любое иное несовпадение;
#   unavailable  — meta.json не прочитан (сеть, креды): ограждение НЕ вычислимо, записи
#                  запрещены, остановки нет — отсутствие ответа не доказывает устаревания.
# FENCE_REASON — ЧТО именно разошлось (для fenced_instances, ADR-115 §8.4): server_mismatch,
# generation_mismatch, uid_mismatch, bad_env_format; пусто при ok/disabled/unavailable/transition.
FENCE_REASON=""
fence_state() {
  local d uid gen sid mg ms mu
  FENCE=""; META=""; FENCE_REASON=""
  d="$(inst_dir "$1")"
  uid="$(env_get "$d/.env" INSTANCE_UID)"; gen="$(env_get "$d/.env" INSTANCE_GENERATION)"
  [ -z "$uid" ] && { FENCE=disabled; return; }
  if ! [[ "$uid" =~ $UUID_RE && "$gen" =~ $GEN_RE ]]; then FENCE=mismatch; FENCE_REASON=bad_env_format; return; fi
  sid="$(server_id)"
  # Без /etc/fleet/server_id ограждение не вычислимо: это НЕ доказательство устаревания, поэтому
  # не останавливаем, а только запрещаем запись (unavailable).
  [ -z "$sid" ] && { FENCE=unavailable; return; }
  meta_read "$uid" || { FENCE=unavailable; return; }
  mu="$(jq -r '.instance_uid // empty' <<<"$META")"
  mg="$(jq -r '.current_generation // empty | tostring' <<<"$META")"
  ms="$(jq -r '.server_id // empty | tostring' <<<"$META")"
  if [ "$mu" != "$uid" ]; then FENCE=mismatch; FENCE_REASON=uid_mismatch; return; fi
  if [ "$ms" != "$sid" ]; then FENCE=other_server; FENCE_REASON=server_mismatch; return; fi
  if [ "$mg" = "$gen" ]; then FENCE=ok; return; fi
  if [[ "$mg" =~ ^[0-9]+$ ]] && [ "$mg" = "$((gen + 1))" ]; then FENCE=transition; return; fi
  FENCE=mismatch; FENCE_REASON=generation_mismatch
}
# fence_enforce <slug> <state> — самоограждение: остановить ВСЕ контейнеры инстанса (без -v).
# Отметка «<время> <причина>» (причина — FENCE_REASON последнего fence_state) хранится в
# $STATE_DIR/fenced/<slug> до ручного разбора и уходит в fenced_instances heartbeat сервера.
fence_enforce() {
  local s="$1" st="$2" why="${FENCE_REASON:-$2}"
  mkdir -p "$STATE_DIR/fenced"
  dc "$s" down >/dev/null 2>&1
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$why" > "$STATE_DIR/fenced/$s"
  logger -t fleet "fence: instance $s stopped ($why)" 2>/dev/null || true
}
# require_fence_ok <slug> — перед ЛЮБОЙ записью/удалением в бакете (ADR-115 §8.2 слой 3).
require_fence_ok() {
  local st; fence_state "$1"; st="$FENCE"
  ev_str fence "$st"
  case "$st" in
    ok) return 0;;
    disabled) fail archive_disabled "архив инстанса не включён (INSTANCE_UID пуст)";;
    transition) fail generation_transition "идёт переход поколения — запись отложена";;
    unavailable) fail meta_unavailable "meta.json не прочитан — ограждение не вычислимо, запись запрещена";;
    other_server|mismatch) fence_enforce "$1" "$st"; fail "fenced:$st" "ограждение: экземпляр остановлен";;
  esac
}

# --- media-assets (ADR-109 §1; логика перенесена из infra/fleet/provision.sh без изменений) ----
ensure_media_assets() {
  local DIR; DIR="$(inst_dir "$1")"
  local d="$DIR/media-assets" m="$DIR/media-assets/.media-assets-root" line="" t
  if [ -L "$d" ] || { [ -e "$d" ] && [ ! -d "$d" ]; }; then log "ВНИМАНИЕ: $d не каталог"; return 1; fi
  mkdir -p "$d" && chown -h 10001:10001 "$d" && chmod 0750 "$d" || return 1
  if [ -L "$m" ] || { [ -e "$m" ] && [ ! -f "$m" ]; }; then log "ВНИМАНИЕ: $m — ссылка или спецфайл"; return 1; fi
  [ "$(stat -c %d "$DIR")" = "$(stat -c %d "$d")" ] || return 1
  [ -f "$m" ] && line="$(timeout 5 grep -m1 -E '^pg_system_identifier=[0-9]+$' -- "$m" 2>/dev/null)"
  t="$(mktemp "$DIR/.media-assets-root.tmp.XXXXXX")" || return 1
  if { [ -z "$line" ] || printf '%s\n' "$line" > "$t"; } && chown 10001:10001 "$t" && chmod 0640 "$t" \
     && mv -fT -- "$t" "$m"; then return 0; fi
  rm -f -- "$t"; return 1
}

# --- обход инстансов сервера -----------------------------------------------------------------
# Инстанс на сервере = каталог /opt/<slug> с файлом .env (так же считает сверка множества CI).
list_instance_dirs() {
  local d n
  for d in /opt/*/; do
    n="$(basename "$d")"
    [ -f "/opt/$n/.env" ] && [ ! -L "/opt/$n" ] && printf '%s\n' "$n"
  done
}
list_archive_enabled() {
  local n
  while read -r n; do
    [ -n "$(env_get "/opt/$n/.env" INSTANCE_UID)" ] && printf '%s\n' "$n"
  done < <(list_instance_dirs)
}

# Сравнение со строкой-правилом для удаления каталога (ADR-115 §9 шаг 5): удаляется только
# /opt/<slug>, где slug проходит регулярное выражение, путь канонизирован и равен /opt/<slug>,
# а .env несёт COMPOSE_PROJECT_NAME=<slug> И INSTANCE_UID=<uid>.
safe_instance_path() {
  local s="$1" uid="$2" d real
  [[ "$s" =~ $SLUG_RE ]] || return 1
  is_reserved "$s" && return 1
  d="/opt/$s"
  [ -d "$d" ] && [ ! -L "$d" ] || return 1
  real="$(readlink -f "$d")" || return 1
  [ "$real" = "$d" ] || return 1
  [ "$(env_get "$d/.env" COMPOSE_PROJECT_NAME)" = "$s" ] || return 1
  [ "$(env_get "$d/.env" INSTANCE_UID)" = "$uid" ] || return 1
  return 0
}

# Остатки инстанса на сервере — для leftovers отката/стирания (ADR-115 §7.3).
collect_leftovers() {
  local s="$1" p x
  p="$s"
  while read -r x; do [ -n "$x" ] && left_add "container:$x"; done \
    < <(docker ps -a --filter "label=com.docker.compose.project=$p" --format '{{.Names}}' 2>/dev/null)
  while read -r x; do [ -n "$x" ] && left_add "volume:$x"; done \
    < <(docker volume ls -q --filter "label=com.docker.compose.project=$p" 2>/dev/null)
  [ -e "/opt/$s" ] && left_add "dir:/opt/$s"
  return 0
}
