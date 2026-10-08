"""Integration: креденшлы и строки провайдера/StoreKit/CloudPayments/карт из CRM (ADR-116).

Реальный PostgreSQL (testcontainers), реальные ручки ``/v1/admin/credentials`` и
``/v1/admin/settings``, реальный снимок и настоящие потребители. Нормы — ADR-116 §2–§5 и
``docs/modules/admin/02-api-contracts.md`` (§GET/PATCH /v1/admin/credentials).

Что здесь доказывается и почему не компонентом:

- ветки отказа записи — по коду ответа И по метке ``admin_override_rejected_total`` (двусторонне:
  своя серия выросла на 1, ни одна соседняя не тронута);
- межэлементные инварианты §4.3 — кейс отказа и кейс успеха на каждый;
- значение не выходит наружу НИ одним каналом: ответ, список, аудит, лог, текст отказа, БД;
- правка применяется без перезапуска: `PATCH` → снимок процесса → следующий вызов фабрики
  клиента видит новый ключ, потребитель (вебхук, страница оплаты, ход чата) — новую величину.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_ADMIN_SECRET = "cred-admin-key-integration-0123456789abcdef"
_H = {"X-Admin-Key": _ADMIN_SECRET, "X-Admin-Actor": "operator@example.com"}
# Узнаваемое значение: его появление в любом канале — доказательство утечки, а не совпадение.
_MARKER = "sk-LEAKMARKER-116-zz9"
_REASONS = (
    "unknown_id",
    "type_mismatch",
    "out_of_range",
    "source_kind_missing",
    "undeclared_bound",
    "unsupported_field",
    "conflict",
    "environment_missing",
)


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _rejected(reason: str, scope: str = "credentials") -> float:
    value = REGISTRY.get_sample_value(
        "admin_override_rejected_total", {"scope": scope, "reason": reason}
    )
    return 0.0 if value is None else value


def _undecryptable(credential_id: str) -> float:
    value = REGISTRY.get_sample_value(
        "admin_credential_undecryptable_total", {"credential_id": credential_id}
    )
    return 0.0 if value is None else value


# ------------------------------ окружение и клиент -----------------------------------------
class _FakeRedis:
    """Счётчик корзин в памяти: лимитер настоящий (fail-open сделал бы `429` недостижимым)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self.counts)


class _FakePipeline:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self._ops: list[tuple[str, str]] = []

    def zremrangebyscore(self, key: str, *_a: Any) -> None:
        self._ops.append(("noop", key))

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self._counts[key] = self._counts.get(key, 0) + 1
        self._ops.append(("noop", key))

    def zcard(self, key: str) -> None:
        self._ops.append(("card", key))

    def expire(self, key: str, _ttl: int) -> None:
        self._ops.append(("noop", key))

    async def execute(self) -> list[Any]:
        return [self._counts.get(key, 0) if op == "card" else None for op, key in self._ops]

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None


_BASE_ENV = {
    "ADMIN_API_SECRET": _ADMIN_SECRET,
    "ADMIN_API_SECRET_PREV": "",
    "ADMIN_API_KEY": "",
    "LLM_PROVIDER": "anthropic",
    "OPENAI_API_KEY": "",
    # Отдельный секрет подписи колбэков задан: запись `proxy.api_key` штатна.
    "PROXY_WEBHOOK_SECRET": "whsec-env-116",
    "ADAPTY_WEBHOOK_SECRET": "adapty-env-secret",
    "FAL_API_KEY": "",
}


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("app.instance_config", "app.admin.economics", "app.subscription.storekit"):
        logging.getLogger(name).disabled = False
    _env(monkeypatch, **_BASE_ENV)
    yield
    get_settings.cache_clear()


@pytest.fixture
async def admin(
    _admin_env: None, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[AsyncClient, _FakeRedis]]:
    from app.api_gateway import rate_limit

    redis = _FakeRedis()
    monkeypatch.setattr(rate_limit, "get_redis", lambda: redis)
    yield client, redis


async def _patch_cred(client: AsyncClient, credential_id: str, value: Any) -> Any:
    return await client.patch(
        f"/v1/admin/credentials/{credential_id}", json={"value": value}, headers=_H
    )


async def _patch_setting(client: AsyncClient, setting_id: str, value: Any) -> Any:
    return await client.patch(f"/v1/admin/settings/{setting_id}", json={"value": value}, headers=_H)


async def _items(client: AsyncClient) -> dict[str, dict[str, Any]]:
    body = (await client.get("/v1/admin/credentials", headers=_H)).json()
    return {item["credential_id"]: item for item in body["items"]}


