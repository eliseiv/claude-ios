# ADR-017 — Deploy-топология: общий сервер за внешним Traefik + GitHub Actions SSH

- **Статус:** Accepted (2026-06-02)
- **Контекст ревизует:** [TD-005](../100-known-tech-debt.md) (зафиксирован VPS + Caddy-standalone). Не отменяет [ADR-001](ADR-001-stack-choice.md) (стек) и [ADR-010](ADR-010-backend-hosted-preview.md) (контракт reverse-proxy на `/v1/preview/*`).

## Контекст

Ранее ([TD-005](../100-known-tech-debt.md), 2026-06-02) deploy-target был зафиксирован как **отдельный VPS** с **собственным** reverse-proxy (Caddy/nginx) внутри нашего `docker compose`-стека: Caddy держал порты 80/443, терминировал TLS (auto-ACME), проксировал на `api` по `127.0.0.1:8000`. Выкатка — `deploy-vps.sh` по SSH.

Владелец инфраструктуры изменил требования: сервис размещается **не на выделенном VPS, а на общем Linux-сервере** (Ubuntu 22.04, `87.239.135.154`), где уже работают другие сервисы (`music-backend`) и **общий edge-прокси Traefik** в `/opt/edge`. Traefik держит порты 80/443, терминирует TLS, авто-выпускает Let's Encrypt-сертификаты и роутит по доменам для всех сервисов сервера.

Жёсткие требования владельца к встраиванию:
1. НЕ публиковать порты 80/443 (конфликт с Traefik) — только `expose` внутреннего `8000`.
2. Контейнер `api` — в **внешней** docker-сети `web` (общая с Traefik, создана `docker network create web`) + `default` (внутренняя для PG/Redis).
3. Маршрутизация — через **docker-labels** (Traefik service discovery), не через наш конфиг прокси.
4. SSL/nginx/Caddy у нас **не настраивается** — TLS целиком ответственность Traefik. `postgres`/`redis` — только в `default`, без публикации портов.
5. CI/CD — GitHub Actions → SSH на сервер → `cd /opt/<service> && git pull && docker compose up -d --build`.

## Решение

**Deploy-топология MVP = общий сервер + внешний Traefik + GitHub Actions SSH-деплой.**

1. **Reverse-proxy и TLS — внешний Traefik, не наша ответственность.** Наш стек **не содержит** reverse-proxy-контейнера, не держит порты 80/443, не терминирует TLS, не управляет сертификатами. Прежние Caddy-артефакты (`infra/legacy/Caddyfile`, `infra/legacy/nginx.conf.example`, наш TLS) в этой схеме **не используются** (перенесены в `infra/legacy/` с DEPRECATED-баннером — см. [07-deployment.md](../07-deployment.md)).

2. **Сеть:** `api` подключён к двум сетям:
   - `web` — `external: true` (общая с Traefik), через неё Traefik проксирует входящий трафик на `api:8000`;
   - `default` — внутренняя сеть стека для связи `api` ↔ `postgres`/`redis`.
   `postgres`/`redis` — **только** в `default`, без публикации портов наружу.

3. **`api`** — `expose: 8000` (uvicorn/gunicorn), **без** `ports:` для 80/443/публичного маппинга. Доступ снаружи — исключительно через Traefik по `web`.

4. **Маршрутизация — docker-labels** на сервисе `api` (Traefik подхватывает):
   ```
   traefik.enable=true
   traefik.docker.network=web
   traefik.http.routers.<service>.rule=Host(`${SERVICE_DOMAIN}`)
   traefik.http.routers.<service>.entrypoints=websecure
   traefik.http.routers.<service>.tls.certresolver=${TRAEFIK_CERTRESOLVER}
   traefik.http.services.<service>.loadbalancer.server.port=8000
   ```
   `certresolver` параметризован через `${TRAEFIK_CERTRESOLVER}` = **`le`** — имя ACME-резолвера общего Traefik (из `/opt/edge`; [Q-017-2](../99-open-questions.md), Closed 2026-06-02). `le` сделан **дефолтным** на entrypoint `websecure` (`--entrypoints.websecure.http.tls.certresolver=le`), поэтому label `tls.certresolver` **опционален** (сертификат выпускается автоматически для любого HTTPS-роутера); явный label рекомендован для надёжности. Домен — `${SERVICE_DOMAIN}` = **`broadnova.shop`** ([Q-017-1](../99-open-questions.md), Closed 2026-06-02).

