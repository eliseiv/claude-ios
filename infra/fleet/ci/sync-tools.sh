#!/bin/bash
# Синхронизация версии инструментов на сервере или R (ADR-115 §10.4). Исполняется НА ХОСТЕ,
# текст приходит со стороны CI через `ssh … bash -s -- <sha>`, архив версии заранее выложен в
# /opt/fleet.d/.incoming/<sha>.tgz.
#
# Правила §10.4:
#   * каждая версия — новый каталог /opt/fleet.d/<sha>/, прежний не перезаписывается;
#   * VERSION пишется ПОСЛЕДНИМ; распаковка идёт в <sha>.partial и переименовывается целиком;
#   * /opt/fleet переключается атомарно: ln -s во временное имя + mv -T;
#   * удаляются версии, кроме действующей и предыдущей, ТОЛЬКО под исключающим flock -n на их
#     .inuse (работающая подкоманда держит разделяемый) — не взяли, версия ждёт следующего деплоя.
# Если /opt/fleet — КАТАЛОГ (прежний bootstrap-server.sh шаг 5), symlink не ставится: это значит,
# что шаг оператора §11 фазы 1 (переименование в /opt/fleet.legacy) на хосте не выполнен.
set -euo pipefail

SHA="${1:?sha}"
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "::error::sync: SHA не 40 hex"; exit 2; }
BASE=/opt/fleet.d
INC="$BASE/.incoming/$SHA.tgz"
DST="$BASE/$SHA"

if [ -e /opt/fleet ] && [ ! -L /opt/fleet ]; then
  echo "::error title=/opt/fleet — каталог::на $(hostname) не выполнен шаг оператора ADR-115 §11 фазы 1 (переименовать /opt/fleet в /opt/fleet.legacy)"
  exit 3
fi
PREV="$(readlink -f /opt/fleet 2>/dev/null || true)"

if [ -f "$DST/VERSION" ] && [ "$(tr -d ' \r\n' < "$DST/VERSION")" = "$SHA" ]; then
  echo "sync: версия $SHA уже на месте"
else
  [ -f "$INC" ] || { echo "::error::sync: архив $INC не выложен"; exit 4; }
  [ -e "$DST" ] && { echo "::error::sync: $DST существует без VERSION=$SHA — разобрать вручную"; exit 5; }
  rm -rf "$DST.partial"
  mkdir -p "$DST.partial"
  tar -xzf "$INC" -C "$DST.partial" --exclude=./VERSION
  : > "$DST.partial/.inuse"
  printf '%s\n' "$SHA" > "$DST.partial/VERSION"
  mv -T "$DST.partial" "$DST"
fi
rm -f "$INC"

tmp="/opt/.fleet.new.$$"
ln -sfn "$DST" "$tmp"
mv -T "$tmp" /opt/fleet

# Обёртка в /usr/local/bin — копия из действующей версии (атомарно) + ссылки групп.
t="$(mktemp /usr/local/bin/.fleet.XXXXXX)"
cp "$DST/fleet.sh" "$t" && chmod 0755 "$t" && mv -f "$t" /usr/local/bin/fleet
for g in server instance backup route; do ln -sfn fleet "/usr/local/bin/fleet-$g"; done

# Уборка: остаются действующая и предыдущая.
for v in "$BASE"/*; do
  [ -d "$v" ] || continue
  case "$(basename "$v")" in .incoming|*.partial) continue;; esac
  rv="$(readlink -f "$v")"
  [ "$rv" = "$DST" ] && continue
  [ -n "$PREV" ] && [ "$rv" = "$PREV" ] && continue
  (
    exec 9>>"$v/.inuse"
    if flock -n 9; then rm -rf --one-file-system -- "$v"; echo "sync: удалена версия $(basename "$v")"
    else echo "sync: версия $(basename "$v") занята работающей подкомандой — оставлена"; fi
  )
done
echo "SYNCED $SHA on $(hostname)"
