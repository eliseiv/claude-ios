#!/bin/bash
# fleet-backup — бэкапы инстансов (ADR-115 §8, §9 шаг 3, §13).
#
#   fleet-backup full <slug>             полная копия (wal-g backup-push) + архив конфигурации
#   fleet-backup final <slug>            pg_dump -Fc → final/, сверка контрольной суммы, финальная копия
#   fleet-backup heartbeat               ограждение + heartbeat.json всех инстансов сервера (таймер 5 мин)
#   fleet-backup restore-check [<slug>]  проверка восстановления в одноразовом контейнере (таймер, еженедельно)
#   fleet-backup nightly                 ночной обход: full + срок хранения по каждому инстансу (таймер)
#
# Все записи в бакет — только после ограждения по поколению (lib/common.sh: fence_state).
# Объекты с данными (config/, final/) шифруются ключом WALG_KEY_ID инстанса и кладутся без
# сжатия (`st put --no-compress`): так ключ объекта в бакете ровно тот, что в ADR-115 §8.4
# (`<дата>.tar`, `<время>.dump`) — со сжатием wal-g дописывает `.lz4` к имени. Служебные
# статусы (heartbeat.json, restore-check.json) секретов не несут и пишутся без шифрования —
# их читает CRM (ADR-115 §8.6).
set -uo pipefail
# shellcheck source=lib/common.sh
. "$(dirname "$(readlink -f "$0")")/lib/common.sh"

SUB="${1:-}"; shift || true
# shellcheck disable=SC2034  # STEP читает lib/common.sh (emit)
STEP="backup.$SUB"
RETENTION_DAYS=30
ERR=""

inst_vars() {  # D UIDV GEN KID PGC для slug
  D="$(inst_dir "$1")"; UIDV="$(env_get "$D/.env" INSTANCE_UID)"; GEN="$(env_get "$D/.env" INSTANCE_GENERATION)"
  KID="$(env_get "$D/.env" WALG_KEY_ID)"; PGC="$(ctr "$1" postgres)"
}
walg_in_pg() {
  local s="$1"; shift
  local u db; u="$(env_get "$D/.env" POSTGRES_USER)"; db="$(env_get "$D/.env" POSTGRES_DB)"
  docker exec -u postgres -e PGHOST=/var/run/postgresql -e "PGUSER=${u:-postgres}" -e "PGDATABASE=${db:-postgres}" \
    "$(ctr "$s" postgres)" nice -n 10 fleet-walg "$@"
}
prefix() { printf 'instances/%s/g%s' "$UIDV" "$1"; }

# do_full <slug> — 0 успех; иначе ERR. Evidence пишется только для одиночного вызова (EVMODE=1).
do_full() {
  local s="$1" io last size cfgkey
  inst_vars "$s"
  local st; fence_state "$s"; st="$FENCE"
  case "$st" in
    ok) ;;
    other_server|mismatch) fence_enforce "$s" "$st"; ERR="fenced:$st"; return 1;;
    *) ERR="fence:$st"; return 1;;
  esac
  ctr_running "$PGC" || { ERR="postgres_not_running"; return 1; }
  walg_in_pg "$s" backup-push /var/lib/postgresql/data >/dev/null 2>&1 || { ERR="backup_push_failed"; return 1; }
  last="$(walg_in_pg "$s" backup-list --json --detail 2>/dev/null | jq -c 'last // empty')"
  size="$(jq -r '.compressed_size // 0' <<<"$last" 2>/dev/null)"
  [ -n "$last" ] && [ "${size:-0}" -gt 0 ] || { ERR="backup_not_in_bucket"; return 1; }
  # Архив конфигурации (.env + .secrets), зашифрован (ADR-115 §8.1, §8.4).
  io="$(mktemp -d /var/tmp/fleet-cfg.XXXXXX)" || { ERR="tmp_failed"; return 1; }
  tmp_track "$io"
  tar -C "$D" -cf "$io/config.tar" .env .secrets 2>/dev/null || { rm -rf "$io"; ERR="config_tar_failed"; return 1; }
  cfgkey="$(prefix "$GEN")/config/$(date -u +%F).tar"
  if ! walg_host -k "$KID" -v "$io:/io" -- --root st put --no-compress /io/config.tar "$cfgkey" >/dev/null 2>&1; then
    rm -rf "$io"; ERR="config_upload_failed"; return 1
  fi
  rm -rf "$io"
  if [ "${EVMODE:-1}" = 1 ]; then
    ev_str backup_name "$(jq -r '.backup_name' <<<"$last")"; ev_str backup_time "$(jq -r '.time' <<<"$last")"
    ev_num compressed_size "$size"; ev_str prefix "$(prefix "$GEN")/wal-g"; ev_str config_object "$cfgkey"
  fi
  return 0
}

