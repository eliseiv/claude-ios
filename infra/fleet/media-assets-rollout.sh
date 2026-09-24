#!/usr/bin/env bash
# Своё хранение результатов медиа-генерации на ОДНОМ инстансе (ADR-109 §Порядок выката пп. 2, 4, 5;
# §6 «Область действия очистки»). Выполняется НА МАРШРУТИЗАТОРЕ: только он видит оба прикладных
# сервера.
#
#   media-assets-rollout.sh <инстанс> [--rewrite-identity] [--apply]  — подготовить каталог (п. 2)
#   media-assets-rollout.sh <инстанс> --enable  [--restart] [--apply] — включить хранение (п. 4)
#   media-assets-rollout.sh <инстанс> --disable [--apply]             — откат (п. 5)
#
# По умолчанию — сухой прогон: печатает состояние обоих серверов и план, ничего не пишет.
#
# Подготовка (без флага режима) — разовый шаг для СУЩЕСТВУЮЩИХ инстансов; новые готовит
# provision.sh (new/adapt) той же раскладкой. На ОБОИХ серверах: каталог /opt/<инстанс>/media-assets
# (10001:10001, 0750) и в его корне пустой маркер `.media-assets-root` (10001:10001, 0640).
# Маркер — часть предиката «корень хранения доступен» (ADR-109 §5): каталог без маркера (его
# создал сам Docker от root при первом up на неподготовленном сервере) приложение считает
# неподготовленным. В маркер пишется одна строка `pg_system_identifier=<ID>` (ADR-109 §6.3): ID
# снимается `SELECT system_identifier FROM pg_control_system()` в контейнере postgres инстанса
# (psql по локальному сокету под POSTGRES_USER — суперпользователь образа, пароль в argv не идёт)
# на ОСНОВНОМ, сверяется с резервом, и на оба сервера пишется одно значение. Без совпадающей
# строки приложение блокирует только очистку сирот (media_asset_cleanup_blocked = 1). Строка,
# отличная от базы (кластер пересоздан), переписывается только с --rewrite-identity — это выход из
# блокировки, и оператор обязан сначала убедиться, что api смотрит в правильную базу.
# Существующий каталог и его содержимое не трогаются — меняются только владелец
# и права самого каталога и маркера. Повторный запуск тождественен.
#
# Включение (--enable) — MEDIA_ASSET_STORAGE_DIR=/data/media-assets на ОБА сервера одинаково
# (значение обязано совпадать на основном и резерве, ADR-109 §1.1): `.env` попадает на резерв
# только целиком копией, а adapt этот ключ не трогает. Требует подготовленного каталога на обоих.
# Перезапуск api — только по --restart и только на основном (у резерва api выключен намеренно).
# ДО включения — замер объёма (ADR-109 §Порядок выката п. 1); скрипт его не делает и в БД не ходит.
# Флот включается по одному инстансу или порциями, а не общим циклом (там же, п. 4).
#
# Откат (--disable) — ключ опустошается на ОБОИХ серверах, api основного перезапускается
# (обязательно: иначе запущенный процесс продолжал бы писать), и ТЕМ ЖЕ шагом содержимое каталога
# очищается на обоих серверах, маркер остаётся (п. 5): при выключенном хранении чистить файлы
# больше некому (§6). Очистка выполняется ТОЛЬКО после того, как контейнер api основного перестал
# видеть ключ; иначе — остановка без удаления.
#
# Коды: 0 — готово/сухой прогон; 1 — сбой ssh/записи; 2 — аргументы; 3 — значение ключа на
# серверах различается или не равно штатному; 4 — роли расходятся; 5 — каталог не подготовлен
# (для --enable); 6 — после перезапуска api всё ещё видит ключ, очистка не выполнена;
# 7 — system_identifier на основном и резерве различается; 8 — не прочитан; 9 — маркер несёт другой
# идентификатор, нужен --rewrite-identity. При 7/8/9 ничего не записывается.
# Значение MEDIA_ASSET_STORAGE_DIR — не секрет и печатается; других значений .env скрипт не читает.
set -uo pipefail

