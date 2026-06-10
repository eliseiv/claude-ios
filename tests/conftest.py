"""Shared test fixtures: PostgreSQL container, migrations, app client, fakes, JWT factory.

PostgreSQL is real (testcontainers) per 06-testing-strategy.md. Anthropic and StoreKit are
mocked at the client boundary (AsyncAnthropic / StoreKitVerifier) keeping the contract.
Redis-backed rate limiting fails open when no Redis is present, so chat/other endpoints work
in tests without a Redis container; dedicated rate-limit behavior is exercised via the
limiter directly with a fake client.
"""

from __future__ import annotations

import datetime
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

# --- Environment must be set before app.config import (lru_cache settings) ---
# Hermetic test env: the suite MUST NOT depend on a root .env (devops keeps one for the
# docker-compose e2e bring-up, where JWT_ISSUER=claude-ios-e2e / JWT_AUDIENCE=claude-ios and
# a foreign JWT_PUBLIC_KEY would otherwise be loaded by pydantic-settings and 401 every
# authenticated request). Process env (os.environ[...] = ...) outranks the .env file in
# pydantic-settings, so we FORCE every auth/behaviour-defining variable here (not setdefault,
# which would leave a value the .env already injected). Per-test overrides still use
# monkeypatch.setenv on top of these.
_MASTER_KEY_B64 = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="  # 32 bytes base64
os.environ["KMS_LOCAL_MASTER_KEY"] = _MASTER_KEY_B64
os.environ["KMS_KEY_ID"] = ""
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-service-test"
os.environ["APPSTORE_BUNDLE_ID"] = "com.example.app"
os.environ["APPSTORE_ENVIRONMENT"] = "sandbox"
os.environ["APPSTORE_ROOT_CERT_DIR"] = ""
# StoreKit must default to the prod posture (fail-closed real JWS). The .env e2e profile sets
# test-mode true + a secret; that would change StoreKit behaviour for tests that don't patch.
os.environ["STOREKIT_TEST_MODE"] = "false"
os.environ["STOREKIT_TEST_SECRET"] = ""
# API docs default true (test_api_documentation asserts get_settings().docs_enabled is True).
os.environ["DOCS_ENABLED"] = "true"
# Observability: tests expect an unprotected /metrics (200 without a token). The .env e2e
# profile sets METRICS_SCRAPE_TOKEN (=> /metrics 403 without header); force it empty.
os.environ["METRICS_SCRAPE_TOKEN"] = ""
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# JWT: tokens are signed below with an ephemeral RSA key (_PRIVATE_PEM); the service must
# verify with the matching JWT_PUBLIC_KEY and the iss/aud baked into make_jwt(). Force a
# static public-key posture (no JWKS) and a fixed issuer/audience the factory mirrors.
_TEST_JWT_ISSUER = "claude-ios-tests"
_TEST_JWT_AUDIENCE = "claude-ios-tests"
os.environ["JWT_JWKS_URL"] = ""
os.environ["JWT_ISSUER"] = _TEST_JWT_ISSUER
os.environ["JWT_AUDIENCE"] = _TEST_JWT_AUDIENCE

import jwt as pyjwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

# ----------------------------- RSA / JWT key material -----------------------------
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
).decode()
_PUBLIC_PEM = (
    _PRIVATE_KEY.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
)

# Force (not setdefault): a root .env injects a foreign JWT_PUBLIC_KEY whose private half we
# do not hold, so tokens signed with _PRIVATE_PEM would fail signature verification.
os.environ["JWT_PUBLIC_KEY"] = _PUBLIC_PEM


def make_jwt(
    user_id: uuid.UUID | str,
    *,
    device_id: str | None = "dev-1",
    expired: bool = False,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.datetime.now(tz=datetime.UTC)
    exp = now - datetime.timedelta(hours=1) if expired else now + datetime.timedelta(hours=1)
    # iss/aud MUST match the forced JWT_ISSUER/JWT_AUDIENCE so verify() (which checks both
    # when configured) accepts the token regardless of any root .env profile.
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "exp": exp,
        "iat": now,
        "iss": _TEST_JWT_ISSUER,
        "aud": _TEST_JWT_AUDIENCE,
    }
    if device_id is not None:
        claims["device_id"] = device_id
    if extra:
        claims.update(extra)
    return pyjwt.encode(claims, _PRIVATE_PEM, algorithm="RS256")


