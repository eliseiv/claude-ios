#!/usr/bin/env bash
# Включение сертификатов на маршрутизаторе. Выполняется ПОСЛЕ смены A-записей.
#
# Почему не раньше: проверка Let's Encrypt обращается к домену, а до смены записей он ведёт
# на прежний сервер. Каждая такая попытка проваливается и расходует часовой лимит неудачных
# проверок — к моменту, когда записи наконец переключат, лимит может быть уже выбран.
#
# Скрипт: (1) возвращает перенаправление с HTTP на HTTPS, (2) перегенерирует маршруты с TLS,
# (3) ждёт появления сертификатов и показывает, для скольких доменов они выпущены.
set -uo pipefail
cd /opt/router || exit 1

echo "проверяю, что записи уже указывают сюда"
MYIP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null)"
BAD=0
while IFS=$'\t' read -r inst domain port srv; do
  case "$inst" in ""|\#*) continue;; esac
  [ "$domain" = "ПОДЛЕЖИТ_УТОЧНЕНИЮ" ] && continue
  ip="$(getent ahostsv4 "$domain" 2>/dev/null | awk '{print $1; exit}')"
  if [ "$ip" != "$MYIP" ]; then
    echo "  $domain -> ${ip:-нет ответа} (ожидался $MYIP)"
    BAD=$((BAD+1))
  fi
done < fleet/instances.tsv
if [ "$BAD" -gt 0 ]; then
  echo "НЕ ВКЛЮЧАЮ: $BAD доменов ещё указывают не сюда. Дождитесь распространения записей."
  echo "Принудительно: FORCE=1 $0"
  [ "${FORCE:-0}" = "1" ] || exit 1
fi

# Перенаправление на HTTPS возвращается вместе с сертификатами, не раньше: до их выпуска
# оно увело бы проверку ACME в цикл.
python3 - <<'PY'
import pathlib
p = pathlib.Path("/opt/router/traefik.yml")
t = p.read_text(encoding="utf-8")
if "redirections:" not in t:
    t = t.replace(
        '  web:\n    address: ":80"',
        '  web:\n    address: ":80"\n    http:\n      redirections:\n'
        '        entryPoint:\n          to: websecure\n          scheme: https\n          permanent: true'
    )
    p.write_text(t, encoding="utf-8")
    print("перенаправление на HTTPS включено")
else:
    print("перенаправление уже включено")
PY

cd /opt/router/fleet && python3 gen-router-config.py --tls && cp dynamic.yml /opt/router/dynamic/fleet.yml
cd /opt/router && docker compose restart >/dev/null 2>&1
echo "жду выпуска сертификатов"
for i in $(seq 1 40); do
  N="$(python3 -c "
import json,sys
try:
    d=json.load(open('/opt/router/letsencrypt/acme.json'))
    print(sum(len(v.get('Certificates') or []) for v in d.values()))
except Exception: print(0)" 2>/dev/null)"
  echo "  выпущено: ${N:-0}"
  [ "${N:-0}" -ge 26 ] && break
  sleep 15
done
echo "готово. Проверьте: curl -sS -o /dev/null -w '%{http_code}' https://webmoria.shop/health"
