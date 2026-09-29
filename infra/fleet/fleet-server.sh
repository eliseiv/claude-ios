#!/bin/bash
# fleet-server — операции над прикладным сервером флота (ADR-115 §2, §2.1, §8.6, §8.7, §13).
#
#   fleet-server version
#   fleet-server facts
#   fleet-server bootstrap                  (stdin JSON, см. ниже)
#   fleet-server tunnel-check [wg_ip_R]
#   fleet-server objstore-set               (stdin: {"objstore":{…},"server_id"?})
#   fleet-server ghcr-login                 (stdin: {"ghcr":{"username","token"},"last_deployed_sha"})
#   fleet-server authorized-keys-add        (stdin: {"public_key"})
#   fleet-server authorized-keys-remove     (stdin: {"new_fingerprint","old_fingerprint"})
#   fleet-server inventory-scan           по каждому /opt/<dir> с .env: dir, compose_project_name,
#                                           service_domain, api_host_port, role, has_instance_uid,
#                                           api_running (docker inspect .State.Running контейнера api)
#   fleet-server walg-key-add               (stdin: {"key_id","key_hex"})
#   fleet-server walg-key-remove <key_id>
#   fleet-server heartbeat                  (таймер fleet-server-heartbeat, ADR-115 §8.6)
#   fleet-server install-timers             (stdin: {"backup_offset_min"}) — таймеры §8 на действующих
#                                           A/B без bootstrap (§11 фазы 1–5; вне перечня §13)
#
# stdin bootstrap (одна JSON-строка; секреты — только здесь, никогда в argv):
#   {"server_id":"…","wg_ip":"10.10.0.N",
#    "router":{"public_key":"…","endpoint":"<адрес R>:51820","wg_ip":"10.10.0.3"},
#    "ghcr":{"username":"…","token":"…"},
#    "objstore":{"access_key":"…","secret_key":"…","endpoint":"https://hel1.your-objectstorage.com",
#                "region":"hel1","bucket":"…"},
#    "backup_key":{"key_id":"<12 hex>","key_hex":"<64 hex>"},
#    "ci_public_key":"ssh-ed25519 …","backup_offset_min":0,"last_deployed_sha":"<40 hex>"}
# key_id = первые 12 hex SHA-256 от 32 СЫРЫХ байт ключа; инструмент пересчитывает и сверяет.
set -uo pipefail
# shellcheck source=lib/common.sh
. "$(dirname "$(readlink -f "$0")")/lib/common.sh"

SUB="${1:-}"; shift || true
# shellcheck disable=SC2034  # STEP читает lib/common.sh (emit)
STEP="server.$SUB"

# --- общие части -----------------------------------------------------------------------------
postgres_gid() { tr -dc '0-9' < "$WALG_DIR/.gid" 2>/dev/null; }
# ensure_postgres_gid — печатает GID postgres образа wal-g. На A/B без bootstrap (импорт) .gid нет:
# признак A/B — /etc/fleet/server_id (на R его не пишет ни один инструмент); GID берётся из образа,
# как в bootstrap шаг 6. На R печатает пусто. Код 1 — GID на A/B не вычислен.
ensure_postgres_gid() {
  local g img
  g="$(postgres_gid)"; [ -n "$g" ] && { printf '%s' "$g"; return 0; }
  [ -n "$(server_id)" ] || return 0
  img="$(walg_image)"; [ -n "$img" ] || return 1
  docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null 2>&1 || return 1
  g="$(docker run --rm --entrypoint id "$img" -g postgres 2>/dev/null | tr -dc '0-9')"; [ -n "$g" ] || return 1
  install -d -m 0750 -o root -g "$g" "$WALG_DIR" || return 1
  { printf '%s\n' "$g" > "$WALG_DIR/.gid" && chmod 0644 "$WALG_DIR/.gid"; } || return 1
  printf '%s' "$g"
}

key_id_of_hex() {  # SHA-256 от сырых байт → первые 12 hex. Ключ не попадает в argv: printf — встроенная
  local esc; esc="$(sed 's/../\\x&/g' <<<"$1")"
  printf '%b' "$esc" | sha256sum | cut -c1-12
}

