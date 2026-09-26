# syntax=docker/dockerfile:1.7
# Образ Postgres инстанса с wal-g (ADR-115 §8.2).
#
# Производный от того же pgvector/pgvector:pg16, что стоит в docker-compose.prod.yml, чтобы
# расширение vector (миграция 0025_memory_rag) было на месте. База закреплена по digest: тег
# `pg16` двигается, а смена минорной версии Postgres — осознанное событие, а не побочный эффект
# пересборки. Фактическая версия под этим digest сверена запуском образа 2026-09-26:
#   postgres (PostgreSQL) 16.15 (Debian 16.15-1.pgdg12+2), Debian 12.15, glibc 2.36,
#   uid/gid пользователя postgres = 999/999.
# Мажор 16 совпадает с docs/02-tech-stack.md и с томами действующего флота (dump не нужен).
#
# Образ собирается и публикуется в GHCR джобом .github/workflows/images.yml; тег образа —
# версия этого файла (см. POSTGRES_WALG_TAG ниже), а не SHA коммита приложения: смена образа
# Postgres — инфраструктурная выкатка порциями (ADR-115 §8.2, §10.2 п. 5), а не штатный деплой.
ARG PG_BASE=pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b
FROM ${PG_BASE}

# wal-g — фиксированная версия и контрольная сумма САМОГО бинарника (ADR-115 §8.2: «проверка
# контрольной суммы бинарника при сборке»). Сборка под Ubuntu 22.04 (glibc 2.35) запускается на
# Debian 12 (glibc 2.36). Значение суммы — опубликованный файл wal-g-pg-22.04-amd64.sha256
# релиза v3.0.9 (github.com/wal-g/wal-g/releases/tag/v3.0.9). ADD --checksum проверяет её сам:
# несовпадение роняет сборку.
ARG WALG_VERSION=v3.0.9
ARG WALG_ASSET=wal-g-pg-22.04-amd64
ARG WALG_SHA256=d6795c663894d836ba20840bffe8970aed81bbe25d3151bd4951f6f9c362def9
ADD --checksum=sha256:${WALG_SHA256} --chmod=0755 \
    https://github.com/wal-g/wal-g/releases/download/${WALG_VERSION}/${WALG_ASSET} \
    /usr/local/bin/wal-g

# jq — разбор meta.json в обёртке fleet-wal-push (ограждение по поколению, ADR-115 §8.4);
# ca-certificates — HTTPS до эндпоинта Hetzner Object Storage (ADR-115 §8.7: канал — публичный
# интернет по HTTPS, проверка сертификата НЕ отключается).
RUN apt-get update \
 && apt-get install -y --no-install-recommends jq ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --chmod=0755 fleet-walg.sh /usr/local/bin/fleet-walg
COPY --chmod=0755 fleet-wal-push.sh /usr/local/bin/fleet-wal-push

# Самопроверка образа при сборке: бинарник запускается и называет свою версию. Без этого
# несовместимая по glibc сборка обнаружилась бы только на первом archive_command в проде.
RUN wal-g --version && jq --version && fleet-walg --self-test
