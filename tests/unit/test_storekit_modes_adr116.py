"""Unit: режим проверки покупок StoreKit — без строки оверлея и под ней (ADR-116 §4.2).

Дом сценариев — ADR-116 §4.2 и «Фронт работ» п. 2 («`sandbox`/`production` — по четыре строки
§4.2»). Проверяется НАСТОЯЩИЙ ``StoreKitVerifier`` на настоящих JWS: HS256 тестовой ветки и ES256
с цепочкой x5c, подписанной самодельным корнем, который кладётся в каталог корней там, где
кейсу нужна привязка цепочки.

⛔ Главный инвариант файла — **пустой оверлей = поведение до ADR-116 бит-в-бит**: сверка
``bundleId`` идёт всегда, когда bundle задан, а тестовая ветка — всегда, когда задан флаг и
секрет, НЕЗАВИСИМО от ``APPSTORE_ENVIRONMENT``. Кейсы (а)–(ж) перечисляют env-комбинации флота;
каждый ловит свою мутацию, контрольные проходят на обеих:

- M1 «``_check_bundle_id = production``» (сверка bundle выведена из окружения) роняет (г), (е),
  (ж) — там окружение ``sandbox``, а bundle задан;
- M2 «тестовая ветка ``… and not production``» роняет (а) — там окружение ``production``, а флаг
  и секрет заданы.
"""

from __future__ import annotations

import base64
import datetime
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from app.config import Settings, get_settings
from app.errors import ValidationFailedError
from app.instance_config.settings_registry import (
    SETTING_STOREKIT_MODE,
    resolve_setting,
)
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    InstanceConfigSnapshot,
    SettingOverlay,
    install_snapshot,
)
from app.subscription import storekit as storekit_mod
from app.subscription.storekit import StoreKitVerifier

_NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)
_OWN = "com.own.app"
_FOREIGN = "com.foreign.app"
_SECRET = "storekit-hs256-fixture-secret"  # noqa: S105 — секрет тестовой ветки, только фикстура


# ------------------------------ материал подписи -------------------------------------------
def _cert(
    subject: str, key: ec.EllipticCurvePrivateKey, issuer: x509.Name | None = None, signer: Any = None
) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW - datetime.timedelta(days=1))
        .not_valid_after(_NOW + datetime.timedelta(days=3650))
        .sign(signer or key, hashes.SHA256())
    )


_ROOT_KEY = ec.generate_private_key(ec.SECP256R1())
_ROOT = _cert("fixture-root", _ROOT_KEY)
_LEAF_KEY = ec.generate_private_key(ec.SECP256R1())
_LEAF = _cert("fixture-leaf", _LEAF_KEY, issuer=_ROOT.subject, signer=_ROOT_KEY)


def _claims(bundle_id: str) -> dict[str, Any]:
    now = datetime.datetime.now(tz=datetime.UTC)
    return {
        "transactionId": "txn-116",
        "originalTransactionId": "otxn-116",
        "productId": "pro.monthly",
        "bundleId": bundle_id,
        "environment": "Sandbox",
        "expiresDate": int((now + datetime.timedelta(days=30)).timestamp() * 1000),
        "exp": int((now + datetime.timedelta(hours=1)).timestamp()),
    }


def _hs256(bundle_id: str) -> str:
    return pyjwt.encode(_claims(bundle_id), _SECRET, algorithm="HS256")


def _es256(bundle_id: str) -> str:
    """ES256 с цепочкой [лист, корень]: проходит привязку только к каталогу с этим корнем."""
    x5c = [base64.b64encode(c.public_bytes(Encoding.DER)).decode() for c in (_LEAF, _ROOT)]
    return pyjwt.encode(
        _claims(bundle_id), _LEAF_KEY, algorithm="ES256", headers={"x5c": x5c}
    )


@pytest.fixture
def roots_dir(tmp_path: Path) -> str:
    (tmp_path / "root.cer").write_bytes(_ROOT.public_bytes(Encoding.DER))
    return str(tmp_path)


