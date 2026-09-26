#!/bin/bash
# Обёртка вызова инструментов флота (ADR-115 §10.4, §13).
#
#   fleet-server   <подкоманда> [аргументы]      (символические ссылки на эту обёртку в /usr/local/bin)
#   fleet-instance <подкоманда> [аргументы]
#   fleet-backup   <подкоманда> [аргументы]
#   fleet-route    <подкоманда> [аргументы]
#   fleet <группа> <подкоманда> [аргументы]      (то же, явной группой)
#
# До `fleet-server bootstrap` ссылок в /usr/local/bin ещё нет — CRM вызывает обёртку прямо из
# действующей версии: /opt/fleet/fleet.sh server version (ADR-115 §2.1 шаг 1).
#
# Правила версий (ADR-115 §10.4):
#   (1) путь /opt/fleet разрешается в абсолютный путь версии ОДИН раз, здесь, и подкоманда до
#       конца работает только из него — переключение symlink деплоем посреди работы её не
#       касается (bash читает скрипт по мере исполнения; запись на месте исполнила бы хвост
#       подкоманды из новой версии — для purge и objstore-set это необратимо);
#   (2) на всё время работы подкоманда держит РАЗДЕЛЯЕМЫЙ flock на <версия>/.inuse — CI удаляет
#       старую версию, только взяв на неё ИСКЛЮЧАЮЩИЙ flock -n.
set -uo pipefail

self="$(basename "$0")"
case "$self" in
  fleet-server|fleet-instance|fleet-backup|fleet-route) group="${self#fleet-}";;
  *) group="${1:-}"; shift || true;;
esac

result() {  # аварийный FLEET-RESULT до загрузки lib (jq и версия могут отсутствовать)
  printf 'FLEET-RESULT {"ok":false,"step":"%s","reason":"%s","leftovers":[],"evidence":{},"tools_version":"%s"}\n' \
    "$group" "$1" "${2:-unknown}"
  exit "${3:-1}"
}

ver="$(readlink -f /opt/fleet 2>/dev/null)" || result tools_not_installed
[ -n "$ver" ] && [ -f "$ver/VERSION" ] || result tools_not_installed
v="$(tr -dc '0-9a-f' < "$ver/VERSION")"
case "$group" in
  server|instance|backup|route) ;;
  *) result unknown_subcommand "$v" 2;;
esac
script="$ver/fleet-$group.sh"
[ -f "$script" ] || result unknown_subcommand "$v" 2
[ -e "$ver/.inuse" ] || : > "$ver/.inuse" 2>/dev/null
command -v flock >/dev/null 2>&1 || result missing_tool:flock "$v"

export FLEET_VERSION_DIR="$ver"
exec flock -s "$ver/.inuse" bash "$script" "$@"
