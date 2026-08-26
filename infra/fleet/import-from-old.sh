#!/usr/bin/env bash
# Перенос инстанса с прежнего одиночного сервера на прикладной сервер новой схемы.
# Выполняется НА НОВОМ сервере (docs/MIGRATION-3-SERVERS.md §Порядок работ, п.2).
#
#   import-from-old.sh <инстанс> <A|B>
#
# Что переносится и почему именно это:
#   .env       — ключи провайдеров, секреты, домен. Существуют ТОЛЬКО там: сгенерировать
#                заново нельзя, ключи оплачены и привязаны к аккаунтам.
#   .secrets/  — ключи подписи токенов. Заменишь их — все выданные токены разом перестанут
#                проверяться, и каждый пользователь получит разлогин.
#   certs/     — корневой сертификат Apple для проверки чеков StoreKit.
#   база       — pg_dump/pg_restore, а не копия тома: перенос между машинами, и дамп
#                переживает различия окружения, которых копия каталога не прощает.
#   redis      — НЕ переносится: там только счётчики частоты запросов, они обнулятся
#                без последствий (проверено по коду: ключи rl:*).
set -uo pipefail

INST="${1:?имя инстанса}"
SELF="${2:?на каком сервере разворачиваем: A или B}"
OLD="${OLD_HOST:-root@87.239.135.154}"
DIR="/opt/$INST"

say() { echo "[$INST] $*"; }

ssh_old() { ssh -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=no "$OLD" "$@"; }

ssh_old "test -d /opt/$INST" || { say "на прежнем сервере нет /opt/$INST"; exit 1; }
[ -d "$DIR" ] || { say "локально нет $DIR"; exit 1; }

# 1. Конфигурация и секреты
say "переношу .env, .secrets, certs"
ssh_old "cat /opt/$INST/.env" > "$DIR/.env.incoming" || { say "не удалось прочитать .env"; exit 1; }
[ -s "$DIR/.env.incoming" ] || { say ".env пуст — прекращаю"; exit 1; }
mv "$DIR/.env.incoming" "$DIR/.env"
ssh_old "tar -C /opt/$INST -cf - .secrets certs 2>/dev/null" | tar -C "$DIR" -xf - 2>/dev/null || true
chown -R 10001:10001 "$DIR/.secrets" 2>/dev/null || true
chmod 700 "$DIR/.secrets" 2>/dev/null || true

# 2. Адресация новой схемы (порты туннеля). Остальное в .env не трогается.
/opt/fleet/provision.sh adapt "$INST" "$SELF" || exit 1

PROJ="$(grep -m1 '^COMPOSE_PROJECT_NAME=' "$DIR/.env" | cut -d= -f2-)"; PROJ="${PROJ:-$INST}"
PGUSER="$(grep -m1 '^POSTGRES_USER=' "$DIR/.env" | cut -d= -f2-)"
PGDB="$(grep -m1 '^POSTGRES_DB=' "$DIR/.env" | cut -d= -f2-)"
CF="-f docker-compose.prod.yml -f docker-compose.fleet.yml"
cd "$DIR" || exit 1

# 3. Поднимаем только хранилища: том создаётся, база инициализируется под теми же
#    учётными данными, что и на прежнем сервере (они пришли в .env).
say "поднимаю postgres и redis"
docker compose -p "$PROJ" $CF --env-file .env up -d --no-build postgres redis >/dev/null 2>&1
for i in $(seq 1 40); do
  docker exec "${PROJ}-postgres-1" pg_isready -U "$PGUSER" >/dev/null 2>&1 && break
  sleep 3
done
docker exec "${PROJ}-postgres-1" pg_isready -U "$PGUSER" >/dev/null 2>&1 || { say "postgres не поднялся"; exit 1; }

# 4. Данные. Дамп идёт потоком: на диске он нигде не оседает.
say "снимаю дамп с прежнего сервера и заливаю"
OLDPROJ="$(ssh_old "grep -m1 '^COMPOSE_PROJECT_NAME=' /opt/$INST/.env | cut -d= -f2-")"
OLDPROJ="${OLDPROJ:-$INST}"
if ! ssh_old "docker exec -i ${OLDPROJ}-postgres-1 pg_dump -U $PGUSER -d $PGDB -Fc --no-owner --no-acl" \
     | docker exec -i "${PROJ}-postgres-1" pg_restore -U "$PGUSER" -d "$PGDB" --no-owner --no-acl --clean --if-exists 2>&1 \
     | grep -viE "^pg_restore: (warning|исход)" | tail -5; then
  say "перенос данных завершился с замечаниями (см. выше)"
fi

# 5. Сверка: число пользователей должно совпасть. Молчаливо потерянные строки — худший исход.
NEW_N="$(docker exec -i "${PROJ}-postgres-1" psql -U "$PGUSER" -d "$PGDB" -tAc 'SELECT count(*) FROM users' 2>/dev/null | tr -d ' ')"
OLD_N="$(ssh_old "docker exec -i ${OLDPROJ}-postgres-1 psql -U $PGUSER -d $PGDB -tAc 'SELECT count(*) FROM users'" 2>/dev/null | tr -d ' \r')"
say "пользователей: было ${OLD_N:-?}, стало ${NEW_N:-?}"
if [ -n "$OLD_N" ] && [ "$OLD_N" != "${NEW_N:-x}" ]; then
  say "ЧИСЛА НЕ СОВПАЛИ — инстанс НЕ запускаю, разбирайтесь до включения"
  exit 1
fi

# 6. Роль и запуск. Основной поднимает api, резерв остаётся выключенным до повышения.
ROLE="$(curl -fsS --max-time 8 http://10.10.0.3:8088/roles.tsv 2>/dev/null | awk -F'\t' -v i="$INST" '$1==i{print $2}')"
if [ "$ROLE" = "$SELF" ]; then
  echo primary > .role
  docker compose -p "$PROJ" $CF --env-file .env up -d --no-build >/dev/null 2>&1
  say "поднят как ОСНОВНОЙ"
else
  echo standby > .role
  say "остаётся РЕЗЕРВОМ (основной — $ROLE); api не поднимается"
fi
