#!/bin/bash
# Сборка версии инструментов флота для /opt/fleet.d/<sha>/ (ADR-115 §4.1, §10.4, §11 фаза 1).
#
#   infra/fleet/make-bundle.sh <sha> <каталог-назначения>
#
# Содержимое версии:
#   <корень>/           инструменты infra/fleet/* (§13), lib/, systemd/, fleet.conf
#   <корень>/compose/   бандл деплоя: docker-compose.prod.yml, docker-compose.fleet.yml,
#                       docker-compose.walg.yml, .env.prod.example, certs/AppleRootCA-G3.cer
#   <корень>/VERSION    = <sha>, пишется ПОСЛЕДНИМ: частичная копия не выдаёт себя за полную.
#
# Файлы compose — побайтные копии из репозитория: `docker compose … run --rm --no-deps migrate`
# и `up -d --no-build` при уже помеченном образе НЕ обращаются к контексту сборки (проверено
# 2026-09-26: docker compose v2 на Docker 29.7.2, каталог без Dockerfile, образ помечен заранее —
# config -q rc=0, run --rm --no-deps rc=0, сборка не запускалась). Вариант без секции build не нужен.
#
# Корневой сертификат Apple (ADR-115 §5 «StoreKit-сертификат»): в репозиторий не коммитится —
# *.cer запрещены .gitignore. Берётся с apple.com и принимается ТОЛЬКО при совпадении SHA-256
# с закреплённым отпечатком (публичный отпечаток Apple Root CA - G3). Можно подложить файл
# заранее: APPLE_ROOT_CERT_FILE=<путь> (тот же отпечаток обязателен).
set -euo pipefail

SHA="${1:?первый аргумент — SHA коммита (40 hex)}"
OUT="${2:?второй аргумент — каталог назначения (не должен существовать)}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "SHA не 40 hex" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "$OUT уже существует — не перезаписываю" >&2; exit 2; }

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd -P)"
SRC="$ROOT/infra/fleet"
APPLE_URL="https://www.apple.com/certificateauthority/AppleRootCA-G3.cer"
APPLE_SHA256="63343abfb89a6a03ebb57e9b3f5fa7be7c4f5c756f3017b3a8c488c3653e9179"

mkdir -p "$OUT/compose/certs"
# Инструменты: всё из infra/fleet, кроме того, что серверу не нужно (ci/ — только раннер CI).
( cd "$SRC" && tar --exclude=./ci -cf - . ) | tar -xf - -C "$OUT"
find "$OUT" -maxdepth 1 -name '*.sh' -exec chmod 0755 {} +
for f in docker-compose.prod.yml docker-compose.fleet.yml docker-compose.walg.yml .env.prod.example; do
  install -m 0644 "$ROOT/$f" "$OUT/compose/$f"
done

cert="$OUT/compose/certs/AppleRootCA-G3.cer"
if [ -n "${APPLE_ROOT_CERT_FILE:-}" ]; then
  install -m 0644 "$APPLE_ROOT_CERT_FILE" "$cert"
else
  curl -fsSL --retry 3 --max-time 30 -o "$cert" "$APPLE_URL"
fi
got="$(sha256sum "$cert" | cut -d' ' -f1)"
[ "$got" = "$APPLE_SHA256" ] || { echo "отпечаток сертификата Apple не совпал ($got) — бандл не собран" >&2; rm -rf "$OUT"; exit 3; }

rm -f "$OUT/VERSION"
printf '%s\n' "$SHA" > "$OUT/VERSION"
echo "bundle $SHA -> $OUT"
