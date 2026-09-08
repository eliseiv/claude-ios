#!/usr/bin/env bash
# Пересборка потоковой репликации для инстансов, у которых резерв стал самостоятельным.
# Выполняется НА МАРШРУТИЗАТОРЕ: только он имеет доступ к обоим прикладным серверам.
#
#   repair-replication.sh <имя> [<имя> ...]
#
# Порядок шагов повторяет раздел 3 migrate-all.sh — он проверен переносом всего флота.
# Два отличия, каждое из-за реального инцидента:
#
# 1. Роли сверяются с ФАЙЛАМИ .role на обеих машинах, и расхождение ПРЕРЫВАЕТ работу.
#    2026-09-02 таблица ролей в репозитории разошлась с фактом, инстансы уехали на сервер с
#    выключенным api и отдали 502. Проверка, не влияющая на управление, обязана либо
#    прерывать выполнение, либо не существовать.
#
# 2. Порт postgres берётся из .env основного (PG_HOST_PORT), а НЕ из instances.tsv.
#    Колонка 3 таблицы — порт api (18005), база слушает 15005. Раздел 3 migrate-all.sh
#    подставлял колонку 3 как порт базы, то есть направлял pg_basebackup в приложение.
set -uo pipefail
cd /opt/router/fleet || exit 1

host_of() { [ "$1" = "A" ] && echo appA || echo appB; }
other()   { [ "$1" = "A" ] && echo B || echo A; }
ip_of()   { [ "$1" = "A" ] && echo 10.10.0.1 || echo 10.10.0.2; }
r() { ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$@"; }

[ $# -gt 0 ] || { echo "нужны имена инстансов"; exit 1; }

for inst in "$@"; do
  primary="$(awk -F'\t' -v i="$inst" '$1==i{print $4}' instances.tsv)"
  api_port="$(awk -F'\t' -v i="$inst" '$1==i{print $3}' instances.tsv)"
  [ -n "$primary" ] || { echo "[$inst] нет в instances.tsv — ПРЕРЫВАЮ"; exit 1; }
  standby="$(other "$primary")"
  ph="$(host_of "$primary")"; sh_="$(host_of "$standby")"

  rp="$(r "$ph" "cat /opt/$inst/.role 2>/dev/null" | tr -d ' \r\n')"
  rs="$(r "$sh_" "cat /opt/$inst/.role 2>/dev/null" | tr -d ' \r\n')"
  if [ "$rp" != "primary" ] || [ "$rs" != "standby" ]; then
    echo "[$inst] РОЛИ РАСХОДЯТСЯ: tsv=$primary, .role на $ph='$rp', на $sh_='$rs' — ПРЕРЫВАЮ"
    exit 1
  fi

  pg_port="$(r "$ph" "grep -m1 '^PG_HOST_PORT=' /opt/$inst/.env | cut -d= -f2-" | tr -d ' \r\n')"
  [ -n "$pg_port" ] || { echo "[$inst] PG_HOST_PORT не задан на основном — ПРЕРЫВАЮ"; exit 1; }

  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://$(ip_of "$primary"):$api_port/ready" 2>/dev/null)"
  if [ "$code" != "200" ]; then
    echo "[$inst] основной не отвечает (ready=$code) — ПРЕРЫВАЮ, резерв у мёртвого основного не пересобирают"
    exit 1
  fi
  echo "[$inst] основной $primary ready=200, база на порту $pg_port, резерв на $standby"

  r "$ph" "/opt/fleet/replication.sh prepare $inst $(ip_of "$standby")" 2>&1 | sed 's/^/    /'

  pw="$(r "$ph" "grep -m1 '^PG_REPL_PASSWORD=' /opt/$inst/.env | cut -d= -f2-" 2>/dev/null | tr -d '\r\n')"
  if [ -z "$pw" ]; then echo "    [$inst] пароль репликации не получен — ПРЕРЫВАЮ"; exit 1; fi

  # Резерву нужен тот же .env, что и основному: учётные данные базы обязаны совпадать, а
  # заодно снимается расхождение настроек (у probotit на резерве не было ни персонажей, ни
  # голоса — при переключении инстанс тихо потерял бы включённые возможности).
  ts="$(date +%Y%m%d-%H%M%S)"
  r "$sh_" "cp -a /opt/$inst/.env /opt/$inst/.env.bak-$ts" 2>/dev/null
  ssh -o BatchMode=yes "$ph" "cat /opt/$inst/.env" | ssh -o BatchMode=yes "$sh_" "cat > /opt/$inst/.env"
  r "$sh_" "/opt/fleet/provision.sh adapt $inst $standby" >/dev/null 2>&1
  ssh -o BatchMode=yes "$ph" "tar -C /opt/$inst -cf - .secrets certs 2>/dev/null" | \
    ssh -o BatchMode=yes "$sh_" "tar -C /opt/$inst -xf - 2>/dev/null; chown -R 10001:10001 /opt/$inst/.secrets 2>/dev/null"

  r "$sh_" "/opt/fleet/replication.sh init $inst $(ip_of "$primary") $pg_port '$pw'" 2>&1 | sed 's/^/    /'
  echo "[$inst] готово"
  sleep 5
done
