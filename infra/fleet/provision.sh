#!/usr/bin/env bash
# Наполнение инстанса на прикладном сервере (docs/MIGRATION-3-SERVERS.md §Порядок работ).
#
#   provision.sh adapt <инстанс> <A|B>   — привести УЖЕ существующий .env к новой схеме
#   provision.sh new   <инстанс> <домен> <A|B>  — создать .env с нуля из .env.prod.example
#
# Режим adapt — основной при переезде: настоящий .env приезжает со старого сервера вместе с
# ключами провайдеров и секретами, и трогать в нём можно ТОЛЬКО адресацию новой схемы.
# Переписывать его целиком нельзя: в нём лежат значения, которых нет больше нигде.
set -uo pipefail

MODE="${1:?режим: adapt | new}"
INST="${2:?имя инстанса}"
DIR="/opt/$INST"
PORTS="/opt/fleet/ports.txt"

wg_of() { [ "$1" = "A" ] && echo 10.10.0.1 || echo 10.10.0.2; }

read_ports() {
  local line; line="$(grep -E "^$INST " "$PORTS" 2>/dev/null | head -1)"
  [ -n "$line" ] || { echo "нет строки для $INST в $PORTS"; exit 1; }
  API_PORT="$(echo "$line" | awk '{print $2}')"
  PG_PORT="$(echo "$line" | awk '{print $3}')"
}

setvar() {  # setvar КЛЮЧ ЗНАЧЕНИЕ — заменить или дописать, не трогая остальное
  local k="$1" v="$2"
  if grep -q "^$k=" "$DIR/.env"; then
    local tmp; tmp="$(mktemp)"
    awk -v k="$k" -v v="$v" -F= 'BEGIN{OFS="="} $1==k{print k, v; next} {print}' "$DIR/.env" > "$tmp"
    mv "$tmp" "$DIR/.env"
  else
    printf '%s=%s\n' "$k" "$v" >> "$DIR/.env"
  fi
}

case "$MODE" in
adapt)
  SELF="${3:?третьим аргументом — на каком сервере наполняем: A или B}"
  [ -f "$DIR/.env" ] || { echo "$DIR/.env отсутствует — нечего приводить"; exit 1; }
  read_ports
  cp "$DIR/.env" "$DIR/.env.bak-adapt-$(date +%Y%m%d-%H%M%S)"
  setvar WG_BIND_IP   "$(wg_of "$SELF")"
  setvar API_HOST_PORT "$API_PORT"
  setvar PG_HOST_PORT  "$PG_PORT"
  # Замер на прежнем сервере: api с четырьмя воркерами занимал 458 МБ, на 27 инстансов это
  # 14 ГБ только под приложение. Нагрузка почти целиком в ожидании ответа провайдера, поэтому
  # два асинхронных воркера обслуживают тот же поток запросов вдвое дешевле по памяти.
  grep -q "^GUNICORN_WORKERS=" "$DIR/.env" || setvar GUNICORN_WORKERS 2
  echo "[$INST] .env приведён: туннель $(wg_of "$SELF"), порты api=$API_PORT pg=$PG_PORT"
  ;;

new)
  DOMAIN="${3:?третьим аргументом — домен}"
  SELF="${4:?четвёртым — сервер A или B}"
  read_ports
  cd "$DIR" || { echo "нет каталога $DIR"; exit 1; }
  [ -f .env ] && { echo "$DIR/.env уже существует — используйте adapt"; exit 1; }
  cp .env.prod.example .env
  PW="$(openssl rand -hex 20)"
  setvar COMPOSE_PROJECT_NAME "$INST"
  setvar SERVICE_DOMAIN "$DOMAIN"
  setvar POSTGRES_USER "app_$INST"
  setvar POSTGRES_PASSWORD "$PW"
  setvar POSTGRES_DB "db_$INST"
  setvar DATABASE_URL "postgresql+asyncpg://app_$INST:$PW@postgres:5432/db_$INST"
  setvar REDIS_URL "redis://redis:6379/0"
  setvar TRAEFIK_CERTRESOLVER "le"
  setvar JWT_ISSUER "https://$DOMAIN"
  setvar ADMIN_API_SECRET "$(openssl rand -base64 32)"
  setvar WG_BIND_IP "$(wg_of "$SELF")"
  setvar API_HOST_PORT "$API_PORT"
  setvar PG_HOST_PORT "$PG_PORT"
  setvar GUNICORN_WORKERS 2
  # --- Эксплуатационная база флота -------------------------------------------------------
  # `.env.prod.example` описывает ПРОИЗВОДСТВЕННУЮ конфигурацию, и правильно делает: в ней
  # StoreKit боевой, документация закрыта. Флот же пока живёт в другом режиме — приложения на
  # ревью, поэтому песочница и открытая документация. Раньше эта разница нигде не была записана,
  # и каждый новый инстанс рождался с умолчаниями шаблона, молча отличаясь от всех живых.
  # Так и вышло 2026-09-01: три новых инстанса отдавали 404 на /docs, потому что DOCS_ENABLED
  # остался false. Инстанс при этом «работал» — health отвечал, — и расхождение было не видно,
  # пока в него не ткнулись руками.
  # Значения ниже — то, что ФАКТИЧЕСКИ работает на флоте. Возвращать к производственным нужно
  # осознанно и вместе с корневым сертификатом Apple (07-deployment.md §Prod-readiness).
  setvar DOCS_ENABLED true
  setvar APPSTORE_ENVIRONMENT sandbox
  setvar APPSTORE_ROOT_CERT_DIR /run/secrets/appstore_root_certs
  setvar STOREKIT_TEST_MODE true
  setvar STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION true
  setvar PRESETS_DEFAULT_LOCALE en
  mkdir -p .secrets && chmod 700 .secrets
  if [ ! -f .secrets/jwt_private.pem ]; then
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out .secrets/jwt_private.pem 2>/dev/null
    openssl rsa -pubout -in .secrets/jwt_private.pem -out .secrets/jwt_public.pem 2>/dev/null
  fi
  chown -R 10001:10001 .secrets 2>/dev/null || true
  chmod 640 .secrets/*.pem 2>/dev/null || true
  mkdir -p certs/appstore
  echo "[$INST] создан: домен $DOMAIN, порты api=$API_PORT pg=$PG_PORT"
  ;;
*) echo "неизвестный режим"; exit 2;;
esac
