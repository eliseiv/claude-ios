#!/bin/bash
# fleet-walg — единственная точка вызова wal-g во флоте (ADR-115 §8.2, §8.4, §8.7).
#
#   fleet-walg [--root] <аргументы wal-g...>
#   fleet-walg --self-test
#
# Зачем обёртка, а не голый wal-g. Префикс инстанса в бакете обязан строиться из проверенных
# величин, а не из того, что оказалось в окружении: при пустом INSTANCE_UID ночной
# `wal-g delete` удалил бы чужие копии (ADR-115 §8.2, «Защита префикса — три слоя»). Здесь —
# второй слой: формат INSTANCE_UID (UUID) и INSTANCE_GENERATION (целое >= 1) проверяется ДО
# любого обращения к хранилищу, и при несоответствии wal-g не запускается вовсе. Первый слой —
# `${…:?}` в docker-compose.walg.yml, третий — сверка с meta.json (fleet-wal-push и инструменты
# флота на хосте).
#
# Режимы:
#   без --root — префикс поколения инстанса: s3://<бакет>/instances/<UID>/g<N>/wal-g
#                (backup-push, wal-push, wal-fetch, backup-fetch, backup-list, delete);
#   --root     — корень бакета: s3://<бакет> (st put/get/cat/ls/rm по полным ключам
#                registry/…, instances/<UID>/…/config/…, router/…, servers/…).
#
# Окружение (значения секретов обёртка не печатает никогда):
#   FLEET_WALG_DIR            каталог кредов в контейнере, по умолчанию /etc/fleet/walg
#                             (walg.json, bucket, server_id, keys/<key_id>) — монтируется целиком
#                             и только на чтение (ADR-115 §8.2);
#   AWS_ACCESS_KEY_ID, …      если заданы в окружении (задания хоста: `docker run --env-file
#                             /etc/fleet/objstore/objstore.env`) — используются они; иначе
#                             `--config $FLEET_WALG_DIR/walg.json` (archive_command в контейнере);
#   FLEET_BUCKET              имя бакета; иначе читается из $FLEET_WALG_DIR/bucket;
#   FLEET_INSTANCE_UID        UUID инстанса (из .env: INSTANCE_UID);
#   FLEET_INSTANCE_GENERATION номер поколения (из .env: INSTANCE_GENERATION);
#   FLEET_WALG_KEY_ID         идентификатор ключа шифрования (из .env: WALG_KEY_ID); ключ читается
#                             из $FLEET_KEYS_DIR/<key_id> (по умолчанию $FLEET_WALG_DIR/keys), hex;
#   FLEET_TEST_FILE_ROOT      ТОЛЬКО для проверок qa в контейнере: локальный каталог вместо S3
#                             (wal-g WALG_FILE_PREFIX). В проде не задаётся.
set -euo pipefail

DIR="${FLEET_WALG_DIR:-/etc/fleet/walg}"
KEYS_DIR="${FLEET_KEYS_DIR:-$DIR/keys}"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
GEN_RE='^[1-9][0-9]{0,8}$'
KEYID_RE='^[0-9a-f]{12}$'
BUCKET_RE='^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$'

die() { echo "fleet-walg: $*" >&2; exit 64; }

self_test() {
  local bad=0
  [[ "0f8fad5b-d9cb-469f-a165-70867728950e" =~ $UUID_RE ]] || bad=1
  [[ "" =~ $UUID_RE ]] && bad=1
  [[ "../x" =~ $UUID_RE ]] && bad=1
  [[ "1" =~ $GEN_RE ]] || bad=1
  [[ "0" =~ $GEN_RE ]] && bad=1
  [[ "" =~ $GEN_RE ]] && bad=1
  [[ "0123456789ab" =~ $KEYID_RE ]] || bad=1
  [[ "../../etc/pw" =~ $KEYID_RE ]] && bad=1
  [ "$bad" = 0 ] || die "self-test: валидаторы работают неверно"
  echo "fleet-walg self-test ok"
}

[ "${1:-}" = "--self-test" ] && { self_test; exit 0; }

root=0
if [ "${1:-}" = "--root" ]; then root=1; shift; fi
[ "$#" -gt 0 ] || die "не передана команда wal-g"

# --- префикс ---------------------------------------------------------------------------------
suffix=""
if [ "$root" = 0 ]; then
  uid="${FLEET_INSTANCE_UID:-}"; gen="${FLEET_INSTANCE_GENERATION:-}"
  [[ "$uid" =~ $UUID_RE ]] || die "INSTANCE_UID пуст или не UUID — к хранилищу не обращаюсь"
  [[ "$gen" =~ $GEN_RE ]] || die "INSTANCE_GENERATION пуст или не целое >= 1 — к хранилищу не обращаюсь"
  suffix="/instances/$uid/g$gen/wal-g"
fi

if [ -n "${FLEET_TEST_FILE_ROOT:-}" ]; then
  unset WALG_S3_PREFIX
  export WALG_FILE_PREFIX="${FLEET_TEST_FILE_ROOT%/}$suffix"
else
  bucket="${FLEET_BUCKET:-}"
  if [ -z "$bucket" ] && [ -r "$DIR/bucket" ]; then bucket="$(tr -d ' \r\n' < "$DIR/bucket")"; fi
  [[ "$bucket" =~ $BUCKET_RE ]] || die "имя бакета не задано или некорректно"
  unset WALG_FILE_PREFIX
  export WALG_S3_PREFIX="s3://$bucket$suffix"
fi

# --- ключ шифрования -------------------------------------------------------------------------
# Без ключа шифрования в хранилище не пишется ничего, кроме явно незашифрованных объектов
# (heartbeat/restore-check, `st put --no-encrypt`): бакет содержит переписку, PII и платежи
# (ADR-115 §8.7). Поэтому для всех команд, кроме чтения служебных объектов, ключ обязателен.
kid="${FLEET_WALG_KEY_ID:-}"
if [ -n "$kid" ]; then
  [[ "$kid" =~ $KEYID_RE ]] || die "WALG_KEY_ID не 12 hex"
  [ -r "$KEYS_DIR/$kid" ] || die "ключ $kid не найден или не читается в $KEYS_DIR"
  export WALG_LIBSODIUM_KEY_PATH="$KEYS_DIR/$kid"
  export WALG_LIBSODIUM_KEY_TRANSFORM=hex
else
  unset WALG_LIBSODIUM_KEY_PATH WALG_LIBSODIUM_KEY
  case "$1 ${2:-}" in
    "st ls"|"st cat"|"st rm"|"st check"|"backup-list "*) ;;
    "st put")
      case " $* " in *" --no-encrypt "*) ;; *) die "st put без ключа шифрования запрещён (нужен --no-encrypt явно)";; esac ;;
    *) die "команда '$1' требует ключ шифрования (FLEET_WALG_KEY_ID)";;
  esac
fi

# --- креды -----------------------------------------------------------------------------------
cfg=()
if [ -z "${FLEET_TEST_FILE_ROOT:-}" ] && [ -z "${AWS_ACCESS_KEY_ID:-}" ]; then
  [ -r "$DIR/walg.json" ] || die "$DIR/walg.json не читается этим пользователем ($(id -un))"
  cfg=(--config "$DIR/walg.json")
fi

exec wal-g "${cfg[@]}" "$@"
