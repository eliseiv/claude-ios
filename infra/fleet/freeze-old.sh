#!/usr/bin/env bash
# Заморозка прежнего сервера сразу после его возвращения (docs/MIGRATION-3-SERVERS.md).
#
# Выполняется НА ПРЕЖНЕМ СЕРВЕРЕ, первым делом, до любых других действий.
#
# Зачем. Машина падала четырежды за трое суток, и один из отказов был диагностирован как
# CPU-голодание: 6 ядер на 240 контейнеров. После загрузки все контейнеры с политикой
# `unless-stopped` стартуют одновременно и держат load трёхзначным минуты. Снимать дампы 27
# баз в этот момент — верный способ уронить сервер снова, на этот раз посреди переноса.
#
# Что останавливается: только `api` и `redis` наших инстансов. Postgres ОСТАЁТСЯ поднятым —
# из него предстоит снимать дамп. Чужие контейнеры (их на машине большинство) не трогаются:
# они принадлежат другим проектам, и их остановка — не наше решение.
set -uo pipefail

MODE="${1:-freeze}"
LIST="${FLEET_LIST:-/opt/claude-ios/infra/fleet/ports.txt}"

instances() {
  if [ -f "$LIST" ]; then
    awk '!/^#/ && NF {print $1}' "$LIST"
  else
    # Запасной путь: список из каталогов с нашим compose-файлом.
    for d in /opt/*/; do [ -f "$d/docker-compose.prod.yml" ] && basename "$d"; done
  fi
}

case "$MODE" in
freeze)
  echo "load до заморозки: $(cut -d' ' -f1-3 /proc/loadavg)"
  n=0
  for inst in $(instances); do
    d="/opt/$inst"; [ -f "$d/.env" ] || continue
    proj="$(grep -m1 '^COMPOSE_PROJECT_NAME=' "$d/.env" | cut -d= -f2-)"; proj="${proj:-$inst}"
    ( cd "$d" && docker compose -p "$proj" -f docker-compose.prod.yml --env-file .env stop api redis >/dev/null 2>&1 )
    n=$((n+1))
    # Останавливаем по одному с паузой: пачкой это даёт тот же пик, от которого уходим.
    sleep 1
  done
  echo "остановлено инстансов (api+redis): $n"
  echo "postgres оставлен поднятым — из него снимается дамп"
  echo "наших контейнеров работает: $(docker ps --format '{{.Names}}' | grep -cE '\-(postgres|api|redis)-1$')"
  echo "load после заморозки: $(cut -d' ' -f1-3 /proc/loadavg)"
  ;;

pg-only)
  # Ещё жёстче: гасим ВСЁ наше, кроме postgres конкретного инстанса. На случай, если даже
  # 27 поднятых postgres машине тяжело.
  KEEP="${2:?укажите инстанс, чей postgres оставить}"
  for inst in $(instances); do
    [ "$inst" = "$KEEP" ] && continue
    d="/opt/$inst"; [ -f "$d/.env" ] || continue
    proj="$(grep -m1 '^COMPOSE_PROJECT_NAME=' "$d/.env" | cut -d= -f2-)"; proj="${proj:-$inst}"
    ( cd "$d" && docker compose -p "$proj" -f docker-compose.prod.yml --env-file .env stop >/dev/null 2>&1 )
  done
  d="/opt/$KEEP"
  proj="$(grep -m1 '^COMPOSE_PROJECT_NAME=' "$d/.env" | cut -d= -f2-)"; proj="${proj:-$KEEP}"
  ( cd "$d" && docker compose -p "$proj" -f docker-compose.prod.yml --env-file .env up -d postgres >/dev/null 2>&1 )
  echo "оставлен поднятым только postgres инстанса $KEEP; load: $(cut -d' ' -f1-3 /proc/loadavg)"
  ;;

status)
  echo "load: $(cut -d' ' -f1-3 /proc/loadavg)"
  echo "всего контейнеров: $(docker ps -q | wc -l)"
  echo "наших api: $(docker ps --format '{{.Names}}' | grep -c '\-api-1$')"
  echo "наших postgres: $(docker ps --format '{{.Names}}' | grep -c '\-postgres-1$')"
  free -m | awk '/^Mem:/{printf "память: занято %d МБ из %d\n", $3, $2}'
  ;;

*) echo "режимы: freeze | pg-only <инстанс> | status"; exit 2;;
esac
