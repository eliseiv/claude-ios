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

# --- Адрес клиента (инцидент 2026-08-30) --------------------------------------------------
# Приложение доверяет X-Forwarded-For только от известного прокси. Пока маршрутизатора не было
# в списке доверия, за клиента принимался ОН САМ: все пользователи инстанса схлопывались в один
# адрес и делили лимиты «на IP». Наружу это выглядело как «сервис не работает» — velunixa отдавал
# 429 на регистрацию всем подряд, потому что 74 тысячи человек делили порог в 10 запросов.
#
# Проверяются ДВЕ вещи, потому что порознь каждая врёт:
#   конфигурация — доверяет ли инстанс туннельной сети (решает всегда, но это лишь настройка);
#   поведение    — есть ли ЖИВОЙ ключ лимита на адресе маршрутизатора (прямая улика, но под
#                  нулевым трафиком ключа нет и молчание ничего не доказывает).
echo
echo "АДРЕС КЛИЕНТА:"
collapsed=0
while IFS=$'	' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  [ -n "$ONE" ] && [ "$inst" != "$ONE" ] && continue
  res="$(ssh -o BatchMode=yes -o ConnectTimeout=6 "app${primary}" "
        cd /opt/$inst 2>/dev/null || exit 1
        proj=\$(grep -m1 '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2-); proj=\${proj:-$inst}
        trusted=\$(docker exec \${proj}-api-1 printenv TRUSTED_PROXY_IPS 2>/dev/null)
        live=\$(docker exec \${proj}-redis-1 redis-cli --scan --pattern 'rl:*:10.10.0.3' 2>/dev/null | head -1)
        case \"\$trusted\" in *10.10.0.*) cfg=ок;; *) cfg=НЕ_ДОВЕРЯЕТ;; esac
        [ -n \"\$live\" ] && echo \"\$cfg СХЛОПНУТ\" || echo \"\$cfg —\"
      " 2>/dev/null | tr -d '')"
  cfg="${res%% *}"; live="${res##* }"
  if [ "$cfg" != "ок" ] || [ "$live" = "СХЛОПНУТ" ]; then
    collapsed=$((collapsed+1))
    printf "  %-14s доверие: %-12s живой ключ маршрутизатора: %s
" "$inst" "${cfg:-нет данных}" "$live"
  fi
done < instances.tsv
[ "$collapsed" = "0" ] && echo "  все инстансы видят реальные адреса клиентов"