5. **CI/CD = GitHub Actions SSH workflow.** Сборка/выкатка на сервере: SSH → `cd /opt/<service>` → `git pull` → `docker compose up -d --build`. Образ собирается **на сервере** (build из исходников в `/opt/<service>`), а не пушится из registry. GitHub Secrets: `SSH_HOST=87.239.135.154`, `SSH_USER=root`, `SSH_PRIVATE_KEY`. `apply_release`-seam ([ADR-001]/инфра) специализируется под этот SSH-flow.

6. **`/healthz`** — публичный endpoint `200` как алиас `GET /health` (для healthcheck Traefik и smoke). Контракт зафиксирован в [API-REFERENCE.md](../API-REFERENCE.md) и [api-gateway/02-api-contracts.md](../modules/api-gateway/02-api-contracts.md). Без auth.

7. **`TRUSTED_PROXY_IPS`** в prod **обязан** включать адрес/подсеть Traefik (docker-сеть `web`). Traefik проставляет `X-Forwarded-For`; без доверия к нему `client_ip` берётся как IP Traefik → per-IP rate limit неработоспособен. См. [05-security.md](../05-security.md#доверенный-reverse-proxy-и-определение-client-ip-anti-spoofing).

8. **DNS:** A-запись домена сервиса `broadnova.shop` → `87.239.135.154` должна существовать **до** запуска (Traefik ACME-challenge выпускает сертификат по домену). Предзапусковый операционный пункт — см. [07-deployment.md prod-checklist](../07-deployment.md#prod-readiness-checklist-must-configure-before-launch).

9. **Инвариант сервера:** `DOCKER_MIN_API_VERSION=1.24` уже задан на сервере — не трогать.

## Последствия

**Плюсы:**
- Меньше нашей ответственности: TLS, ACME, edge-роутинг — на общем Traefik. Наш стек проще (нет proxy-контейнера, Caddyfile, certbot).
- Совместное использование сервера с другими сервисами без конфликта портов (изоляция через сети + labels).
- Деплой проще операционно: `git pull && docker compose up -d --build` по SSH из GitHub Actions.

**Минусы / риски:**
- Сборка образа **на сервере** при каждом деплое (нет immutable registry-tag) → дольше выкатка, нагрузка на сервер при build, rollback — через `git checkout <prev-commit>` + rebuild, а не переключение тега. Зафиксировано в [07-deployment.md §Откат](../07-deployment.md#откат).
- Контракт pass-through заголовков на `/v1/preview/*` ([ADR-010](ADR-010-backend-hosted-preview.md)) теперь — ответственность **внешнего** Traefik (вне нашего репозитория). Требование к Traefik: не перетирать/не дублировать sandbox-заголовки приложения и не инжектить cookies на `/v1/preview/*`. Зафиксировано как операционное требование к владельцу Traefik в [07-deployment.md](../07-deployment.md).
- Зависимость от чужого Traefik: его недоступность/мисконфиг роняет роутинг. Контроль конфигурации Traefik вне нашей зоны.
- `root`-доступ по SSH из CI — повышенный риск компрометации `SSH_PRIVATE_KEY`; ключ — только в GitHub Secrets, ротация при подозрении.

**Закрытое тех-долговое последствие:** [TD-005](../100-known-tech-debt.md) обновлён — финальная схема = shared-server + Traefik + GitHub Actions SSH (не противоречит, уточняет deploy-target).

## Альтернативы

- **Выделенный VPS + собственный Caddy (прежний TD-005).** Отклонено: владелец предоставляет общий сервер с уже работающим Traefik; дублировать edge-прокси нельзя (конфликт 80/443).
- **Сборка образа в CI + push в registry, на сервере только pull.** Лучше для скорости/immutability, но требует registry-инфраструктуры и креденшелов; владелец зафиксировал `git pull && compose up --build` на сервере. Оставлено как возможное улучшение (новый ADR при вводе registry).
- **Свой Traefik/nginx внутри нашего стека за общим Traefik (двойной прокси).** Отклонено: избыточно, второй прокси не нужен — Traefik роутит напрямую на `api:8000` по labels.
