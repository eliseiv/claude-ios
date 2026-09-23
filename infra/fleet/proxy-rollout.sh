#!/usr/bin/env bash
# Подготовка ОДНОГО инстанса к генерации через прокси (ADR-108 §Порядок выката пп. 1, 3).
# Выполняется НА МАРШРУТИЗАТОРЕ: только он видит оба прикладных сервера.
#
#   proxy-rollout.sh <инстанс> [--apply] [--proxy-key-from-stdin | --proxy-key-file <путь>]
#                              [--replace-key] [--restart] [--no-proxy-jobs-in-flight]
#
# По умолчанию — сухой прогон (--dry-run): печатает, ЧТО будет сделано, и ничего не пишет.
#
# Почему отдельный скрипт, а не provision.sh adapt. У инстанса два сервера: основной и резерв.
# `.env` попадает на резерв только целиком копией с основного (migrate-all.sh, раздел 3;
# repair-replication.sh), а adapt меняет `.env` лишь там, где запущен. PROXY_WEBHOOK_SECRET
# подписывает колбэки задач в полёте: разный секрет на двух серверах значит, что после
# повышения резерва каждый такой колбэк получит 401 и задача провисит до дедлайна (6 ч) с
# возвратом при оплаченной у вендора генерации. Поэтому секрет выставляется ОДНИМ И ТЕМ ЖЕ на
# ОБА сервера:
#   пуст на обоих             — генерируется на основном и переносится на резерв;
#   задан на одном            — переносится на второй;
#   задан на обоих одинаковый — ничего не делается;
#   задан на обоих РАЗНЫЙ     — ОСТАНОВКА: какой из них подписал задачи в полёте, скрипт не знает
#                               и молча не выбирает.
#
# PROXY_API_KEY (выдаёт владелец прокси) вписывается на оба сервера только по явному флагу и
# только из stdin или файла — НИКОГДА из аргументов командной строки (они видны в `ps` любому
# пользователю машины). Условия ADR-108 §1: FAL_API_KEY задан на обоих серверах (без него не
# работают загрузки), SERVICE_DOMAIN задан. Уже заданный другой ключ заменяется только с
# --replace-key.
#
# Смена ключа подписи на РАБОТАЮЩЕМ основном. Пока PROXY_WEBHOOK_SECRET пуст, код подписывает
# колбэки ключом PROXY_API_KEY (ADR-108 §4.1; webhook_secret в src/app/media_generation/webhook.py).
# Если на основном ключ прокси уже задан, запись секрета на основной (планы «сгенерировать» и
# «перенести с резерва») МЕНЯЕТ ключ подписи: колбэки задач, уже отправленных в прокси, получат
# 401, и задачи провисят до дедлайна (6 ч). Поэтому такой план — ОСТАНОВКА (код 6); продолжить
# можно только с --no-proxy-jobs-in-flight, получив 0 по запросу из ADR-108 §Порядок выката п.5.
# Скрипт сам в БД не ходит. Перенос секрета на резерв ключ подписи основного не меняет.
#
# Значения секретов и их хэши в вывод не попадают: значения идут с сервера на сервер через
# конвейер ssh | ssh, наружу печатаются только состояния («пуст», «задан», «совпадает»).
# Перезапуск api — только по --restart и только на основном (у резерва api выключен намеренно).
set -uo pipefail

FLEET_DIR="${FLEET_DIR:-/opt/router/fleet}"
host_of() { [ "$1" = "A" ] && echo appA || echo appB; }
other()   { [ "$1" = "A" ] && echo B || echo A; }
die() { echo "[$INST] $2" >&2; exit "$1"; }

INST="${1:-}"
case "$INST" in
  ""|-*) echo "использование: proxy-rollout.sh <инстанс> [--apply] [--proxy-key-from-stdin | --proxy-key-file <путь>] [--replace-key] [--restart] [--no-proxy-jobs-in-flight]" >&2; exit 2;;
  *[!a-z0-9_-]*) echo "недопустимое имя инстанса" >&2; exit 2;;
esac
shift
APPLY=0; KEY_SRC=""; KEY_FILE=""; REPLACE_KEY=0; RESTART=0; NO_JOBS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0;;
    --apply) APPLY=1;;
    --proxy-key-from-stdin) KEY_SRC=stdin;;
    --proxy-key-file) KEY_SRC=file; KEY_FILE="${2:?путь к файлу с ключом}"; shift;;
    --replace-key) REPLACE_KEY=1;;
    --restart) RESTART=1;;
    --no-proxy-jobs-in-flight) NO_JOBS=1;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2;;
  esac
  shift
