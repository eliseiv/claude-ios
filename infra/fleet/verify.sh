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

ok=0
repl_bad=0; repl_split=""; repl_nostream=""; repl_down=""; bad=0
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
    ""|*ERROR*)  s_state="нет"; lag="—"; repl_bad=$((repl_bad+1)); repl_down="$repl_down $inst";;
    НЕ_РЕЗЕРВ)   s_state="ОСНОВНОЙ!"; lag="—"; repl_bad=$((repl_bad+1)); repl_split="$repl_split $inst";;
    streaming:*) s_state="ок"; lag="${st#streaming:}б";;
    НЕТ_ПОТОКА*) s_state="БЕЗ ПОТОКА"; lag="—"; repl_bad=$((repl_bad+1)); repl_nostream="$repl_nostream $inst";;
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
# Состояние резерва печаталось только в своей строке таблицы. На четырёх десятках строк отметка
# «ОСНОВНОЙ!» не читается — 2026-09-07 выяснилось, что пятнадцать инстансов месяцами живут БЕЗ
# резерва (после переключения на другую машину поток в новую сторону не поднимался), и проверка
# это честно показывала, но никто не смотрел. Итог обязан называть число, иначе проверка есть,
# а знания нет.
if [ "$repl_bad" = "0" ]; then
  echo "репликация: резерв на потоке у всех инстансов"
else
  echo "РЕПЛИКАЦИЯ НАРУШЕНА у $repl_bad инстанс(ов):"
  [ -n "$repl_split" ] && echo "  резерв стал самостоятельным основным (расхождение данных):$repl_split"
  [ -n "$repl_nostream" ] && echo "  резерв не получает поток:$repl_nostream"
  [ -n "$repl_down" ] && echo "  база резерва не отвечает:$repl_down"
fi

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

# --- Самоподписанный сертификат вместо выпущенного (инцидент 2026-09-04) --------------------
# Traefik пытается выпустить сертификат в момент, когда роутер домена появляется в конфигурации.
# Если A-записи в тот момент ещё нет, Let's Encrypt отвечает NXDOMAIN, попытка проваливается —
# и БОЛЬШЕ НЕ ПОВТОРЯЕТСЯ сама: нужно перечитывание конфигурации. Инстанс при этом выглядит
# полностью рабочим — маршрутизация есть, /ready и /docs открываются, — потому что Traefik
# отдаёт собственный самоподписанный «TRAEFIK DEFAULT CERT». Браузер ругается, а проверка по
# коду ответа этого не видит: curl без -k просто не соединяется, а с -k соединяется молча.
# Поэтому проверяем ИМЕННО издателя, а не доступность.
echo
echo "СЕРТИФИКАТЫ:"
tls_bad=0
while IFS=$'	' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  [ -n "$ONE" ] && [ "$inst" != "$ONE" ] && continue
  # Одна неудачная попытка соединения — не приговор: при обходе четырёх десятков доменов
  # подряд случайный обрыв даёт ложную тревогу (2026-09-07, terunavo: проверка объявила
  # сертификат отсутствующим, три повтора подряд показали живой Let's Encrypt).
  issuer=""
  for _try in 1 2; do
    issuer="$(echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null         | openssl x509 -noout -issuer 2>/dev/null)"
    [ -n "$issuer" ] && break
  done
  case "$issuer" in
    *"Let's Encrypt"*) ;;
    "") tls_bad=$((tls_bad+1)); printf "  %-14s сертификат не получен (соединение не установлено)
" "$inst";;
    *)  tls_bad=$((tls_bad+1)); printf "  %-14s НЕ выпущен: %s
