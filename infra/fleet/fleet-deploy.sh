#!/bin/bash
# Шаг деплоя CI на ОДНОМ сервере (ADR-115 §10.2 пп. 3–5). Не подкоманда CRM: его вызывает только
# CI (infra/fleet/ci/deploy-fleet.sh) из только что синхронизированной версии:
#
#   flock -s /opt/fleet.d/<sha>/.inuse env FLEET_VERSION_DIR=/opt/fleet.d/<sha> \
#     bash /opt/fleet.d/<sha>/fleet-deploy.sh  < {"sha":"…","instances":[{slug,instance_uid,state},…]}
#
# instances — ВСЕ инстансы inventory этого сервера (creating|active|erasing|stopped|failed).
# Выкатываются только active, порциями; прочие пропускаются с ::notice — их ведёт CRM.
# Для каждого active: лок инстанса (тот же, что у CRM-операций, ADR-115 §13) → файлы compose из
# бандла /opt/fleet/compose этой версии → docker tag образа в <проект>-backend:prod →
# migrate (--no-deps) → up -d --no-build --no-deps api → гейт готовности. postgres и redis НЕ
# пересоздаются (ADR-115 §10.2 п. 5). .role НЕ читается.
# Затем сверка множества (п. 4): каталог с .env вне inventory — ::warning (сирота, не трогаем);
# инстанс inventory без каталога — красный.
set -uo pipefail
# shellcheck source=lib/common.sh
. "$(dirname "$(readlink -f "$0")")/lib/common.sh"
# shellcheck disable=SC2034  # STEP читает lib/common.sh (emit)
STEP="deploy"

require_root; read_stdin_json
jset SHA .sha; check_sha "$SHA"
[ "$(tr -d ' \r\n' < "$FLEET_VERSION_DIR/VERSION")" = "$SHA" ] || fail bundle_version_mismatch "версия инструментов ≠ SHA деплоя"
IMG="$(app_image "$SHA")"
BUNDLE="$FLEET_VERSION_DIR/compose"
COMPOSE_SET="docker-compose.prod.yml docker-compose.fleet.yml docker-compose.walg.yml .env.prod.example"

# ---- пред-полётная проверка нагрузки. Функция ОПРЕДЕЛЕНА ДО первого вызова: в прежнем
# workflow wait_for_calm вызывался раньше своего определения и молча не делал ничего
# («command not found» без set -e), то есть пред-полётной проверки не было вовсе.
BATCH_SIZE="${FLEET_BATCH_SIZE:-4}"
LOAD_CEILING="$(nproc)"
BATCH_WAIT_MAX=60   # 60 × 10 с = 10 минут на порцию
load1() { awk '{printf "%d", $1}' /proc/loadavg; }
wait_for_calm() {
  local i=0
  while [ "$(load1)" -ge "$LOAD_CEILING" ] && [ "$i" -lt "$BATCH_WAIT_MAX" ]; do
    [ "$i" -eq 0 ] && echo "[deploy] load=$(load1) >= $LOAD_CEILING — ждём разгрузки"
    sleep 10; i=$((i+1))
  done
  if [ "$i" -ge "$BATCH_WAIT_MAX" ]; then
    echo "::warning title=Load did not settle ($(hostname))::load=$(load1) >= $LOAD_CEILING после $((BATCH_WAIT_MAX*10))s; продолжаем"
  fi
}

echo "[deploy] $(hostname): docker pull $IMG (один раз на сервер)"
nice -n 19 docker pull -q "$IMG" >/dev/null || fail app_image_pull_failed "docker pull $IMG не прошёл — ни один инстанс не тронут"
wait_for_calm

DEPLOYED=(); FAILED=(); SKIPPED=(); MISSING=(); ORPHANS=(); N=0
mapfile -t ROWS < <(jq -r '.instances[] | [.slug, (.instance_uid // ""), .state] | @tsv' <<<"$STDIN_JSON")