FLEET_DIR="${FLEET_DIR:-/opt/router/fleet}"
VALUE="/data/media-assets"   # цель bind-mount'а в docker-compose.prod.yml
host_of() { [ "$1" = "A" ] && echo appA || echo appB; }
other()   { [ "$1" = "A" ] && echo B || echo A; }
die() { echo "[$INST] $2" >&2; exit "$1"; }

INST="${1:-}"
case "$INST" in
  ""|-*) echo "использование: media-assets-rollout.sh <инстанс> [--enable [--restart] | --disable] [--rewrite-identity] [--apply]" >&2; exit 2;;
  *[!a-z0-9_-]*) echo "недопустимое имя инстанса" >&2; exit 2;;
esac
shift
APPLY=0; MODE=prepare; RESTART=0; REWRITE_ID=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0;;
    --apply) APPLY=1;;
    --enable)  [ "$MODE" = prepare ] || { echo "--enable и --disable несовместимы" >&2; exit 2; }; MODE=enable;;
    --disable) [ "$MODE" = prepare ] || { echo "--enable и --disable несовместимы" >&2; exit 2; }; MODE=disable;;
    --restart) RESTART=1;;
    --rewrite-identity) REWRITE_ID=1;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2;;
  esac
  shift
done
[ "$REWRITE_ID" = 1 ] && [ "$MODE" = disable ] && { echo "--rewrite-identity не применяется с --disable" >&2; exit 2; }
[ "$RESTART" = 1 ] && [ "$MODE" != enable ] && { echo "--restart только с --enable (--disable перезапускает всегда)" >&2; exit 2; }

