#!/bin/bash
# Деплой флота по inventory CRM (ADR-115 §10.2). Исполняется на раннере CI (ci.yml, deploy.yml).
#
# Вход (окружение; значения секретов не печатаются):
#   DEPLOY_SHA                 SHA, образ которого уже опубликован в GHCR (§10.2 п. 1)
#   FLEET_INVENTORY_URL        адрес inventory CRM (§10.1)
#   FLEET_INVENTORY_TOKEN      токен ТОЛЬКО на чтение inventory
#   FLEET_DEPLOY_REPORT_URL    адрес записи last_deployed_sha (форма — broad-crm ADR; см. отчёт devops)
#   FLEET_DEPLOY_REPORT_TOKEN  токен ТОЛЬКО на запись отчёта деплоя
#   SSH_USER, SSH_KEY_FILE     пользователь и файл закрытого ключа CI
#   SERVER_FILTER              (необяз.) server_id через запятую — раскатить только их; отчёт в CRM
#                              при этом НЕ пишется (прогон заведомо неполный)
#   SERVER_TIMEOUT             потолок на ОДИН сервер (по умолчанию 60m; §10.2 п. 7)
#
# Порядок (§10.2): inventory до любых изменений → серверы последовательно (синхронизация
# инструментов §10.4, deploy) → R (синхронизация) → ПОВТОРНЫЙ inventory: догнать ставшие active,
# сверить образ api КАЖДОГО active (п. 6) → только при зелёном п. 6 отчёт в CRM (п. 8).
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd -P)"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
: "${DEPLOY_SHA:?}" "${FLEET_INVENTORY_URL:?}" "${FLEET_INVENTORY_TOKEN:?}" "${SSH_USER:?}" "${SSH_KEY_FILE:?}"
[[ "$DEPLOY_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::DEPLOY_SHA не 40 hex"; exit 1; }
SERVER_TIMEOUT="${SERVER_TIMEOUT:-60m}"
FAILED=()

# ---- inventory (§10.2 п. 2): таймаут и 3 попытки; токен — заголовком из файла, не в argv ----
fetch_inventory() {  # fetch_inventory <файл>
  local out="$1" hdr="$W/auth.hdr" code i
  ( umask 077; printf 'Authorization: Bearer %s\n' "$FLEET_INVENTORY_TOKEN" > "$hdr" )
  for i in 1 2 3; do
    code="$(curl -sS -o "$out" -w '%{http_code}' --max-time 30 -H @"$hdr" -H 'Accept: application/json' "$FLEET_INVENTORY_URL" 2>/dev/null)"
    [ "$code" = 200 ] && { rm -f "$hdr"; return 0; }
    echo "inventory: попытка $i — код ${code:-нет ответа}"; sleep $((i * 5))
  done
  rm -f "$hdr"
  echo "::error title=inventory недоступен::CRM не ответила 200 за 3 попытки — ни один сервер не тронут"
  return 1
}
fetch_inventory "$W/inv1.json" || exit 1
bash "$HERE/inventory-validate.sh" "$W/inv1.json" || exit 1

# ---- known_hosts из inventory: host-ключи ЗАКРЕПЛЯЮТСЯ (§10.1), а не принимаются любыми ----
build_known_hosts() {
  jq -r '([.router] + .servers)[] | [.ssh_host, (.ssh_port // 22 | tostring), .ssh_host_key] | @tsv' "$1" |
  while IFS=$'\t' read -r h p k; do
    if [ "$p" = 22 ]; then printf '%s %s\n' "$h" "$k"; else printf '[%s]:%s %s\n' "$h" "$p" "$k"; fi
  done > "$W/known_hosts"
}
build_known_hosts "$W/inv1.json"
sshx() {  # sshx <host> <port> <команда…>
  local h="$1" p="$2"; shift 2
  ssh -i "$SSH_KEY_FILE" -p "$p" -o BatchMode=yes -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile="$W/known_hosts" -o ConnectTimeout=15 -o ServerAliveInterval=30 \
      -o ServerAliveCountMax=6 "$SSH_USER@$h" "$@"
}

# ---- бандл версии (§10.4) ----
bash "$HERE/../make-bundle.sh" "$DEPLOY_SHA" "$W/bundle" || { echo "::error::бандл не собран"; exit 1; }
tar -C "$W/bundle" -czf "$W/fleet.tgz" .

sync_host() {  # sync_host <host> <port> <имя для лога>
  local h="$1" p="$2" n="$3"
  echo "[sync] $n: инструменты $DEPLOY_SHA"
  sshx "$h" "$p" "mkdir -p /opt/fleet.d/.incoming && cat > /opt/fleet.d/.incoming/$DEPLOY_SHA.tgz" < "$W/fleet.tgz" \
    && sshx "$h" "$p" bash -s -- "$DEPLOY_SHA" < "$HERE/sync-tools.sh"
}

in_filter() { [ -z "${SERVER_FILTER:-}" ] && return 0; case ",$SERVER_FILTER," in *",$1,"*) return 0;; esac; return 1; }