# do_retention <slug> — срок хранения 30 суток В ПРЕФИКСЕ СВОЕГО ПОКОЛЕНИЯ (ADR-115 §8.4).
do_retention() {
  local s="$1" cutoff cutday obj
  inst_vars "$s"
  cutoff="$(date -u -d "-$RETENTION_DAYS days" +%FT%TZ)"
  walg_in_pg "$s" delete before FIND_FULL "$cutoff" --confirm >/dev/null 2>&1 || { ERR="retention_delete_failed"; return 1; }
  cutday="$(date -u -d "-$RETENTION_DAYS days" +%F)"
  while read -r obj; do
    case "$obj" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].tar) ;; *) continue;; esac
    [[ "${obj%.tar}" < "$cutday" ]] || continue
    # `st rm` удаляет ПО ПРЕФИКСУ — ключ передаётся полным именем объекта, не каталогом.
    walg_host -- --root st rm "$(prefix "$GEN")/config/$obj" >/dev/null 2>&1 || { ERR="config_retention_failed"; return 1; }
  done < <(walg_host -- --root st ls "$(prefix "$GEN")/config/" 2>/dev/null | awk 'NR>1 && $1=="obj" {print $NF}')
  return 0
}

case "$SUB" in
full)
  require_root; SLUG="${1:-}"; check_slug "$SLUG"
  lock_instance "$SLUG"
  [ -f "$(inst_dir "$SLUG")/.env" ] || fail no_instance
  do_full "$SLUG" || fail "$ERR"
  ok
  ;;

final)
  require_root; SLUG="${1:-}"; check_slug "$SLUG"
  lock_instance "$SLUG"
  inst_vars "$SLUG"
  [ -f "$D/.env" ] || fail no_instance
  require_fence_ok "$SLUG"
  ctr_running "$PGC" || fail postgres_not_running
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 "$D/.final"
  tmp_track "$D/.final/$ts.dump"; tmp_track "$D/.final/$ts.check"
  u="$(env_get "$D/.env" POSTGRES_USER)"; db="$(env_get "$D/.env" POSTGRES_DB)"
  docker exec "$PGC" pg_dump -Fc -U "$u" -d "$db" > "$D/.final/$ts.dump" 2>/dev/null || fail pg_dump_failed
  size="$(stat -c %s "$D/.final/$ts.dump")"; [ "$size" -gt 0 ] || fail dump_empty
  sum_local="$(sha256sum "$D/.final/$ts.dump" | cut -d' ' -f1)"
  key="instances/$UIDV/final/$ts.dump"
  walg_host -k "$KID" -v "$D/.final:/io" -- --root st put --no-compress "/io/$ts.dump" "$key" >/dev/null 2>&1 || fail final_upload_failed
  # Сверка выгрузки: объект скачивается обратно, расшифровывается и сравнивается побайтно.
  rm -f "$D/.final/$ts.check"
  walg_host -k "$KID" -v "$D/.final:/io" -- --root st get --no-decompress "$key" "/io/$ts.check" >/dev/null 2>&1 || fail final_download_failed
  sum_remote="$(sha256sum "$D/.final/$ts.check" | cut -d' ' -f1)"; rm -f "$D/.final/$ts.check"
  [ "$sum_local" = "$sum_remote" ] || fail final_checksum_mismatch
  psql_inst "$SLUG" "SELECT pg_switch_wal()" >/dev/null 2>&1
  walg_in_pg "$SLUG" backup-push /var/lib/postgresql/data >/dev/null 2>&1 || fail final_backup_push_failed
  printf '%s %s %s\n' "$key" "$sum_local" "$ts" > "$D/.final-verified"; chmod 0600 "$D/.final-verified"
  # Незашифрованная копия дампа после сверки не нужна: единственная копия — в бакете, зашифрована.
  rm -f "$D/.final/$ts.dump"; rmdir "$D/.final" 2>/dev/null
  ev_str object "$key"; ev_num size "$size"; ev_str sha256 "$sum_local"; ev_bool checksum_match true
  ok
  ;;