done

# --- Удалённые функции. Текст не содержит одинарных кавычек: он передаётся в bash -c '...'. ---
# st КЛЮЧ — состояние ключа: 0 (пуст), 2 (заглушка <...>), h:<sha256> (задан). Хэш остаётся в
#           переменных этого скрипта и нигде не печатается.
# gv КЛЮЧ — значение ключа в stdout (только в конвейер на другой сервер).
# gen    — свежие 32 байта hex; сбой openssl — код 11, а не пустое значение.
# wr КЛЮЧ — значение из stdin в .env: бэкап .env.bak.proxy-rollout-<метка>, запись через
#           mktemp в том же каталоге + mv, права и владелец сохраняются.
# rs ПРОЕКТ — перезапуск api fleet-compose файлами (как promote.sh).
REMOTE_LIB='
st(){ v=$(grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- | tr -d "\047\042[:space:]"); case "$v" in "") echo "$1=0";; \<*) echo "$1=2";; *) printf "%s=h:%s\n" "$1" "$(printf %s "$v" | sha256sum | cut -c1-64)";; esac; }
gv(){ grep -m1 "^$1=" .env | cut -d= -f2- | tr -d "\047\042[:space:]"; }
gen(){ v=$(openssl rand -hex 32 2>/dev/null); case "$v" in ""|*[!0-9a-f]*) exit 11;; esac; [ "${#v}" = 64 ] || exit 11; printf "%s\n" "$v"; }
wr(){ k=$1; v=""; IFS= read -r v || [ -n "$v" ] || exit 12; [ -n "$v" ] || exit 12; case "$v" in *[[:space:]]*|\<*) exit 13;; esac; [ -f .env ] || exit 14; b=.env.bak.proxy-rollout-$(date +%Y%m%d-%H%M%S); [ -e "$b" ] && b="$b-$$"; cp -a .env "$b" || exit 15; t=$(mktemp .env.tmp.XXXXXX) || exit 16; f=0; while IFS= read -r line || [ -n "$line" ]; do case "$line" in "$k="*) printf "%s=%s\n" "$k" "$v"; f=1;; *) printf "%s\n" "$line";; esac; done < .env > "$t"; [ "$f" = 1 ] || printf "%s=%s\n" "$k" "$v" >> "$t"; if chmod --reference=.env "$t" && chown --reference=.env "$t"; then mv "$t" .env; else rm -f "$t"; exit 17; fi; }
rs(){ p=$(grep -m1 "^COMPOSE_PROJECT_NAME=" .env | cut -d= -f2-); p=${p:-$1}; docker compose -p "$p" -f docker-compose.prod.yml -f docker-compose.fleet.yml --env-file .env up -d --no-build api; }
'
case "$REMOTE_LIB" in *"'"*) echo "внутренняя ошибка: одинарная кавычка в REMOTE_LIB" >&2; exit 1;; esac

# rcall ХОСТ КОМАНДА — выполнить функцию библиотеки в /opt/<инстанс> на сервере; stdin передаётся.
rcall() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$1" "cd /opt/$INST 2>/dev/null || exit 20; bash -c '$REMOTE_LIB
$2'"; }
rcall_n() { rcall "$1" "$2" < /dev/null; }

# --- 1. Роли: таблица и файлы .role на обеих машинах обязаны совпадать (как repair-replication.sh).
cd "$FLEET_DIR" || die 1 "нет каталога $FLEET_DIR"
PRIMARY="$(awk -F'\t' -v i="$INST" '$1==i{print $4}' instances.tsv)"
case "$PRIMARY" in A|B) ;; *) die 4 "нет в instances.tsv или колонка сервера не A/B — ПРЕРЫВАЮ";; esac
PH="$(host_of "$PRIMARY")"; SH="$(host_of "$(other "$PRIMARY")")"
rp="$(rcall_n "$PH" "cat .role 2>/dev/null" | tr -d ' \r\n')"
rsb="$(rcall_n "$SH" "cat .role 2>/dev/null" | tr -d ' \r\n')"
[ "$rp" = "primary" ] && [ "$rsb" = "standby" ] || \
  die 4 "РОЛИ РАСХОДЯТСЯ: tsv=$PRIMARY, .role на $PH='$rp', на $SH='$rsb' — ПРЕРЫВАЮ"

