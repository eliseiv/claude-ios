# Deploy / rollback runbook (infra)

Implements the contract in `docs/07-deployment.md`. This file is an infra artifact;
the authoritative deployment spec lives in `docs/07-deployment.md` (owned by architect).

## Deploy target — shared server + EXTERNAL Traefik + GitHub Actions SSH (ADR-017)
The deploy target is fixed by the infrastructure owner (`docs/07-deployment.md` §Топология MVP,
`docs/adr/ADR-017-shared-server-traefik-deploy.md`, revises `docs/100-known-tech-debt.md#TD-005`):

- **Shared Linux server** (Ubuntu 22.04, `87.239.135.154`, root). Each instance is its own stack
  dir `/opt/<dir>` deployed with `-p <project>`. **The set of live instances is NOT listed in this
  file** (a copy goes stale the moment an instance is added): the source of truth is the registry
  table in `docs/07-deployment.md` §CI/CD-контракт: INSTANCES-loop, mirrored character-for-character
  by `$INSTANCES` in `.github/workflows/ci.yml` + `.github/workflows/deploy.yml`. `claude-ios`
  (`/opt/claude-ios`) is the first entry — deploying it with `-p claude-ios` equals the historical
  no-`-p` deploy (backward-compat), which is why single-instance examples below use it.
- **External edge-proxy Traefik** in `/opt/edge` owns ports 80/443, terminates TLS, issues
  Let's Encrypt certs (ACME) and routes by Host. **We do NOT run a reverse proxy / TLS / nginx /
  Caddy.** Our `api` is reached only through Traefik over the **external** docker network `web`
  (`docker network create web` — already created on the server).
- **Image is BUILT ON THE SERVER** (`docker compose up -d --build`), not pulled from a registry —
  there is no immutable registry tag in this scheme.

The active prod artifacts are:
- `docker-compose.prod.yml` — prod stack: `api` (`expose: 8000`, no published ports, on `web`
  external + `default`, Traefik docker-labels) + `postgres` 16 + `redis` 7 (both `default` only,
  no ports) + one-shot `migrate`. No proxy/Caddy service.
- `.env.prod.example` — prod env template (placeholders only; the real `.env` lives on the server
  and is gitignored). Ships `SERVICE_DOMAIN=broadnova.shop` (the FIRST instance's value; every other
  instance overwrites it with its own domain from the registry table), `TRAEFIK_CERTRESOLVER=le`
  (both PUBLIC config, not secrets), and `TRUSTED_PROXY_IPS` (filled on the server).
- `.github/workflows/ci.yml` — CI gate **plus** the automatic, CI-gated SSH deploy job (push to
  `main`), looping over `$INSTANCES`.
- `.github/workflows/deploy.yml` — the same SSH deploy loop, **manual only** (`workflow_dispatch`).
- `docker-compose.prod.observability.yml` + `infra/observability/prometheus.prod.yml` — optional
  Prometheus overlay (loopback only, internal `default` network, never on `web`).