# write_objstore <каталог-назначения-временный-суффикс> — пишет файлы кредов ВО ВРЕМЕННЫЕ имена в
# тех же каталогах с итоговыми владельцем и правами; подмена — отдельно (commit_objstore).
# Входы — переменные OS_ACCESS OS_SECRET OS_ENDPOINT OS_REGION OS_BUCKET (не печатаются).
OBJ_TMP=""; WALG_TMP=""; BUCKET_TMP=""
write_objstore() {
  local gid; gid="$(postgres_gid)"
  install -d -m 0700 -o root -g root "$OBJSTORE_DIR"
  OBJ_TMP="$(mktemp "$OBJSTORE_DIR/.objstore.env.XXXXXX")" || return 1
  tmp_track "$OBJ_TMP"
  chmod 0600 "$OBJ_TMP"
  {
    printf 'AWS_ACCESS_KEY_ID=%s\n' "$OS_ACCESS"
    printf 'AWS_SECRET_ACCESS_KEY=%s\n' "$OS_SECRET"
    printf 'AWS_ENDPOINT=%s\n' "$OS_ENDPOINT"
    printf 'AWS_REGION=%s\n' "$OS_REGION"
    printf 'AWS_S3_FORCE_PATH_STYLE=true\n'
    printf 'FLEET_BUCKET=%s\n' "$OS_BUCKET"
  } > "$OBJ_TMP"
  if [ -n "$gid" ] && [ -d "$WALG_DIR" ]; then
    WALG_TMP="$(mktemp "$WALG_DIR/.walg.json.XXXXXX")" || return 1
    BUCKET_TMP="$(mktemp "$WALG_DIR/.bucket.XXXXXX")" || return 1
    tmp_track "$WALG_TMP"; tmp_track "$BUCKET_TMP"
    jq -n --arg a "$OS_ACCESS" --arg s "$OS_SECRET" --arg e "$OS_ENDPOINT" --arg r "$OS_REGION" \
      '{AWS_ACCESS_KEY_ID:$a, AWS_SECRET_ACCESS_KEY:$s, AWS_ENDPOINT:$e, AWS_REGION:$r, AWS_S3_FORCE_PATH_STYLE:"true"}' > "$WALG_TMP"
    printf '%s\n' "$OS_BUCKET" > "$BUCKET_TMP"
    chown "root:$gid" "$WALG_TMP" "$BUCKET_TMP"; chmod 0640 "$WALG_TMP" "$BUCKET_TMP"
  fi
}
commit_objstore() {
  mv -f "$OBJ_TMP" "$OBJSTORE_DIR/objstore.env"
  [ -n "$WALG_TMP" ] && mv -f "$WALG_TMP" "$WALG_DIR/walg.json"
  [ -n "$BUCKET_TMP" ] && mv -f "$BUCKET_TMP" "$WALG_DIR/bucket"
  return 0
}
drop_objstore_tmp() { rm -f "$OBJ_TMP" "$WALG_TMP" "$BUCKET_TMP"; }

# probe_bucket <env-file> [key_id] — запись, чтение и удаление тестового объекта. Печатает ok|fail.
probe_bucket() {
  local envf="$1" kid="${2:-}" io key img flags=() getflags=() rc=1
  img="$(walg_image)"; [ -n "$img" ] || { echo fail; return 1; }
  io="$(mktemp -d /var/tmp/fleet-probe.XXXXXX)" || { echo fail; return 1; }
  tmp_track "$io"
  head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$io/probe"
  # `st rm` удаляет ПО ПРЕФИКСУ: суффикс .probe не даёт ключу «…-12» задеть «…-123.probe».
  key="fleet-probe/$(server_id || echo unknown)/$(date -u +%Y%m%dT%H%M%SZ)-$$.probe"
  if [ -n "$kid" ]; then flags=(--no-compress); getflags=(--no-decompress); else flags=(--no-compress --no-encrypt); getflags=(--no-decompress --no-decrypt); fi
  local envs=(); [ -n "$kid" ] && envs=(-e "FLEET_WALG_KEY_ID=$kid")
  local mounts=(-v "$io:/io"); [ -d "$WALG_DIR" ] && mounts+=(-v "$WALG_DIR:/etc/fleet/walg:ro")
  if docker run --rm --network host --env-file "$envf" "${envs[@]}" "${mounts[@]}" --entrypoint fleet-walg "$img" \
       --root st put "${flags[@]}" /io/probe "$key" >/dev/null 2>&1 \
     && docker run --rm --network host --env-file "$envf" "${envs[@]}" "${mounts[@]}" --entrypoint fleet-walg "$img" \
       --root st get "${getflags[@]}" "$key" /io/back >/dev/null 2>&1 \
     && cmp -s "$io/probe" "$io/back"; then rc=0; fi
  docker run --rm --network host --env-file "$envf" "${mounts[@]}" --entrypoint fleet-walg "$img" \
    --root st rm "$key" >/dev/null 2>&1 || rc=1
  rm -rf "$io"
  [ "$rc" = 0 ] && echo ok || echo fail
  return "$rc"
}

# check_in_containers <команда-проверки> — на каждом инстансе с включённым архивом проверка
# ИЗНУТРИ контейнера postgres от пользователя postgres: ровно тот путь, которым идёт archive_command.
CHECKED=0; CHECK_FAILED=()
check_in_containers() {
  local s c
  CHECKED=0; CHECK_FAILED=()
  while read -r s; do
    [ -n "$s" ] || continue
    c="$(ctr "$s" postgres)"
    ctr_running "$c" || continue
    CHECKED=$((CHECKED+1))
    docker exec -u postgres "$c" bash -c "$1" >/dev/null 2>&1 || CHECK_FAILED+=("$s")
  done < <(list_archive_enabled)
}

install_wrapper() {  # /usr/local/bin/fleet + ссылки групп — копия из действующей версии
  local t; t="$(mktemp /usr/local/bin/.fleet.XXXXXX)" || return 1
  cp "$FLEET_VERSION_DIR/fleet.sh" "$t" && chmod 0755 "$t" && mv -f "$t" /usr/local/bin/fleet || return 1
  local g; for g in server instance backup route; do ln -sfn fleet "/usr/local/bin/fleet-$g"; done
}

