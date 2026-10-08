"""Unit: креденшлы оверлея, «действующие настройки» и клиенты-синглтоны (ADR-116 §2, §5).

Компонент без БД: снимок ставится напрямую (``install_snapshot``), а сквозная цепь
«`PATCH` → снимок → клиент» доказывается в ``tests/integration/test_admin_credentials_adr116.py``.
Здесь — то, что на интеграционном уровне ненаблюдаемо или дорого:

- отпечаток, запрет пробельных/управляющих символов, шифрование с associated data;
- пустой оверлей возвращает ТОТ ЖЕ объект настроек (бит-в-бит);
- каждый процессный клиент, захватывающий ключ, пересоздаётся при смене ключа и НЕ
  пересоздаётся без неё; подменённый тестом клиент не пересоздаётся;
- пул выведенного клиента закрывается, когда на него не осталось ссылок, и НЕ закрывается,
  пока ссылка (идущий вызов) жива.
"""

from __future__ import annotations

import asyncio
import datetime
import gc
import hashlib
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from app.byok.kms import LocalKmsClient
from app.config import get_settings
from app.instance_config.credentials import (
    credential_fingerprint,
    declared_credentials,
    decrypt_credential,
    encrypt_credential,
    has_undeclared_character,
)
from app.instance_config.effective import apply_overlay, get_effective_settings
from app.instance_config.settings_registry import SETTING_LLM_DUAL_ENABLED, SETTING_LLM_PROVIDER
from app.instance_config.snapshot import (
    EMPTY_SNAPSHOT,
    CredentialOverlay,
    InstanceConfigSnapshot,
    SettingOverlay,
    install_snapshot,
)

_NOW = datetime.datetime(2026, 9, 26, 12, 0, tzinfo=datetime.UTC)
_KMS = LocalKmsClient(b"0123456789abcdef0123456789abcdef")


def _creds(**values: str) -> dict[str, CredentialOverlay]:
    """``openai__api_key="x"`` → строка креденшла ``openai.api_key``."""
    out: dict[str, CredentialOverlay] = {}
    for key, value in values.items():
        credential_id = key.replace("__", ".")
        out[credential_id] = CredentialOverlay(
            credential_id=credential_id,
            value=value,
            fingerprint=credential_fingerprint(value),
            updated_at=_NOW,
        )
    return out


def _overlay(settings: dict[str, Any] | None = None, **credentials: str) -> InstanceConfigSnapshot:
    return InstanceConfigSnapshot(
        settings={
            sid: SettingOverlay(setting_id=sid, value=value, updated_at=_NOW)
            for sid, value in (settings or {}).items()
        },
        credentials=_creds(**credentials),
    )


# ============================== отпечаток и форма значения =================================
def test_the_fingerprint_is_the_first_twelve_hex_of_sha256_of_utf8() -> None:
    value = "sk-проверка-ключа"

    assert credential_fingerprint(value) == hashlib.sha256(value.encode()).hexdigest()[:12]
    assert len(credential_fingerprint(value)) == 12


@pytest.mark.parametrize(
    "value",
    ["sk key", "sk\nkey", "sk\tkey", "sk\x07key", "sk​key", "sk key", " sk"],
    ids=["space", "newline", "tab", "bell", "zero_width", "nbsp", "leading_space"],
)
def test_whitespace_and_control_characters_are_an_undeclared_bound(value: str) -> None:
    assert has_undeclared_character(value)


@pytest.mark.parametrize(
    "value", ["", "sk-proj-AbC_123.xyz", "ключ-без-пробелов"], ids=["empty", "ascii", "cyrillic"]
)
def test_ordinary_values_pass_the_character_check(value: str) -> None:
    assert not has_undeclared_character(value)


# ============================== шифрование ==================================================
def test_the_ciphertext_round_trips_and_never_carries_the_plaintext() -> None:
    value = "sk-plaintext-marker-116"

    encrypted_value, encrypted_dek = encrypt_credential(_KMS, "openai.api_key", value)

    assert value.encode() not in encrypted_value
    assert value.encode() not in encrypted_dek
    assert decrypt_credential(_KMS, "openai.api_key", encrypted_value, encrypted_dek) == value


def test_a_row_moved_under_another_credential_id_does_not_decrypt() -> None:
    """Associated data = идентификатор: шифротекст, переставленный в другую строку, мёртв."""
    encrypted_value, encrypted_dek = encrypt_credential(_KMS, "openai.api_key", "sk-a")

    with pytest.raises(Exception):  # noqa: B017 — любой отказ AEAD
        decrypt_credential(_KMS, "anthropic.api_key", encrypted_value, encrypted_dek)


