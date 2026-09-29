#!/bin/bash
# fleet-route — операции на маршрутизаторе R (ADR-115 §6, §2.1, §1, §11 фаза 2, §13).
#
#   fleet-route add <slug>          (stdin: {"domain","wg_ip","api_port","router_ip","allow_legacy"?})
#   fleet-route remove <slug>
#   fleet-route wg-peer-add         (stdin: {"public_key","wg_ip","endpoint"?})
#   fleet-route wg-peer-remove      (stdin: {"public_key","wg_ip"})
#   fleet-route tunnel-check <wg_ip>
#   fleet-route tools-export        архив действующей версии инструментов в stdout; FLEET-RESULT — в stderr
#   fleet-route roles-dump
#   fleet-route backup-config       ночной бэкап конфигурации R (ADR-115 §6.3; таймер fleet-router-backup)
#   fleet-route setup               установка обёртки и таймера бэкапа на R (stdin: {"backup_offset_min"})
#
# Маршрут: ОДИН файл на инстанс — /opt/router/dynamic/inst-<slug>.yml, роутер и сервис
# `inst-<slug>`, один сервер без failover (ADR-115 §6.1). Запись атомарная: временный файл в ТОМ
# ЖЕ каталоге (с суффиксом .tmp — провайдер file его не читает) + mv. Каталог смонтирован в
# Traefik целиком, поэтому новый inode виден.
set -uo pipefail
# shellcheck source=lib/common.sh
. "$(dirname "$(readlink -f "$0")")/lib/common.sh"

SUB="${1:-}"; shift || true
# shellcheck disable=SC2034  # STEP читает lib/common.sh (emit)
STEP="route.$SUB"
LEGACY_FILES="dynamic.yml fleet.yml"
PUBKEY_RE='^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$'

route_file() { printf '%s/inst-%s.yml' "$ROUTER_DYNAMIC_DIR" "$1"; }

issuer_of() {  # издатель сертификата, который Traefik отдаёт на этот домен (ADR-115 §6.2)
  echo | timeout 10 openssl s_client -connect 127.0.0.1:443 -servername "$1" 2>/dev/null \
    | openssl x509 -noout -issuer 2>/dev/null | sed 's/^issuer=//'
}