install_timers() {  # $1 — backup_offset_min
  local off="$1" u h m
  for u in "$FLEET_VERSION_DIR"/systemd/fleet-*.service "$FLEET_VERSION_DIR"/systemd/fleet-*.timer; do
    case "$(basename "$u")" in fleet-router-*) continue;; esac
    install -m 0644 "$u" /etc/systemd/system/
  done
  # Сдвиг ночного окна (ADR-115 §2.1 шаг 2, §8.3): 01:00 UTC + offset, проверка — вс 04:00 + offset.
  h=$(( (60 + off) / 60 )); m=$(( (60 + off) % 60 ))
  install -d -m 0755 /etc/systemd/system/fleet-backup.timer.d /etc/systemd/system/fleet-restore-check.timer.d
  printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* %02d:%02d:00 UTC\n' "$h" "$m" > /etc/systemd/system/fleet-backup.timer.d/offset.conf
  h=$(( (240 + off) / 60 )); m=$(( (240 + off) % 60 ))
  printf '[Timer]\nOnCalendar=\nOnCalendar=Sun *-*-* %02d:%02d:00 UTC\n' "$h" "$m" > /etc/systemd/system/fleet-restore-check.timer.d/offset.conf
  systemctl daemon-reload
  systemctl enable --now fleet-backup.timer fleet-backup-heartbeat.timer fleet-server-heartbeat.timer fleet-restore-check.timer >/dev/null 2>&1
}

# --- подкоманды ------------------------------------------------------------------------------
case "$SUB" in
version)
  ev_str version "$TOOLS_VERSION"
  [ "$TOOLS_VERSION" != "unknown" ] || fail version_unreadable
  ok
  ;;

facts)
  n="$(nproc 2>/dev/null)"; mem="$(awk '/^MemTotal:/{printf "%.0f", $2 * 1024}' /proc/meminfo 2>/dev/null)"
  read -r dtot dfree < <(df -B1 --output=size,avail /opt 2>/dev/null | tail -1)
  [ -n "$n" ] && [ -n "$mem" ] && [ -n "${dtot:-}" ] || fail facts_unavailable
  ev_num nproc "$n"; ev_num mem_bytes "$mem"; ev_num disk_bytes "$dtot"; ev_num disk_free_bytes "$dfree"
  ok
  ;;