# --- 2. Состояние обоих серверов (только признаки и хэши, в переменные).
state() {  # state ХОСТ -> строки КЛЮЧ=состояние
  rcall_n "$1" "st PROXY_WEBHOOK_SECRET; st PROXY_API_KEY; st FAL_API_KEY; st SERVICE_DOMAIN"
}
field() { printf '%s\n' "$1" | awk -F= -v k="$2" '$1==k{print substr($0, length(k)+2); exit}'; }
say_state() { case "$1" in 0) echo "пуст";; 2) echo "ЗАГЛУШКА <...>";; h:*) echo "задан";; *) echo "нет данных";; esac; }

S_P="$(state "$PH")"; S_S="$(state "$SH")"
[ -n "$S_P" ] || die 1 "не удалось прочитать .env на $PH — ПРЕРЫВАЮ"
[ -n "$S_S" ] || die 1 "не удалось прочитать .env на $SH — ПРЕРЫВАЮ"
W_P="$(field "$S_P" PROXY_WEBHOOK_SECRET)"; W_S="$(field "$S_S" PROXY_WEBHOOK_SECRET)"
K_P="$(field "$S_P" PROXY_API_KEY)";        K_S="$(field "$S_S" PROXY_API_KEY)"
F_P="$(field "$S_P" FAL_API_KEY)";          F_S="$(field "$S_S" FAL_API_KEY)"
D_P="$(field "$S_P" SERVICE_DOMAIN)"
echo "[$INST] основной $PH, резерв $SH"
echo "  PROXY_WEBHOOK_SECRET: $PH $(say_state "$W_P"), $SH $(say_state "$W_S")"
echo "  PROXY_API_KEY:        $PH $(say_state "$K_P"), $SH $(say_state "$K_S")"
echo "  FAL_API_KEY:          $PH $(say_state "$F_P"), $SH $(say_state "$F_S")"
echo "  SERVICE_DOMAIN:       $PH $(say_state "$D_P")"

# --- 3. План по секрету подписи.
case "$W_P" in 0|2|h:*) ;; *) die 1 "состояние секрета на $PH не прочитано — ПРЕРЫВАЮ";; esac
case "$W_S" in 0|2|h:*) ;; *) die 1 "состояние секрета на $SH не прочитано — ПРЕРЫВАЮ";; esac
[ "$W_P" = "2" ] || [ "$W_S" = "2" ] && die 3 "секрет задан заглушкой <...> — ПРЕРЫВАЮ, исправить руками"
W_PLAN=""
case "$W_P:$W_S" in
  0:0) W_PLAN="gen";   echo "  план: секрет пуст на обоих — сгенерировать на $PH и перенести на $SH";;
  h:*:0) W_PLAN="p2s"; echo "  план: перенести секрет с $PH на $SH";;
  0:h:*) W_PLAN="s2p"; echo "  план: перенести секрет с $SH на $PH";;
  *)
    if [ "$W_P" = "$W_S" ]; then echo "  план: секрет одинаковый на обоих — не трогать"
    else die 3 "РАСХОЖДЕНИЕ: PROXY_WEBHOOK_SECRET на $PH и $SH РАЗНЫЙ — ПРЕРЫВАЮ (выбрать верный может только оператор)"
    fi;;
esac
# Запись секрета на основной при уже заданном на нём ключе прокси меняет ключ подписи колбэков.
case "$W_PLAN" in
  gen|s2p)
    if [ "$K_P" != "0" ]; then
      echo "  ВНИМАНИЕ: на $PH задан PROXY_API_KEY, а секрета нет — колбэки сейчас подписаны ключом;"
      echo "  запись секрета сменит ключ подписи, и задачи в полёте получат 401 (ADR-108 §4.1)."
      echo "  Перед продолжением на основном инстанса должно быть 0 по запросу:"
      echo "    SELECT count(*) FROM media_jobs WHERE provider <> '' AND status IN ('queued','running');"
      [ "$NO_JOBS" = "1" ] || die 6 "ОСТАНОВКА: нужен --no-proxy-jobs-in-flight после проверки, что запрос дал 0"
      echo "  --no-proxy-jobs-in-flight: оператор подтвердил 0 задач в полёте — продолжаю"
    fi;;
esac