async def _rows(maker: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with maker() as s:
        return list(
            (
                await s.execute(
                    text(
                        "SELECT credential_id, encrypted_value, encrypted_dek, fingerprint, "
                        "updated_at FROM admin_credentials ORDER BY credential_id"
                    )
                )
            ).all()
        )


async def _audit(maker: async_sessionmaker[AsyncSession], event_type: str) -> list[Any]:
    async with maker() as s:
        return [
            r[0]
            for r in (
                await s.execute(
                    text("SELECT payload FROM audit_logs WHERE event_type=:t ORDER BY created_at"),
                    {"t": event_type},
                )
            ).all()
        ]


async def _refresh(maker: async_sessionmaker[AsyncSession]) -> bool:
    from app.instance_config.snapshot import refresh_snapshot

    async with maker() as s:
        return await refresh_snapshot(s)


def _effective() -> Any:
    from app.instance_config.effective import get_effective_settings

    return get_effective_settings()


# ============================== ветки отказа записи (§2.3) =================================
_BRANCHES: tuple[tuple[str, str, Any, int, str], ...] = (
    ("unknown_id", "no.such.credential", "sk-x", 400, "unknown_id"),
    ("int", "fal.api_key", 5, 422, "type_mismatch"),
    ("bool", "fal.api_key", True, 422, "type_mismatch"),
    ("object", "fal.api_key", {"value": "sk-x"}, 422, "type_mismatch"),
    ("len_513", "fal.api_key", "a" * 513, 422, "out_of_range"),
    ("space", "fal.api_key", "sk key", 400, "undeclared_bound"),
    ("newline", "fal.api_key", "sk\nkey", 400, "undeclared_bound"),
    ("tab", "fal.api_key", "sk\tkey", 400, "undeclared_bound"),
    ("control", "fal.api_key", "sk\x01key", 400, "undeclared_bound"),
    # Основной ключ выбранного провайдера (anthropic) не может стать пустым.
    ("primary_emptied", "anthropic.api_key", "", 400, "conflict"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credential_id", "value", "status", "reason"),
    [row[1:] for row in _BRANCHES],
    ids=[row[0] for row in _BRANCHES],
)
async def test_each_refusal_is_one_code_and_one_reason_and_writes_nothing(
    admin: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    credential_id: str,
    value: Any,
    status: int,
    reason: str,
) -> None:
    """ADR-116 §2.3: одна ветка — один код и один `reason`; ни одна соседняя серия не растёт."""
    client, _ = admin
    before = {r: _rejected(r) for r in _REASONS}

    response = await _patch_cred(client, credential_id, value)

    assert response.status_code == status, response.text
    assert _rejected(reason) == before[reason] + 1
    assert {r: _rejected(r) - before[r] for r in _REASONS if r != reason} == {
        r: 0.0 for r in _REASONS if r != reason
    }
    assert await _rows(db_sessionmaker) == []
    assert await _audit(db_sessionmaker, "admin_credential_set") == []


@pytest.mark.asyncio
async def test_the_declared_max_length_itself_is_accepted(admin: Any) -> None:
    """Граница `max_length: 512` объявлена включительно: 512 — `200`, 513 — `422` (выше)."""
    client, _ = admin

    body = (await client.get("/v1/admin/credentials", headers=_H)).json()["items"]
    assert {item["constraints"]["max_length"] for item in body} == {512}

    response = await _patch_cred(client, "fal.api_key", "a" * 512)
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_proxy_key_without_a_separate_callback_secret_is_environment_missing(
    admin: Any, monkeypatch: pytest.MonkeyPatch, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """`proxy.api_key` при пустом `PROXY_WEBHOOK_SECRET` — `400 environment_missing` (§4.3)."""
    client, _ = admin
    _env(monkeypatch, PROXY_WEBHOOK_SECRET="")
    before = {r: _rejected(r) for r in _REASONS}

    refused = await _patch_cred(client, "proxy.api_key", "px-new")

    assert refused.status_code == 400, refused.text
    assert _rejected("environment_missing") == before["environment_missing"] + 1
    assert _rejected("conflict") == before["conflict"]
    assert await _rows(db_sessionmaker) == []

    _env(monkeypatch, PROXY_WEBHOOK_SECRET="whsec-env-116")
    assert (await _patch_cred(client, "proxy.api_key", "px-new")).status_code == 200


@pytest.mark.asyncio
async def test_without_the_master_key_a_string_is_environment_missing_and_null_still_passes(
    admin: Any, monkeypatch: pytest.MonkeyPatch, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """§2.3: пуст `KMS_LOCAL_MASTER_KEY` — строку зашифровать нечем: `400`, в БД ничего."""
    from app.byok import kms as kms_mod

    client, _ = admin
    monkeypatch.setattr(kms_mod, "_kms_singleton", None)
    _env(monkeypatch, KMS_LOCAL_MASTER_KEY="")
    before = _rejected("environment_missing")

    refused = await _patch_cred(client, "fal.api_key", "fal-new")

    assert refused.status_code == 400, refused.text
    assert _rejected("environment_missing") == before + 1
    assert await _rows(db_sessionmaker) == []
    # Удаление строки мастер-ключа не требует.
    cleared = await _patch_cred(client, "fal.api_key", None)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["changed"] is False


@pytest.mark.asyncio
async def test_a_lost_first_write_race_is_409_not_500(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Гонка первой записи: второй `INSERT` падает на PK → `409 conflict` (§2.3)."""
    from app.admin.economics_service import AdminEconomicsService
    from app.models import AdminCredential
    from app.schemas.admin_economics import AdminCredentialPatchRequest

    client, _ = admin
    assert (await _patch_cred(client, "fal.api_key", "fal-winner")).status_code == 200
    before = _rejected("conflict")

    async with db_sessionmaker() as session:
        service = AdminEconomicsService(session, get_settings())
        original = session.scalar

        async def _blind(statement: Any, *args: Any, **kwargs: Any) -> Any:
            entity = getattr(statement, "column_descriptions", [{}])
            if entity and entity[0].get("entity") is AdminCredential:
                return None  # конкурент уже вставил, а мы не видим
            return await original(statement, *args, **kwargs)

        session.scalar = _blind  # type: ignore[method-assign]
        with pytest.raises(Exception) as raised:  # noqa: B017 — важен status_code
            await service.patch_credential(
                "fal.api_key", AdminCredentialPatchRequest(value="fal-loser"), actor_claim="x"
            )

    assert getattr(raised.value, "status_code", None) == 409, repr(raised.value)
    assert _rejected("conflict") == before + 1
    assert len(await _audit(db_sessionmaker, "admin_credential_set")) == 1


# ============================== инварианты §4.3: отказ и успех ============================
@pytest.mark.asyncio
async def test_switching_the_provider_needs_a_primary_key_of_the_target(admin: Any) -> None:
    client, _ = admin

    refused = await _patch_setting(client, "llm.provider", "openai")
    assert refused.status_code == 400, refused.text

    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200
    accepted = await _patch_setting(client, "llm.provider", "openai")
    assert accepted.status_code == 200, accepted.text
    assert _effective().llm_provider == "openai"


@pytest.mark.asyncio
async def test_a_backup_key_alone_is_not_an_active_key(admin: Any) -> None:
    """«Действующий ключ» — непустой ОСНОВНОЙ; резервный его не заменяет (§4.3)."""
    client, _ = admin
    assert (await _patch_cred(client, "openai.api_key_backup", "sk-backup")).status_code == 200

    assert (await _patch_setting(client, "llm.provider", "openai")).status_code == 400
    assert (await _patch_setting(client, "llm.dual_enabled", True)).status_code == 400


@pytest.mark.asyncio
async def test_enabling_dual_needs_a_primary_key_of_the_second_provider(admin: Any) -> None:
    client, _ = admin
    before = _rejected("conflict", "settings")

    refused = await _patch_setting(client, "llm.dual_enabled", True)
    assert refused.status_code == 400, refused.text
    assert _rejected("conflict", "settings") == before + 1

    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200
    accepted = await _patch_setting(client, "llm.dual_enabled", True)
    assert accepted.status_code == 200, accepted.text
    assert set(_effective().credits_providers()) == {"anthropic", "openai"}
    # Выключение второго провайдера не отвергается никогда.
    assert (await _patch_setting(client, "llm.dual_enabled", False)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", None], ids=["empty_string", "null_with_empty_env"])
async def test_the_primary_key_of_the_selected_provider_cannot_be_emptied(
    admin: Any, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    client, _ = admin
    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200
    assert (await _patch_setting(client, "llm.provider", "openai")).status_code == 200

    refused = await _patch_cred(client, "openai.api_key", value)

    assert refused.status_code == 400, refused.text
    assert _effective().openai_api_key == "sk-openai-crm"


@pytest.mark.asyncio
async def test_null_on_the_selected_primary_passes_when_env_still_holds_a_key(
    admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = admin
    _env(monkeypatch, OPENAI_API_KEY="sk-openai-env")
    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200
    assert (await _patch_setting(client, "llm.provider", "openai")).status_code == 200

    cleared = await _patch_cred(client, "openai.api_key", None)

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["source"] == "env"
    assert _effective().openai_api_key == "sk-openai-env"


@pytest.mark.asyncio
async def test_the_second_provider_under_dual_is_protected_too(admin: Any) -> None:
    client, _ = admin
    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200
    assert (await _patch_setting(client, "llm.dual_enabled", True)).status_code == 200

    assert (await _patch_cred(client, "openai.api_key", "")).status_code == 400
    # Не выбранный и не второй провайдер — правка свободна.
    assert (await _patch_setting(client, "llm.dual_enabled", False)).status_code == 200
    assert (await _patch_cred(client, "openai.api_key", "")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_id", ["anthropic.api_key_backup", "openai.api_key_backup"])
async def test_emptying_a_backup_key_is_never_refused(admin: Any, credential_id: str) -> None:
    client, _ = admin
    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200
    assert (await _patch_setting(client, "llm.dual_enabled", True)).status_code == 200

    assert (await _patch_cred(client, credential_id, "")).status_code == 200
    assert (await _patch_cred(client, credential_id, None)).status_code == 200


@pytest.fixture
def apple_roots(tmp_path: Any) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture-apple-root")])
    now = datetime.datetime.now(tz=datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "AppleRootCA-G3.cer").write_bytes(cert.public_bytes(Encoding.DER))
    return str(tmp_path)


@pytest.mark.asyncio
async def test_production_mode_checks_bundle_first_and_roots_second(
    admin: Any, monkeypatch: pytest.MonkeyPatch, apple_roots: str
) -> None:
    """§4.3: `conflict` (пуст bundle) раньше `environment_missing` (нет корней Apple)."""
    client, _ = admin
    _env(monkeypatch, APPSTORE_BUNDLE_ID="", APPSTORE_ROOT_CERT_DIR="")

    both_missing = await _patch_setting(client, "storekit.mode", "production")
    assert both_missing.status_code == 400
    before = {r: _rejected(r, "settings") for r in _REASONS}

    _env(monkeypatch, APPSTORE_BUNDLE_ID="com.own.app")
    roots_missing = await _patch_setting(client, "storekit.mode", "production")
    assert roots_missing.status_code == 400, roots_missing.text
    assert _rejected("environment_missing", "settings") == before["environment_missing"] + 1
    assert _rejected("conflict", "settings") == before["conflict"]

    _env(monkeypatch, APPSTORE_BUNDLE_ID="", APPSTORE_ROOT_CERT_DIR=apple_roots)
    bundle_missing = await _patch_setting(client, "storekit.mode", "production")
    assert bundle_missing.status_code == 400
    assert _rejected("conflict", "settings") == before["conflict"] + 1

    _env(monkeypatch, APPSTORE_BUNDLE_ID="com.own.app")
    accepted = await _patch_setting(client, "storekit.mode", "production")
    assert accepted.status_code == 200, accepted.text


@pytest.mark.asyncio
async def test_the_order_puts_conflict_before_environment_missing_when_both_are_true(
    admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = admin
    _env(monkeypatch, APPSTORE_BUNDLE_ID="", APPSTORE_ROOT_CERT_DIR="")
    before = {r: _rejected(r, "settings") for r in _REASONS}

    response = await _patch_setting(client, "storekit.mode", "production")

    assert response.status_code == 400
    assert _rejected("conflict", "settings") == before["conflict"] + 1
    assert _rejected("environment_missing", "settings") == before["environment_missing"]


@pytest.mark.asyncio
async def test_the_bundle_cannot_be_emptied_under_production_but_can_without_a_mode_row(
    admin: Any, monkeypatch: pytest.MonkeyPatch, apple_roots: str
) -> None:
    client, _ = admin
    _env(monkeypatch, APPSTORE_BUNDLE_ID="com.own.app", APPSTORE_ROOT_CERT_DIR=apple_roots)

    # Без строки `storekit.mode` (value=null на этом env) правка bundle в "" свободна.
    items = {
        i["setting_id"]: i
        for i in (await client.get("/v1/admin/settings", headers=_H)).json()["items"]
    }
    assert items["storekit.mode"]["value"] is None
    free = await _patch_setting(client, "storekit.bundle_id", "")
    assert free.status_code == 200, free.text
    assert (await _patch_setting(client, "storekit.bundle_id", "com.own.app")).status_code == 200

    assert (await _patch_setting(client, "storekit.mode", "production")).status_code == 200
    refused = await _patch_setting(client, "storekit.bundle_id", "")
    assert refused.status_code == 400, refused.text


# ============================== storekit.mode в GET /settings и лог перехода ==============
_NULL_PHRASE = (
    "Пусто — режим из CRM не задан, а настройки сервера не совпадают ни с одним из двух "
    "режимов; выбор режима заменит их"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "expected"),
    [
        (
            {
                "APPSTORE_ENVIRONMENT": "sandbox",
                "STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION": "true",
                "STOREKIT_TEST_MODE": "true",
                "APPSTORE_BUNDLE_ID": "",
            },
            "sandbox",
        ),
        (
            {
                "APPSTORE_ENVIRONMENT": "production",
                "STOREKIT_TEST_MODE": "false",
                "APPSTORE_BUNDLE_ID": "com.own.app",
            },
            "production",
        ),
        (
            {
                "APPSTORE_ENVIRONMENT": "sandbox",
                "STOREKIT_TEST_MODE": "false",
                "APPSTORE_BUNDLE_ID": "com.own.app",
            },
            None,
        ),
    ],
    ids=["exact_sandbox", "exact_production", "other"],
)
async def test_the_mode_row_shows_a_mode_only_on_an_exact_match_and_explains_the_null(
    admin: Any, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str | None
) -> None:
    client, _ = admin
    _env(monkeypatch, **env)

    items = {
        i["setting_id"]: i
        for i in (await client.get("/v1/admin/settings", headers=_H)).json()["items"]
    }

    assert items["storekit.mode"]["value"] == expected
    assert items["storekit.mode"]["options"] and {
        o["value"] for o in items["storekit.mode"]["options"]
    } == {
        "sandbox",
        "production",
    }
    assert _NULL_PHRASE in items["storekit.mode"]["description"]


@pytest.mark.asyncio
async def test_switching_to_sandbox_is_logged_as_a_warning_and_never_leaks_a_secret(
    admin: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """§6 (в): переход в `sandbox` — событие `admin_storekit_mode_changed` — WARNING."""
    client, _ = admin

    with caplog.at_level(logging.INFO, logger="app.admin.economics"):
        assert (await _patch_setting(client, "storekit.mode", "sandbox")).status_code == 200
        assert (await _patch_setting(client, "storekit.mode", "sandbox")).status_code == 200

    events = [r for r in caplog.records if r.getMessage() == "admin_storekit_mode_changed"]
    assert len(events) == 1  # повтор без перехода — без события
    assert events[0].levelno == logging.WARNING


# ============================== значение не утекает ни одним каналом =====================
@pytest.mark.asyncio
async def test_the_value_never_leaves_through_response_list_audit_log_or_refusal(
    admin: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = admin

    with caplog.at_level(logging.DEBUG):
        written = await _patch_cred(client, "fal.api_key", _MARKER)
        listed = await client.get("/v1/admin/credentials", headers=_H)
        too_long = await _patch_cred(client, "fal.api_key", _MARKER + "x" * 512)
        with_space = await _patch_cred(client, "fal.api_key", _MARKER + " tail")
        cleared = await _patch_cred(client, "fal.api_key", None)

    assert written.status_code == 200 and cleared.status_code == 200
    for response in (written, listed, too_long, with_space, cleared):
        assert _MARKER not in response.text, response.request.url
    audit = await _audit(db_sessionmaker, "admin_credential_set") + await _audit(
        db_sessionmaker, "admin_credential_cleared"
    )
    assert len(audit) == 2
    assert _MARKER not in repr(audit)
    rendered = "\n".join(
        f"{r.getMessage()} {getattr(r, 'extra_fields', '')} {r.args!r}" for r in caplog.records
    )
    assert "admin_override_applied" in rendered  # лог правки есть — и значения в нём нет
    assert _MARKER not in rendered


@pytest.mark.asyncio
async def test_the_audit_names_what_changed_by_id_source_and_fingerprint(
    admin: Any, monkeypatch: pytest.MonkeyPatch, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """§2.5: ключи детали — `scope`, `id`, `source` и `fingerprint` «до->после», `actorClaim`."""
    client, _ = admin
    _env(monkeypatch, FAL_API_KEY="fal-env")

    assert (await _patch_cred(client, "fal.api_key", "fal-crm")).status_code == 200
    assert (await _patch_cred(client, "fal.api_key", None)).status_code == 200

    [set_event] = await _audit(db_sessionmaker, "admin_credential_set")
    [cleared_event] = await _audit(db_sessionmaker, "admin_credential_cleared")
    set_event.pop("requestId", None)  # сквозной идентификатор запроса ставит сам аудит
    assert set_event == {
        "scope": "credentials",
        "id": "fal.api_key",
        "source": "env->overlay",
        "fingerprint": f"{_fp('fal-env')}->{_fp('fal-crm')}",
        "actorClaim": "operator@example.com",
    }
    assert cleared_event["source"] == "overlay->env"
    assert cleared_event["fingerprint"] == f"{_fp('fal-crm')}->{_fp('fal-env')}"


@pytest.mark.asyncio
async def test_the_list_describes_source_fingerprint_and_configured_without_values(
    admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = admin
    _env(monkeypatch, FAL_API_KEY="fal-env", PROXY_API_KEY="")

    assert (await _patch_cred(client, "anthropic.api_key_backup", "")).status_code == 200
    assert (await _patch_cred(client, "cloudpayments.api_token", "cp-crm")).status_code == 200
    items = await _items(client)

    assert len(items) == 10
    assert (items["fal.api_key"]["source"], items["fal.api_key"]["fingerprint"]) == (
        "env",
        _fp("fal-env"),
    )
    assert items["fal.api_key"]["configured"] is True
    assert items["fal.api_key"]["updated_at"] is None
    assert items["proxy.api_key"] == {
        **items["proxy.api_key"],
        "source": "unset",
        "fingerprint": None,
        "configured": False,
    }
    # Пустая строка в оверлее — «явно выключено»: источник overlay, но не configured.
    empty = items["anthropic.api_key_backup"]
    assert (empty["source"], empty["configured"], empty["fingerprint"]) == (
        "overlay",
        False,
        _fp(""),
    )
    assert empty["updated_at"] is not None
    assert items["cloudpayments.api_token"]["fingerprint"] == _fp("cp-crm")
    assert set(items["fal.api_key"]) == {
        "credential_id",
        "label",
        "group",
        "description",
        "constraints",
        "configured",
        "source",
        "fingerprint",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_only_ciphertext_reaches_the_database(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = admin

    assert (await _patch_cred(client, "fal.api_key", _MARKER)).status_code == 200

    [row] = await _rows(db_sessionmaker)
    assert row[0] == "fal.api_key"
    assert _MARKER.encode() not in bytes(row[1])
    assert _MARKER.encode() not in bytes(row[2])
    assert row[3] == _fp(_MARKER)
    async with db_sessionmaker() as s:
        dumped = await s.scalar(text("SELECT row_to_json(c)::text FROM admin_credentials c"))
    assert _MARKER not in str(dumped)


@pytest.mark.asyncio
async def test_a_row_moved_under_another_id_does_not_decrypt_and_env_stays_in_force(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """AAD: шифротекст `fal.api_key`, переставленный в `anthropic.api_key`, мёртв (§2.2)."""
    from app.instance_config.snapshot import reset_snapshot

    client, _ = admin
    assert (await _patch_cred(client, "fal.api_key", "fal-crm")).status_code == 200
    async with db_sessionmaker() as s:
        await s.execute(text("UPDATE admin_credentials SET credential_id='anthropic.api_key'"))
        await s.commit()
    reset_snapshot()  # холодный старт
    before = _undecryptable("anthropic.api_key")

    assert await _refresh(db_sessionmaker)

    assert _undecryptable("anthropic.api_key") == before + 1
    assert _effective().anthropic_api_key == get_settings().anthropic_api_key  # env
    assert _effective().anthropic_api_key != "fal-crm"


# ============================== идемпотентность ==========================================
@pytest.mark.asyncio
async def test_repeating_the_same_value_is_changed_false_without_touching_the_row(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = admin
    first = await _patch_cred(client, "fal.api_key", "fal-crm")
    [row_before] = await _rows(db_sessionmaker)

    repeat = await _patch_cred(client, "fal.api_key", "fal-crm")

    assert first.json()["changed"] is True
    assert repeat.status_code == 200 and repeat.json()["changed"] is False
    [row_after] = await _rows(db_sessionmaker)
    assert row_after == row_before  # ни шифротекст, ни отметка не сдвинулись
    assert len(await _audit(db_sessionmaker, "admin_credential_set")) == 1


@pytest.mark.asyncio
async def test_null_without_a_row_is_changed_false_and_writes_no_audit(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = admin

    response = await _patch_cred(client, "fal.api_key", None)

    assert response.status_code == 200, response.text
    assert response.json()["changed"] is False
    assert await _audit(db_sessionmaker, "admin_credential_cleared") == []


@pytest.mark.asyncio
async def test_null_with_a_row_deletes_it_and_env_is_back(
    admin: Any, monkeypatch: pytest.MonkeyPatch, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client, _ = admin
    _env(monkeypatch, FAL_API_KEY="fal-env")
    assert (await _patch_cred(client, "fal.api_key", "fal-crm")).status_code == 200
    assert _effective().fal_api_key == "fal-crm"

    cleared = await _patch_cred(client, "fal.api_key", None)

    assert cleared.json()["changed"] is True
    assert cleared.json()["source"] == "env"
    assert await _rows(db_sessionmaker) == []
    assert _effective().fal_api_key == "fal-env"
    assert len(await _audit(db_sessionmaker, "admin_credential_cleared")) == 1


# ============================== применение без перезапуска ================================
@pytest.fixture
def real_factories(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app import deps
    from app.chat import llm_client
    from app.memory import embedding

    for name in ("_openai_singleton", "_openai_built"):
        monkeypatch.setattr(llm_client, name, None)
    monkeypatch.setenv("MEMORY_EMBEDDING_FAKE", "false")
    get_settings.cache_clear()
    for factory in (
        deps.get_speech_client,
        deps.get_moderation_service,
        embedding.get_embedding_client,
    ):
        factory.cache_clear()
    yield
    for factory in (
        deps.get_speech_client,
        deps.get_moderation_service,
        embedding.get_embedding_client,
    ):
        factory.cache_clear()


def _inner_key(client: Any) -> str:
    inner = getattr(client, "_client", None)
    if inner is None and hasattr(client, "_ensure_client"):
        inner = client._ensure_client()
    return str(inner.api_key)


@pytest.mark.asyncio
async def test_a_patched_openai_key_reaches_every_process_client_on_the_next_call(
    admin: Any, real_factories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сквозная цепь: `PATCH` → снимок процесса → следующий вызов фабрики несёт новый ключ."""
    from app import deps
    from app.chat import llm_client
    from app.memory import embedding

    client, _ = admin
    _env(monkeypatch, OPENAI_API_KEY="sk-openai-env", MEMORY_EMBEDDING_FAKE="false")
    factories = {
        "openai": llm_client._get_openai_singleton,
        "speech": deps.get_speech_client,
        "embedding": embedding.get_embedding_client,
        "moderation": deps.get_moderation_service,
    }
    assert {name: _inner_key(f()) for name, f in factories.items()} == dict.fromkeys(
        factories, "sk-openai-env"
    )

    response = await _patch_cred(client, "openai.api_key", "sk-openai-rotated")
    assert response.status_code == 200, response.text

    assert {name: _inner_key(f()) for name, f in factories.items()} == dict.fromkeys(
        factories, "sk-openai-rotated"
    )


@pytest.mark.asyncio
async def test_a_test_substituted_anthropic_singleton_survives_a_key_change(
    admin: Any, fake_anthropic: FakeAnthropicClient
) -> None:
    from app.chat import anthropic_client, llm_client

    client, _ = admin
    assert (await _patch_cred(client, "anthropic.api_key", "sk-ant-rotated")).status_code == 200

    assert anthropic_client.get_anthropic_client() is fake_anthropic
    assert llm_client.get_llm_client() is fake_anthropic


# ============================== порядок сборки снимка (§4.3) ==============================
@pytest.mark.asyncio
async def test_after_switching_to_openai_a_stored_anthropic_model_is_ignored_everywhere(
    admin: Any,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Строки `llm.*` и креденшлы накладываются ДО проверки модельных строк (§4.3)."""
    from app.instance_config.snapshot import get_snapshot, reset_snapshot

    client, _ = admin
    settings = get_settings()
    anthropic_models = [
        m for m in settings.allowed_models_for("anthropic") if m != settings.anthropic_model
    ]
    assert anthropic_models, "нужна вторая модель Anthropic, отличная от дефолта"
    stored = anthropic_models[0]
    assert (await _patch_setting(client, "chat.default_model", stored)).status_code == 200
    assert (await _patch_cred(client, "openai.api_key", "sk-openai-crm")).status_code == 200

    assert (await _patch_setting(client, "llm.provider", "openai")).status_code == 200
    reset_snapshot()  # холодный старт: снимок собирается из БД с нуля
    with caplog.at_level(logging.WARNING, logger="app.instance_config"):
        assert await _refresh(db_sessionmaker)

    assert "chat.default_model" not in get_snapshot().settings
    ignored = [
        r.extra_fields["setting_id"]  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "admin_override_value_ignored"
    ]
    assert "chat.default_model" in ignored, ignored

    async with db_sessionmaker() as s:
        uid = await seed_user(s)
    models = (await client.get("/v1/models", headers=auth_headers(uid))).json()["models"]
    assert models and {m["provider"] for m in models} == {"openai"}
    assert models[0]["id"] == settings.openai_model
    pricing = (await client.get("/v1/admin/pricing", headers=_H)).json()["items"]
    chat_rows = [row["tariff_id"] for row in pricing if row["tariff_id"].startswith("chat:")]
    assert chat_rows and all(row.startswith("chat:openai:") for row in chat_rows), chat_rows


# ============================== отказ расшифровки ========================================
@pytest.mark.asyncio
async def test_an_undecryptable_row_keeps_the_previous_value_and_falls_back_to_env_on_cold_start(
    admin: Any,
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.instance_config.snapshot import reset_snapshot

    client, _ = admin
    _env(monkeypatch, FAL_API_KEY="fal-env")
    assert (await _patch_cred(client, "fal.api_key", _MARKER)).status_code == 200
    async with db_sessionmaker() as s:
        await s.execute(
            text("UPDATE admin_credentials SET encrypted_dek = decode('00ff00ff', 'hex')")
        )
        await s.commit()
    before = _undecryptable("fal.api_key")

    with caplog.at_level(logging.ERROR, logger="app.instance_config"):
        assert await _refresh(db_sessionmaker)

    assert _undecryptable("fal.api_key") == before + 1
    assert _effective().fal_api_key == _MARKER  # «отказ обновления не откатывает»
    records = [r for r in caplog.records if r.getMessage() == "admin_credential_undecryptable"]
    assert records and records[0].levelno == logging.ERROR
    assert _MARKER not in repr([r.__dict__ for r in records])

    reset_snapshot()  # холодный старт — прежнего значения нет
    assert await _refresh(db_sessionmaker)
    assert _undecryptable("fal.api_key") == before + 2
    assert _effective().fal_api_key == "fal-env"


@pytest.mark.asyncio
async def test_a_row_of_an_unknown_credential_is_ignored_with_unknown_id(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    from app.instance_config.snapshot import get_snapshot

    async with db_sessionmaker() as s:
        await s.execute(
            text(
                "INSERT INTO admin_credentials (credential_id, encrypted_value, encrypted_dek, "
                "fingerprint) VALUES ('retired.key', decode('00', 'hex'), decode('00', 'hex'), 'x')"
            )
        )
        await s.commit()

    with caplog.at_level(logging.WARNING, logger="app.instance_config"):
        assert await _refresh(db_sessionmaker)

    assert "retired.key" not in get_snapshot().credentials
    reasons = [
        r.extra_fields.get("reason")  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "admin_override_value_ignored"
    ]
    assert reasons == ["unknown_id"]


# ============================== пустые таблицы = как до выката ============================
@pytest.mark.asyncio
async def test_an_empty_credentials_table_never_asks_kms_and_keeps_env_settings_as_they_are(
    admin: Any, monkeypatch: pytest.MonkeyPatch, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from app.byok import kms as kms_mod

    client, _ = admin
    calls: list[int] = []

    def _count() -> Any:
        calls.append(1)
        raise RuntimeError("KMS must not be asked on an empty table")

    monkeypatch.setattr(kms_mod, "get_kms_client", _count)

    assert await _refresh(db_sessionmaker)

    assert calls == []
    assert _effective() is get_settings()
    items = await _items(client)
    assert {i["source"] for i in items.values()} <= {"env", "unset"}


# ============================== потребители из оверлея ===================================
@pytest.mark.asyncio
async def test_the_adapty_webhook_checks_the_secret_written_from_the_crm(admin: Any) -> None:
    client, _ = admin
    url = "/v1/billing/adapty/webhook"

    env_accepted = await client.post(
        url, json={}, headers={"Authorization": "Bearer adapty-env-secret"}
    )
    assert env_accepted.status_code != 401, env_accepted.text

    assert (
        await _patch_cred(client, "adapty.webhook_secret", "adapty-crm-secret")
    ).status_code == 200

    old = await client.post(url, json={}, headers={"Authorization": "Bearer adapty-env-secret"})
    new = await client.post(url, json={}, headers={"Authorization": "Bearer adapty-crm-secret"})
    assert old.status_code == 401, old.text
    assert new.status_code != 401, new.text


_PAY_PATH = "/cp/pay/3f2c9a1e-8b7d-4c6e-9a0f-1b2c3d4e5f60"


@pytest.fixture
def pay_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Апстрим страницы оплаты подменён: кейс проверяет ГЕЙТ, а не прокси."""
    from starlette.responses import Response

    from app.billing_cloudpayments import pay_page as pay_page_mod

    async def _forward(self: Any, request: Any, **_kwargs: Any) -> Response:
        return Response("proxied", status_code=200)

    monkeypatch.setattr(pay_page_mod.PayPageProxy, "forward", _forward)
    _env(
        monkeypatch,
        CLOUDPAYMENTS_APP_ID="cp-app-env",
        CLOUDPAYMENTS_API_TOKEN="cp-token-env",
        CLOUDPAYMENTS_API_BASE="https://pay.broadapps.dev/api/v1",
        CLOUDPAYMENTS_PAY_PAGE_PROXY_ENABLED="false",
    )


@pytest.mark.asyncio
async def test_the_pay_page_follows_the_proxy_flag_app_id_and_token_from_the_overlay(
    admin: Any, pay_page: None
) -> None:
    client, _ = admin
    assert (await client.get(_PAY_PATH)).status_code == 404  # env: прокси выключен

    assert (
        await _patch_setting(client, "cloudpayments.pay_page_proxy_enabled", True)
    ).status_code == 200
    assert (await client.get(_PAY_PATH)).status_code == 200

    # `app_id ∧ api_token` — гейт RU-пути; обе величины приходят из оверлея.
    assert (await _patch_setting(client, "cloudpayments.app_id", "")).status_code == 200
    assert (await client.get(_PAY_PATH)).status_code == 404
    assert (await _patch_setting(client, "cloudpayments.app_id", "cp-app-crm")).status_code == 200
    assert (await client.get(_PAY_PATH)).status_code == 200
    assert (await _patch_cred(client, "cloudpayments.api_token", "")).status_code == 200
    assert (await client.get(_PAY_PATH)).status_code == 404


@pytest.mark.asyncio
async def test_the_maps_row_reaches_the_tools_offered_to_the_provider(
    admin: Any,
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    from app.chat.tools import MAPS_TOOLS

    client, _ = admin
    _env(monkeypatch, MAPS_TOOLS_ENABLED="false")
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    maps = {n.replace(".", "_") for n in MAPS_TOOLS}
    first = await _chat_turn(client, fake_anthropic, uid)
    assert not maps & {t["name"] for t in first["tools"]}
    assert (await _patch_setting(client, "chat.maps_tools_enabled", True)).status_code == 200
    second = await _chat_turn(client, fake_anthropic, uid)
    assert maps <= {t["name"] for t in second["tools"]}


# Узнаваемый фрагмент инструкции оси E (`_MAPS_TOOLS_INSTRUCTION`, `chat/orchestrator.py`).
_MAPS_INSTRUCTION_FRAGMENT = "maps.geocode or maps.search_places"


async def _chat_turn(client: AsyncClient, fake: FakeAnthropicClient, uid: Any) -> dict[str, Any]:
    fake.responses = [fake.text_result("готово")]
    fake.calls.clear()
    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "как доехать", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    return dict(fake.calls[0])


@pytest.mark.asyncio
async def test_the_maps_row_reaches_the_system_prompt(
    admin: Any,
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
) -> None:
    """Строка `chat.maps_tools_enabled=true` при env `false` включает и инструкцию карт (§4.1)."""
    client, _ = admin
    _env(monkeypatch, MAPS_TOOLS_ENABLED="false")
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    first = await _chat_turn(client, fake_anthropic, uid)
    assert _MAPS_INSTRUCTION_FRAGMENT not in str(first["system_prompt"])
    assert (await _patch_setting(client, "chat.maps_tools_enabled", True)).status_code == 200
    second = await _chat_turn(client, fake_anthropic, uid)
    assert _MAPS_INSTRUCTION_FRAGMENT in str(second["system_prompt"])


@pytest.mark.asyncio
async def test_an_openai_key_from_the_crm_reaches_the_voice_handshake(
    voice_stand: Any, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Env без ключа OpenAI → `503 voice_mode_not_configured`; ключ из CRM → апгрейд (§2.1)."""
    from app.admin.economics_service import AdminEconomicsService
    from app.schemas.admin_economics import AdminCredentialPatchRequest
    from tests.integration.test_voice_mode_session_adr104 import seed_voice_user
    from tests.voice_harness import VoiceHandshakeDenied

    stand = await voice_stand(OPENAI_API_KEY="")
    uid = await seed_voice_user(stand)
    with pytest.raises(VoiceHandshakeDenied) as denied:
        await stand.connect(uid)
    assert denied.value.status == 503

    async with db_sessionmaker() as session:
        await AdminEconomicsService(session, get_settings()).patch_credential(
            "openai.api_key", AdminCredentialPatchRequest(value="sk-openai-crm"), actor_claim="x"
        )

    socket = await stand.connect(uid)
    assert socket.accepted is True


# ============================== наблюдаемость и поверхность ==============================
@pytest.mark.asyncio
async def test_the_credentials_gauge_and_the_composition_event_follow_the_rows(
    admin: Any, db_sessionmaker: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = admin

    with caplog.at_level(logging.INFO, logger="app.instance_config"):
        assert (await _patch_cred(client, "fal.api_key", "fal-crm")).status_code == 200
        assert await _refresh(db_sessionmaker)  # состав не менялся — без события

    gauge = REGISTRY.get_sample_value("admin_overrides_active", {"scope": "credentials"})
    assert gauge == 1.0
    events = [
        r.extra_fields  # type: ignore[attr-defined]
        for r in caplog.records
        if r.getMessage() == "admin_overrides_snapshot_changed"
    ]
    assert len(events) == 1
    assert events[0]["credentials"] == ["fal.api_key"]


_CRED_SURFACE: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/v1/admin/credentials", None),
    ("PATCH", "/v1/admin/credentials/fal.api_key", {"value": "fal-x"}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path", "payload"), _CRED_SURFACE)
async def test_both_credential_paths_spend_the_economics_bucket_and_require_the_admin_key(
    admin: Any, method: str, path: str, payload: dict[str, Any] | None
) -> None:
    client, redis = admin
    assert (await client.request(method, path, json=payload)).status_code == 403
    assert (
        await client.request(method, path, json=payload, headers={"X-Admin-Key": "nope"})
    ).status_code == 401

    settings = get_settings()
    original = settings.admin_economics_rate_limit_per_min
    settings.admin_economics_rate_limit_per_min = 2
    try:
        seen = [
            (await client.request(method, path, json=payload, headers=_H)).status_code
            for _ in range(4)
        ]
    finally:
        settings.admin_economics_rate_limit_per_min = original

    assert 429 in seen, seen
    assert any(key.startswith("rl:admin_econ:") for key in redis.counts)


@pytest.mark.asyncio
async def test_the_settings_surface_still_carries_no_credential_value(
    admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Креденшлы живут ТОЛЬКО в `/credentials`: ни env, ни оверлей не видны в `/settings`."""
    client, _ = admin
    _env(monkeypatch, FAL_API_KEY="SENTINEL-fal-env", OPENAI_API_KEY="SENTINEL-openai-env")
    assert (await _patch_cred(client, "anthropic.api_key", "SENTINEL-ant-crm")).status_code == 200

    body = (await client.get("/v1/admin/settings", headers=_H)).text

    assert "SENTINEL" not in body