# --- Удалённые функции. Текст не содержит одинарных кавычек: он передаётся в bash -c '...'. ---
# Все пути относительные: rcall уже сделал cd /opt/<инстанс>.
# ds    — состояние каталога одной строкой: D=absent|link|notdir, либо
#         D=dir O=<uid:gid> A=<права> MK=<0|1> MO=<uid:gid маркера|-> N=<записей кроме маркера>
#         B=<байт в каталоге> FREE=<свободно байт на его ФС> MI=<pg_system_identifier маркера|->
# si    — system_identifier базы инстанса на ЭТОМ сервере (только цифры; пусто — не прочитан)
# wm ID — строка маркера `pg_system_identifier=<ID>`: временный файл рядом + mv, 10001:10001 0640
# ev    — значение MEDIA_ASSET_STORAGE_DIR из .env (не секрет)
# prep  — каталог + маркер, владелец и права (ADR-109 §10)
# mr    — строка ID маркера (только если маркер — обычный файл, не ссылка; чтение с таймаутом)
# mw V|keep — маркер: временный файл в /opt/<инстанс> (root, контейнеру не виден) -> владелец/права
#         -> `mv -T` в media-assets. Root никогда не пишет/chown/chmod ПО ПУТИ внутри каталога,
#         который пишет контейнер: ссылка или FIFO, подложенные им, не разыменовываются.
#         Маркер-ссылка/спецфайл -> отказ (32) без изменений.
# sv on|off — ключ в .env: бэкап .env.bak.media-assets-rollout-<метка>, запись через mktemp + mv,
#         права и владелец сохраняются
# ce    — V=<значение ключа в окружении api> | NOT_RUNNING | EXEC_FAILED
# cl    — удалить содержимое каталога, КРОМЕ маркера; сам каталог остаётся (цель bind-mount'а)
# rs    — перезапуск api fleet-compose файлами (как promote.sh / proxy-rollout.sh)
REMOTE_LIB='
M=.media-assets-root
ds(){ d=media-assets; if [ -L "$d" ]; then echo D=link; return; fi; if [ ! -e "$d" ]; then echo D=absent; return; fi; if [ ! -d "$d" ]; then echo D=notdir; return; fi; o=$(stat -c %u:%g "$d"); a=$(stat -c %a "$d"); if [ -f "$d/$M" ] && [ ! -L "$d/$M" ]; then mk=1; mo=$(stat -c %u:%g "$d/$M"); else mk=0; mo=-; fi; n=$(find "$d" -mindepth 1 -maxdepth 1 ! -name "$M" | wc -l); b=$(du -sb "$d" 2>/dev/null | cut -f1); fr=$(df -B1 --output=avail "$d" 2>/dev/null | tail -1 | tr -d " "); mi=$(mr); echo "D=dir O=$o A=$a MK=$mk MO=$mo N=$n B=${b:-0} FREE=${fr:-0} MI=${mi:--}"; }
ev(){ grep -m1 "^MEDIA_ASSET_STORAGE_DIR=" .env 2>/dev/null | cut -d= -f2- | tr -d "\047\042[:space:]"; }
mr(){ m=media-assets/$M; if [ -f "$m" ] && [ ! -L "$m" ]; then timeout 5 grep -m1 "^pg_system_identifier=" -- "$m" 2>/dev/null | cut -d= -f2- | tr -dc "0-9"; fi; }
mw(){ v=$1; d=media-assets; if [ ! -d "$d" ] || [ -L "$d" ]; then exit 41; fi; if [ -L "$d/$M" ] || { [ -e "$d/$M" ] && [ ! -f "$d/$M" ]; }; then exit 32; fi; [ "$(stat -c %d .)" = "$(stat -c %d "$d")" ] || exit 44; [ "$v" = keep ] && v=$(mr); case "$v" in *[!0-9]*) exit 40;; esac; t=$(mktemp ./.media-assets-root.tmp.XXXXXX) || exit 42; if { [ -z "$v" ] || printf "pg_system_identifier=%s\n" "$v" > "$t"; } && chown 10001:10001 "$t" && chmod 0640 "$t" && mv -fT -- "$t" "$d/$M"; then :; else rm -f -- "$t"; exit 43; fi; }
prep(){ d=media-assets; if [ -L "$d" ] || { [ -e "$d" ] && [ ! -d "$d" ]; }; then exit 30; fi; mkdir -p "$d" && chown -h 10001:10001 "$d" && chmod 0750 "$d" || exit 31; mw keep; }
sv(){ k=MEDIA_ASSET_STORAGE_DIR; case "$1" in on) v=/data/media-assets;; off) v="";; *) exit 13;; esac; [ -f .env ] || exit 14; b=.env.bak.media-assets-rollout-$(date +%Y%m%d-%H%M%S); [ -e "$b" ] && b="$b-$$"; cp -a .env "$b" || exit 15; t=$(mktemp .env.tmp.XXXXXX) || exit 16; f=0; while IFS= read -r line || [ -n "$line" ]; do case "$line" in "$k="*) printf "%s=%s\n" "$k" "$v"; f=1;; *) printf "%s\n" "$line";; esac; done < .env > "$t"; [ "$f" = 1 ] || printf "%s=%s\n" "$k" "$v" >> "$t"; if chmod --reference=.env "$t" && chown --reference=.env "$t"; then mv "$t" .env; else rm -f "$t"; exit 17; fi; }
si(){ p=$(grep -m1 "^COMPOSE_PROJECT_NAME=" .env | cut -d= -f2-); p=${p:-$1}; u=$(grep -m1 "^POSTGRES_USER=" .env | cut -d= -f2-); db=$(grep -m1 "^POSTGRES_DB=" .env | cut -d= -f2-); docker exec "${p}-postgres-1" psql -U "$u" -d "$db" -tAc "SELECT system_identifier FROM pg_control_system()" 2>/dev/null | tr -dc "0-9"; }
wm(){ case "$1" in ""|*[!0-9]*) exit 40;; esac; mw "$1"; }
ce(){ p=$(grep -m1 "^COMPOSE_PROJECT_NAME=" .env | cut -d= -f2-); p=${p:-$1}; r=$(docker inspect -f "{{.State.Running}}" "${p}-api-1" 2>/dev/null); [ "$r" = true ] || { echo NOT_RUNNING; return; }; v=$(docker exec "${p}-api-1" printenv MEDIA_ASSET_STORAGE_DIR 2>/dev/null) || { echo EXEC_FAILED; return; }; echo "V=$v"; }
cl(){ d=media-assets; if [ ! -d "$d" ] || [ -L "$d" ]; then exit 0; fi; find "$d" -mindepth 1 -maxdepth 1 ! -name "$M" -exec rm -rf -- {} + || exit 33; }
rs(){ p=$(grep -m1 "^COMPOSE_PROJECT_NAME=" .env | cut -d= -f2-); p=${p:-$1}; docker compose -p "$p" -f docker-compose.prod.yml -f docker-compose.fleet.yml --env-file .env up -d --no-build api; }
'
case "$REMOTE_LIB" in *"'"*) echo "внутренняя ошибка: одинарная кавычка в REMOTE_LIB" >&2; exit 1;; esac