deploy_one() {  # в подоболочке: лок держится до её выхода
  local s="$1" uid="$2" d f t proj up_rc
  d="$(inst_dir "$s")"
  lock_instance "$s" >/dev/null
  have="$(env_get "$d/.env" INSTANCE_UID)"
  if [ -n "$have" ] && [ -n "$uid" ] && [ "$have" != "$uid" ]; then
    echo "::error title=foreign dir ($s)::INSTANCE_UID в /opt/$s/.env не совпадает с inventory"; return 1
  fi
  for f in $COMPOSE_SET; do
    t="$(mktemp "$d/.$f.XXXXXX")" && cp "$BUNDLE/$f" "$t" && chmod 0644 "$t" && mv -f "$t" "$d/$f" \
      || { echo "::error title=compose copy failed ($s)::$f"; return 1; }
  done
  proj="$(img_proj_of "$s")"
  docker tag "$IMG" "${proj}-backend:prod" || { echo "::error title=tag failed ($s)::"; return 1; }
  echo "[deploy:$s] migrate"
  dc "$s" run --rm --no-deps migrate >/dev/null 2>&1 || { echo "::error title=migrate failed ($s)::alembic upgrade head returned non-zero"; return 1; }
  up_rc=0
  dc "$s" up -d --no-build --no-deps api >/dev/null 2>&1 || up_rc=$?
  echo "[deploy:$s] up api rc=$up_rc (решает гейт готовности)"
  if ! wait_healthy "$(ctr "$s" api)" 30; then
    echo "::error title=api not healthy ($s)::api не стал healthy за ~60s. Последние строки лога:"
    docker logs "$(ctr "$s" api)" --tail 40 2>&1 || true
    return 1
  fi
  dom="$(env_get "$d/.env" SERVICE_DOMAIN)"
  if [ -n "$dom" ]; then
    local ok_=0
    for _ in $(seq 1 12); do curl -fsS --max-time 5 "https://$dom/healthz" >/dev/null 2>&1 && { ok_=1; break; }; sleep 5; done
    [ "$ok_" = 1 ] || echo "::warning title=Smoke /healthz not green ($s)::https://$dom/healthz не ответил 200 за 60s (api healthy в контейнере)"
  fi
  return 0
}

for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r slug uid state <<<"$row"
  [[ "$slug" =~ $SLUG_RE ]] || { FAILED+=("$slug:bad_slug"); continue; }
  if [ "$state" != "active" ]; then
    echo "::notice title=skip ($slug)::state=$state — инстанс ведёт CRM, деплой его не трогает"
    SKIPPED+=("$slug:$state"); continue
  fi
  if [ ! -f "/opt/$slug/.env" ]; then MISSING+=("$slug"); continue; fi
  echo "[deploy] === $slug ==="
  if ( deploy_one "$slug" "$uid" ); then DEPLOYED+=("$slug"); else FAILED+=("$slug"); fi
  N=$((N+1)); [ $((N % BATCH_SIZE)) -eq 0 ] && wait_for_calm
done

# ---- сверка множества (ADR-115 §10.2 п. 4) ----
declare -A INV=()
for row in "${ROWS[@]}"; do IFS=$'\t' read -r slug _ state <<<"$row"; INV["$slug"]="$state"; done
for row in "${ROWS[@]}"; do
  IFS=$'\t' read -r slug _ state <<<"$row"
  [ "$state" = active ] && continue
  [ -f "/opt/$slug/.env" ] || MISSING+=("$slug")
done
while read -r s; do
  [ -n "${INV[$s]:-}" ] && continue
  ORPHANS+=("$s")
  echo "::warning title=orphan dir ($s)::/opt/$s/.env на $(hostname) вне inventory этого сервера — CI не трогает (до §11 фазы 10 это ожидаемо для резервов)"
done < <(list_instance_dirs)
for m in "${MISSING[@]:-}"; do
  [ -n "$m" ] && echo "::error title=missing dir ($m)::инстанс inventory без каталога /opt/$m/.env на $(hostname)"
done

docker image prune -f >/dev/null 2>&1 || true
arr() { printf '%s\n' "$@" | jq -R 'select(length>0)' | jq -sc .; }
ev_json deployed "$(arr "${DEPLOYED[@]:-}")"; ev_json failed "$(arr "${FAILED[@]:-}")"
ev_json skipped "$(arr "${SKIPPED[@]:-}")"; ev_json missing "$(arr "${MISSING[@]:-}")"
ev_json orphans "$(arr "${ORPHANS[@]:-}")"
nf=0; for x in "${FAILED[@]:-}" "${MISSING[@]:-}"; do [ -n "$x" ] && nf=$((nf+1)); done
[ "$nf" -eq 0 ] || fail deploy_failed "упали или отсутствуют: ${FAILED[*]:-} ${MISSING[*]:-}"
ok