heartbeat)
  require_root
  n=0; written=0; fenced=0; skipped=0; errs=()
  while read -r s; do
    n=$((n+1)); inst_vars "$s"
    fence_state "$s"; st="$FENCE"
    case "$st" in
      other_server|mismatch) fence_enforce "$s" "$st"; fenced=$((fenced+1)); continue;;
      unavailable|disabled) skipped=$((skipped+1)); errs+=("$s:$st"); continue;;
    esac
    # ok или transition: heartbeat пишется в префикс ДЕЙСТВУЮЩЕГО поколения из meta.json.
    mgen="$(jq -r '.current_generation|tostring' <<<"$META")"
    row="null"
    if ctr_running "$PGC"; then
      row="$(psql_inst "$s" "SELECT json_build_object('archived_count', archived_count, 'last_archived_wal', last_archived_wal,
        'last_archived_time', last_archived_time, 'failed_count', failed_count, 'last_failed_time', last_failed_time,
        'pg_wal_bytes', (SELECT COALESCE(sum(size),0) FROM pg_ls_waldir())) FROM pg_stat_archiver" 2>/dev/null)"
      [ -n "$row" ] || row="null"
    fi
    io="$(mktemp -d /var/tmp/fleet-hb.XXXXXX)"; tmp_track "$io"
    jq -nc --arg ts "$(date -u +%FT%TZ)" --arg state "$st" --argjson a "$row" \
      --argjson run "$(ctr_running "$PGC" && echo true || echo false)" \
      '{ts:$ts, state:$state, postgres_running:$run} + (if $a == null then {} else
        {last_archived_wal:$a.last_archived_wal, last_archived_time:$a.last_archived_time,
         failed_count:$a.failed_count, last_failed_time:$a.last_failed_time, pg_wal_bytes:$a.pg_wal_bytes} end)' > "$io/heartbeat.json"
    if walg_host -v "$io:/io" -- --root st put --no-compress --no-encrypt /io/heartbeat.json \
         "instances/$UIDV/g$mgen/status/heartbeat.json" >/dev/null 2>&1; then
      written=$((written+1))
    else
      errs+=("$s:upload_failed")
    fi
    rm -rf "$io"
  done < <(list_archive_enabled)
  ev_num instances "$n"; ev_num heartbeats_written "$written"; ev_num fenced "$fenced"; ev_num skipped "$skipped"
  if [ "${#errs[@]}" -gt 0 ]; then
    ev_json errors "$(printf '%s\n' "${errs[@]}" | jq -R . | jq -sc .)"
  fi
  [ "$written" -eq $((n - fenced - skipped)) ] || fail heartbeat_partial
  ok
  ;;