bootstrap)
  require_root
  RAW="$(cat)"                        # jq ещё может отсутствовать — сначала читаем, потом ставим
  [ -n "$RAW" ] || fail stdin_empty
  # bootstrap переписывает wg0.conf (один пир — R) и управляемую строку authorized_keys. На
  # ДЕЙСТВУЮЩИХ A/B это оборвало бы туннель репликации до ADR-115 §11 фазы 8. Поэтому отказ ДО
  # любых изменений на хосте, если: (а) wg0.conf есть и написан НЕ этим инструментом (нет
  # заголовка управления); (б) на сервере есть инстанс с .env без INSTANCE_UID — признак
  # прежней схемы (новый сервер получает инстансы только через create, который пишет UID).
  # Для A/B — fleet-server install-timers / objstore-set / walg-key-add, не bootstrap.
  if [ -f /etc/wireguard/wg0.conf ] && [ "$(head -1 /etc/wireguard/wg0.conf)" != "# Управляется fleet-server bootstrap (ADR-115 §2). Пир — только маршрутизатор R." ]; then
    fail foreign_wg_config "/etc/wireguard/wg0.conf не управляется fleet-server — это не новый сервер (A/B?)"
  fi
  legacy=()
  for d in /opt/*/; do
    n="$(basename "$d")"
    [ -f "/opt/$n/.env" ] && ! grep -q '^INSTANCE_UID=.' "/opt/$n/.env" && legacy+=("$n")
  done
  if [ "${#legacy[@]}" -gt 0 ]; then
    ev_str legacy_instances "${legacy[*]}"
    fail legacy_instances_present "на сервере есть инстансы прежней схемы (.env без INSTANCE_UID) — bootstrap запрещён"
  fi
  export DEBIAN_FRONTEND=noninteractive
  log "1. пакеты и Docker"
  if command -v apt-get >/dev/null; then
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq ca-certificates curl gnupg wireguard-tools jq util-linux openssl iputils-ping >/dev/null 2>&1
    if ! command -v docker >/dev/null; then
      install -m 0755 -d /etc/apt/keyrings
      . /etc/os-release
      curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
      apt-get update -qq >/dev/null 2>&1
      apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null 2>&1
    fi
  else
    dnf -y -q install dnf-plugins-core wireguard-tools jq util-linux openssl iputils >/dev/null 2>&1
    if ! command -v docker >/dev/null; then
      dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1 \
        || dnf config-manager addrepo --from-repofile=https://download.docker.com/linux/centos/docker-ce.repo >/dev/null 2>&1
      dnf -y -q install docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null 2>&1
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1
  need jq docker wg flock openssl sha256sum
  STDIN_JSON="$RAW"; unset RAW
  jq -e 'type == "object"' >/dev/null 2>&1 <<<"$STDIN_JSON" || fail stdin_not_json

  jset SID .server_id; jset WGIP .wg_ip; jset R_PUB .router.public_key; jset R_EP .router.endpoint
  jset R_WG .router.wg_ip opt; R_WG="${R_WG:-$ROUTER_WG_IP}"
  jset GH_USER .ghcr.username; jset GH_TOKEN .ghcr.token
  jset OS_ACCESS .objstore.access_key; jset OS_SECRET .objstore.secret_key
  jset OS_ENDPOINT .objstore.endpoint; jset OS_REGION .objstore.region; jset OS_BUCKET .objstore.bucket
  jset KID .backup_key.key_id; jset KHEX .backup_key.key_hex
  jset CI_KEY .ci_public_key; jset OFF .backup_offset_min; jset SHA .last_deployed_sha
  [[ "$SID" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || fail bad_server_id
  [[ "$R_PUB" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || fail bad_router_public_key
  [[ "$R_EP" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] || fail bad_router_endpoint "endpoint R — <host>:<port>"
  [[ "$R_WG" =~ ^10\.10\.0\.[0-9]{1,3}$ ]] || fail bad_router_wg_ip
  for p in .ghcr.username .ghcr.token .objstore.access_key .objstore.secret_key .objstore.endpoint .objstore.region; do
    json_has_ctl "$p" && fail "bad_value:${p#.}" "значение содержит перевод строки или нулевой байт"
  done
  valid_pubkey_line "$CI_KEY" || fail bad_ci_key "ci_public_key — ровно одна строка ключа известного типа"
  [[ "$WGIP" =~ ^10\.10\.0\.([0-9]{1,3})$ ]] || fail bad_wg_ip "wg_ip вне 10.10.0.0/24"
  n="${BASH_REMATCH[1]}"; { [ "$n" -ge 1 ] && [ "$n" -le 254 ] && [ "$n" != 3 ]; } || fail bad_wg_ip "N ∉ 1..254 или N = 3 (занят R)"
  [[ "$OFF" =~ ^[0-9]+$ ]] && [ "$OFF" -le 179 ] || fail bad_backup_offset "backup_offset_min — целое 0–179"
  check_keyid "$KID"; check_sha "$SHA"
  [[ "$KHEX" =~ ^[0-9a-f]{64}$ ]] || fail bad_backup_key "ключ — 64 hex (32 байта)"
  [ "$(key_id_of_hex "$KHEX")" = "$KID" ] || fail key_id_mismatch "key_id не равен первым 12 hex SHA-256 ключа"
  [[ "$OS_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail bad_bucket
  case "$OS_ENDPOINT" in https://*) ;; *) fail bad_endpoint "эндпоинт хранилища — только https://";; esac

  log "2. идентичность сервера"
  install -d -m 0755 "$FLEET_ETC"
  if [ -f "$FLEET_ETC/server_id" ] && [ "$(server_id)" != "$SID" ]; then
    fail server_id_conflict "на сервере уже записан другой server_id — повторный bootstrap чужого сервера запрещён"
  fi
  printf '%s\n' "$SID" > "$FLEET_ETC/server_id"; chmod 0644 "$FLEET_ETC/server_id"

  log "3. swap и сеть web"
  if ! swapon --show 2>/dev/null | grep -q .; then
    fallocate -l 8G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
  mkdir -p /etc/sysctl.d && echo 'vm.swappiness=10' > /etc/sysctl.d/99-fleet.conf && sysctl -qp /etc/sysctl.d/99-fleet.conf >/dev/null 2>&1
  docker network inspect web >/dev/null 2>&1 || docker network create web >/dev/null || fail docker_network_failed

  log "4. WireGuard (один пир — R; закрытый ключ сервер не покидает)"
  install -d -m 0700 /etc/wireguard
  if [ -f /etc/wireguard/wg0.conf ]; then
    others="$(awk -v k="$R_PUB" '$1 == "PublicKey" && $3 != k {print $3}' /etc/wireguard/wg0.conf)"
    [ -z "$others" ] || fail foreign_wg_config "в wg0.conf есть пир, отличный от R — не перезаписываю"
  fi
  [ -s /etc/wireguard/privatekey ] || { (umask 077; wg genkey > /etc/wireguard/privatekey) || fail wg_genkey_failed; }
  WG_PUB="$(wg pubkey < /etc/wireguard/privatekey)" || fail wg_pubkey_failed
  t="$(mktemp /etc/wireguard/.wg0.XXXXXX)"
  cat > "$t" <<EOF
# Управляется fleet-server bootstrap (ADR-115 §2). Пир — только маршрутизатор R.
[Interface]
Address = $WGIP/24
ListenPort = 51820
PostUp = wg set %i private-key /etc/wireguard/privatekey

[Peer]
PublicKey = $R_PUB
AllowedIPs = $R_WG/32
Endpoint = $R_EP
PersistentKeepalive = 25
EOF
  chmod 0600 "$t"; mv -f "$t" /etc/wireguard/wg0.conf
  if ip link show wg0 >/dev/null 2>&1; then
    wg syncconf wg0 <(wg-quick strip wg0) || fail wg_sync_failed
  else
    systemctl enable --now wg-quick@wg0 >/dev/null 2>&1 || fail wg_up_failed
  fi

  log "5. GHCR и образы"
  printf '%s' "$GH_TOKEN" | docker login ghcr.io -u "$GH_USER" --password-stdin >/dev/null 2>&1 || fail ghcr_login_failed
  unset GH_TOKEN
  WIMG="$(walg_image)"; [ -n "$WIMG" ] || fail walg_image_unknown "в бандле нет docker-compose.walg.yml"
  docker pull -q "$WIMG" >/dev/null 2>&1 || fail walg_image_pull_failed
  docker pull -q "$(app_image "$SHA")" >/dev/null 2>&1 || fail app_image_pull_failed "docker pull образа last_deployed_sha не прошёл"
  GID="$(docker run --rm --entrypoint id "$WIMG" -g postgres 2>/dev/null | tr -dc '0-9')"
  [ -n "$GID" ] || fail postgres_gid_unknown

  log "6. каталоги кредов и ключ шифрования (ADR-115 §8.2, §8.7)"
  install -d -m 0750 -o root -g "$GID" "$WALG_DIR" "$WALG_DIR/keys"
  printf '%s\n' "$GID" > "$WALG_DIR/.gid"; chmod 0644 "$WALG_DIR/.gid"
  install -m 0640 -o root -g "$GID" "$FLEET_ETC/server_id" "$WALG_DIR/server_id"
  kt="$(mktemp "$WALG_DIR/keys/.k.XXXXXX")"; tmp_track "$kt"; printf '%s' "$KHEX" > "$kt"; unset KHEX
  chown "root:$GID" "$kt"; chmod 0640 "$kt"; mv -f "$kt" "$WALG_DIR/keys/$KID"
  write_objstore || fail objstore_write_failed
  # Проверка НОВЫМИ кредами ДО подмены файлов (тот же порядок, что objstore-set, ADR-115 §8.7).
  PROBE="$(probe_bucket "$OBJ_TMP" "$KID")"
  [ "$PROBE" = ok ] || fail bucket_probe_failed "тестовый объект не записан/прочитан/удалён новыми кредами — файлы кредов не подменены"
  commit_objstore

  log "7. ключ CI (управляемая строка authorized_keys)"
  # Пометка управления — КОММЕНТАРИЙ САМОЙ строки ключа («<тип> <ключ> fleet:ci-key»), а не
  # отдельная строка перед ней: отдельный маркер, переживший свою строку (authorized-keys-remove),
  # указывал бы на ЧУЖОЙ ключ ниже. Заменяется ровно строка с этой пометкой; одиночные строки-маркеры
  # прежнего вида удаляются сами по себе, соседние строки не трогаются.
  install -d -m 0700 /root/.ssh; touch /root/.ssh/authorized_keys; chmod 0600 /root/.ssh/authorized_keys
  t="$(mktemp /root/.ssh/.ak.XXXXXX)"; tmp_track "$t"
  awk '$0 == "# fleet:ci-key" { next } $NF == "fleet:ci-key" && $1 !~ /^#/ { next } { print }' /root/.ssh/authorized_keys > "$t"
  set -- $CI_KEY
  printf '%s %s fleet:ci-key\n' "$1" "$2" >> "$t"
  chmod 0600 "$t"; mv -f "$t" /root/.ssh/authorized_keys

  log "8. обёртка и таймеры"
  install_wrapper || fail wrapper_install_failed
  install -d -m 0755 "$STATE_DIR" "$LOCK_DIR"
  install_timers "$OFF"
  for tmr in fleet-backup.timer fleet-backup-heartbeat.timer fleet-server-heartbeat.timer fleet-restore-check.timer; do
    systemctl is-enabled "$tmr" >/dev/null 2>&1 || fail "timer_not_enabled:$tmr"
  done

  ev_str wg_public_key "$WG_PUB"
  ev_num postgres_gid "$GID"
  ev_str docker_version "$(docker version --format '{{.Server.Version}}' 2>/dev/null)"
  ev_str pulled_image "$(app_image "$SHA")"
  ev_bool bucket_probe true
  ev_str server_id "$SID"
  ok
  ;;

tunnel-check)
  peer_ip="${1:-$ROUTER_WG_IP}"
  [[ "$peer_ip" =~ $IPV4_RE ]] || fail bad_wg_ip
  need wg ping
  now="$(date +%s)"
  hs="$(wg show wg0 dump 2>/dev/null | awk -v ip="$peer_ip/32" 'NR>1 && index($4, ip) {print $5; exit}')"
  [ -n "$hs" ] || fail peer_not_found "пира с allowed-ips $peer_ip/32 нет в wg0"
  age=$(( hs > 0 ? now - hs : 999999 ))
  pout="$(ping -c 3 -W 2 "$peer_ip" 2>/dev/null)"
  loss="$(sed -n 's/.* \([0-9.]*\)% packet loss.*/\1/p' <<<"$pout")"
  rtt="$(sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p' <<<"$pout")"
  ev_num handshake_age_s "$age"; ev_num loss_pct "${loss:-100}"; ev_num rtt_ms "${rtt:-0}"
  [ "$age" -le 120 ] || fail handshake_stale "latest-handshake старше 120 с"
  [ "${loss:-100}" = "0" ] || fail ping_loss "ping с потерями"
  ok
  ;;

