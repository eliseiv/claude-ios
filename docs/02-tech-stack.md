# 02 — Tech Stack

Единственное место, где фиксируются язык, библиотеки, версии и команды. Все остальные агенты берут команды отсюда. Если чего-то нет здесь — это блокер, не угадывай.

## Язык и runtime
| Компонент | Версия | Примечание |
|---|---|---|
| Python | **3.12.x** | целевой `3.12`, CI на `3.12` |
| FastAPI | **0.115.x** | ASGI framework |
| Uvicorn | **0.32.x** (`uvicorn[standard]`) | ASGI server (dev); в prod за Gunicorn |
| Gunicorn | **23.x** | process manager, worker class `uvicorn.workers.UvicornWorker` |
| Pydantic | **2.9.x** | v2, строгая валидация схем |
| pydantic-settings | **2.5.x** | конфиг из env |

## Данные
| Компонент | Версия | Примечание |
|---|---|---|
| PostgreSQL | **16.x** | единственное хранилище состояния |
| SQLAlchemy | **2.0.x** | async ORM (`AsyncSession`) |
| asyncpg | **0.29.x** | async драйвер PostgreSQL |
| Alembic | **1.13.x** | миграции |
| Redis | **7.x** | rate limiting, idempotency-метки, policy cache |
| redis-py | **5.x** | async client (`redis.asyncio`) |

## Внешние SDK / интеграции
| Компонент | Версия | Назначение |
|---|---|---|
| anthropic | **0.39.x** (Python SDK) | Claude messages API + prompt caching |
| httpx | **0.27.x** | App Store Server API, прочие HTTP |
| pyjwt | **2.9.x** | JWT verify (или App Store JWS) |
| cryptography | **43.x** | AES-GCM envelope encryption (data layer); StoreKit JWS x5c chain/signature verify |

> **Admin-auth и preview signed URL новых внешних зависимостей не требуют.** HMAC-SHA256 для preview signed URL и
> constant-time сравнение admin/preview-токенов — stdlib `hmac`/`hashlib`/`secrets` (Python 3.12). `cryptography` (уже в стеке)
> при необходимости. См. [ADR-009](adr/ADR-009-admin-token-auth.md), [ADR-010](adr/ADR-010-backend-hosted-preview.md).
| prometheus-client | **0.21.x** (`>=0.21,<0.22`) | Prometheus exposition для `GET /metrics` (observability cross-cutting) |
| pypdf | **5.x** | извлечение текста из PDF-вложений (модуль attachments, [ADR-014](adr/ADR-014-multimodal-attachments.md)). Pure-Python, без системных зависимостей. |
| python-multipart | **0.0.x** (актуальная) | парсинг `multipart/form-data` для `POST /v1/attachments` (загрузка бинаря, [ADR-014](adr/ADR-014-multimodal-attachments.md)). Требуется FastAPI для form/file. |

> **Расширение Figma-gap (2026-06-02):** добавлены `pypdf` (извлечение `extracted_text` из PDF, модуль attachments) и `python-multipart` (multipart-upload вложений). Прочие новые модули (chats/profile/preferences/workspaces/snippets/token-purchase/notifications) **новых внешних зависимостей не требуют** — используют существующий стек (FastAPI/SQLAlchemy/Pydantic, stdlib `hmac`/`hashlib`/`secrets` для accountId-производной). Определение media_type вложений по magic bytes — stdlib (`mimetypes`/собственная сигнатурная проверка), без новой зависимости. APNs-отправка push отложена ([TD-011](100-known-tech-debt.md)) — APNs-зависимость добавится при её реализации, не в этом проходе.

### Модели Claude (значения по умолчанию)
- Оркестрация: `claude-sonnet-4-5` (значение конфигурируемо через env `ANTHROPIC_MODEL`).
- Prompt caching: включён через `cache_control` на системном промте и стабильном контексте.
- Точную модель/версию pin фиксирует Chat Orchestrator config; пользователь BYOK использует ту же модель, но со своим ключом.

## Инструменты разработки
| Инструмент | Версия | Роль |
|---|---|---|
| uv | **0.4.30** (CI pin) | менеджер зависимостей и venv (`uv.lock`). CI запинен на `0.4.30` (`.github/workflows/ci.yml`, `UV_VERSION`); локальное dev-окружение может быть новее (наблюдалось `0.11.6`) — `uv.lock` обеспечивает идентичность зависимостей независимо от версии uv. Каноничен CI-pin `0.4.30`. |
| Ruff | **0.7.x** | linter + formatter |
| mypy | **1.11.x** | статическая типизация (strict для бизнес-логики) |
| pytest | **8.x** | тесты |
| pytest-asyncio | **0.24.x** | async тесты |
| pytest-cov | **5.x** | coverage |
| testcontainers | **4.x** | реальный PostgreSQL/Redis в интеграционных тестах |
| respx | **0.21.x** | мок HTTP (Anthropic/Apple) в unit-тестах |