# --- 4. План по ключу прокси (только по явному флагу).
KEY=""
if [ -n "$KEY_SRC" ]; then
  [ "$F_P" = "${F_P#h:}" ] || [ "$F_S" = "${F_S#h:}" ] && \
    die 5 "FAL_API_KEY не задан на обоих серверах — ключ прокси не вписывается (ADR-108 §1)"
  [ "$D_P" = "${D_P#h:}" ] && die 5 "SERVICE_DOMAIN не задан на $PH — колбэку некуда прийти (ADR-108 §1)"
  if [ "$KEY_SRC" = "stdin" ]; then
    if [ -t 0 ]; then printf 'ключ прокси (ввод не отображается): ' >&2; IFS= read -rs KEY; echo >&2
    else IFS= read -r KEY || [ -n "$KEY" ]; fi
  else
    [ -r "$KEY_FILE" ] || die 5 "файл ключа не читается"
    IFS= read -r KEY < "$KEY_FILE" || [ -n "$KEY" ]
  fi
  KEY="${KEY%$'\r'}"
  case "$KEY" in ""|*[[:space:]]*|"<"*) KEY=""; die 5 "ключ пуст, содержит пробелы или похож на заглушку — ПРЕРЫВАЮ";; esac
  KEY_H="h:$(printf '%s' "$KEY" | sha256sum | cut -c1-64)"
  for pair in "$PH:$K_P" "$SH:$K_S"; do
    h="${pair%%:*}"; cur="${pair#*:}"
    if [ "$cur" = "$KEY_H" ]; then echo "  план: PROXY_API_KEY на $h уже этот — не трогать"
    elif [ "$cur" = "0" ]; then echo "  план: вписать PROXY_API_KEY на $h"
    elif [ "$REPLACE_KEY" = "1" ]; then echo "  план: ЗАМЕНИТЬ PROXY_API_KEY на $h (--replace-key)"
    else KEY=""; die 5 "на $h уже задан ДРУГОЙ PROXY_API_KEY (или заглушка) — нужен --replace-key"
    fi
  done
fi
[ "$RESTART" = "1" ] && echo "  план: перезапустить api на $PH"

if [ "$APPLY" != "1" ]; then
  echo "[$INST] сухой прогон — ничего не записано (для записи: --apply)"
  KEY=""; exit 0
fi

# --- 5. Выполнение.
case "$W_PLAN" in
  gen) rcall_n "$PH" gen | rcall "$PH" "wr PROXY_WEBHOOK_SECRET" || die 1 "не удалось сгенерировать/записать секрет на $PH"
       rcall_n "$PH" "gv PROXY_WEBHOOK_SECRET" | rcall "$SH" "wr PROXY_WEBHOOK_SECRET" || die 1 "не удалось перенести секрет на $SH";;
  p2s) rcall_n "$PH" "gv PROXY_WEBHOOK_SECRET" | rcall "$SH" "wr PROXY_WEBHOOK_SECRET" || die 1 "не удалось перенести секрет на $SH";;
  s2p) rcall_n "$SH" "gv PROXY_WEBHOOK_SECRET" | rcall "$PH" "wr PROXY_WEBHOOK_SECRET" || die 1 "не удалось перенести секрет на $PH";;
esac
if [ -n "$KEY" ]; then
  for pair in "$PH:$K_P" "$SH:$K_S"; do
    h="${pair%%:*}"; cur="${pair#*:}"
    [ "$cur" = "$KEY_H" ] && continue
    printf '%s\n' "$KEY" | rcall "$h" "wr PROXY_API_KEY" || { KEY=""; die 1 "не удалось записать PROXY_API_KEY на $h"; }
  done
  KEY=""
fi

# --- 6. Сверка после записи: одинаков ли секрет на обоих (печатается только вердикт).
W_P="$(field "$(state "$PH")" PROXY_WEBHOOK_SECRET)"; W_S="$(field "$(state "$SH")" PROXY_WEBHOOK_SECRET)"
case "$W_P" in h:*) ;; *) die 1 "после записи секрет на $PH не задан";; esac
[ "$W_P" = "$W_S" ] || die 3 "после записи секрет на $PH и $SH РАЗНЫЙ — проверить руками"
echo "[$INST] PROXY_WEBHOOK_SECRET: совпадает на $PH и $SH"
if [ -n "$KEY_SRC" ]; then
  K_P="$(field "$(state "$PH")" PROXY_API_KEY)"; K_S="$(field "$(state "$SH")" PROXY_API_KEY)"
  [ "$K_P" = "$KEY_H" ] && [ "$K_S" = "$KEY_H" ] || die 1 "после записи PROXY_API_KEY не совпадает с введённым"
  echo "[$INST] PROXY_API_KEY: вписан на $PH и $SH"
fi

if [ "$RESTART" = "1" ]; then
  if rcall_n "$PH" "rs $INST" >/dev/null 2>&1; then echo "[$INST] api на $PH перезапущен"
  else die 1 "перезапуск api на $PH не удался"; fi
fi
echo "[$INST] готово. Дальше: verify.sh $INST (проба вебхука = 401, секрет совпадает на обоих)"