wg_conf_has_peer() { awk -v k="$1" '$1=="PublicKey" && $3==k {f=1} END {exit !f}' /etc/wireguard/wg0.conf 2>/dev/null; }
wg_conf_drop_peer() {  # удалить блок [Peer] с этим ключом из wg0.conf (атомарно)
  local t; t="$(mktemp /etc/wireguard/.wg0.XXXXXX)"
  awk -v k="$1" '
    function flush() { if (buf != "" && !drop) printf "%s", buf; buf = ""; drop = 0 }
    /^\[/ { flush() }
    { buf = buf $0 "\n"; if ($1 == "PublicKey" && $3 == k) drop = 1 }
    END { flush() }' /etc/wireguard/wg0.conf > "$t"
  chmod 0600 "$t"; mv -f "$t" /etc/wireguard/wg0.conf
}

case "$SUB" in
add)
  require_root; SLUG="${1:-}"; check_slug "$SLUG"; read_stdin_json
  jset DOMAIN .domain; [[ "$DOMAIN" =~ $DOMAIN_RE ]] || fail bad_domain
  jset WGIP .wg_ip; [[ "$WGIP" =~ ^10\.10\.0\.[0-9]{1,3}$ ]] || fail bad_wg_ip
  jset PORT .api_port; [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge "$API_PORT_MIN" ] && [ "$PORT" -le "$API_PORT_MAX" ] || fail bad_api_port
  jset RIP .router_ip; [[ "$RIP" =~ $IPV4_RE ]] || fail bad_router_ip
  jset LEG .allow_legacy opt
  [ -d "$ROUTER_DYNAMIC_DIR" ] || fail no_router_dir
  mkdir -p "$LOCK_DIR"; exec {RFD}>"$LOCK_DIR/route.lock"; flock -w 120 "$RFD" || fail lock_timeout
  # A-запись домена = R — ПЕРЕД записью файла (ADR-115 §6.2): иначе ACME проваливается и сам
  # не повторяется.
  ips="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
  ev_str a_records "${ips% }"
  [ "${ips% }" = "$RIP" ] || fail dns_not_router "A-записи домена не равны ровно адресу R"
  # Общего файла нет (ADR-115 §6.1): посторонний файл с роутером на тот же домен — отказ.
  # Исключение — переходная фаза 6 §11, когда рядом ещё лежат прежние dynamic.yml/fleet.yml
  # (allow_legacy=true; управляет оператор, CRM его не передаёт).
  legacy_seen=()
  for f in "$ROUTER_DYNAMIC_DIR"/*.yml "$ROUTER_DYNAMIC_DIR"/*.yaml "$ROUTER_DYNAMIC_DIR"/*.toml; do
    [ -f "$f" ] || continue
    b="$(basename "$f")"; [ "$b" = "inst-$SLUG.yml" ] && continue
    grep -qF "Host(\`$DOMAIN\`)" "$f" || continue
    case " $LEGACY_FILES " in
      *" $b "*) if [ "$LEG" = "true" ]; then legacy_seen+=("$b"); continue; fi;;
    esac
    ev_str conflicting_file "$b"
    fail foreign_route "в каталоге маршрутов уже есть $b с роутером на $DOMAIN"
  done
  [ "${#legacy_seen[@]}" -gt 0 ] && ev_json legacy_files_with_domain "$(printf '%s\n' "${legacy_seen[@]}" | jq -R . | jq -sc .)"
  F="$(route_file "$SLUG")"
  T="$(mktemp "$ROUTER_DYNAMIC_DIR/.inst-$SLUG.XXXXXX.tmp")"; tmp_track "$T"
  cat > "$T" <<EOF
# Управляется fleet-route add (ADR-115 §6.1). Не править руками: следующий вызов перезапишет.
http:
  routers:
    inst-$SLUG:
      rule: "Host(\`$DOMAIN\`)"
      entryPoints: [websecure]
      service: inst-$SLUG
      tls:
        certResolver: le
  services:
    inst-$SLUG:
      loadBalancer:
        servers:
          - url: "http://$WGIP:$PORT"
        healthCheck:
          path: /ready
          interval: 10s
          timeout: 3s
EOF
  chmod 0644 "$T"
  if [ -f "$F" ] && cmp -s "$T" "$F"; then rm -f "$T"; else mv -f "$T" "$F"; fi
  [ -f "$F" ] || fail route_write_failed
  ev_str file "$F"
  # Проверка по ИЗДАТЕЛЮ, а не по коду ответа: TRAEFIK DEFAULT CERT = провал (ADR-115 §6.2).
  wait_s="${FLEET_CERT_WAIT:-300}"; iss=""
  for _ in $(seq 1 $(( wait_s / 10 + 1 ))); do
    iss="$(issuer_of "$DOMAIN")"
    case "$iss" in *"Let's Encrypt"*) break;; esac
    sleep 10
  done
  ev_str certificate_issuer "$iss"
  case "$iss" in *"Let's Encrypt"*) ;; *) fail cert_not_issued "файл маршрута на месте; сертификат ещё не выпущен — повтор идемпотентен";; esac
  ok
  ;;

remove)
  # fleet-route remove <slug> [<домен>] — домен НЕ секрет; если передан, файл удаляется только
  # при совпадении его правила с доменом (второй признак, ADR-115 §6.1). Удаляется только файл,
  # написанный этим инструментом (заголовок управления).
  require_root; SLUG="${1:-}"; check_slug "$SLUG"; DOMAIN="${2:-}"
  [ -z "$DOMAIN" ] || [[ "$DOMAIN" =~ $DOMAIN_RE ]] || fail bad_domain
  mkdir -p "$LOCK_DIR"; exec {RFD}>"$LOCK_DIR/route.lock"; flock -w 120 "$RFD" || fail lock_timeout
  F="$(route_file "$SLUG")"
  if [ -e "$F" ]; then
    head -1 "$F" | grep -q '^# Управляется fleet-route add' || fail foreign_route "$F написан не fleet-route — не удаляю"
    if [ -n "$DOMAIN" ]; then
      grep -qF "Host(\`$DOMAIN\`)" "$F" || fail domain_mismatch "правило $F не на $DOMAIN — не удаляю"
    fi
  fi
  rm -f "$F"
  [ ! -e "$F" ] || fail route_remove_failed
  ev_str file "$F"; ev_bool present false
  ok
  ;;

wg-peer-add)
  require_root; read_stdin_json; need wg
  jset PK .public_key; [[ "$PK" =~ $PUBKEY_RE ]] || fail bad_public_key
  jset WGIP .wg_ip; [[ "$WGIP" =~ ^10\.10\.0\.([0-9]{1,3})$ ]] || fail bad_wg_ip
  n="${BASH_REMATCH[1]}"; { [ "$n" -ge 1 ] && [ "$n" -le 254 ] && [ "$n" != 3 ]; } || fail bad_wg_ip
  jset EP .endpoint opt
  [ -z "$EP" ] || [[ "$EP" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]] || fail bad_endpoint "endpoint — <host>:<port>"
  other="$(wg show wg0 allowed-ips 2>/dev/null | awk -v ip="$WGIP/32" -v k="$PK" 'index($0, ip) && $1 != k {print $1; exit}')"
  [ -z "$other" ] || fail wg_ip_taken "адрес $WGIP уже выдан другому пиру"
  args=(peer "$PK" allowed-ips "$WGIP/32"); [ -n "$EP" ] && args+=(endpoint "$EP")
  wg set wg0 "${args[@]}" || fail wg_set_failed
  if wg_conf_has_peer "$PK"; then wg_conf_drop_peer "$PK"; fi
  { printf '\n[Peer]\n# fleet: сервер %s\nPublicKey = %s\nAllowedIPs = %s/32\n' "$WGIP" "$PK" "$WGIP"
    [ -n "$EP" ] && printf 'Endpoint = %s\n' "$EP"; } >> /etc/wireguard/wg0.conf
  wg show wg0 peers | grep -qxF "$PK" || fail peer_not_live
  wg_conf_has_peer "$PK" || fail peer_not_persisted
  ev_bool in_wg_show true; ev_bool in_wg0_conf true; ev_str wg_ip "$WGIP"
  ok
  ;;

wg-peer-remove)
  require_root; read_stdin_json; need wg
  jset PK .public_key; [[ "$PK" =~ $PUBKEY_RE ]] || fail bad_public_key
  jset WGIP .wg_ip; [[ "$WGIP" =~ ^10\.10\.0\.[0-9]{1,3}$ ]] || fail bad_wg_ip
  live_ips="$(wg show wg0 allowed-ips 2>/dev/null | awk -v k="$PK" '$1 == k { $1 = ""; print }' | tr -s ' ' | sed 's/^ //')"
  # Блок [Peer] читается целиком: порядок PublicKey/AllowedIPs внутри блока в ручных файлах R любой.
  conf_ips="$(awk -v k="$PK" '
    function flush() { if (pk == k) print ips; pk = ""; ips = "" }
    /^\[/ { flush() }
    $1 == "PublicKey"  { pk = $3 }
    $1 == "AllowedIPs" { v = $0; sub(/^[^=]*=[ \t]*/, "", v); gsub(/[ \t]/, "", v); gsub(/,/, " ", v); ips = v }
    END { flush() }' /etc/wireguard/wg0.conf 2>/dev/null)"
  for ips in "$live_ips" "$conf_ips"; do
    [ -z "$ips" ] || [ "$ips" = "$WGIP/32" ] || fail peer_ip_mismatch "allowed-ips пира ($ips) ≠ $WGIP/32 — пир не удалён"
  done
  wg show wg0 peers | grep -qxF "$PK" && { wg set wg0 peer "$PK" remove || fail wg_set_failed; }
  wg_conf_has_peer "$PK" && wg_conf_drop_peer "$PK"
  wg show wg0 peers | grep -qxF "$PK" && fail peer_still_live
  wg_conf_has_peer "$PK" && fail peer_still_persisted
  ev_bool in_wg_show false; ev_bool in_wg0_conf false; ev_str wg_ip "$WGIP"
  ok
  ;;

