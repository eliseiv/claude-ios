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
  # -n обязателен: без него ssh читает stdin цикла (instances.tsv) и съедает остаток файла —
  # цикл отработает ОДНУ строку и завершится, а сводка отрапортует успех по одному инстансу.
  st="$(ssh -n -o BatchMode=yes -o ConnectTimeout=6 "app$(other "$primary")" "
        cd /opt/$inst 2>/dev/null || exit 1
        proj=\$(grep -m1 '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2-); proj=\${proj:-$inst}
        u=\$(grep -m1 '^POSTGRES_USER=' .env | cut -d= -f2-)
        d=\$(grep -m1 '^POSTGRES_DB=' .env | cut -d= -f2-)
        docker exec -i \${proj}-postgres-1 psql -U \$u -d \$d -tAc \
          \"SELECT CASE WHEN NOT pg_is_in_recovery() THEN 'НЕ_РЕЗЕРВ' ELSE COALESCE((SELECT status FROM pg_stat_wal_receiver LIMIT 1),'НЕТ_ПОТОКА') || ':' || COALESCE(pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn())::bigint::text,'0') END\"
      " 2>/dev/null | tr -d ' \r')"
  # Отставание меряется в БАЙТАХ журнала, а не в секундах с последней транзакции. Секундная
  # мера врала на тихих инстансах: на базе без записей `now() - pg_last_xact_replay_timestamp()`
  # растёт бесконечно при совершенно здоровой репликации (наблюдалось 72890с на claude-ios), и
  # настоящий затор в этом шуме было бы не различить.
  case "$st" in
    ""|*ERROR*)  s_state="нет"; lag="—";;
    НЕ_РЕЗЕРВ)   s_state="ОСНОВНОЙ!"; lag="—";;
    streaming:*) s_state="ок"; lag="${st#streaming:}б";;
    НЕТ_ПОТОКА*) s_state="БЕЗ ПОТОКА"; lag="—";;
    *)           s_state="${st%%:*}"; lag="${st#*:}б";;
  esac

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
  host="app${primary}"
  # Имя проекта в этом флоте совпадает с именем каталога (provision.sh задаёт
  # COMPOSE_PROJECT_NAME=<инстанс>), поэтому имя контейнера выводится прямо из него —
  # без чтения .env через вложенное экранирование, которое здесь и ломалось.
  trusted="$(ssh -n -o BatchMode=yes -o ConnectTimeout=6 "$host"       "docker exec ${inst}-api-1 printenv TRUSTED_PROXY_IPS" 2>/dev/null | tr -d '')"
  live="$(ssh -n -o BatchMode=yes -o ConnectTimeout=6 "$host"       "docker exec ${inst}-redis-1 redis-cli --scan --pattern 'rl:*:10.10.0.3'" 2>/dev/null | head -1 | tr -d '')"
  case "$trusted" in
    *10.10.0.*) cfg="ок";;
    "")         cfg="нет данных";;
    *)          cfg="НЕ ДОВЕРЯЕТ";;
  esac
  if [ "$cfg" != "ок" ] || [ -n "$live" ]; then
    collapsed=$((collapsed+1))
    printf "  %-14s доверие: %-12s живой ключ маршрутизатора: %s
"       "$inst" "$cfg" "${live:-—}"
  fi
done < instances.tsv
[ "$collapsed" = "0" ] && echo "  все инстансы видят реальные адреса клиентов"

# --- Расхождение с базой флота (инцидент 2026-09-01) ---------------------------------------
# Новый инстанс рождался с умолчаниями `.env.prod.example`, а флот живёт в другом режиме. Три
# инстанса отдавали 404 на /docs при живом health: «основной отвечает» — правда, «инстанс
# работает» — нет. Проверка health этого не видит по устройству, поэтому нужна отдельная.
# Спрашиваем инстанс СНАРУЖИ, через вход: так же, как в него ткнётся человек.
echo
echo "ДОКУМЕНТАЦИЯ:"
docs_bad=0
while IFS=$'	' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  [ -n "$ONE" ] && [ "$inst" != "$ONE" ] && continue
  [ "$domain" = "ПОДЛЕЖИТ_УТОЧНЕНИЮ" ] && continue
  # Через HTTPS и через локальный вход: по HTTP вход отвечает перенаправлением (301), и
  # проверка по нему меряла бы редирект, а не доступность документации.
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8         --resolve "$domain:443:127.0.0.1" "https://$domain/docs" 2>/dev/null)"
  if [ "$c" != "200" ]; then
    docs_bad=$((docs_bad+1))
    printf "  %-14s /docs -> %s
" "$inst" "${c:-нет ответа}"
  fi
done < instances.tsv
[ "$docs_bad" = "0" ] && echo "  документация открыта на всех инстансах"

# --- Заглушки шаблона в боевом инстансе (инцидент 2026-09-02) ------------------------------
# `APPSTORE_BUNDLE_ID=<com.example.app>` не безобиден: проверка bundle активна, пока значение
# НЕПУСТОЕ, поэтому сервер сравнивает транзакцию с литералом-пустышкой и отвергает КАЖДУЮ
# покупку. Наружу это выглядит как «bundleId mismatch», и разбор уходит в сторону подписи.
# Вымышленные продукты (`tokens_1500`, `weekly_xxx`) отвергают покупку настоящего как
# «unknown token product». Ни то, ни другое не видно ни по health, ни по документации.
echo
echo "ЗАГЛУШКИ ШАБЛОНА:"
stub_bad=0
while IFS=$'	' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  [ -n "$ONE" ] && [ "$inst" != "$ONE" ] && continue
  out="$(ssh -n -o BatchMode=yes -o ConnectTimeout=6 "app${primary}"       "grep -hE '^(APPSTORE_BUNDLE_ID|TOKEN_PRODUCTS|ADAPTY_PRODUCT_TOKENS)=' /opt/$inst/.env"       2>/dev/null | tr -d '')"
  probs=""
  case "$out" in *"<"*) probs="$probs bundle-заглушка";; esac
  case "$out" in *tokens_1500*) probs="$probs продукты-заглушки";; esac
  case "$out" in *_xxx*|*_yyy*) probs="$probs подписки-заглушки";; esac
  if [ -n "$probs" ]; then
    stub_bad=$((stub_bad+1))
    printf "  %-14s%s
" "$inst" "$probs"
  fi
done < instances.tsv
[ "$stub_bad" = "0" ] && echo "  заглушек шаблона нет"
