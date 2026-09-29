#!/usr/bin/env bash
# Подготовка прикладного сервера типа «Chat Bot» (ADR-115 §2, §2.1 шаг 2).
#
# Прежняя редакция (A/B, репликация, пир «второй прикладной сервер», клон репозитория
# deploy-ключом, страж ролей, запасной вход Traefik) СНЯТА ADR-115 §2: сервер больше не получает
# доступа к репозиторию (образ — из GHCR, файлы compose — бандлом /opt/fleet/compose), пир
# WireGuard у него ОДИН — маршрутизатор R, стража ролей и запасного входа нет (§6.4).
#
# Этот файл — тонкий вход в `fleet-server bootstrap` ТОЙ ЖЕ версии инструментов, из которой он
# запущен: его можно вызвать до установки обёртки в /usr/local/bin, сразу после распаковки версии
# (§2.1 шаг 1):
#
#   bash /opt/fleet/bootstrap-server.sh < bootstrap.json      (одна JSON-строка, см. fleet-server.sh)
#
# Секреты — только в stdin, никогда в аргументах. Идемпотентен: повтор с тем же входом — no-op по
# существу (ключ WireGuard сохраняется, управляемая строка ключа CI заменяется переданной).
set -uo pipefail
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd -P)"
export FLEET_VERSION_DIR="$here"
[ -e "$here/.inuse" ] || : > "$here/.inuse"
if command -v flock >/dev/null 2>&1; then
  exec flock -s "$here/.inuse" bash "$here/fleet-server.sh" bootstrap
fi
exec bash "$here/fleet-server.sh" bootstrap