tunnel-check)
  peer_ip="${1:-}"; [[ "$peer_ip" =~ $IPV4_RE ]] || fail bad_wg_ip
  need wg ping
  now="$(date +%s)"
  hs="$(wg show wg0 dump 2>/dev/null | awk -v ip="$peer_ip/32" 'NR>1 && index($4, ip) {print $5; exit}')"
  [ -n "$hs" ] || fail peer_not_found
  age=$(( hs > 0 ? now - hs : 999999 ))
  pout="$(ping -c 3 -W 2 "$peer_ip" 2>/dev/null)"
  loss="$(sed -n 's/.* \([0-9.]*\)% packet loss.*/\1/p' <<<"$pout")"
  rtt="$(sed -n 's#.*= [0-9.]*/\([0-9.]*\)/.*#\1#p' <<<"$pout")"
  ev_num handshake_age_s "$age"; ev_num loss_pct "${loss:-100}"; ev_num rtt_ms "${rtt:-0}"
  [ "$age" -le 120 ] || fail handshake_stale
  [ "${loss:-100}" = "0" ] || fail ping_loss
  ok
  ;;

tools-export)
  # shellcheck disable=SC2034  # читает lib/common.sh (emit): FLEET-RESULT — в stderr, stdout занят архивом
  RESULT_TO_STDERR=1
  t="$(mktemp /var/tmp/fleet-export.XXXXXX)"; tmp_track "$t"
  tar -C "$FLEET_VERSION_DIR" --exclude=./.inuse -czf "$t" . 2>/dev/null || { rm -f "$t"; fail export_failed; }
  size="$(stat -c %s "$t")"
  cat "$t"; rc=$?; rm -f "$t"
  [ "$rc" = 0 ] || fail export_write_failed
  ev_str version "$TOOLS_VERSION"; ev_num archive_bytes "$size"
  ok
  ;;