objstore-set)
  require_root; read_stdin_json
  jset OS_ACCESS .objstore.access_key; jset OS_SECRET .objstore.secret_key
  jset OS_ENDPOINT .objstore.endpoint; jset OS_REGION .objstore.region; jset OS_BUCKET .objstore.bucket
  jset NEW_SID .server_id opt
  for p in .objstore.access_key .objstore.secret_key .objstore.endpoint .objstore.region; do
    json_has_ctl "$p" && fail "bad_value:${p#.}" "значение содержит перевод строки или нулевой байт"
  done
  [[ "$OS_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail bad_bucket
  case "$OS_ENDPOINT" in https://*) ;; *) fail bad_endpoint;; esac
  # server_id на A/B до §11 фазы 1 не записан (его пишет bootstrap новых серверов): без него
  # ограждение не вычислимо. Разрешаем записать его здесь — но только если его ещё нет.
  if [ -n "$NEW_SID" ]; then
    [[ "$NEW_SID" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || fail bad_server_id
    if [ -f "$FLEET_ETC/server_id" ] && [ "$(server_id)" != "$NEW_SID" ]; then fail server_id_conflict; fi
    install -d -m 0755 "$FLEET_ETC"; printf '%s\n' "$NEW_SID" > "$FLEET_ETC/server_id"; chmod 0644 "$FLEET_ETC/server_id"
    gid="$(ensure_postgres_gid)" || fail postgres_gid_unknown "GID postgres из образа wal-g не получен"
    [ -n "$gid" ] && install -m 0640 -o root -g "$gid" "$FLEET_ETC/server_id" "$WALG_DIR/server_id"
  fi
  write_objstore || { drop_objstore_tmp; fail objstore_write_failed; }
  # Проверка НОВЫМИ кредами ДО подмены (ADR-115 §8.7).
  PROBE="$(probe_bucket "$OBJ_TMP")"
  [ "$PROBE" = ok ] || { drop_objstore_tmp; fail bucket_probe_failed "новые креды не прошли запись/чтение/удаление — прежние файлы не тронуты"; }
  commit_objstore
  ev_bool bucket_probe true
  ev_str objstore_env_mode "$(stat -c '%U:%G %a' "$OBJSTORE_DIR/objstore.env")"
  if [ -f "$WALG_DIR/walg.json" ]; then
    ev_str walg_json_mode "$(stat -c '%U:%g %a' "$WALG_DIR/walg.json")"
    check_in_containers 'fleet-walg --root st ls registry/ >/dev/null'
    ev_num containers_checked "$CHECKED"
    if [ "${#CHECK_FAILED[@]}" -gt 0 ]; then
      ev_json containers_failed "$(printf '%s\n' "${CHECK_FAILED[@]}" | jq -R . | jq -sc .)"
      fail container_check_failed "wal-g от postgres изнутри контейнера не прочитал хранилище новыми кредами"
    fi
  fi
  ok
  ;;

ghcr-login)
  require_root; read_stdin_json
  jset GH_USER .ghcr.username; jset GH_TOKEN .ghcr.token; jset SHA .last_deployed_sha
  check_sha "$SHA"
  [[ "$GH_USER" =~ ^[A-Za-z0-9-]{1,39}$ ]] || fail bad_ghcr_username
  printf '%s' "$GH_TOKEN" | docker login ghcr.io -u "$GH_USER" --password-stdin >/dev/null 2>&1 || fail ghcr_login_failed
  unset GH_TOKEN
  docker pull -q "$(app_image "$SHA")" >/dev/null 2>&1 || fail app_image_pull_failed
  ev_str pulled_image "$(app_image "$SHA")"
  ok
  ;;

authorized-keys-add)
  require_root; read_stdin_json
  jset PK .public_key
  json_has_ctl .public_key && fail bad_public_key "ключ содержит перевод строки"
  valid_pubkey_line "$PK" || fail bad_public_key "ровно одна строка ключа известного типа"
  FPR="$(printf '%s\n' "$PK" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')"
  [ -n "$FPR" ] || fail bad_public_key
  install -d -m 0700 /root/.ssh; touch /root/.ssh/authorized_keys; chmod 0600 /root/.ssh/authorized_keys
  present=0
  while read -r line; do
    case "$line" in ""|\#*) continue;; esac
    [ "$(echo "$line" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')" = "$FPR" ] && present=1
  done < /root/.ssh/authorized_keys
  [ "$present" = 1 ] || printf '%s\n' "$PK" >> /root/.ssh/authorized_keys
  install -d -m 0700 "$STATE_DIR/keys"
  mark="$STATE_DIR/keys/$(printf '%s' "$FPR" | sha256sum | cut -c1-16).added"
  [ -f "$mark" ] || date -u +%s > "$mark"
  ev_str fingerprint "$FPR"
  ev_str added_at "$(date -u -d "@$(cat "$mark")" +%FT%TZ)"
  ok
  ;;

authorized-keys-remove)
  require_root; read_stdin_json
  jset NEWF .new_fingerprint; jset OLDF .old_fingerprint
  [ "$NEWF" != "$OLDF" ] || fail same_key "новый и удаляемый отпечатки совпадают"
  mark="$STATE_DIR/keys/$(printf '%s' "$NEWF" | sha256sum | cut -c1-16).added"
  [ -f "$mark" ] || fail new_key_not_added "authorized-keys-add для нового ключа на этом сервере не выполнялся"
  since="$(cat "$mark")"
  # Доказанный вход новым ключом ПОСЛЕ момента add (ADR-115 §8.7): иначе удаление запрещено.
  seen=""
  if command -v journalctl >/dev/null; then
    seen="$(journalctl -q --no-pager --since "@$since" -t sshd -t sshd-session 2>/dev/null | grep -F 'Accepted publickey' | grep -F "$NEWF" | tail -1)"
  fi
  if [ -z "$seen" ]; then
    for f in /var/log/auth.log /var/log/secure; do
      [ -r "$f" ] || continue
      # Файловый журнал не несёт года — берём только строки, записанные после отметки add по mtime файла.
      [ "$(stat -c %Y "$f")" -ge "$since" ] && seen="$(grep -F 'Accepted publickey' "$f" | grep -F "$NEWF" | tail -1)"
      [ -n "$seen" ] && break
    done
  fi
  [ -n "$seen" ] || fail new_key_login_unproven "нет успешного входа новым ключом после add — прежний ключ НЕ удалён"
  t="$(mktemp /root/.ssh/.ak.XXXXXX)"; tmp_track "$t"; removed=0
  while IFS= read -r line; do
    if [ -n "$line" ] && [ "${line#\#}" = "$line" ] && [ "$(echo "$line" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')" = "$OLDF" ]; then
      removed=$((removed+1)); continue
    fi
    [ "$line" = "# fleet:ci-key" ] && continue   # маркер прежнего вида не переживает свою строку
    printf '%s\n' "$line"
  done < /root/.ssh/authorized_keys > "$t"
  chmod 0600 "$t"; mv -f "$t" /root/.ssh/authorized_keys
  ev_bool new_key_login_seen true
  ev_num removed_lines "$removed"
  ev_bool old_key_absent true
  ok
  ;;

inventory-scan)
  rows="[]"; cnt=0
  while read -r s; do
    d="/opt/$s"
    row="$(jq -nc --arg dir "$d" --arg p "$(env_get "$d/.env" COMPOSE_PROJECT_NAME)" \
      --arg dom "$(env_get "$d/.env" SERVICE_DOMAIN)" --arg port "$(env_get "$d/.env" API_HOST_PORT)" \
      --arg role "$(tr -d ' \r\n' 2>/dev/null < "$d/.role")" \
      --argjson uid "$([ -n "$(env_get "$d/.env" INSTANCE_UID)" ] && echo true || echo false)" \
      --argjson api "$(ctr_running "$(ctr "$s" api)" && echo true || echo false)" \
      '{dir:$dir, compose_project_name:$p, service_domain:$dom, api_host_port:$port, role:$role, has_instance_uid:$uid, api_running:$api}')"
    rows="$(jq -c --argjson r "$row" '. + [$r]' <<<"$rows")"; cnt=$((cnt+1))
  done < <(list_instance_dirs)
  ev_json rows "$rows"; ev_num count "$cnt"
  ok
  ;;