rcall_n() { ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$1" "cd /opt/$INST 2>/dev/null || exit 20; bash -c '$REMOTE_LIB
$2'"; }

# --- 1. Роли: таблица и файлы .role на обеих машинах обязаны совпадать (как proxy-rollout.sh).
cd "$FLEET_DIR" || die 1 "нет каталога $FLEET_DIR"
PRIMARY="$(awk -F'\t' -v i="$INST" '$1==i{print $4}' instances.tsv)"
case "$PRIMARY" in A|B) ;; *) die 4 "нет в instances.tsv или колонка сервера не A/B — ПРЕРЫВАЮ";; esac
PH="$(host_of "$PRIMARY")"; SH="$(host_of "$(other "$PRIMARY")")"
rp="$(rcall_n "$PH" "cat .role 2>/dev/null" | tr -d ' \r\n')"
rsb="$(rcall_n "$SH" "cat .role 2>/dev/null" | tr -d ' \r\n')"
[ "$rp" = "primary" ] && [ "$rsb" = "standby" ] || \
  die 4 "РОЛИ РАСХОДЯТСЯ: tsv=$PRIMARY, .role на $PH='$rp', на $SH='$rsb' — ПРЕРЫВАЮ"

# --- 2. Состояние обоих серверов.
dstate() { rcall_n "$1" ds | tr -d '\r' | grep -m1 -E '^D=(absent|link|notdir|dir( [A-Z]+=[^ ]+)+)$'; }
fv() { printf '%s\n' "$1" | tr ' ' '\n' | awk -F= -v k="$2" '$1==k{print substr($0, length(k)+2); exit}'; }
ready() {  # ready СТРОКА_СОСТОЯНИЯ -> 0, если каталог и маркер как по ADR-109 §10
  [ "$(fv "$1" D)" = dir ] && [ "$(fv "$1" O)" = 10001:10001 ] && [ "$(fv "$1" A)" = 750 ] \
    && [ "$(fv "$1" MK)" = 1 ] && [ "$(fv "$1" MO)" = 10001:10001 ]
}
say() {
  case "$(fv "$1" D)" in
    "") echo "нет данных (ssh/каталог инстанса)";;
    absent) echo "каталога нет";;
    link|notdir) echo "НЕ КАТАЛОГ ($(fv "$1" D))";;
    dir) echo "каталог $(fv "$1" O) $(fv "$1" A), маркер $( [ "$(fv "$1" MK)" = 1 ] && echo "есть ($(fv "$1" MO))" || echo НЕТ), записей $(fv "$1" N), $(fv "$1" B) байт; свободно на ФС $(fv "$1" FREE) байт";;
  esac
}
S_P="$(dstate "$PH")"; S_S="$(dstate "$SH")"
V_P="$(rcall_n "$PH" ev | tr -d '\r')"; V_S="$(rcall_n "$SH" ev | tr -d '\r')"
echo "[$INST] основной $PH, резерв $SH"
echo "  media-assets: $PH $(say "$S_P")"
echo "  media-assets: $SH $(say "$S_S")"
echo "  MEDIA_ASSET_STORAGE_DIR: $PH '${V_P:-<пусто>}', $SH '${V_S:-<пусто>}'"
[ -n "$S_P" ] || die 1 "состояние каталога на $PH не прочитано — ПРЕРЫВАЮ"
[ -n "$S_S" ] || die 1 "состояние каталога на $SH не прочитано — ПРЕРЫВАЮ"
for pair in "$PH|$S_P" "$SH|$S_S"; do
  case "$(fv "${pair#*|}" D)" in link|notdir) die 1 "на ${pair%%|*} /opt/$INST/media-assets не каталог — исправить руками, ПРЕРЫВАЮ";; esac
done
for v in "$V_P" "$V_S"; do
  case "$v" in ""|"$VALUE") ;; *) die 3 "MEDIA_ASSET_STORAGE_DIR='$v' не равен '$VALUE' (цель bind-mount'а) — исправить руками, ПРЕРЫВАЮ";; esac
