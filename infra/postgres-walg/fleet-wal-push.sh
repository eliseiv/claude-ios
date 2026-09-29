#!/bin/bash
# fleet-wal-push — archive_command Postgres инстанса (ADR-115 §8.2, §8.4).
#
#   archive_command = 'fleet-wal-push %p'
#
# Не голый `wal-g wal-push`: перед выгрузкой сегмента сверяется ограждение по поколению —
# `registry/<INSTANCE_UID>/meta.json` в бакете обязан называть ЭТОТ инстанс (instance_uid), ЭТО
# поколение (current_generation) и ЭТОТ сервер (server_id = /etc/fleet/walg/server_id).
# Иначе сегмент не выгружается (код ≠ 0): Postgres оставляет его в pg_wal и повторит позже.
# Так вернувшийся после аварии сервер не допишет свою устаревшую линию ни в префикс
# восстановленного инстанса, ни в собственный прежний (ADR-115 §8.4, §12 шаг 0).
# Остановку контейнеров («самоограждение») делает не эта обёртка, а таймер хоста
# fleet-backup-heartbeat (fleet-backup heartbeat): процесс внутри контейнера этого не может.
#
# Прочитанный meta.json кэшируется не дольше 5 минут (ADR-115 §8.2): WAL-сегменты при нагрузке
# идут чаще, и читать объект на каждый сегмент незачем. Кэш — в /tmp контейнера и исчезает при
# пересоздании контейнера.
set -uo pipefail

WAL="${1:?путь сегмента (%p)}"
DIR="${FLEET_WALG_DIR:-/etc/fleet/walg}"
UID_="${FLEET_INSTANCE_UID:-}"
GEN="${FLEET_INSTANCE_GENERATION:-}"
MAX_AGE="${FLEET_META_CACHE_SECONDS:-300}"

log() { echo "fleet-wal-push: $*" >&2; }

[[ "$UID_" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || { log "INSTANCE_UID не UUID — сегмент не выгружаю"; exit 1; }
[[ "$GEN" =~ ^[1-9][0-9]{0,8}$ ]] || { log "INSTANCE_GENERATION не целое >= 1 — сегмент не выгружаю"; exit 1; }
SID="$(tr -d ' \r\n' < "$DIR/server_id" 2>/dev/null || true)"
[ -n "$SID" ] || { log "$DIR/server_id не читается — сегмент не выгружаю"; exit 1; }

CACHE="${FLEET_META_CACHE:-/tmp/fleet-meta-$UID_.json}"
fresh=0
if [ -f "$CACHE" ]; then
  age=$(( $(date +%s) - $(stat -c %Y "$CACHE" 2>/dev/null || echo 0) ))
  [ "$age" -ge 0 ] && [ "$age" -lt "$MAX_AGE" ] && fresh=1
fi
if [ "$fresh" = 0 ]; then
  tmp="$(mktemp "${CACHE}.XXXXXX")" || { log "mktemp не удался"; exit 1; }
  if ! fleet-walg --root st cat "registry/$UID_/meta.json" > "$tmp" 2>/dev/null; then
    rm -f "$tmp"; log "meta.json не прочитан — ограждение не проверить, сегмент не выгружаю"; exit 1
  fi
  mv -f "$tmp" "$CACHE"
fi

if ! jq -e --arg u "$UID_" --arg g "$GEN" --arg s "$SID" \
     '.instance_uid == $u and ((.current_generation|tostring) == $g) and ((.server_id|tostring) == $s)' \
     "$CACHE" >/dev/null 2>&1; then
  rm -f "$CACHE"
  log "ограждение: meta.json не совпадает с этим экземпляром (uid/поколение/сервер) — сегмент не выгружаю"
  exit 1
fi

exec fleet-walg wal-push "$WAL"