" "$inst" "${issuer#issuer=}";;
  esac
done < instances.tsv
[ "$tls_bad" = "0" ] && echo "  у всех инстансов сертификат Let's Encrypt"

# --- Генерация через прокси (ADR-108 §1, §10) -----------------------------------------------
# Инстанс на прокси узнаёт о готовности задачи ТОЛЬКО колбэком. Если колбэк до инстанса не
# доходит (домен, TLS, маршрут), каждая задача висит `running` до дедлайна (6 ч) и закрывается
# возвратом, а закупка у вендора оплачена, — и ни health, ни /docs этого не видят.
#
# Проба снаружи хоста приложения: POST на ручку колбэка БЕЗ token обязан дать 401 — отказ по
# подписи до БД, то есть DNS, TLS, вход и роутер пропускают путь до приложения. 404/502/таймаут
# — вебхук недостижим. Адрес строится из SERVICE_DOMAIN самого инстанса, нормализованного так же,
# как это делает код для callbackUrl (Settings.normalized_service_domain: снять пробелы по краям,
# схему http(s):// без учёта регистра и «/» по краям), а НЕ из instances.tsv: колбэк приходит
# туда, куда указывает .env. Расхождение домена .env с таблицей — нарушение. Печатается ТОЛЬКО
# HTTP-код. Что прокси сам доходит до инстанса (его исходящая сеть), проба не доказывает — это
# видно по метрике media_proxy_jobs_awaiting_callback.
#
# Секрет подписи колбэка обязан совпадать на основном и резерве: после повышения резерва колбэки
# задач в полёте проверяются ЕГО секретом. Хэш секрета считается на каждом сервере, сюда приходит
# только хэш, наружу — только вердикт «совпадает / РАЗНЫЙ / нет данных».
#
# С .env читаются ТОЛЬКО признаки: 0 — пусто, 1 — задан, 2 — задан ЗАГЛУШКОЙ <...>. Заглушка
# для кода — ЗАДАННОЕ значение (любая непустая строка): в PROXY_API_KEY она переключает инстанс на
# прокси с негодным ключом, в FAL_API_KEY — включает fal с негодным ключом. Поэтому заглушка —
# нарушение, а не «не задан». Значения ключей сюда не приходят; домен не секрет и приходит.
# Классы:
#   на прокси (PROXY_API_KEY задан или заглушка) — проба обязательна; любое отклонение = НАРУШЕНИЕ;
#   кандидат  (задан только FAL_API_KEY)        — проба перед переключением (§Порядок выката п.3);
#             отклонение = переключать нельзя, но сегодня инстанс работает (прямой fal);
#   без генерации — пропуск (заглушка в FAL_API_KEY всё равно нарушение).
echo
echo "ПРОКСИ ГЕНЕРАЦИИ:"
hook_bad=0; hook_cand_bad=0; hook_proxy=0; hook_cand=0
hook_path="/v1/media/webhooks/proxy/00000000-0000-0000-0000-000000000000"
# Удалённый фрагмент печатает одну строку: P=<0|1|2> F=<0|1|2> W=<0|1> H=<sha256|-> D=<домен|->
# R=<MEDIA_RESULT_HOST_SUFFIXES|-> (хосты результата — не секрет).
# Текст фрагмента без одинарных кавычек: он передаётся внутри двойных.
flag_snippet='c(){ v=$(grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- | tr -d "\047\042[:space:]"); case "$v" in "") printf 0;; \<*) printf 2;; *) printf 1;; esac; }; w=$(grep -m1 "^PROXY_WEBHOOK_SECRET=" .env 2>/dev/null | cut -d= -f2- | tr -d "\047\042[:space:]"); if [ -n "$w" ]; then wh=$(printf %s "$w" | sha256sum | cut -c1-64); wf=1; else wh=-; wf=0; fi; w=; d=$(grep -m1 "^SERVICE_DOMAIN=" .env 2>/dev/null | cut -d= -f2- | tr -d "\047\042[:space:]"); l=$(printf %s "$d" | tr "[:upper:]" "[:lower:]"); case "$l" in https://*) d=${d#????????};; http://*) d=${d#???????};; esac; d=$(printf %s "$d" | sed "s#^/*##; s#/*\$##"); r=$(grep -m1 "^MEDIA_RESULT_HOST_SUFFIXES=" .env 2>/dev/null | cut -d= -f2- | tr -d "\047\042[:space:]"); printf "P=%s F=%s W=%s H=%s D=%s R=%s\n" "$(c PROXY_API_KEY)" "$(c FAL_API_KEY)" "$wf" "$wh" "${d:--}" "${r:--}"'
flags_of() {  # flags_of ХОСТ ИНСТАНС — строка признаков или пусто
  ssh -n -o BatchMode=yes -o ConnectTimeout=6 "$1" "cd /opt/$2 2>/dev/null || exit 1; $flag_snippet" 2>/dev/null \
    | tr -d '\r' | grep -m1 -E '^P=[012] F=[012] W=[01] H=([0-9a-f]{64}|-) D=[A-Za-z0-9.:-]+ R=[A-Za-z0-9.,-]+$'
}
fval() { printf '%s\n' "$1" | tr ' ' '\n' | awk -F= -v k="$2" '$1==k{print substr($0, length(k)+2); exit}'; }
while IFS=$'\t' read -r inst domain port primary; do
  case "$inst" in ""|\#*) continue;; esac
  [ -n "$ONE" ] && [ "$inst" != "$ONE" ] && continue
  fl="$(flags_of "app${primary}" "$inst")"
  if [ -z "$fl" ]; then
    printf "  %-14s нет данных о .env основного (ssh/каталог)\n" "$inst"
    continue
  fi
  f_proxy="$(fval "$fl" P)"; f_fal="$(fval "$fl" F)"; f_sec="$(fval "$fl" W)"
  h_pri="$(fval "$fl" H)"; env_dom="$(fval "$fl" D)"; [ "$env_dom" = "-" ] && env_dom=""
  probs=""
  # Резерв: признаки читаются для ВСЕХ классов — ключ прокси, заданный только на резерве, после
  # повышения включил бы прокси на инстансе, который до этого на прокси не был.
  sfl="$(flags_of "app$(other "$primary")" "$inst")"
  h_sb="$(fval "$sfl" H)"; p_sb="$(fval "$sfl" P)"
  # Состояние ключа прокси (0/1/2 — не значение и не хэш) обязано совпадать: обрыв записи между
  # серверами (proxy-rollout.sh пишет ключ по очереди) иначе всплыл бы только при повышении резерва.
  [ -n "$sfl" ] && [ "$p_sb" != "$f_proxy" ] && probs="$probs ключ-прокси-на-серверах-различается"
  # Хосты результата: прокси отдаёт файлы со своего хоста (*.mediabackender.com, пилот
  # 2026-09-24); без MEDIA_RESULT_HOST_SUFFIXES каждая задача на прокси -> no_usable_asset.
  r_pri="$(fval "$fl" R)"; r_sb="$(fval "$sfl" R)"
  [ -n "$sfl" ] && [ "$r_pri" != "$r_sb" ] && probs="$probs хосты-результата-на-серверах-различаются"
  [ "$f_proxy" != "0" ] && [ "$r_pri" = "-" ] && probs="$probs на-прокси-без-хоста-результата"
  [ "$f_fal" = "2" ] && probs="$probs FAL_API_KEY-заглушка"
  [ "$f_proxy" = "2" ] && probs="$probs PROXY_API_KEY-заглушка"
  if [ "$f_proxy" != "0" ]; then cls="на прокси"; hook_proxy=$((hook_proxy+1))
  elif [ "$f_fal" = "1" ]; then cls="кандидат"; hook_cand=$((hook_cand+1))
  else
    if [ -n "$probs" ]; then hook_bad=$((hook_bad+1)); printf "  %-14s %-10s%s\n" "$inst" "без генер." "$probs"; fi
    continue
  fi
  # Домен колбэка — из .env; расхождение с таблицей (регистр DNS не различает) — нарушение.
  if [ -z "$env_dom" ]; then
    probs="$probs SERVICE_DOMAIN-пуст"
  elif [ "$(printf %s "$env_dom" | tr '[:upper:]' '[:lower:]')" != "$(printf %s "$domain" | tr '[:upper:]' '[:lower:]')" ]; then
    probs="$probs домен-.env≠таблицы"
  fi
  if [ -n "$env_dom" ]; then
    code=""
    for _try in 1 2; do
      code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -X POST \
        -H 'Content-Type: application/json' --data '{}' "https://$env_dom$hook_path" 2>/dev/null)"
      [ "$code" = "401" ] && break
    done
    [ "$code" = "401" ] || probs="$probs вебхук->${code:-нет ответа}"
  fi
  if [ "$f_proxy" != "0" ]; then
    [ "$f_sec" = "1" ] || probs="$probs PROXY_WEBHOOK_SECRET-пуст"
    [ "$f_fal" = "1" ] || probs="$probs FAL_API_KEY-не-задан"
  fi
  # Секрет на резерве: хэш сравнивается здесь, наружу — только вердикт.
  if [ -z "$sfl" ]; then
    [ "$f_sec" = "1" ] && probs="$probs секрет-на-резерве:нет-данных"
  elif [ "$h_pri" != "$h_sb" ]; then
    probs="$probs секрет-основной≠резерв"
  fi
  if [ -n "$probs" ]; then
    if [ "$f_proxy" != "0" ]; then hook_bad=$((hook_bad+1)); else hook_cand_bad=$((hook_cand_bad+1)); fi
    printf "  %-14s %-10s%s\n" "$inst" "$cls" "$probs"
  fi
done < instances.tsv
echo "  на прокси: $hook_proxy; кандидатов: $hook_cand; нарушений: $hook_bad; кандидатов не готово к переключению: $hook_cand_bad"