## Команды (канонические — использовать именно их)
> Запускаются из корня репозитория. Менеджер — `uv`.

```bash
# install deps
uv sync

# format (пишет изменения)
uv run ruff format .

# format check (CI, без записи)
uv run ruff format --check .

# lint
uv run ruff check .

# lint с автофиксом
uv run ruff check --fix .

# type-check
uv run mypy src

# тесты + покрытие
uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=80

# миграции
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "<message>"

# запуск приложения (dev)
uv run uvicorn app.main:app --reload

# запуск (prod)
uv run gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4
```

## Структура проекта (фактическая)
```
src/app/
  main.py                 # FastAPI app factory: middleware, routers, exception handlers
  config.py               # pydantic-settings (Settings, get_settings)
  db.py                   # async engine, sessionmaker, dispose_engine
  deps.py                 # FastAPI dependencies (auth/JWT, db session, correlation id)
  errors.py               # AppError иерархия (ValidationFailedError и пр.)
  api_gateway/
    auth.py               # JWT (RS256) verify, JWKS; require_admin (X-Admin-Token, изолирован от get_current_user — ADR-009)
    middleware.py         # CorrelationId, SecurityHeaders, SizeLimit
    rate_limit.py         # Redis rate limiting, redis_ping, close_redis
    routers/              # chat, policy, wallet, subscription, byok, health, admin (/v1/admin/*), preview (/v1/preview/*)
  chat/                   # orchestrator, anthropic_client, repository, tools (client-side files.*/calendar.*/reminders.* + server-side site.* dispatch)
  admin/                  # service-обёртка над Wallet.grant/get_wallet_view (ADR-009)
  website/                # projects/site_files service, signed URL (HMAC), site.* tool-хэндлеры (server-side, ADR-010/011)
  policy/                 # engine (pure), loader (state из репозиториев)
  wallet/                 # service (ledger, consume, grant)
  subscription/           # service, storekit (JWS x5c chain/signature verify)
  byok/                   # service (envelope encrypt/toggle/delete), kms (KmsClient + LocalKmsClient)
  audit/                  # service (append-only logging)
  observability/          # metrics (prometheus), logging, context (request id), redaction
  models/                 # base, tables (17 SQLAlchemy таблиц: 9 базовых + projects, site_files + расширение Figma-gap: user_preferences, workspace_projects, workspace_files, snippets, attachments, device_push_tokens)
  schemas/                # Pydantic request/response (chat, policy, wallet, subscription, byok, common)
  # Расширение Figma-gap (новые пакеты — каждый со своим router/service/repository):
  chats/                  # CRUD/список/поиск/steps-view поверх chat_sessions (модуль chats)
  profile/                # displayName + производный accountId
  preferences/            # default_assistant_mode, notif toggle, code defaults
  workspaces/             # рабочие пространства чатов (≠ website projects, ADR-013)
  snippets/               # сохранённые код-фрагменты
  attachments/            # мультимодальные вложения (upload, extract_text, резолв в Anthropic content; ADR-014)
  token_purchase/         # consumable IAP → grant кредитов (reuse storekit verifier + Wallet, ADR-015)
  notifications/          # device push-token CRUD (отправка push → TD-011)
migrations/               # alembic
tests/
  unit/  integration/  e2e/
pyproject.toml  uv.lock
```

> Служебные маршруты `/health`, `/healthz` (алиас `/health`, [ADR-017](adr/ADR-017-shared-server-traefik-deploy.md)), `/ready`, `/metrics` — в `api_gateway/routers/health.py` (см. [07-deployment.md](07-deployment.md#health--readiness)).

## Соглашения по коду
- Форматирование и линт — Ruff (line length 100). Конфиг в `pyproject.toml`.
- mypy `strict = true` для пакетов `policy`, `wallet`, `byok`, `chat`. Остальное — `disallow_untyped_defs`.
- Pydantic v2 модели для всех request/response; запрет «голых» dict на границе API.
- Async везде, где есть I/O (БД, Redis, HTTP).
- Идемпотентность через ключи в Redis + уникальные индексы в PostgreSQL (не полагаться только на одно из двух).
