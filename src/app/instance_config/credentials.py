"""Реестр креденшлов инстанса и их шифрование (ADR-116 §2).

Креденшлы — отдельная от ``/settings`` поверхность **только на запись**: значение уходит в БД
зашифрованным (envelope encryption BYOK, ADR-003) и наружу не отдаётся никогда — ни в ответе,
ни в аудите, ни в логе. Наружу уходят только метаданные и ``fingerprint``.

Перечень ЗАКРЫТ (ADR-116 §2.1): ручки «запиши переменную по имени» нет, граница поверхности —
этот реестр. Материал подписи и шифрования (мастер-ключ KMS, ключ JWT, admin-секреты, секрет
подписи колбэков прокси) сюда не входит и войти не может (ADR-116 §1 п. 1).
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings

if TYPE_CHECKING:
    # Только тип: импорт пакета `app.byok` в рантайме тянет его сервис и клиентов чата.
    from app.byok.kms import KmsClient

CREDENTIAL_OPENAI_API_KEY = "openai.api_key"
CREDENTIAL_OPENAI_API_KEY_BACKUP = "openai.api_key_backup"
CREDENTIAL_ANTHROPIC_API_KEY = "anthropic.api_key"
CREDENTIAL_ANTHROPIC_API_KEY_BACKUP = "anthropic.api_key_backup"
CREDENTIAL_FAL_API_KEY = "fal.api_key"
CREDENTIAL_PROXY_API_KEY = "proxy.api_key"
CREDENTIAL_KIE_API_KEY = "kie.api_key"
CREDENTIAL_SOSANA_API_KEY = "sosana.api_key"
CREDENTIAL_CLOUDPAYMENTS_API_TOKEN = "cloudpayments.api_token"
CREDENTIAL_ADAPTY_WEBHOOK_SECRET = "adapty.webhook_secret"

# Объявленная верхняя граница длины значения (`constraints.max_length`, ADR-116 §2.3).
CREDENTIAL_MAX_LENGTH = 512

# Длина отпечатка: первые 12 hex SHA-256 (48 бит) — достаточно, чтобы CRM опознала свой
# экземпляр ключа, и недостаточно, чтобы восстановить ключ высокой энтропии (ADR-116 §2.4).
FINGERPRINT_LENGTH = 12

_DEK_LEN = 32
_NONCE_LEN = 12

SOURCE_OVERLAY = "overlay"
SOURCE_ENV = "env"
SOURCE_UNSET = "unset"


@dataclass(frozen=True)
class CredentialSpec:
    """Объявление одного креденшла: то, что уходит в контракт, плюс поле ``Settings``."""

    credential_id: str
    settings_field: str
    label: str
    group: str
    description: str


_AFTER_WRITE_NOTE = (
    " После записи из панели значение из конфигурации сервера больше не действует, пока запись "
    "не удалена."
)

_SPECS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        credential_id=CREDENTIAL_OPENAI_API_KEY,
        settings_field="openai_api_key",
        label="Ключ OpenAI",
        group="Провайдер",
        description=(
            "Основной ключ OpenAI: чат, распознавание и синтез речи, память." + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_OPENAI_API_KEY_BACKUP,
        settings_field="openai_api_key_backup",
        label="Резервный ключ OpenAI",
        group="Провайдер",
        description=(
            "Используется, если основной ключ OpenAI отклонён провайдером." + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_ANTHROPIC_API_KEY,
        settings_field="anthropic_api_key",
        label="Ключ Anthropic",
        group="Провайдер",
        description="Основной ключ Anthropic для чата." + _AFTER_WRITE_NOTE,
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_ANTHROPIC_API_KEY_BACKUP,
        settings_field="anthropic_api_key_backup",
        label="Резервный ключ Anthropic",
        group="Провайдер",
        description=(
            "Используется, если основной ключ Anthropic отклонён провайдером." + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_FAL_API_KEY,
        settings_field="fal_api_key",
        label="Ключ fal",
        group="Генерация медиа",
        description=(
            "Ключ сервиса генерации фото и видео. Пустое значение выключает прямую генерацию."
            + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_PROXY_API_KEY,
        settings_field="proxy_api_key",
        label="Ключ прокси генерации",
        group="Генерация медиа",
        description=("Ключ прокси-сервиса генерации медиа." + _AFTER_WRITE_NOTE),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_KIE_API_KEY,
        settings_field="kie_api_key",
        label="Ключ kie",
        group="Генерация медиа",
        description=(
            "Ключ kie для задач видео через прокси. Пусто — прокси генерирует своим ключом."
            + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_SOSANA_API_KEY,
        settings_field="sosana_api_key",
        label="Ключ sosana",
        group="Генерация медиа",
        description=(
            "Ключ sosana для задач изображений через прокси. Пусто — прокси генерирует своим "
            "ключом." + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_CLOUDPAYMENTS_API_TOKEN,
        settings_field="cloudpayments_api_token",
        label="API KEY CloudPayments",
        group="CloudPayments",
        description=(
            "Токен исходящих запросов к платёжному партнёру: оформление оплаты и сверка платежей."
            + _AFTER_WRITE_NOTE
        ),
    ),
    CredentialSpec(
        credential_id=CREDENTIAL_ADAPTY_WEBHOOK_SECRET,
        settings_field="adapty_webhook_secret",
        label="Секрет вебхука Adapty",
        group="Adapty",
        description=(
            "Секрет, которым Adapty подписывает вебхук начисления. Должен совпадать со значением "
            "в консоли Adapty." + _AFTER_WRITE_NOTE
        ),
    ),
)

_BY_ID: dict[str, CredentialSpec] = {spec.credential_id: spec for spec in _SPECS}


def declared_credentials() -> tuple[CredentialSpec, ...]:
    """Все объявленные креденшлы в порядке отображения."""
    return _SPECS


def find_credential(credential_id: str) -> CredentialSpec | None:
    """Объявленный креденшл по идентификатору, либо ``None`` (→ `400 unknown_id`)."""
    return _BY_ID.get(credential_id)


def env_credential_value(spec: CredentialSpec, settings: Settings) -> str:
    """Значение креденшла из переданных настроек (из `.env`, если настройки — базовые)."""
    value = getattr(settings, spec.settings_field)
    return value if isinstance(value, str) else ""


def credential_fingerprint(value: str) -> str:
    """Первые 12 hex SHA-256 от UTF-8 значения (ADR-116 §2.4)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def has_undeclared_character(value: str) -> bool:
    """Пробельный или управляющий символ в значении (ADR-116 §2.3, `undeclared_bound`).

    Граница НЕ объявлена и объявить её нечем: замороженный набор ключей ``constraints`` —
    ``max_length``/``min_items``/``max_items``. Отсюда код `400`, а не `422`.
    """
    return any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value)


def encrypt_credential(kms: KmsClient, credential_id: str, value: str) -> tuple[bytes, bytes]:
    """Зашифровать значение: ``(encrypted_value, encrypted_dek)``.

    Одноразовый DEK шифрует значение AES-256-GCM, идентификатор креденшла — associated data:
    шифротекст одной строки, переставленный в другую, не расшифруется.
    """
    dek = os.urandom(_DEK_LEN)
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(dek).encrypt(nonce, value.encode("utf-8"), credential_id.encode("utf-8"))
    return nonce + ciphertext, kms.encrypt_dek(dek)


def decrypt_credential(
    kms: KmsClient, credential_id: str, encrypted_value: bytes, encrypted_dek: bytes
) -> str:
    """Расшифровать значение. Ошибка расшифровки поднимается наружу как есть."""
    dek = kms.decrypt_dek(bytes(encrypted_dek))
    blob = bytes(encrypted_value)
    nonce, ciphertext = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    plaintext = AESGCM(dek).decrypt(nonce, ciphertext, credential_id.encode("utf-8"))
    return plaintext.decode("utf-8")
