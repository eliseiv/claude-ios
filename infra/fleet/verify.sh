#!/usr/bin/env bash
# Проверка состояния флота. Выполняется на маршрутизаторе: он единственный видит обе стороны.
#
#   verify.sh          — сводка по всем инстансам
#   verify.sh <имя>    — подробно по одному
#
# Для каждого инстанса проверяется ТРИ вещи, потому что они отказывают независимо:
#   основной   — отвечает ли api на сервере, который сейчас основной;
#   резерв     — жива ли база резерва и не отстала ли репликация;
#   через вход — доходит ли запрос по всему пути (это и есть то, что видит пользователь).
set -uo pipefail
cd /opt/router/fleet || exit 1
WG_A=10.10.0.1; WG_B=10.10.0.2
ip_of() { [ "$1" = "A" ] && echo $WG_A || echo $WG_B; }
other() { [ "$1" = "A" ] && echo B || echo A; }

ONE="${1:-}"
printf "%-14s %-22s %-9s %-9s %-9s %s\n" ИНСТАНС ДОМЕН ОСНОВНОЙ РЕЗЕРВ ЧЕРЕЗ_ВХОД ОТСТАВАНИЕ
printf '%.0s-' {1..82}; echo

ok=0; bad=0
while IFS=$'\t' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  [ -n "$ONE" ] && [ "$inst" != "$ONE" ] && continue

  p_ip="$(ip_of "$primary")"; s_ip="$(ip_of "$(other "$primary")")"
  c_pri="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "http://$p_ip:$port/ready" 2>/dev/null)"
  # У резерва api намеренно выключен, поэтому проверяем не его, а живость базы через ssh.
  st="$(ssh -o BatchMode=yes -o ConnectTimeout=6 "app$(other "$primary")" "
        cd /opt/$inst 2>/dev/null || exit 1
        proj=\$(grep -m1 '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2-); proj=\${proj:-$inst}
        u=\$(grep -m1 '^POSTGRES_USER=' .env | cut -d= -f2-)
        d=\$(grep -m1 '^POSTGRES_DB=' .env | cut -d= -f2-)
        docker exec -i \${proj}-postgres-1 psql -U \$u -d \$d -tAc \
          \"SELECT CASE WHEN pg_is_in_recovery() THEN COALESCE(EXTRACT(EPOCH FROM now()-pg_last_xact_replay_timestamp())::int,0)::text ELSE 'НЕ_РЕЗЕРВ' END\"
      " 2>/dev/null | tr -d ' \r')"
  case "$st" in ""|*ERROR*) s_state="нет"; lag="—";; НЕ_РЕЗЕРВ) s_state="ОСНОВНОЙ!"; lag="—";; *) s_state="ок"; lag="${st}с";; esac

  if [ "$domain" = "ПОДЛЕЖИТ_УТОЧНЕНИЮ" ]; then
    c_edge="—"
  else
    c_edge="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H "Host: $domain" http://127.0.0.1/health 2>/dev/null)"
  fi

  [ "$c_pri" = "200" ] && ok=$((ok+1)) || bad=$((bad+1))
  printf "%-14s %-22s %-9s %-9s %-9s %s\n" "$inst" "$domain" "${c_pri:-нет}($primary)" "$s_state" "${c_edge:-нет}" "$lag"
done < instances.tsv
printf '%.0s-' {1..82}; echo
echo "основных отвечает: $ok, не отвечает: $bad"