declare -A DEPLOYED_ON=()      # slug -> server_id, выкаченные в этом прогоне
declare -A SYNCED=()
deploy_server() {  # deploy_server <inventory-файл> <server_id> [slug…] — без slug: все инстансы сервера
  local inv="$1" sid="$2"; shift 2
  local h p name payload out rc res
  h="$(jq -r --arg s "$sid" '.servers[] | select(.server_id == $s) | .ssh_host' "$inv")"
  p="$(jq -r --arg s "$sid" '.servers[] | select(.server_id == $s) | (.ssh_port // 22)' "$inv")"
  name="$(jq -r --arg s "$sid" '.servers[] | select(.server_id == $s) | .name // .server_id' "$inv")"
  if [ -z "${SYNCED[$sid]:-}" ]; then
    sync_host "$h" "$p" "$name" || { echo "::error title=sync failed ($name)::"; FAILED+=("server:$sid:sync"); return 1; }
    SYNCED[$sid]=1
  fi
  if [ "$#" -gt 0 ]; then
    payload="$(jq -c --arg s "$sid" --arg sha "$DEPLOY_SHA" --args '{sha:$sha, instances:[.instances[] | select(.server_id == $s)
      | select(.slug as $x | $ARGS.positional | index($x))]}' "$inv" "$@")"
  else
    payload="$(jq -c --arg s "$sid" --arg sha "$DEPLOY_SHA" '{sha:$sha, instances:[.instances[] | select(.server_id == $s)]}' "$inv")"
  fi
  echo "[deploy] === сервер $name ($sid): $(jq '.instances | length' <<<"$payload") инстансов inventory ==="
  out="$W/deploy-$sid.log"
  # Потолок — на СЕРВЕР, а не на флот (§10.2 п. 7). Вывод идёт в лог прогона вживую.
  timeout "$SERVER_TIMEOUT" bash -c 'printf "%s" "$1" | "${@:2}"' _ "$payload" sshx_fn "$h" "$p" 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}
  res="$(grep '^FLEET-RESULT ' "$out" | tail -1 | cut -d' ' -f2-)"
  for s in $(jq -r '.evidence.deployed[]? // empty' <<<"$res" 2>/dev/null); do DEPLOYED_ON[$s]="$sid"; done
  if [ "$(jq -r '.ok' <<<"$res" 2>/dev/null)" != "true" ]; then
    echo "::error title=deploy failed ($name)::$(jq -c '{reason, failed: .evidence.failed, missing: .evidence.missing}' <<<"$res" 2>/dev/null || echo "нет FLEET-RESULT (rc=$rc)")"
    FAILED+=("server:$sid"); return 1
  fi
}
sshx_fn() {  # вызов шага деплоя на сервере из синхронизированной версии $DEPLOY_SHA
  local h="$1" p="$2"
  sshx "$h" "$p" "flock -s /opt/fleet.d/$DEPLOY_SHA/.inuse env FLEET_VERSION_DIR=/opt/fleet.d/$DEPLOY_SHA bash /opt/fleet.d/$DEPLOY_SHA/fleet-deploy.sh"
}
export -f sshx sshx_fn; export W SSH_KEY_FILE SSH_USER DEPLOY_SHA

# ---- п. 3, п. 7: серверы последовательно ----
for sid in $(jq -r '.servers[] | select(.state == "active" or .state == "draining") | .server_id' "$W/inv1.json"); do
  in_filter "$sid" || { echo "::notice::сервер $sid вне SERVER_FILTER — пропущен"; continue; }
  deploy_server "$W/inv1.json" "$sid" || true
done
# R тоже получает версию инструментов (§10.4): с него CRM копирует их на новые серверы (§2.1 ш.1).
rh="$(jq -r '.router.ssh_host' "$W/inv1.json")"; rp="$(jq -r '.router.ssh_port // 22' "$W/inv1.json")"
sync_host "$rh" "$rp" "router" || { echo "::error title=sync failed (router)::"; FAILED+=("router:sync"); }

# ---- п. 6: повторный inventory, догон и сверка образа КАЖДОГО active ----
fetch_inventory "$W/inv2.json" || exit 1
bash "$HERE/inventory-validate.sh" "$W/inv2.json" || exit 1
build_known_hosts "$W/inv2.json"
declare -A CATCH=()
while IFS=$'\t' read -r slug sid; do
  in_filter "$sid" || continue
  [ "${DEPLOYED_ON[$slug]:-}" = "$sid" ] && continue
  CATCH[$sid]="${CATCH[$sid]:-} $slug"
