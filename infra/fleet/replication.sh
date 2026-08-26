#!/usr/bin/env bash
# Потоковая репликация Postgres между прикладными серверами (docs/MIGRATION-3-SERVERS.md).
#
# Почему поток, а не выгрузка по расписанию: отставание — доли секунды вместо интервала, и при
# отказе не теряются покупки, списания кредитов и переписка. Плюс 27 периодических выгрузок дали
# бы ровно ту дисковую нагрузку, из-за которой прежний сервер падал 2026-08-24.
#
# Два режима:
#   prepare <инстанс>                  — на ОСНОВНОМ: завести роль репликации и пустить резервный
#   init    <инстанс> <ip_основного>   — на РЕЗЕРВНОМ: снять базовую копию и встать на поток
#
# Скрипт идемпотентен: prepare можно звать повторно, init — только на пустой/пересоздаваемый резерв.
set -uo pipefail

MODE="${1:?режим: prepare | init | status}"
INST="${2:?имя инстанса (каталог в /opt)}"
DIR="/opt/$INST"
cd "$DIR" || { echo "нет каталога $DIR"; exit 1; }

envget() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2-; }
PROJ="$(envget COMPOSE_PROJECT_NAME)"; PROJ="${PROJ:-$INST}"
PGUSER="$(envget POSTGRES_USER)"; PGUSER="${PGUSER:-postgres}"
PGDB="$(envget POSTGRES_DB)"; PGDB="${PGDB:-postgres}"
CF="-f docker-compose.prod.yml -f docker-compose.fleet.yml"
PGC="${PROJ}-postgres-1"

case "$MODE" in

prepare)
  PEER_IP="${3:?третьим аргументом — адрес резервного сервера в туннеле}"
  REPL_PW="$(envget PG_REPL_PASSWORD)"
  if [ -z "$REPL_PW" ]; then
    REPL_PW="$(openssl rand -hex 24)"
    if grep -q "^PG_REPL_PASSWORD=" .env; then
      sed -i "s|^PG_REPL_PASSWORD=.*|PG_REPL_PASSWORD=$REPL_PW|" .env
    else
      printf '\n# Пароль роли репликации (создан replication.sh). Один на пару серверов.\nPG_REPL_PASSWORD=%s\n' "$REPL_PW" >> .env
    fi
  fi
  # Роль репликации: только поток, без доступа к данным через SQL.
  docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -v ON_ERROR_STOP=0 <<SQL >/dev/null 2>&1
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
    CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '$REPL_PW';
  ELSE
    ALTER ROLE replicator WITH REPLICATION LOGIN PASSWORD '$REPL_PW';
  END IF;
END \$\$;
SQL
  # Слот репликации: без него основной удалит журнал, который резервный ещё не забрал,
  # и репликация порвётся молча — восстановление тогда только через новую базовую копию.
  docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -tAc \
    "SELECT 1 FROM pg_replication_slots WHERE slot_name='standby_slot'" 2>/dev/null | grep -q 1 || \
    docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -tAc \
      "SELECT pg_create_physical_replication_slot('standby_slot')" >/dev/null 2>&1
  # Доступ резервному — только на репликацию и только из туннеля.
  HBA="/var/lib/postgresql/data/pg_hba.conf"
  docker exec -i "$PGC" sh -c "grep -q '$PEER_IP/32' $HBA || echo 'host replication replicator $PEER_IP/32 scram-sha-256' >> $HBA"
  docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -c "SELECT pg_reload_conf()" >/dev/null 2>&1
  echo "[$INST] основной готов: роль replicator, слот standby_slot, доступ с $PEER_IP"
  ;;

init)
  PRIMARY_IP="${3:?третьим аргументом — адрес ОСНОВНОГО сервера в туннеле}"
  PRIMARY_PORT="${4:?четвёртым — порт postgres основного}"
  REPL_PW="${5:?пятым — пароль роли replicator (из .env основного)}"
  echo "[$INST] останавливаю api и postgres резерва"
  docker compose -p "$PROJ" $CF --env-file .env stop api postgres >/dev/null 2>&1
  docker compose -p "$PROJ" $CF --env-file .env rm -f postgres >/dev/null 2>&1
  VOL="${PROJ}_pgdata"
  echo "[$INST] очищаю том $VOL и снимаю базовую копию с $PRIMARY_IP:$PRIMARY_PORT"
  docker run --rm -v "$VOL":/var/lib/postgresql/data -e PGPASSWORD="$REPL_PW" \
    pgvector/pgvector:pg16 sh -c "
      rm -rf /var/lib/postgresql/data/* &&
      pg_basebackup -h $PRIMARY_IP -p $PRIMARY_PORT -U replicator -D /var/lib/postgresql/data \
        -Fp -Xs -P -R -S standby_slot &&
      chown -R postgres:postgres /var/lib/postgresql/data &&
      chmod 700 /var/lib/postgresql/data
    " || { echo "[$INST] базовая копия НЕ снята"; exit 1; }
  # -R уже положил standby.signal и primary_conninfo; поднимаем только базу, api остаётся выключен.
  docker compose -p "$PROJ" $CF --env-file .env up -d --no-build postgres >/dev/null 2>&1
  echo standby > .role
  echo "[$INST] резерв поднят и встал на поток"
  ;;

status)
  if docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -tAc "SELECT pg_is_in_recovery()" 2>/dev/null | grep -q t; then
    LAG=$(docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -tAc \
      "SELECT COALESCE(EXTRACT(EPOCH FROM now()-pg_last_xact_replay_timestamp())::int, 0)" 2>/dev/null)
    echo "[$INST] РЕЗЕРВ, отставание ${LAG:-?} с"
  else
    N=$(docker exec -i "$PGC" psql -U "$PGUSER" -d "$PGDB" -tAc \
      "SELECT count(*) FROM pg_stat_replication" 2>/dev/null)
    echo "[$INST] ОСНОВНОЙ, подключено резервных: ${N:-0}"
  fi
  ;;

*) echo "неизвестный режим: $MODE"; exit 2;;
esac