restore-check)
  require_root
  mkdir -p "$LOCK_DIR"
  exec {RCFD}>"$LOCK_DIR/restore-check.lock"
  flock -n "$RCFD" || fail already_running "на сервере уже идёт проверка восстановления (не больше одной, ADR-115 §8.8)"
  if [ -n "${1:-}" ]; then check_slug "$1"; targets=("$1"); else mapfile -t targets < <(list_archive_enabled); fi
  IMG="$(walg_image)"; results=(); bad=0; NAME=""
  # Одноразовые контейнер, том и сеть с именем slug + отметка времени; удаляются в любом исходе.
  cleanup() { [ -n "$NAME" ] || return 0; docker rm -f "$NAME" >/dev/null 2>&1; docker volume rm "$NAME" >/dev/null 2>&1; docker network rm "$NAME" >/dev/null 2>&1; }
  on_exit cleanup
  for s in "${targets[@]}"; do
    inst_vars "$s"
    fence_state "$s"; st="$FENCE"; [ "$st" = ok ] || { results+=("$s:fence:$st"); bad=$((bad+1)); continue; }
    u="$(env_get "$D/.env" POSTGRES_USER)"; db="$(env_get "$D/.env" POSTGRES_DB)"
    NAME="fleet-rc-$s-$(date -u +%Y%m%d%H%M%S)"; t0="$(date +%s)"
    docker volume create "$NAME" >/dev/null && docker network create "$NAME" >/dev/null || { results+=("$s:setup_failed"); bad=$((bad+1)); cleanup; continue; }
    # Сеть — отдельный мост без публикации портов; не --internal: backup-fetch и restore_command
    # ходят в хранилище по HTTPS (ADR-115 §8.7). archive_mode=off — проверочная база ничего не пишет.
    docker run -d --name "$NAME" --network "$NAME" --cpus 1 -u postgres \
      -v "$NAME:/var/lib/postgresql/data" -v "$WALG_DIR:/etc/fleet/walg:ro" \
      -e FLEET_INSTANCE_UID="$UIDV" -e FLEET_INSTANCE_GENERATION="$GEN" -e FLEET_WALG_KEY_ID="$KID" \
      --entrypoint bash "$IMG" -c 'fleet-walg backup-fetch /var/lib/postgresql/data LATEST && chmod 0700 /var/lib/postgresql/data && touch /var/lib/postgresql/data/recovery.signal && exec postgres -c archive_mode=off -c "restore_command=fleet-walg wal-fetch %f %p" -c recovery_target_action=promote' \
      >/dev/null 2>&1 || { results+=("$s:start_failed"); bad=$((bad+1)); cleanup; continue; }
    done_=0; err=""
    for _ in $(seq 1 3600); do
      ctr_running "$NAME" || { err="container_exited"; break; }
      [ "$(docker exec "$NAME" psql -X -U "$u" -d "$db" -tAc 'SELECT pg_is_in_recovery()' 2>/dev/null)" = "f" ] && { done_=1; break; }
      sleep 2
    done
    [ "$done_" = 1 ] || err="${err:-recovery_timeout}"
    users_r=""; users_l=""; ledger=""; bname=""; restored_to=""
    if [ "$done_" = 1 ]; then
      users_r="$(docker exec "$NAME" psql -X -U "$u" -d "$db" -tAc 'SELECT count(*) FROM users' 2>/dev/null)"
      ledger="$(docker exec "$NAME" psql -X -U "$u" -d "$db" -tAc 'SELECT max(created_at) FROM ledger_transactions' 2>/dev/null)" || err="ledger_query_failed"
      users_l="$(psql_inst "$s" 'SELECT count(*) FROM users' 2>/dev/null)"
      bname="$(docker logs "$NAME" 2>&1 | sed -n 's/.*backup-fetch.*\(base_[0-9A-F_D]*\).*/\1/p' | tail -1)"
      restored_to="$(docker logs "$NAME" 2>&1 | sed -n 's/.*last completed transaction was at log time \(.*\)$/\1/p' | tail -1)"
      if ! [[ "$users_r" =~ ^[0-9]+$ && "$users_l" =~ ^[0-9]+$ ]]; then err="${err:-users_query_failed}"
      elif [ "$users_l" -gt 0 ] && [ "$users_r" -eq 0 ]; then err="restored_empty"
      elif [ "$users_r" -gt "$users_l" ]; then err="restored_more_than_live"
      fi
    fi
    okc=false; [ -z "$err" ] && okc=true
    io="$(mktemp -d /var/tmp/fleet-rc.XXXXXX)"; tmp_track "$io"
    jq -nc --arg ts "$(date -u +%FT%TZ)" --argjson ok "$okc" --arg b "$bname" --arg rt "$restored_to" \
      --arg ur "$users_r" --arg ul "$users_l" --arg lg "$ledger" --argjson d "$(( $(date +%s) - t0 ))" --arg e "$err" \
      '{ts:$ts, ok:$ok, backup_name:$b, restored_to:$rt, users_restored:($ur|tonumber? // null),
        users_live:($ul|tonumber? // null), ledger_max_created_at:$lg, duration_s:$d} + (if $e == "" then {} else {error:$e} end)' \
      > "$io/restore-check.json"
    walg_host -v "$io:/io" -- --root st put --no-compress --no-encrypt /io/restore-check.json \
      "instances/$UIDV/g$GEN/status/restore-check.json" >/dev/null 2>&1 || err="${err:-status_upload_failed}"
    rm -rf "$io"; cleanup
    if [ -z "$err" ]; then results+=("$s:ok"); else results+=("$s:$err"); bad=$((bad+1)); fi
  done
  ev_json results "$(printf '%s\n' "${results[@]:-}" | jq -R 'select(length>0)' | jq -sc .)"
  ev_num checked "${#targets[@]}"; ev_num failed "$bad"
  [ "$bad" -eq 0 ] || fail restore_check_failed
  ok
  ;;

nightly)
  require_root
  results=(); bad=0; EVMODE=0
  while read -r s; do
    # Последовательно, по одному, под nice (ADR-115 §8.3): одновременный старт тяжёлых операций
    # всех инстансов сервера — CPU-голодание, которым машина уже падала.
    ( lock_instance "$s"; do_full "$s" && do_retention "$s"; rc=$?; [ "$rc" = 0 ] || echo "$ERR" >&2; exit "$rc" ) >/dev/null 2>"/var/tmp/fleet-nightly-$s.err"
    if [ "$?" = 0 ]; then results+=("$s:ok"); else results+=("$s:$(tail -1 "/var/tmp/fleet-nightly-$s.err")"); bad=$((bad+1)); fi
    rm -f "/var/tmp/fleet-nightly-$s.err"
  done < <(list_archive_enabled)
  ev_json results "$(printf '%s\n' "${results[@]:-}" | jq -R 'select(length>0)' | jq -sc .)"
  ev_num failed "$bad"
  [ "$bad" -eq 0 ] || fail nightly_partial
  ok
  ;;

*) unknown_subcommand "backup.$SUB";;
esac