done < <(jq -r '.instances[] | select(.state == "active") | [.slug, .server_id] | @tsv' "$W/inv2.json")
for sid in "${!CATCH[@]}"; do
  echo "[deploy] догон на $sid:${CATCH[$sid]}"
  # shellcheck disable=SC2086  # список slug через пробел — разбиение намеренное
  deploy_server "$W/inv2.json" "$sid" ${CATCH[$sid]} || true
done

MISMATCH=()
for sid in $(jq -r '.servers[].server_id' "$W/inv2.json"); do
  in_filter "$sid" || continue
  slugs="$(jq -r --arg s "$sid" '[.instances[] | select(.server_id == $s and .state == "active") | .slug] | join(" ")' "$W/inv2.json")"
  [ -n "$slugs" ] || continue
  h="$(jq -r --arg s "$sid" '.servers[] | select(.server_id == $s) | .ssh_host' "$W/inv2.json")"
  p="$(jq -r --arg s "$sid" '.servers[] | select(.server_id == $s) | (.ssh_port // 22)' "$W/inv2.json")"
  out="$(sshx "$h" "$p" "for s in $slugs; do printf '%s ' \"\$s\"; fleet-instance image \"\$s\" $DEPLOY_SHA 2>/dev/null | grep '^FLEET-RESULT ' | tail -1; done" 2>/dev/null)"
  for s in $slugs; do
    line="$(grep "^$s FLEET-RESULT " <<<"$out" | head -1 | cut -d' ' -f3-)"
    [ "$(jq -r '.evidence.match' <<<"$line" 2>/dev/null)" = "true" ] || MISMATCH+=("$s@$sid")
  done
done

if [ "${#MISMATCH[@]}" -gt 0 ]; then
  echo "::error title=Образ не совпал (ADR-115 §10.2 п. 6)::${MISMATCH[*]}"
  FAILED+=("image_mismatch")
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "::error title=Deploy failed::${FAILED[*]}"
  exit 1
fi
echo "[deploy] все active инстансы на образе $DEPLOY_SHA"

# ---- п. 8: отчёт в CRM — только при зелёном п. 6 и полном (не отфильтрованном) прогоне ----
if [ -n "${SERVER_FILTER:-}" ]; then
  echo "::notice::SERVER_FILTER задан — прогон неполный, last_deployed_sha в CRM НЕ записан"; exit 0
fi
: "${FLEET_DEPLOY_REPORT_URL:?}" "${FLEET_DEPLOY_REPORT_TOKEN:?}"
gen_at="$(jq -r '.generated_at' "$W/inv2.json")"
body="$(jq -nc --arg sha "$DEPLOY_SHA" --arg g "$gen_at" '{last_deployed_sha:$sha, inventory_generated_at:$g}')"
( umask 077; printf 'Authorization: Bearer %s\n' "$FLEET_DEPLOY_REPORT_TOKEN" > "$W/report.hdr" )
code="$(curl -sS -o "$W/report.body" -w '%{http_code}' --max-time 30 --retry 3 -X POST -H @"$W/report.hdr" \
  -H 'Content-Type: application/json' --data "$body" "$FLEET_DEPLOY_REPORT_URL" 2>/dev/null)"
rm -f "$W/report.hdr"
# Исходы ADR-115 §10.2 п. 8: 2xx — зелёный; 409 с error.code = fleet_deploy_report_stale — тоже
# зелёный с ::notice (более поздний прогон уже доложил; образ этого прогона п. 6 проверил).
# Код отказа сверяется по ПОЛЮ error.code единого формата ошибки CRM (broad-crm docs/04-api.md
# §Единый формат ошибки), а не по тексту: иной 409 — красный. Прочие коды и отсутствие ответа — красный.
ecode="$(jq -r '.error.code // empty' "$W/report.body" 2>/dev/null)"
case "$code" in
  2??) echo "[deploy] отчёт в CRM записан: $DEPLOY_SHA / $gen_at";;
  409) if [ "$ecode" = "fleet_deploy_report_stale" ]; then
         echo "::notice title=Отчёт деплоя устарел::CRM уже хранит отчёт более позднего прогона (fleet_deploy_report_stale) — образ этого прогона сверен, прогон зелёный"
       else
         echo "::error title=Отчёт деплоя не записан::CRM ответила 409 (${ecode:-без error.code})"; exit 1
       fi;;
  *) echo "::error title=Отчёт деплоя не записан::CRM ответила ${code:-нет ответа}${ecode:+ ($ecode)} — last_deployed_sha не обновлён"; exit 1;;
esac