done
[ "$V_P" = "$V_S" ] || echo "  ВНИМАНИЕ: MEDIA_ASSET_STORAGE_DIR на серверах различается (ADR-109 §1.1) — выровнять через --enable или --disable"

# --- 2б. Тождество базы для строки маркера (ADR-109 §6.3, §Порядок выката п.2).
# system_identifier снимается на ОСНОВНОМ и сверяется с резервом: резерв — физическая копия
# (pg_basebackup в replication.sh init), и идентификатор у неё обязан совпасть; это свойство здесь
# ПРОВЕРЯЕТСЯ, а не предполагается. Не прочитан на любом сервере или различается — ОСТАНОВКА до
# любой записи (коды 8 / 7). Откату (--disable) тождество не нужно: он работает и при лежащей базе.
# Значение не секрет; печатается только вердикт.
SI_P=""; SI_S=""
if [ "$MODE" != disable ]; then
  SI_P="$(rcall_n "$PH" "si $INST" | tr -dc '0-9')"; SI_S="$(rcall_n "$SH" "si $INST" | tr -dc '0-9')"
  [ -n "$SI_P" ] || die 8 "system_identifier базы на $PH не прочитан (postgres инстанса не отвечает?) — ничего не записано, ПРЕРЫВАЮ"
  [ -n "$SI_S" ] || die 8 "system_identifier базы на $SH не прочитан (postgres резерва не отвечает?) — ничего не записано, ПРЕРЫВАЮ"
  [ "$SI_P" = "$SI_S" ] || die 7 "system_identifier базы на $PH и $SH РАЗНЫЙ (резерв не копия основного — repair-replication.sh) — ничего не записано, ПРЕРЫВАЮ"
  echo "  system_identifier базы: совпадает на $PH и $SH"
fi

# --- 3. План.
PREP=""
for pair in "$PH|$S_P" "$SH|$S_S"; do
  h="${pair%%|*}"; s="${pair#*|}"
  if ready "$s"; then echo "  план: каталог на $h готов — не трогать"
  else echo "  план: подготовить каталог и маркер на $h"; PREP="$PREP $h"; fi
done
WID=""
if [ "$MODE" != disable ]; then
  for pair in "$PH|$S_P" "$SH|$S_S"; do
    h="${pair%%|*}"; mi="$(fv "${pair#*|}" MI)"
    if [ "$mi" = "$SI_P" ]; then echo "  план: строка маркера на $h совпадает с базой — не трогать"
    elif [ -z "$mi" ] || [ "$mi" = "-" ]; then echo "  план: вписать строку pg_system_identifier в маркер на $h"; WID="$WID $h"
    elif [ "$REWRITE_ID" = 1 ]; then echo "  план: ПЕРЕПИСАТЬ строку маркера на $h (--rewrite-identity: база инстанса пересоздана)"; WID="$WID $h"
    else die 9 "на $h маркер несёт ДРУГОЙ pg_system_identifier — база пересоздана или api смотрит не туда; убедиться, что база правильная, и повторить с --rewrite-identity (ADR-109 §6.3)"
    fi
  done
fi
case "$MODE" in
  enable)
    for h in "$PH:$V_P" "$SH:$V_S"; do
      [ "${h#*:}" = "$VALUE" ] && echo "  план: хранение на ${h%%:*} уже включено — не трогать" \
        || echo "  план: вписать MEDIA_ASSET_STORAGE_DIR=$VALUE на ${h%%:*}"
    done
    [ "$RESTART" = 1 ] && echo "  план: перезапустить api на $PH"
    echo "  НАПОМИНАНИЕ: до включения — замер объёма за 30 дней (ADR-109 §Порядок выката п. 1)";;
  disable)
    echo "  план: опустошить MEDIA_ASSET_STORAGE_DIR на $PH и $SH, перезапустить api на $PH,"
    echo "        затем удалить содержимое media-assets на $PH ($(fv "$S_P" N) записей) и $SH ($(fv "$S_S" N) записей), маркер оставить";;