def test_the_registry_is_closed_at_ten_and_never_names_signing_material() -> None:
    ids = {spec.credential_id for spec in declared_credentials()}

    assert ids == {
        "openai.api_key",
        "openai.api_key_backup",
        "anthropic.api_key",
        "anthropic.api_key_backup",
        "fal.api_key",
        "proxy.api_key",
        "kie.api_key",
        "sosana.api_key",
        "cloudpayments.api_token",
        "adapty.webhook_secret",
    }
    fields = {spec.settings_field for spec in declared_credentials()}
    # ADR-116 §1 п. 1: материал подписи и шифрования в поверхность не входит никогда.
    assert not fields & {
        "kms_local_master_key",
        "jwt_private_key",
        "admin_api_secret",
        "proxy_webhook_secret",
        "storekit_test_secret",
        "apple_test_secret",
        "preview_url_secret",
        "metrics_scrape_token",
    }


# ============================== действующие настройки ======================================
def test_an_empty_overlay_returns_the_very_same_settings_object() -> None:
    base = get_settings()

    assert apply_overlay(base, EMPTY_SNAPSHOT) is base
    assert get_effective_settings() is base


def test_a_credential_row_overrides_env_and_an_empty_string_means_switched_off() -> None:
    base = get_settings()
    assert base.anthropic_api_key  # conftest: сервисный ключ задан в env

    on = apply_overlay(base, _overlay(openai__api_key="sk-from-crm"))
    off = apply_overlay(base, _overlay(anthropic__api_key=""))

    assert on.openai_api_key == "sk-from-crm"
    assert off.anthropic_api_key == ""  # «явно выключено» перекрывает `.env`
    assert base.anthropic_api_key  # базовые настройки не тронуты


def test_the_dual_row_names_the_other_provider_and_credits_providers_follows_it() -> None:
    base = get_settings()
    snapshot = _overlay(
        {SETTING_LLM_PROVIDER: "anthropic", SETTING_LLM_DUAL_ENABLED: True},
        openai__api_key="sk-openai-crm",
    )

    effective = apply_overlay(base, snapshot)

    assert effective.llm_providers_raw == "openai"
    assert set(effective.credits_providers()) == {"anthropic", "openai"}
    assert apply_overlay(base, _overlay({SETTING_LLM_DUAL_ENABLED: False})).llm_providers_raw == ""