walg-key-add)
  require_root; read_stdin_json
  jset KID .key_id; jset KHEX .key_hex
  check_keyid "$KID"
  [[ "$KHEX" =~ ^[0-9a-f]{64}$ ]] || fail bad_backup_key
  [ "$(key_id_of_hex "$KHEX")" = "$KID" ] || fail key_id_mismatch
  gid="$(ensure_postgres_gid)" || fail postgres_gid_unknown "GID postgres из образа wal-g не получен"
  owner="root:${gid:-root}"; mode=0640; [ -n "$gid" ] || mode=0600
  install -d -m 0750 -o root -g "${gid:-root}" "$WALG_DIR/keys"
  kt="$(mktemp "$WALG_DIR/keys/.k.XXXXXX")"; printf '%s' "$KHEX" > "$kt"; unset KHEX
  chown "$owner" "$kt"; chmod "$mode" "$kt"; mv -f "$kt" "$WALG_DIR/keys/$KID"
  ev_str key_file_mode "$(stat -c '%U:%g %a' "$WALG_DIR/keys/$KID")"
  if [ -n "$gid" ]; then
    # wal-g от пользователя postgres ИЗНУТРИ контейнера образа Postgres шифрует и расшифровывает
    # этим ключом (одноразовый контейнер, без хранилища — FLEET_TEST_FILE_ROOT во временном каталоге).
    img="$(walg_image)"
    if docker run --rm -u postgres -v "$WALG_DIR:/etc/fleet/walg:ro" -e FLEET_WALG_KEY_ID="$KID" \
         -e FLEET_TEST_FILE_ROOT=/tmp/st --entrypoint bash "$img" -c \
         'mkdir -p /tmp/st && echo probe > /tmp/p && fleet-walg --root st put --no-compress /tmp/p k/p >/dev/null 2>&1 && fleet-walg --root st get --no-decompress k/p /tmp/b >/dev/null 2>&1 && cmp -s /tmp/p /tmp/b && ! grep -q probe /tmp/st/k/p' ; then
      ev_bool read_by_postgres_in_container true
    else
      fail key_unreadable_in_container "wal-g от пользователя postgres не зашифровал/не расшифровал этим ключом"
    fi
  else
    ev_bool read_by_postgres_in_container false
    ev_str note "контейнеров Postgres на этом хосте нет (R): ключ root:root 0600"
    # На R нет .env, называющего действующий ключ: бэкап конфигурации R (fleet-route
    # backup-config) шифруется последним добавленным ключом.
    install -d -m 0755 "$FLEET_ETC"; printf '%s\n' "$KID" > "$FLEET_ETC/active_key_id"
    ev_str active_key_id "$KID"
  fi
  ok
  ;;

