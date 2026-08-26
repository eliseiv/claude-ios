#!/usr/bin/env bash
# Перенос всего флота с прежнего сервера. Выполняется НА МАРШРУТИЗАТОРЕ: только он имеет
# доступ к обоим прикладным серверам и держит таблицу ролей (docs/MIGRATION-3-SERVERS.md).
#
#   migrate-all.sh            перенести все инстансы
#   migrate-all.sh <имя> ...  перенести только указанные
#
# Порядок намеренный и менять его нельзя:
#   1. заморозка прежнего сервера — он падал от CPU-голодания, и снимать 27 дампов на
#      работающем флоте значит уронить его посреди переноса;
#   2. перенос каждого инстанса на ТОТ сервер, где он основной по таблице ролей;
#   3. репликация на соседа — только после того, как данные на месте;
#   4. сводка.
set -uo pipefail
cd /opt/router/fleet || exit 1
OLD="${OLD_HOST:-root@87.239.135.154}"

ssh_old() { ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no "$OLD" "$@"; }
host_of() { [ "$1" = "A" ] && echo appA || echo appB; }
other()   { [ "$1" = "A" ] && echo B || echo A; }
ip_of()   { [ "$1" = "A" ] && echo 10.10.0.1 || echo 10.10.0.2; }

echo "== 0. Прежний сервер доступен? =="
ssh_old 'echo ok' >/dev/null 2>&1 || { echo "прежний сервер недоступен — перенос невозможен"; exit 1; }
echo "доступен, load: $(ssh_old 'cut -d" " -f1-3 /proc/loadavg')"

echo
echo "== 1. Заморозка прежнего сервера =="
ssh_old 'bash -s' < /opt/router/fleet/freeze-old.sh freeze 2>/dev/null || \
  ssh_old '/opt/fleet/freeze-old.sh freeze' 2>/dev/null || echo "заморозка не выполнена — продолжаю осторожно"

WANT="$*"
declare -a DONE=() FAILED=()

echo
echo "== 2. Перенос инстансов =="
while IFS=$'\t' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  if [ -n "$WANT" ]; then case " $WANT " in *" $inst "*) ;; *) continue;; esac; fi

  th="$(host_of "$primary")"
  echo "--- $inst -> сервер $primary"
  if ssh -o BatchMode=yes -o ConnectTimeout=15 "$th" "OLD_HOST=$OLD /opt/fleet/import-from-old.sh $inst $primary" 2>&1 | sed 's/^/    /'; then
    DONE+=("$inst")
  else
    FAILED+=("$inst"); echo "    ОШИБКА переноса"
  fi
  # Пауза между инстансами: прежний сервер уже показал, что не выдерживает плотной череды.
  sleep 3
done < instances.tsv

echo
echo "== 3. Репликация на резервный сервер =="
for inst in "${DONE[@]}"; do
  primary="$(awk -F'\t' -v i="$inst" '$1==i{print $4}' instances.tsv)"
  standby="$(other "$primary")"
  pg_port="$(awk -F'\t' -v i="$inst" '$1==i{print $3}' instances.tsv)"
  ph="$(host_of "$primary")"; sh_="$(host_of "$standby")"

  ssh -o BatchMode=yes "$ph" "/opt/fleet/replication.sh prepare $inst $(ip_of "$standby")" 2>&1 | sed 's/^/    /'
  pw="$(ssh -o BatchMode=yes "$ph" "grep -m1 '^PG_REPL_PASSWORD=' /opt/$inst/.env | cut -d= -f2-" 2>/dev/null)"
  if [ -z "$pw" ]; then echo "    $inst: пароль репликации не получен — резерв не поднят"; continue; fi
  # Резерву нужен тот же .env, что и основному: учётные данные базы обязаны совпадать.
  ssh -o BatchMode=yes "$ph" "cat /opt/$inst/.env" 2>/dev/null | \
    ssh -o BatchMode=yes "$sh_" "cat > /opt/$inst/.env"
  ssh -o BatchMode=yes "$sh_" "/opt/fleet/provision.sh adapt $inst $standby" >/dev/null 2>&1
  ssh -o BatchMode=yes "$sh_" "tar -C /opt/$inst -cf - .secrets certs 2>/dev/null" >/dev/null 2>&1
  ssh -o BatchMode=yes "$ph" "tar -C /opt/$inst -cf - .secrets certs 2>/dev/null" | \
    ssh -o BatchMode=yes "$sh_" "tar -C /opt/$inst -xf - 2>/dev/null; chown -R 10001:10001 /opt/$inst/.secrets 2>/dev/null"
  ssh -o BatchMode=yes "$sh_" "/opt/fleet/replication.sh init $inst $(ip_of "$primary") $pg_port '$pw'" 2>&1 | sed 's/^/    /'
  sleep 2
done

echo
echo "== 4. Сводка =="
echo "перенесено: ${#DONE[@]}   с ошибкой: ${#FAILED[@]}"
[ ${#FAILED[@]} -gt 0 ] && echo "не перенесены: ${FAILED[*]}"
/opt/router/fleet/verify.sh