# ============================== клиенты-синглтоны ==========================================
@pytest.fixture
def fresh_clients(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Фабрики без чужих фейков и кэшей: `client`-фикстура оставляет свои в модулях."""
    from app import deps
    from app.chat import anthropic_client, llm_client
    from app.memory import embedding

    for module, names in (
        (
            llm_client,
            (
                "_openai_singleton",
                "_openai_built",
                "_openai_responses_singleton",
                "_openai_responses_built",
            ),
        ),
        (anthropic_client, ("_anthropic_singleton", "_anthropic_built")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-env")
    monkeypatch.setenv("MEMORY_EMBEDDING_FAKE", "false")
    get_settings.cache_clear()
    deps.get_speech_client.cache_clear()
    deps.get_moderation_service.cache_clear()
    embedding.get_embedding_client.cache_clear()
    yield
    deps.get_speech_client.cache_clear()
    deps.get_moderation_service.cache_clear()
    embedding.get_embedding_client.cache_clear()
    get_settings.cache_clear()


def _key_of(client: Any) -> str:
    """Ключ, захваченный клиентом, — из ВНУТРЕННЕГО SDK-клиента, куда он реально ушёл."""
    inner = getattr(client, "_client", None)
    if inner is None and hasattr(client, "_ensure_client"):
        inner = client._ensure_client()
    return str(inner.api_key)


def _factories() -> dict[str, Callable[[], Any]]:
    from app import deps
    from app.chat import anthropic_client, llm_client
    from app.memory import embedding

    return {
        "openai": llm_client._get_openai_singleton,
        "openai_responses": llm_client._get_openai_responses_singleton,
        "anthropic": anthropic_client.get_anthropic_client,
        "speech": deps.get_speech_client,
        "moderation": deps.get_moderation_service,
        "embedding": embedding.get_embedding_client,
    }


_CREDENTIAL_OF = {
    "openai": "openai__api_key",
    "openai_responses": "openai__api_key",
    "anthropic": "anthropic__api_key",
    "speech": "openai__api_key",
    # Модерация без собственного ключа следует за ключом OpenAI (ADR-116 §2.1).
    "moderation": "openai__api_key",
    "embedding": "openai__api_key",
}


@pytest.mark.parametrize("name", list(_CREDENTIAL_OF))
def test_a_new_key_from_the_overlay_reaches_the_next_call_without_a_restart(
    fresh_clients: None, name: str
) -> None:
    factory = _factories()[name]
    before = factory()
    assert factory() is before  # без смены входов — тот же клиент

    install_snapshot(_overlay(**{_CREDENTIAL_OF[name]: "sk-rotated-from-crm"}))
    after = factory()

    assert after is not before
    assert _key_of(after) == "sk-rotated-from-crm"
    assert _key_of(before) != "sk-rotated-from-crm"  # идущий на старом клиенте вызов не тронут
    assert factory() is after


@pytest.mark.parametrize(
    ("module_path", "attr", "factory_name"),
    [
        ("app.chat.llm_client", "_openai_singleton", "_get_openai_singleton"),
        ("app.chat.llm_client", "_openai_responses_singleton", "_get_openai_responses_singleton"),
        ("app.chat.anthropic_client", "_anthropic_singleton", "get_anthropic_client"),
    ],
)
def test_a_singleton_substituted_by_a_test_is_never_rebuilt(
    fresh_clients: None,
    monkeypatch: pytest.MonkeyPatch,
    module_path: str,
    attr: str,
    factory_name: str,
) -> None:
    import importlib

    module = importlib.import_module(module_path)
    fake = object()
    monkeypatch.setattr(module, attr, fake)

    install_snapshot(_overlay(openai__api_key="sk-x", anthropic__api_key="sk-y"))

    assert getattr(module, factory_name)() is fake


def test_the_active_provider_follows_the_provider_row(
    fresh_clients: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.chat import llm_client
    from app.chat.openai_client import OpenAIClient

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    get_settings.cache_clear()
    install_snapshot(_overlay({SETTING_LLM_PROVIDER: "openai"}))

    assert isinstance(llm_client.get_llm_client(), OpenAIClient)


# ============================== закрытие пула выведенного клиента ==========================
class _ClosableFake:
    def __init__(self) -> None:
        self.closed = 0
        self.api_key = "fake"

    async def close(self) -> None:
        self.closed += 1


async def _drain() -> None:
    for _ in range(5):
        gc.collect()
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(_CREDENTIAL_OF))
async def test_the_retired_pool_is_closed_only_after_its_last_reference_is_gone(
    fresh_clients: None, name: str
) -> None:
    """Пока ссылка на прежний клиент жива (вызов держит `self`), пул не закрыт; ушла — закрыт.

    Внутренний SDK-клиент подменяется фейком с `close()` — у модерации он ленивый и создаётся
    ПОСЛЕ вывода из оборота (вызов, начавшийся до смены ключа): такой тоже обязан закрыться.
    """
    factory = _factories()[name]
    held = factory()
    inner = _ClosableFake()
    if name != "moderation":
        held._client = inner

    install_snapshot(_overlay(**{_CREDENTIAL_OF[name]: "sk-rotated-from-crm"}))
    replacement = factory()
    assert replacement is not held
    if name == "moderation":
        held._client = inner  # ленивый клиент, созданный уже на выведенной обёртке

    await _drain()
    assert inner.closed == 0, "пул закрыт под живой ссылкой — идущий вызов бы упал"

    del held
    await _drain()
    assert inner.closed == 1


@pytest.mark.asyncio
async def test_the_current_client_is_never_closed(fresh_clients: None) -> None:
    from app.chat import llm_client

    current = llm_client._get_openai_singleton()
    inner = _ClosableFake()
    current._client = inner  # type: ignore[attr-defined]
    del current

    await _drain()

    assert inner.closed == 0


# ============================== TTS_AUDIO_FORMAT: WARNING не размножается ==================
def test_an_invalid_tts_format_warns_once_however_often_the_factory_is_called(
    fresh_clients: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app import deps

    logging.getLogger("app.config").disabled = False
    monkeypatch.setenv("TTS_AUDIO_FORMAT", "flac")
    get_settings.cache_clear()
    deps.get_speech_client.cache_clear()

    with caplog.at_level(logging.WARNING, logger="app.config"):
        for _ in range(5):
            deps.get_speech_client()

    warnings = [r for r in caplog.records if "TTS_AUDIO_FORMAT" in r.getMessage()]
    assert len(warnings) == 1