# ----------------------------- PostgreSQL container -----------------------------
@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        url = pg.get_connection_url()
        os.environ["DATABASE_URL"] = url
        yield url


@pytest.fixture(scope="session")
def _migrated(pg_url: str) -> Iterator[str]:
    """Run alembic migrations once against the container."""
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(cfg, "head")
    yield pg_url


@pytest.fixture
async def _engine(_migrated: str):
    # Function-scoped engine so its asyncpg connections live on the same loop as the test
    # (pytest-asyncio runs each test on a fresh function-scoped loop). The container and
    # migrations remain session-scoped; only the connection pool is per-test.
    engine = create_async_engine(_migrated, future=True, poolclass=NullPool)
    yield engine
    await engine.dispose()


_TABLES = (
    "audit_logs",
    "tool_calls",
    "chat_steps",
    "chat_sessions",
    "byok_keys",
    # user_preferences must be truncated between tests so preferences-integration state does
    # not leak across tests (Figma-gap migration 0004 table; FK→users, but TRUNCATE CASCADE on
    # users would not reset RESTART IDENTITY for it unless listed explicitly).
    "user_preferences",
    "ledger_transactions",
    "wallets",
    "subscriptions",
    "users",
)


@pytest.fixture
async def db_sessionmaker(_engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Per-test clean DB: truncate all tables, yield a sessionmaker bound to the container."""
    maker = async_sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    async with _engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    yield maker


@pytest.fixture
async def db_session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as session:
        yield session


# ----------------------------- Fakes for external clients -----------------------------
class FakeAnthropicClient:
    """In-memory stand-in for AnthropicClient honoring the same contract.

    Scriptable: `responses` is a list of AnthropicResult returned in order on create_message.
    Records every call. validate_key returns `valid_keys` membership.
    """

    def __init__(self) -> None:
        from app.chat.anthropic_client import AnthropicResult, AnthropicUsage

        self._AnthropicResult = AnthropicResult
        self._AnthropicUsage = AnthropicUsage
        self.responses: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.valid_keys: set[str] = set()
        # Keys for which validate_key must report KeyValidation.offline (network/non-401).
        self.offline_keys: set[str] = set()
        self.auth_error_keys: set[str] = set()
        self.raise_upstream = False

    def text_result(self, text: str = "hello") -> Any:
        usage = self._AnthropicUsage(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return self._AnthropicResult(
            stop_reason="end_turn",
            content_blocks=[{"type": "text", "text": text}],
            usage=usage,
            text=text,
            tool_uses=[],
        )

    def parallel_tool_result(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        *,
        text: str = "",
        tool_ids: list[str] | None = None,
    ) -> Any:
        """ADR-025: one assistant turn with MULTIPLE tool_use blocks (parallel tool use).

        ``calls`` is an ordered list of (tool_name, args). content_blocks carry an optional
        leading text block then one tool_use block per call (in order); tool_uses mirrors them.
        Each tool_use.id is a realistic ``toolu_...`` (BUG-4 invariant), distinct per block.
        ``tool_ids`` (optional) pins the provider ids in order.
        """
        usage = self._AnthropicUsage(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        content_blocks: list[dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        tool_uses: list[dict[str, Any]] = []
        for i, (tool_name, args) in enumerate(calls):
            tid = (
                tool_ids[i]
                if tool_ids is not None and i < len(tool_ids)
                else f"toolu_{uuid.uuid4().hex[:24]}"
            )
            block = {"type": "tool_use", "id": tid, "name": tool_name, "input": args}
            content_blocks.append(block)
            tool_uses.append({"id": tid, "name": tool_name, "input": args})
        return self._AnthropicResult(
            stop_reason="tool_use",
            content_blocks=content_blocks,
            usage=usage,
            text=text,
            tool_uses=tool_uses,
        )

    def max_tokens_result(
        self,
        *,
        text: str = "",
        truncated_tool: tuple[str, dict[str, Any]] | None = None,
        tool_id: str | None = None,
        output_tokens: int = 16000,
    ) -> Any:
        """ADR-025: a turn TRUNCATED by the output-token limit (stop_reason="max_tokens").

        Mirrors production: content_blocks may carry a partial text block plus an INCOMPLETE
        tool_use block (e.g. files.write missing ``content``). The orchestrator must NOT execute
        nor surface these blocks; tool_uses is left empty (no executable tool_use on truncation —
        the orchestrator dispatches purely on stop_reason). ``output_tokens`` ≈ the max_tokens cap.
        """
        usage = self._AnthropicUsage(
            input_tokens=1240,
            output_tokens=output_tokens,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        content_blocks: list[dict[str, Any]] = []
        if text:
            content_blocks.append({"type": "text", "text": text})
        if truncated_tool is not None:
            tname, partial_args = truncated_tool
            tid = tool_id or f"toolu_{uuid.uuid4().hex[:24]}"
            content_blocks.append(
                {"type": "tool_use", "id": tid, "name": tname, "input": partial_args}
            )
        return self._AnthropicResult(
            stop_reason="max_tokens",
            content_blocks=content_blocks,
            usage=usage,
            text=text,
            tool_uses=[],
        )

    def tool_result(self, tool_name: str, args: dict[str, Any], tool_id: str | None = None) -> Any:
        # ADR-008 / BUG-4: the raw Anthropic tool_use.id has the realistic "toolu_..." shape, NOT a
        # UUID. The previous UUID-like default masked BUG-4 (domain uuid4 leaking into
        # tool_result.tool_use_id). A toolu_-shaped default makes tests exercise the real semantics:
        # the raw provider id must round-trip unchanged into the continuation history.
        tid = tool_id or f"toolu_{uuid.uuid4().hex[:24]}"
        usage = self._AnthropicUsage(
            input_tokens=10,
            output_tokens=5,
            model="claude-sonnet-4-5",
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
        return self._AnthropicResult(
            stop_reason="tool_use",
            content_blocks=[{"type": "tool_use", "id": tid, "name": tool_name, "input": args}],
            usage=usage,
            text="",
            tool_uses=[{"id": tid, "name": tool_name, "input": args}],
        )

    async def create_message(self, **kwargs: Any) -> Any:
        from app.chat.anthropic_client import AnthropicAuthError
        from app.errors import UpstreamError

        self.calls.append(kwargs)
        api_key = kwargs.get("api_key")
        if self.raise_upstream:
            raise UpstreamError("anthropic upstream error")
        if api_key is not None and api_key in self.auth_error_keys:
            raise AnthropicAuthError("unauthorized")
        if not self.responses:
            return self.text_result()
        return self.responses.pop(0)

    async def validate_key(self, api_key: str) -> Any:
        # ADR-016: production BYOKService.set_key expects a KeyValidation enum
        # (valid|invalid|offline), NOT a bool. Membership in valid_keys → valid; membership in
        # offline_keys → offline (network/non-401); otherwise → invalid (401).
        from app.chat.anthropic_client import KeyValidation

        if api_key in self.offline_keys:
            return KeyValidation.offline
        if api_key in self.valid_keys:
            return KeyValidation.valid
        return KeyValidation.invalid


class FakeStoreKitVerifier:
    """Scriptable StoreKit verifier. raise=True simulates a forged transaction (→422)."""

    def __init__(self) -> None:
        self.next_transaction: Any = None
        self.raise_error = False

    def verify(self, signed_transaction: str) -> Any:
        from app.errors import ValidationFailedError

        if self.raise_error:
            raise ValidationFailedError("StoreKit JWS signature invalid")
        assert self.next_transaction is not None, "FakeStoreKitVerifier not scripted"
        return self.next_transaction


@pytest.fixture
def fake_anthropic() -> FakeAnthropicClient:
    return FakeAnthropicClient()


@pytest.fixture
def fake_storekit() -> FakeStoreKitVerifier:
    return FakeStoreKitVerifier()


# ----------------------------- App client with overrides -----------------------------
@pytest.fixture
async def client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with DB pointed at the container and external clients faked."""
    from app import deps
    from app.api_gateway import rate_limit
    from app.byok import service as byok_service
    from app.chat import anthropic_client as anthropic_mod
    from app.main import create_app
    from app.subscription import storekit as storekit_mod

    # Override the DB dependency to use the container sessionmaker.
    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Patch external-client singletons used by deps wiring.
    anthropic_mod._anthropic_singleton = fake_anthropic  # type: ignore[assignment]
    byok_service.AnthropicClient = type(fake_anthropic)  # type: ignore[misc]
    storekit_mod._verifier_singleton = fake_storekit  # type: ignore[assignment]

    # Rate limiting fails open without Redis; force it explicitly so tests are deterministic.
    async def _allow_chat(**_kwargs: Any) -> bool:
        return True

    async def _allow_other(**_kwargs: Any) -> bool:
        return True

    orig_chat = rate_limit.enforce_chat_limits
    orig_other = rate_limit.enforce_other_limits
    rate_limit.enforce_chat_limits = _allow_chat  # type: ignore[assignment]
    rate_limit.enforce_other_limits = _allow_other  # type: ignore[assignment]
    # The routers imported the names at module load — patch there too.
    from app.api_gateway.routers import byok as byok_router
    from app.api_gateway.routers import chat as chat_router
    from app.api_gateway.routers import subscription as sub_router
    from app.api_gateway.routers import wallet as wallet_router

    chat_router.enforce_chat_limits = _allow_chat  # type: ignore[assignment]
    wallet_router.enforce_other_limits = _allow_other  # type: ignore[assignment]
    byok_router.enforce_other_limits = _allow_other  # type: ignore[assignment]
    sub_router.enforce_other_limits = _allow_other  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    rate_limit.enforce_chat_limits = orig_chat  # type: ignore[assignment]
    rate_limit.enforce_other_limits = orig_other  # type: ignore[assignment]
    chat_router.enforce_chat_limits = orig_chat  # type: ignore[assignment]
    wallet_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    byok_router.enforce_other_limits = orig_other  # type: ignore[assignment]
    sub_router.enforce_other_limits = orig_other  # type: ignore[assignment]


# ----------------------------- DB seeding helpers -----------------------------
async def seed_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    trial_used: bool = False,
    subscription: str | None = None,
    expires_in_hours: float | None = 24,
    balance: int | None = None,
    byok_enabled: bool = False,
    byok_status: str | None = None,
) -> uuid.UUID:
    """Insert a user with optional subscription / wallet / byok rows. Commits."""
    uid = user_id or uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, trial_used) VALUES (:id, :tu)"),
        {"id": str(uid), "tu": trial_used},
    )
    if subscription is not None:
        expires = None
        if expires_in_hours is not None:
            expires = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
                hours=expires_in_hours
            )
        await session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at) "
                "VALUES (:uid, :st, 'pro', :exp)"
            ),
            {"uid": str(uid), "st": subscription, "exp": expires},
        )
    if balance is not None:
        await session.execute(
            text("INSERT INTO wallets (user_id, balance) VALUES (:uid, :bal)"),
            {"uid": str(uid), "bal": balance},
        )
    if byok_status is not None:
        from app.byok.kms import get_kms_client

        kms = get_kms_client()
        import os as _os

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        dek = _os.urandom(32)
        nonce = _os.urandom(12)
        enc_key = AESGCM(dek).encrypt(nonce, b"sk-ant-user-key", None)
        enc_dek = kms.encrypt_dek(dek)
        await session.execute(
            text(
                "INSERT INTO byok_keys (user_id, encrypted_key, encrypted_dek, nonce, "
                "key_status, enabled) VALUES (:uid, :ek, :ed, :n, :ks, :en)"
            ),
            {
                "uid": str(uid),
                "ek": enc_key,
                "ed": enc_dek,
                "n": nonce,
                "ks": byok_status,
                "en": byok_enabled,
            },
        )
    await session.commit()
    return uid


def auth_headers(user_id: uuid.UUID | str, **kwargs: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_jwt(user_id, **kwargs)}"}