### Legacy artifacts (NOT used under ADR-017)
Moved to `infra/legacy/` and marked DEPRECATED — kept for reference only, never deployed:
- `infra/legacy/Caddyfile`, `infra/legacy/nginx.conf.example` — our own reverse-proxy/TLS is no
  longer used (TLS/ACME is the external Traefik's job).
- `infra/legacy/deploy-vps.sh` — dedicated-VPS + registry/immutable-tag + in-stack Caddy SSH
  script, superseded by `.github/workflows/deploy.yml`.

`infra/deploy/deploy.sh` is the platform-neutral generic seam (registry/immutable-tag oriented).
It is **not** the active prod path under ADR-017 (image is built on the server, no registry).
Kept only as a generic reference for a future registry-based target (would need a new ADR).

## Release contract (ADR-017)
Applies **per instance**: the automated deploy runs the same sequence for every entry of
`$INSTANCES` (`dir:project`), so the commands below are written with `/opt/<dir>` + `-p <project>`.

- The image is built on the server from sources in `/opt/<dir>` (no registry / immutable tag).
- Pre-deploy: `docker compose -p <project> ... run --rm migrate` (`alembic upgrade head`;
  expand/contract, backward-compatible) — runs to completion before the new `api` starts.
- Rebuild + recreate `api`: explicit `build api migrate` then `up -d --no-build` (the fused
  `up --build` exit code is not trusted — see the workflow comments), followed by a readiness gate
  on the `<project>-api-1` container health. Gunicorn `-w 4`; expand/contract keeps the old image
  compatible during the swap. The external Traefik picks up the new container by labels/network
  `web`.
- Health gate: container health (`/ready`, DB + Redis), then per-instance public smoke
  `GET https://<that instance's SERVICE_DOMAIN>/healthz` (non-fatal).
- **Rollback** (no immutable tag): `git checkout <prev-commit>` in `/opt/<dir>` +
  `docker compose -p <project> -f docker-compose.prod.yml up -d --build`. Schema is NOT reverted
  (expand/contract keeps the old code compatible). Rollback is per instance — it does not touch the
  other stacks.

## CI/CD
- `.github/workflows/ci.yml` — gate: ruff format/lint, mypy, pytest+coverage, docker build
  (validation only, **no registry push** under ADR-017). Blocks merge on failure. It also carries
  the **automatic** `deploy` job (`needs: [quality, test, build-image]`, `if` ref == `main`), which
  loops over `$INSTANCES` and deploys every live instance.
- `.github/workflows/deploy.yml` — **manual only** (`workflow_dispatch`, no `push` trigger): the
  same per-instance SSH loop for out-of-band redeploys. Steps are identical to the gated job above.

GitHub Secrets (Settings -> Secrets and variables -> Actions) — NEVER hardcoded:
- `SSH_HOST=87.239.135.154`
- `SSH_USER=root`
- `SSH_PRIVATE_KEY` (private key; its public half in the server's `~/.ssh/authorized_keys`)

**No repo-level domain Variable is used.** The public `/healthz` smoke runs **inside** the SSH loop,
once per instance, against the `SERVICE_DOMAIN` read from **that** instance's `/opt/<dir>/.env` — a
single global `vars.SERVICE_DOMAIN` would cover one domain only and silently skip every other
instance. Smoke is non-fatal (DNS/ACME may still be settling); an instance whose `.env` has no
`SERVICE_DOMAIN` has its smoke skipped for that instance alone.

## Launch checklist — do this before an instance's first deploy
Written for the **first** instance (`claude-ios` / `broadnova.shop`, first entry of the registry
table). It is the **per-instance** shape: a newly added instance runs the same steps against its own
`/opt/<dir>`, its own `SERVICE_DOMAIN` and its own secrets — see `docs/07-deployment.md`
§Мульти-инстанс (clone `.env`-контракт) and the per-instance
§Prod-readiness checklist (must-configure-before-launch), which applies to each live instance
separately.
1. **DNS A-record:** `broadnova.shop` -> `87.239.135.154` (required for Traefik's Let's Encrypt
   ACME challenge; must exist BEFORE launch — Q-017-1 resolved).
2. **GitHub Secrets** (Settings -> Secrets and variables -> Actions): `SSH_HOST=87.239.135.154`,
   `SSH_USER=root`, `SSH_PRIVATE_KEY` (public half in the server's `~/.ssh/authorized_keys`).
3. **Registry row + `$INSTANCES`:** the instance must have its row in `docs/07-deployment.md`
   §CI/CD-контракт: INSTANCES-loop and the matching `dir:project` entry in `$INSTANCES` of both
   workflows — otherwise the automated deploy never visits it (and its smoke never runs).
4. **Server prerequisites** (server owner): external network `docker network create web` (already
   created on 87.239.135.154); stack dir `/opt/<dir>` (`git clone`); `cp .env.prod.example .env`.
5. **Fill `/opt/<dir>/.env`** from the secret manager:
   - `SERVICE_DOMAIN` = **this instance's own domain** from the registry table (the template ships
     the first instance's value, `broadnova.shop`; any other instance overwrites it).
   - `COMPOSE_PROJECT_NAME` = this instance's project name (left unset on `claude-ios`, where the
     default already resolves to `claude-ios`).
   - `TRAEFIK_CERTRESOLVER=le` (already in the template; ACME resolver name in the shared Traefik
     `/opt/edge`, default on the `websecure` entrypoint — Q-017-2 resolved).
   - `TRUSTED_PROXY_IPS` = the `web` network subnet (`docker network inspect web` ->
     `.[0].IPAM.Config[].Subnet`, typically `172.x.0.0/16`).
   - all real secrets (Anthropic / JWT / KMS / DB / Redis / admin / preview / metrics token).
6. **First bring-up** (on the server): `migrate` then `up -d --build` (see below).
7. **Verify:** `curl -fsS https://<that instance's SERVICE_DOMAIN>/healthz` returns `200` once
   Traefik routes and the `le` resolver has issued the TLS cert.

## First deploy / manual usage (run ON the server, in `/opt/<dir>` of THAT instance)
`<dir>` / `<project>` are that instance's row in the registry table (`docs/07-deployment.md`
§CI/CD-контракт: INSTANCES-loop). For `claude-ios`, `-p claude-ios` equals the historical no-`-p`
deploy, so the commands are unchanged for it.
```bash
# 0) one-time prerequisites (server owner): external network + stack dir + .env
docker network create web            # already created on 87.239.135.154
git clone <repo> /opt/<dir>
cp .env.prod.example .env            # then fill .env from the secret manager (see below)

# 1) migrate then bring up the stack (image built on the server)
docker compose -p <project> -f docker-compose.prod.yml --env-file .env run --rm migrate
docker compose -p <project> -f docker-compose.prod.yml --env-file .env up -d --build

# 2) smoke (that instance's own domain, from its .env)
curl -fsS https://${SERVICE_DOMAIN}/healthz     # 200 once Traefik routes + TLS is issued
```

### Required `.env` values to launch (filled on the server, not committed)
- `SERVICE_DOMAIN` — that instance's domain from the registry table (template default is the first
  instance's `broadnova.shop`, Q-017-1 resolved). Its **A-record MUST point to 87.239.135.154
  before launch** (Traefik ACME challenge).
- `TRAEFIK_CERTRESOLVER=le` (Q-017-2 resolved) — the ACME resolver name configured in the shared
  Traefik (`/opt/edge`), default on the `websecure` entrypoint. Already set in `.env.prod.example`.
- `TRUSTED_PROXY_IPS` — the `web` network subnet so per-IP rate limiting sees the real client IP
  from Traefik's `X-Forwarded-For`. Find it on the server:
  `docker network inspect web` -> `.[0].IPAM.Config[].Subnet` (typically `172.x.0.0/16`).
- All secrets (see below).

## Rollback (no registry/immutable tag — ADR-017)
Per instance: run it in the `/opt/<dir>` of the instance being rolled back, with that instance's
`-p <project>`. Other instances stay on the current commit unless rolled back too.
```bash
cd /opt/<dir>
git log --oneline -n 5                # find the previous good commit
git checkout <prev-commit>
docker compose -p <project> -f docker-compose.prod.yml --env-file .env run --rm migrate   # if needed
docker compose -p <project> -f docker-compose.prod.yml --env-file .env up -d --build
curl -fsS https://${SERVICE_DOMAIN}/healthz
# return to the branch tip once a fix is ready: git checkout main && git pull
```

## Secrets
All secrets (`ANTHROPIC_API_KEY`, JWT keys, `KMS_LOCAL_MASTER_KEY`/`KMS_*`, `APPSTORE_*`,
DB creds (`DATABASE_URL`/`POSTGRES_PASSWORD`), `REDIS_URL`, `METRICS_SCRAPE_TOKEN`,
`ADMIN_API_SECRET` (+ `ADMIN_API_SECRET_PREV` during rotation), `PREVIEW_URL_SECRET`) come from
the server's secret manager — never from a committed file or baked into the image
(05-security.md). In prod they live in `.env` in each instance's `/opt/<dir>` (gitignored, own fresh
values per instance — never copied from a sibling), loaded by the
api/migrate containers via `env_file` in `docker-compose.prod.yml`. The isolated secrets (admin
token + preview HMAC + KMS master key) are mutually independent and independent of the
JWT/Anthropic secrets (ADR-009, ADR-010, ADR-003): each is provisioned and rotated separately.
`redaction` (05-security.md) keeps `X-Admin-Token`, `*secret*`, `*token*`, `*key*` out of logs.