esac

if [ "$APPLY" != "1" ]; then
  echo "[$INST] сухой прогон — ничего не записано (для записи: --apply)"
  exit 0
fi

# --- 4. Выполнение.
for h in $PREP; do
  rcall_n "$h" prep || die 1 "не удалось подготовить каталог на $h"
done
for h in "$PH" "$SH"; do
  ready "$(dstate "$h")" || die 5 "после подготовки каталог на $h не соответствует ADR-109 §10 — ПРЕРЫВАЮ"
done
for h in $WID; do
  rcall_n "$h" "wm $SI_P" || die 1 "не удалось записать строку маркера на $h"
done
if [ "$MODE" != disable ]; then
  for h in "$PH" "$SH"; do
    [ "$(fv "$(dstate "$h")" MI)" = "$SI_P" ] || die 5 "после записи строка маркера на $h не равна system_identifier базы — ПРЕРЫВАЮ"
  done
fi
echo "[$INST] каталог и маркер готовы на $PH и $SH"

case "$MODE" in
  enable)
    for pair in "$PH|$V_P" "$SH|$V_S"; do
      [ "${pair#*|}" = "$VALUE" ] && continue
      rcall_n "${pair%%|*}" "sv on" || die 1 "не удалось записать ключ на ${pair%%|*}"
    done
    a="$(rcall_n "$PH" ev | tr -d '\r')"; b="$(rcall_n "$SH" ev | tr -d '\r')"
    [ "$a" = "$VALUE" ] && [ "$b" = "$VALUE" ] || die 1 "после записи MEDIA_ASSET_STORAGE_DIR не равен '$VALUE' на обоих"
    echo "[$INST] MEDIA_ASSET_STORAGE_DIR=$VALUE на $PH и $SH"
    if [ "$RESTART" = 1 ]; then
      rcall_n "$PH" "rs $INST" >/dev/null 2>&1 || die 1 "перезапуск api на $PH не удался"
      echo "[$INST] api на $PH перезапущен"
    else
      echo "[$INST] api НЕ перезапущен — хранение начнёт работать после перезапуска (--restart)"
    fi;;
  disable)
    for h in "$PH" "$SH"; do
      rcall_n "$h" "sv off" || die 1 "не удалось опустошить ключ на $h — очистка НЕ выполнена"
    done
    a="$(rcall_n "$PH" ev | tr -d '\r')"; b="$(rcall_n "$SH" ev | tr -d '\r')"
    [ -z "$a" ] && [ -z "$b" ] || die 1 "после записи ключ не пуст на $PH или $SH — очистка НЕ выполнена"
    rcall_n "$PH" "rs $INST" >/dev/null 2>&1 || die 1 "перезапуск api на $PH не удался — очистка НЕ выполнена"
    # Пустой printenv сам по себе ничего не доказывает (контейнер не запущен / не тот). Очистка —
    # только когда api основного ЗАПУЩЕН и его окружение ключа не содержит.
    c="$(rcall_n "$PH" "ce $INST" | tr -d '\r')"
    [ "$c" = "V=" ] || die 6 "api на $PH: '${c:-нет ответа}' (нужно: запущен и ключ пуст) — очистка НЕ выполнена"
    for h in "$PH" "$SH"; do
      rcall_n "$h" cl || die 1 "очистка на $h не удалась"
      s="$(dstate "$h")"
      [ "$(fv "$s" N)" = 0 ] && [ "$(fv "$s" MK)" = 1 ] || die 1 "после очистки на $h: $(say "$s")"
    done
    echo "[$INST] хранение выключено; каталог очищен на $PH и $SH, маркер на месте";;
esac
echo "[$INST] готово. Дальше: verify.sh $INST (раздел ХРАНЕНИЕ РЕЗУЛЬТАТОВ)"
