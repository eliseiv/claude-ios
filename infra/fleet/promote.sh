#!/usr/bin/env bash
# Повышение резерва до основного. Выполняется НА МАРШРУТИЗАТОРЕ (docs/MIGRATION-3-SERVERS.md).
#
# Почему здесь, а не на прикладном сервере: право называть основного принадлежит ровно одному
# узлу. Если такое решение сможет принять каждый сервер сам, после разрыва связи оба объявят
# себя основными, обе базы примут записи и разойдутся необратимо. Маршрутизатор — единственный
# вход, поэтому «недоступен маршрутизатору» практически совпадает с «недоступен пользователям».
#
#   promote.sh <инстанс> <A|B>      повысить экземпляр на указанном сервере
#   promote.sh --all-from <A|B>     повысить ВСЕ инстансы, чей основной — отказавший сервер
set -uo pipefail
cd /opt/router/fleet || { echo "нет /opt/router/fleet"; exit 1; }

WG_A=10.10.0.1; WG_B=10.10.0.2
host_of() { [ "$1" = "A" ] && echo appA || echo appB; }
other()   { [ "$1" = "A" ] && echo B || echo A; }

promote_one() {
  local inst="$1" target="$2" src; src="$(other "$target")"
  local th; th="$(host_of "$target")"
  local sh_; sh_="$(host_of "$src")"

  echo "[$inst] повышаю экземпляр на сервере $target"
  # 1. Повышение базы. pg_promote возвращает t только если база действительно была резервом.
  if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$th" "
      cd /opt/$inst || exit 1
      proj=\$(grep -m1 '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2-); proj=\${proj:-$inst}
      u=\$(grep -m1 '^POSTGRES_USER=' .env | cut -d= -f2-); u=\${u:-postgres}
      d=\$(grep -m1 '^POSTGRES_DB=' .env | cut -d= -f2-); d=\${d:-postgres}
      docker exec -i \${proj}-postgres-1 psql -U \$u -d \$d -tAc 'SELECT pg_promote(true, 60)'
    " 2>/dev/null | grep -q t; then
    echo "[$inst] база НЕ повышена — прекращаю, роль не меняю"
    return 1
  fi

  # 2. Таблица ролей меняется ТОЛЬКО после успешного повышения базы: иначе маршрутизатор
  #    отправил бы трафик туда, где записи ещё невозможны.
  awk -F'\t' -v i="$inst" -v t="$target" 'BEGIN{OFS="\t"} $1==i{$2=t} {print}' \
      /opt/router/roles/roles.tsv > /tmp/roles.new && mv /tmp/roles.new /opt/router/roles/roles.tsv
  awk -F'\t' -v i="$inst" -v t="$target" 'BEGIN{OFS="\t"} /^#/{print;next} $1==i{$4=t} {print}' \
      instances.tsv > /tmp/inst.new && mv /tmp/inst.new instances.tsv

  # 3. Старый основной отсекается. Недоступен — не беда: страж ролей на нём при загрузке
  #    прочитает уже изменённую таблицу и сам не даст api подняться.
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$sh_" "
      cd /opt/$inst 2>/dev/null || exit 0
      proj=\$(grep -m1 '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2-); proj=\${proj:-$inst}
      echo standby > .role
      docker compose -p \$proj -f docker-compose.prod.yml -f docker-compose.fleet.yml --env-file .env stop api
    " >/dev/null 2>&1 && echo "[$inst] прежний основной ($src) отсечён" \
                     || echo "[$inst] прежний основной ($src) недоступен — отсечёт страж ролей при его загрузке"

  # 4. Поднимаем api на новом основном.
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$th" "
      cd /opt/$inst || exit 1
      proj=\$(grep -m1 '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2-); proj=\${proj:-$inst}
      echo primary > .role
      docker compose -p \$proj -f docker-compose.prod.yml -f docker-compose.fleet.yml --env-file .env up -d --no-build api
    " >/dev/null 2>&1
  echo "[$inst] повышен на $target"
}

case "${1:-}" in
  --all-from)
    failed="${2:?укажите отказавший сервер: A или B}"; to="$(other "$failed")"
    n=0
    while IFS=$'\t' read -r inst primary; do
      case "$inst" in ""|\#*) continue;; esac
      [ "$primary" = "$failed" ] || continue
      promote_one "$inst" "$to" && n=$((n+1))
    done < /opt/router/roles/roles.tsv
    echo "повышено инстансов: $n"
    ;;
  "") echo "использование: promote.sh <инстанс> <A|B> | promote.sh --all-from <A|B>"; exit 2;;
  *) promote_one "$1" "${2:?целевой сервер: A или B}";;
esac

# 5. Конфигурация маршрутизатора перегенерируется из обновлённой таблицы — руками не правится.
#
# TLS определяется по ДЕЙСТВУЮЩЕЙ конфигурации, а не по переменной окружения. Прежде здесь стояло
# `${FLEET_TLS:+--tls}`: забыл выставить переменную — и повышение ОДНОГО инстанса молча снимало
# HTTPS со ВСЕГО флота. Ровно это и произошло 2026-09-02 при отказе сервера A: аварийное
# переключение выключило сертификаты на всех 36 доменах, и к отказу восьми инстансов добавился
# отказ остальных двадцати восьми. Признак «включён ли TLS» уже записан в самой конфигурации,
# и спрашивать его у оператора в момент аварии — худшее время для вопроса.
LIVE="/opt/router/dynamic/dynamic.yml"
TLS_FLAG=""
if [ -f "$LIVE" ] && grep -q "certResolver" "$LIVE"; then
  TLS_FLAG="--tls"
fi
# Имя файла ТО ЖЕ, что у действующего. Прежде копировалось в `fleet.yml` — Traefik читает каталог
# целиком, поэтому рядом оказывались два набора одноимённых роутеров, и какой из них победит,
# зависело от порядка чтения.
python3 gen-router-config.py $TLS_FLAG && cp dynamic.yml "$LIVE"
echo "конфигурация маршрутизатора обновлена (TLS: ${TLS_FLAG:-нет}; Traefik подхватит сам)"