roles-dump)
  [ -r "$ROUTER_ROLES_FILE" ] || fail no_roles_file
  rows="$(awk -F'\t' '!/^#/ && NF >= 2 {print $1 "\t" $2}' "$ROUTER_ROLES_FILE" \
    | jq -R 'split("\t") | {instance: .[0], primary_server: .[1]}' | jq -sc .)"
  ev_json rows "$rows"; ev_num count "$(jq 'length' <<<"$rows")"
  # Отдаются ТОЛЬКО строки файла. Инстанса без строки здесь нет (замер 2026-09-26: 37 строк при
  # 46 инстансах): его основной сервер CRM определяет по inventory-scan (.role + api_running,
  # ADR-115 §11 фаза 2). Строка с сервером вне {A,B} — не угадывается, а отвергается.
  bad="$(jq -c '[.[] | select((.primary_server | IN("A","B")) | not)]' <<<"$rows")"
  if [ "$bad" != "[]" ]; then ev_json invalid_rows "$bad"; fail bad_roles_row "в roles.tsv есть строки с сервером вне {A,B}"; fi
  ok
  ;;

backup-config)
  require_root
  KID="$(tr -dc '0-9a-f' 2>/dev/null < "$FLEET_ETC/active_key_id")"; check_keyid "$KID"
  HID="$(tr -dc '0-9a-f' 2>/dev/null < /etc/machine-id)"; [ -n "$HID" ] || fail no_machine_id
  io="$(mktemp -d /var/tmp/fleet-router.XXXXXX)"; tmp_track "$io"
  # acme.json содержит закрытые ключи сертификатов, /etc/wireguard — ключ туннеля R: только
  # шифрованным (ADR-115 §6.3, §8.7).
  tar -cf "$io/router.tar" -C / opt/router etc/wireguard 2>/dev/null || { rm -rf "$io"; fail tar_failed; }
  key="router/$HID/$(date -u +%F).tar"
  walg_host -k "$KID" -v "$io:/io" -- --root st put --no-compress /io/router.tar "$key" >/dev/null 2>&1 \
    || { rm -rf "$io"; fail upload_failed; }
  size="$(stat -c %s "$io/router.tar")"; rm -rf "$io"
  cut="$(date -u -d '-30 days' +%F)"; removed=0
  while read -r obj; do
    case "$obj" in [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].tar) ;; *) continue;; esac
    [[ "${obj%.tar}" < "$cut" ]] || continue
    walg_host -- --root st rm "router/$HID/$obj" >/dev/null 2>&1 && removed=$((removed+1))
  done < <(walg_host -- --root st ls "router/$HID/" 2>/dev/null | awk 'NR>1 && $1=="obj" {print $NF}')
  ev_str object "$key"; ev_num size "$size"; ev_num expired_removed "$removed"
  ok
  ;;

setup)
  require_root; read_stdin_json
  jset OFF .backup_offset_min; [[ "$OFF" =~ ^[0-9]+$ ]] && [ "$OFF" -le 179 ] || fail bad_backup_offset
  t="$(mktemp /usr/local/bin/.fleet.XXXXXX)"
  cp "$FLEET_VERSION_DIR/fleet.sh" "$t" && chmod 0755 "$t" && mv -f "$t" /usr/local/bin/fleet || fail wrapper_install_failed
  for g in server instance backup route; do ln -sfn fleet "/usr/local/bin/fleet-$g"; done
  install -m 0644 "$FLEET_VERSION_DIR"/systemd/fleet-router-backup.service "$FLEET_VERSION_DIR"/systemd/fleet-router-backup.timer /etc/systemd/system/
  h=$(( (60 + OFF) / 60 )); m=$(( (60 + OFF) % 60 ))
  install -d -m 0755 /etc/systemd/system/fleet-router-backup.timer.d
  printf '[Timer]\nOnCalendar=\nOnCalendar=*-*-* %02d:%02d:00 UTC\n' "$h" "$m" > /etc/systemd/system/fleet-router-backup.timer.d/offset.conf
  systemctl daemon-reload && systemctl enable --now fleet-router-backup.timer >/dev/null 2>&1 || fail timer_enable_failed
  systemctl is-enabled fleet-router-backup.timer >/dev/null 2>&1 || fail timer_not_enabled
  ev_bool timer_enabled true; ev_str on_calendar "$(printf '%02d:%02d UTC' "$h" "$m")"
  ok
  ;;

*) unknown_subcommand "route.$SUB";;
esac
