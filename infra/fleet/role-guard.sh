#!/usr/bin/env bash
# Страж ролей: приводит состояние ЭТОГО прикладного сервера в соответствие с таблицей,
# которую держит маршрутизатор (docs/MIGRATION-3-SERVERS.md §Переключение).
#
# Зачем. После аварии отказавший сервер возвращается с прежним маркером «основной» и, если ему
# не помешать, поднимет api и начнёт писать в СВОЮ устаревшую базу. Две базы разойдутся
# необратимо. Поэтому право называть основного принадлежит одному узлу — маршрутизатору, а
# прикладные серверы спрашивают его при каждой загрузке и раз в несколько минут.
#
# Источник: http://10.10.0.3:8088/roles.tsv  (строки: инстанс<TAB>основной_сервер)
# Недоступен источник -> НИЧЕГО НЕ МЕНЯЕМ. Молчание распорядителя не повод менять роли.
set -uo pipefail

SELF="${FLEET_SELF:?переменная FLEET_SELF (A или B) не задана в /etc/fleet.conf}"
SRC="http://10.10.0.3:8088/roles.tsv"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if ! curl -fsS --max-time 10 "$SRC" -o "$TMP"; then
  echo "страж ролей: таблица недоступна ($SRC) — состояние не трогаю"
  exit 0
fi
[ -s "$TMP" ] || { echo "страж ролей: таблица пуста — состояние не трогаю"; exit 0; }

changed=0
while IFS=$'\t' read -r inst primary; do
  case "$inst" in ""|\#*) continue;; esac
  dir="/opt/$inst"
  [ -f "$dir/.env" ] || continue
  want="standby"; [ "$primary" = "$SELF" ] && want="primary"
  have="$(cat "$dir/.role" 2>/dev/null || echo unknown)"
  proj="$(grep -m1 '^COMPOSE_PROJECT_NAME=' "$dir/.env" | cut -d= -f2-)"; proj="${proj:-$inst}"
  cf="-f docker-compose.prod.yml -f docker-compose.fleet.yml"

  if [ "$want" != "$have" ]; then
    echo "$want" > "$dir/.role"
    changed=$((changed+1))
    echo "страж ролей: $inst  $have -> $want"
  fi

  running="$(docker inspect -f '{{.State.Running}}' "${proj}-api-1" 2>/dev/null || echo false)"
  if [ "$want" = "standby" ] && [ "$running" = "true" ]; then
    # Резерв не имеет права держать api поднятым: он писал бы в реплику.
    ( cd "$dir" && docker compose -p "$proj" $cf --env-file .env stop api >/dev/null 2>&1 )
    echo "страж ролей: $inst — api остановлен (резерв)"
  fi
done < "$TMP"

echo "страж ролей: проверено, изменений ролей: $changed"
