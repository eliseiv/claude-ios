#!/bin/bash
# Проверка ответа inventory CRM (ADR-115 §10.1, §10.2 п. 2). Исполняется на раннере CI.
#
#   inventory-validate.sh <inventory.json>
#
# Красный (код 1, НИ ОДИН сервер не тронут), если: тело не JSON; schema ≠ 1; ноль инстансов в
# active; инстанс ссылается на несуществующий сервер; дубли slug, instance_uid, домена или пары
# (server_id, api_port). Пустой список — ОШИБКА, а не «нечего делать» (Р5: деплой не пропускает
# инстансы молча). Дополнительно (форма контракта §10.1): допустимые состояния, формат slug и
# instance_uid, host-ключи серверов и R (без них CI не закрепит known_hosts).
set -uo pipefail
F="${1:?файл inventory}"

jq -e 'type == "object"' "$F" >/dev/null 2>&1 || { echo "::error title=inventory::ответ CRM — не JSON-объект"; exit 1; }

errs="$(jq -r '
  def dups(f): [.[] | f] | group_by(.) | map(select(length > 1) | .[0]) ;
  [
    (if .schema != 1 then "schema != 1" else empty end),
    (if (.servers | type) != "array" or (.instances | type) != "array" then "servers/instances не массивы" else empty end),
    (if ([.instances[]? | select(.state == "active")] | length) == 0 then "ноль инстансов в active" else empty end),
    (([.servers[]?.server_id]) as $ids | .instances[]? | select((.server_id as $s | $ids | index($s)) == null)
       | "инстанс \(.slug) ссылается на несуществующий сервер \(.server_id)"),
    (.instances | dups(.slug)[]? | "дубль slug: \(.)"),
    (.instances | dups(.instance_uid)[]? | "дубль instance_uid: \(.)"),
    (.instances | dups(.domain)[]? | "дубль домена: \(.)"),
    (.instances | dups("\(.server_id)|\(.api_port)")[]? | "дубль (server_id, api_port): \(.)"),
    (.servers | dups(.server_id)[]? | "дубль server_id: \(.)"),
    (.instances[]? | select((.state | IN("creating","active","erasing","stopped","failed")) | not)
       | "инстанс \(.slug): недопустимое state \(.state)"),
    (.servers[]? | select((.state | IN("active","draining")) | not) | "сервер \(.server_id): недопустимое state \(.state)"),
    (.instances[]? | select((.slug | test("^[a-z][a-z0-9-]{1,30}$")) | not) | "некорректный slug: \(.slug)"),
    (.instances[]? | select((.instance_uid | tostring | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")) | not)
       | "некорректный instance_uid у \(.slug)"),
    (.servers[]? | select((.ssh_host_key // "") == "" or (.ssh_host // "") == "") | "сервер \(.server_id): нет ssh_host/ssh_host_key"),
    (if ((.router.ssh_host_key // "") == "" or (.router.ssh_host // "") == "") then "router: нет ssh_host/ssh_host_key" else empty end),
    (if (.last_deployed_sha != null and ((.last_deployed_sha | tostring) | test("^[0-9a-f]{40}$") | not)) then "last_deployed_sha не 40 hex" else empty end)
  ] | .[]' "$F" 2>/dev/null)"
rc=$?
[ "$rc" = 0 ] || { echo "::error title=inventory::разбор ответа CRM упал (форма не соответствует ADR-115 §10.1)"; exit 1; }
if [ -n "$errs" ]; then
  while IFS= read -r e; do echo "::error title=inventory::$e"; done <<<"$errs"
  exit 1
fi
echo "inventory: серверов $(jq '.servers|length' "$F"), инстансов $(jq '.instances|length' "$F"), active $(jq '[.instances[]|select(.state=="active")]|length' "$F")"