def _settings(
    *,
    environment: str,
    test_mode: bool,
    skip_chain: bool,
    bundle: str,
    roots: str = "",
    secret: str = _SECRET,
) -> Settings:
    return Settings(
        APPSTORE_ENVIRONMENT=environment,
        STOREKIT_TEST_MODE=test_mode,
        STOREKIT_TEST_SECRET=secret,
        STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION=skip_chain,
        APPSTORE_BUNDLE_ID=bundle,
        APPSTORE_ROOT_CERT_DIR=roots,
    )


def _accepts(verifier: StoreKitVerifier, jws: str) -> bool:
    try:
        verifier.verify(jws)
    except ValidationFailedError:
        return False
    return True


def _mode_overlay(mode: str) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        settings={SETTING_STOREKIT_MODE: SettingOverlay(SETTING_STOREKIT_MODE, mode, _NOW)}
    )


@pytest.fixture(autouse=True)
def _storekit_logger_enabled() -> None:
    # Alembic выключает `app.*`-логгеры на весь процесс (см. `_migrated` в conftest).
    logging.getLogger("app.subscription.storekit").disabled = False


# ============== пустой оверлей: env-комбинации флота, поведение до ADR-116 ==================
def test_a_production_env_with_test_flag_and_secret_still_accepts_hs256() -> None:
    """(а) production + test=true + секрет + bundle пуст: тестовая ветка ЖИВА без строки.

    Ловит M2: вывод «в production тестовой ветки нет» из `APPSTORE_ENVIRONMENT` выключил бы её
    на инстансе, где из CRM ничего не трогали.
    """
    verifier = StoreKitVerifier(
        _settings(environment="production", test_mode=True, skip_chain=False, bundle="")
    )

    assert _accepts(verifier, _hs256(_FOREIGN))


def test_b_production_env_with_bundle_rejects_a_foreign_bundle_and_accepts_its_own(
    roots_dir: str,
) -> None:
    """(б) production + test=false + bundle задан — контрольный кейс (стережёт саму сверку)."""
    verifier = StoreKitVerifier(
        _settings(
            environment="production", test_mode=False, skip_chain=False, bundle=_OWN, roots=roots_dir
        )
    )

    assert _accepts(verifier, _es256(_OWN))
    assert not _accepts(verifier, _es256(_FOREIGN))


def test_c_production_env_without_bundle_does_not_reject_a_foreign_bundle(roots_dir: str) -> None:
    """(в) production + test=false + bundle пуст: сверять не с чем — контрольный кейс."""
    verifier = StoreKitVerifier(
        _settings(
            environment="production", test_mode=False, skip_chain=False, bundle="", roots=roots_dir
        )
    )

    assert _accepts(verifier, _es256(_FOREIGN))


def test_d_sandbox_test_skip_with_bundle_rejects_hs256_of_a_foreign_bundle() -> None:
    """(г) sandbox + test=true + skip=true + bundle задан: сверка bundle ЖИВА без строки (M1)."""
    verifier = StoreKitVerifier(
        _settings(environment="sandbox", test_mode=True, skip_chain=True, bundle=_OWN)
    )

    assert _accepts(verifier, _hs256(_OWN))  # отказ ниже — именно сверка bundle, а не подпись
    assert not _accepts(verifier, _hs256(_FOREIGN))


def test_e_sandbox_test_skip_without_bundle_accepts() -> None:
    """(д) sandbox + test=true + skip=true + bundle пуст — нынешняя база флота, контрольный."""
    verifier = StoreKitVerifier(
        _settings(environment="sandbox", test_mode=True, skip_chain=True, bundle="")
    )

    assert _accepts(verifier, _hs256(_FOREIGN))
    assert _accepts(verifier, _es256(_FOREIGN))


def test_f_sandbox_without_test_flag_checks_the_bundle_of_es256_and_refuses_hs256() -> None:
    """(е) sandbox + test=false + skip=true + bundle задан: ES256 чужого bundle — отказ (M1)."""
    verifier = StoreKitVerifier(
        _settings(environment="sandbox", test_mode=False, skip_chain=True, bundle=_OWN)
    )

    assert _accepts(verifier, _es256(_OWN))  # цепочка пропущена, подпись листа верна
    assert not _accepts(verifier, _es256(_FOREIGN))
    assert not _accepts(verifier, _hs256(_OWN))  # тестовой ветки нет