walg-key-remove)
  require_root
  KID="${1:-}"; check_keyid "$KID"
  users=()
  while read -r s; do
    [ "$(env_get "/opt/$s/.env" WALG_KEY_ID)" = "$KID" ] && users+=("$s")
  done < <(list_instance_dirs)
  [ "$(tr -dc '0-9a-f' 2>/dev/null < "$FLEET_ETC/active_key_id")" = "$KID" ] && users+=("router-backup")
  if [ "${#users[@]}" -gt 0 ]; then
    ev_json used_by "$(printf '%s\n' "${users[@]}" | jq -R . | jq -sc .)"
    fail key_in_use "ключ назван в .env инстансов сервера"
  fi
  # Удаляемый файл идентифицируется ДВУМЯ признаками: именем и содержимым (SHA-256 ключа).
  if [ -e "$WALG_DIR/keys/$KID" ]; then
    kh="$(tr -dc '0-9a-f' < "$WALG_DIR/keys/$KID")"
    [ "$(key_id_of_hex "$kh")" = "$KID" ] || fail key_file_mismatch "содержимое keys/$KID не соответствует key_id — не удаляю"
    unset kh
  fi
  rm -f "$WALG_DIR/keys/$KID"
  [ ! -e "$WALG_DIR/keys/$KID" ] || fail key_remove_failed
  ev_bool key_file_absent true; ev_num used_by_count 0
  ok
  ;;