**Not a secret:** `TOKEN_PRODUCTS` (consumable productId->credits mapping, ADR-015) and
`SERVICE_DOMAIN`/`TRAEFIK_CERTRESOLVER` are config, not credentials. `TOKEN_PRODUCTS` must MATCH
the IAP products configured in App Store Connect (prod-checklist).

`.env.example` and `.env.prod.example` are placeholder templates only — never real values.

## Pre-commit security checklist (MANDATORY before adding/committing)
```bash
# Each path MUST be reported as ignored (non-empty output + exit 0).
git check-ignore -v .env .env.prod .env.e2e \
  .secrets/e2e/jwt_private_key.pem \
  .secrets/e2e/generated_secrets.txt
```
- All listed paths must be reported as ignored. If any is missing, STOP — do not commit.
- The committed `.env.prod.example` MUST contain only placeholders (`<...>`), never real
  secrets. Sanity-check before commit (each line below should print NOTHING):
  ```bash
  grep -nE 'sk-ant-[A-Za-z0-9]' .env.prod.example      # real Anthropic key
  grep -nE '(ADMIN_API_SECRET|PREVIEW_URL_SECRET|KMS_LOCAL_MASTER_KEY)=[A-Za-z0-9+/]{20,}' .env.prod.example
  grep -nE '(POSTGRES_PASSWORD|METRICS_SCRAPE_TOKEN)=[A-Za-z0-9+/]{16,}' .env.prod.example
  ```
- NEVER use `git add -f` (force) on `.env*` (except `.env*.example`), `.secrets/`, `*.pem`, `*.key`.
- Defense-in-depth: `.gitignore` ignores `.env`, `.env.prod`, `.secrets/`, `*.pem`, `*.key`;
  `.dockerignore` keeps `.env*` (except `.env.example`), `.secrets/`, `*.pem`, `*.key`, and
  `infra/` out of the Docker build context (no proxy config / deploy scripts / secrets baked in).