def test_g_sandbox_test_without_skip_with_bundle_rejects_hs256_of_a_foreign_bundle() -> None:
    """(ж) sandbox + test=true + skip=false + bundle задан: HS256 чужого bundle — отказ (M1)."""
    verifier = StoreKitVerifier(
        _settings(environment="sandbox", test_mode=True, skip_chain=False, bundle=_OWN)
    )

    assert _accepts(verifier, _hs256(_OWN))
    assert not _accepts(verifier, _hs256(_FOREIGN))


# ============== строка оверлея `storekit.mode`: таблица §4.2 ===============================
def test_the_sandbox_row_accepts_a_foreign_bundle_and_hs256_with_a_configured_secret() -> None:
    """Строка `sandbox`: цепочка не привязывается, тестовая ветка включена, bundle НЕ сверяется.

    Env нарочно противоположен колонке `sandbox` (production, без тестовой ветки, bundle задан):
    всё, что кейс видит, приходит из строки, а не из env.
    """
    install_snapshot(_mode_overlay("sandbox"))
    verifier = StoreKitVerifier(
        _settings(environment="production", test_mode=False, skip_chain=False, bundle=_OWN)
    )

    assert _accepts(verifier, _hs256(_FOREIGN))
    assert _accepts(verifier, _es256(_FOREIGN))  # самодельный корень не в каталоге — и не нужен


def test_the_sandbox_row_without_a_secret_keeps_hs256_closed() -> None:
    """Таблица §4.2: тестовая ветка «действует, только если задан `STOREKIT_TEST_SECRET`»."""
    install_snapshot(_mode_overlay("sandbox"))
    verifier = StoreKitVerifier(
        _settings(environment="production", test_mode=False, skip_chain=False, bundle="", secret="")
    )

    assert not _accepts(verifier, _hs256(_FOREIGN))


def test_the_production_row_refuses_hs256_even_with_the_env_test_flag(roots_dir: str) -> None:
    """Строка `production`: тестовая ветка выключена ПРИ `STOREKIT_TEST_MODE=true`, bundle сверяется."""
    install_snapshot(_mode_overlay("production"))
    verifier = StoreKitVerifier(
        _settings(
            environment="sandbox", test_mode=True, skip_chain=True, bundle=_OWN, roots=roots_dir
        )
    )

    assert not _accepts(verifier, _hs256(_OWN))
    assert _accepts(verifier, _es256(_OWN))
    assert not _accepts(verifier, _es256(_FOREIGN))


def test_the_production_row_anchors_the_chain_even_with_the_env_skip_flag() -> None:
    """Строка `production` + каталог корней без нашего корня: самодельная цепочка — отказ."""
    install_snapshot(_mode_overlay("production"))
    verifier = StoreKitVerifier(
        _settings(environment="sandbox", test_mode=True, skip_chain=True, bundle=_OWN)
    )

    assert not _accepts(verifier, _es256(_OWN))