install-timers)
  # Для ДЕЙСТВУЮЩИХ A/B (ADR-115 §11 фазы 1–5): им нельзя выполнять bootstrap — он переписал бы
  # wg0.conf одним пиром R и оборвал туннель репликации до §11 фазы 8. Ставит только обёртку,
  # каталоги состояния и таймеры §8 (stdin: {"backup_offset_min"}).
  require_root; read_stdin_json
  jset OFF .backup_offset_min; [[ "$OFF" =~ ^[0-9]+$ ]] && [ "$OFF" -le 179 ] || fail bad_backup_offset
  install_wrapper || fail wrapper_install_failed
  install -d -m 0755 "$STATE_DIR" "$LOCK_DIR"
  install_timers "$OFF"
  for tmr in fleet-backup.timer fleet-backup-heartbeat.timer fleet-server-heartbeat.timer fleet-restore-check.timer; do
    systemctl is-enabled "$tmr" >/dev/null 2>&1 || fail "timer_not_enabled:$tmr"
  done
  ev_bool timers_enabled true; ev_num backup_offset_min "$OFF"
  ok
  ;;

heartbeat)
  require_root
  sid="$(server_id)"; [ -n "$sid" ] || fail no_server_id
  read -r dtot dfree < <(df -B1 --output=size,avail /opt | tail -1)
  load1="$(awk '{print $1}' /proc/loadavg)"
  mavail="$(awk '/^MemAvailable:/{print $2*1024}' /proc/meminfo)"
  # fenced_instances — {slug, reason, since} по отметкам самоограждения (ADR-115 §8.4).
  fenced="[]"
  for f in "$STATE_DIR"/fenced/*; do
    [ -f "$f" ] || continue
    read -r since why < "$f"
    fenced="$(jq -c --arg s "$(basename "$f")" --arg r "${why:-unknown}" --arg t "${since:-}" '. + [{slug:$s, reason:$r, since:$t}]' <<<"$fenced")"
  done
  io="$(mktemp -d /var/tmp/fleet-hb.XXXXXX)"
  jq -nc --arg ts "$(date -u +%FT%TZ)" --argjson t "$dtot" --argjson f "$dfree" --argjson l "$load1" \
     --argjson m "$mavail" --argjson fz "${fenced:-[]}" \
     '{ts:$ts, disk_total_bytes:$t, disk_free_bytes:$f, load1:$l, mem_available_bytes:$m, fenced_instances:$fz}' > "$io/heartbeat.json"
  if walg_host -v "$io:/io" -- --root st put --no-compress --no-encrypt /io/heartbeat.json "servers/$sid/status/heartbeat.json" >/dev/null 2>&1; then
    rm -rf "$io"; ev_str object "servers/$sid/status/heartbeat.json"; ev_num disk_free_bytes "$dfree"; ok
  fi
  rm -rf "$io"; fail heartbeat_upload_failed
  ;;

*) unknown_subcommand "server.$SUB";;
esac
