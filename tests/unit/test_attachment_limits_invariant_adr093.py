"""Инвариант транспортного лимита для вложений (ADR-093).

Вложения едут внутри JSON как base64, который увеличивает объём на треть. Поэтому
``ATTACHMENT_REQUEST_BODY_LIMIT`` ограничивает сумму вложений РАНЬШЕ, чем прикладной
``ATTACHMENT_TOTAL_BYTES``, и если соотношение между ними нарушено, заявленная сумма
недостижима: клиент получает транспортный ``413`` вместо предметного
``422 attachments_total_too_large``. Ровно это и было до ADR-093 — тело 12 MiB при заявленной
сумме 10 MiB (требовалось 13.3 MiB).

Тест закрепляет соотношение, а не конкретные числа: оператор вправе калибровать лимиты
(TD-004), но обязан сохранять инвариант. Отдельно проверены дефолты — как регрессионный
маркер на случай молчаливой правки. Без I/O, сети и обращений к LLM.
"""

from __future__ import annotations

import math

import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings()


def test_body_limit_covers_base64_of_total_attachments(settings: Settings) -> None:
    # ИНВАРИАНТ (config.py, ADR-093): тело должно вмещать base64-раздутую сумму вложений.
    base64_inflated = math.ceil(settings.attachment_total_bytes * 4 / 3)
    assert settings.attachment_request_body_limit >= base64_inflated


def test_body_limit_covers_base64_of_single_largest_attachment(settings: Settings) -> None:
    # Одно вложение предельного размера обязано проходить в любом случае — иначе разрешённый
    # per-attachment потолок был бы фикцией. Проверяем оба класса: изображение и документ.
    largest = max(settings.attachment_max_bytes_image, settings.attachment_max_bytes_document)
    assert settings.attachment_request_body_limit >= math.ceil(largest * 4 / 3)


def test_total_is_not_below_single_attachment_cap(settings: Settings) -> None:
    # Сумма не может быть меньше одного разрешённого вложения: иначе одиночная предельная
    # картинка ловила бы attachments_total_too_large, противореча своему же лимиту.
    assert settings.attachment_total_bytes >= settings.attachment_max_bytes_image
    assert settings.attachment_total_bytes >= settings.attachment_max_bytes_document


def test_defaults_are_the_adr093_values(settings: Settings) -> None:
    # Регрессионный маркер на калибровку 2026-08-28.
    assert settings.attachment_max_bytes_image == 20 * 1024 * 1024
    assert settings.attachment_total_bytes == 60 * 1024 * 1024
    assert settings.attachment_request_body_limit == 80 * 1024 * 1024
    # Не менялись — жалоба и замер касались изображений.
    assert settings.attachment_max_bytes_document == 8 * 1024 * 1024
    assert settings.attachment_max_count == 10


def test_invariant_holds_under_operator_calibration() -> None:
    # Инвариант — про соотношение, а не про конкретные числа: перекалиброванный набор,
    # сохраняющий его, обязан оставаться валидным.
    s = Settings(
        ATTACHMENT_TOTAL_BYTES=30 * 1024 * 1024,
        ATTACHMENT_REQUEST_BODY_LIMIT=41 * 1024 * 1024,
    )
    assert s.attachment_request_body_limit >= math.ceil(s.attachment_total_bytes * 4 / 3)