# ============== пересоздание по отпечатку входов ============================================
@pytest.fixture
def fresh_factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Фабрика процесса без чужого фейка: `client`-фикстура оставляет его в синглтоне."""
    monkeypatch.setattr(storekit_mod, "_verifier_singleton", None)
    monkeypatch.setattr(storekit_mod, "_verifier_built", None)
    for name, value in {
        "APPSTORE_ENVIRONMENT": "sandbox",
        "STOREKIT_TEST_MODE": "true",
        "STOREKIT_TEST_SECRET": _SECRET,
        "STOREKIT_DEV_SKIP_CERT_CHAIN_VERIFICATION": "true",
        "APPSTORE_BUNDLE_ID": _OWN,
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_appearance_and_removal_of_the_mode_row_rebuilds_the_verifier(
    fresh_factory: None,
) -> None:
    """Env = колонка `sandbox` ровно, КРОМЕ bundle: строка меняет ТОЛЬКО факт «строка есть».

    Единственный вход, по которому отличаются два состояния, — наличие строки; если он выпадет
    из отпечатка, фабрика вернёт прежний верификатор, и чужой bundle останется запертым.
    """
    first = storekit_mod.get_storekit_verifier()
    assert not _accepts(first, _hs256(_FOREIGN))  # без строки bundle сверяется

    install_snapshot(_mode_overlay("sandbox"))
    second = storekit_mod.get_storekit_verifier()
    assert second is not first
    assert _accepts(second, _hs256(_FOREIGN))

    install_snapshot(EMPTY_SNAPSHOT)
    third = storekit_mod.get_storekit_verifier()
    assert third is not second
    assert not _accepts(third, _hs256(_FOREIGN))


def test_an_unchanged_input_returns_the_same_verifier(fresh_factory: None) -> None:
    assert storekit_mod.get_storekit_verifier() is storekit_mod.get_storekit_verifier()


def test_a_verifier_substituted_by_a_test_is_never_rebuilt(
    fresh_factory: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = object()
    monkeypatch.setattr(storekit_mod, "_verifier_singleton", fake)

    install_snapshot(_mode_overlay("production"))

    assert storekit_mod.get_storekit_verifier() is fake


# ============== наблюдаемость: `storekit_verifier_built` ====================================
def _built(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.getMessage() == "storekit_verifier_built"]


@pytest.mark.parametrize(
    ("test_mode", "secret", "expected_level", "expected_flag"),
    [
        (True, _SECRET, logging.WARNING, True),
        (False, _SECRET, logging.INFO, False),
        # Флаг без секрета тестовую ветку НЕ включает — и событие обязано назвать это INFO.
        (True, "", logging.INFO, False),
    ],
    ids=["test_branch_on", "flag_off", "flag_without_secret"],
)
def test_the_build_event_is_warning_exactly_when_the_test_branch_is_live(
    caplog: pytest.LogCaptureFixture,
    test_mode: bool,
    secret: str,
    expected_level: int,
    expected_flag: bool,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.subscription.storekit"):
        StoreKitVerifier(
            _settings(
                environment="sandbox",
                test_mode=test_mode,
                skip_chain=False,
                bundle=_OWN,
                secret=secret,
            )
        )

    [record] = _built(caplog.records)
    assert record.levelno == expected_level
    assert record.extra_fields["testMode"] is expected_flag  # type: ignore[attr-defined]


def test_the_build_event_is_written_on_every_rebuild_and_never_carries_the_secret(
    fresh_factory: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.subscription.storekit"):
        storekit_mod.get_storekit_verifier()
        storekit_mod.get_storekit_verifier()  # без смены входов — без события
        install_snapshot(_mode_overlay("production"))
        storekit_mod.get_storekit_verifier()

    events = _built(caplog.records)
    assert len(events) == 2
    assert [e.extra_fields["testMode"] for e in events] == [True, False]  # type: ignore[attr-defined]
    rendered = " ".join(repr(r.__dict__) for r in caplog.records)
    assert _SECRET not in rendered


# ============== GET /settings: `storekit.mode` без строки оверлея ==========================
@pytest.mark.parametrize(
    ("environment", "test_mode", "skip_chain", "bundle", "expected"),
    [
        # Точная колонка `sandbox` (нынешняя база флота из provision.sh).
        ("sandbox", True, True, "", "sandbox"),
        # Точная колонка `production`; флаг пропуска цепочки в сравнение не входит.
        ("production", False, False, _OWN, "production"),
        ("production", False, True, _OWN, "production"),
        # Всё, что не совпадает ни с одной колонкой, — `null`.
        ("sandbox", False, True, "", None),  # песочница без тестовой ветки
        ("sandbox", True, False, "", None),  # тестовая ветка, но цепочка проверяется
        ("sandbox", True, True, _OWN, None),  # песочница с заданным bundle
        ("production", True, False, _OWN, None),  # production с тестовой веткой
        ("production", False, False, "", None),  # production без bundle
    ],
    ids=[
        "exact_sandbox",
        "exact_production",
        "production_skip_flag_ignored",
        "sandbox_without_test",
        "sandbox_with_chain",
        "sandbox_with_bundle",
        "production_with_test",
        "production_without_bundle",
    ],
)
def test_the_mode_without_a_row_is_named_only_on_an_exact_column_match(
    environment: str, test_mode: bool, skip_chain: bool, bundle: str, expected: str | None
) -> None:
    settings = _settings(
        environment=environment, test_mode=test_mode, skip_chain=skip_chain, bundle=bundle
    )

    assert (
        resolve_setting(SETTING_STOREKIT_MODE, settings=settings, snapshot=EMPTY_SNAPSHOT)
        == expected
    )
