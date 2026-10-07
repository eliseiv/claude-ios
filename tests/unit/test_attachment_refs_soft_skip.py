"""Unit: сбой fal-загрузки вложения хода — МЯГКИЙ пропуск, а не падение хода.

Регрессия, которую закрывает файл: ветка мягкого пропуска логировала через
`logger.warning(..., extra={"filename": ...})`. Сырой `extra` кладёт ключи ПРЯМО в `LogRecord`,
а `filename` — его собственный атрибут, поэтому `makeRecord` поднимал `KeyError` — и код,
написанный ради «пропустить и жить дальше», ронял ход в `500` при КАЖДОМ сбое fal.

Ловимых исключений четыре, и каждое проверяется отдельно: `except`-кортеж легко сузить правкой,
и тогда одно из них снова полетит наружу.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from app.chat.attachment_refs import upload_image_ref
from app.chat.attachments import ImageAttachmentRef
from app.errors import (
    MediaGenerationNotConfiguredError,
    PayloadTooLargeError,
    UpstreamError,
    ValidationFailedError,
)
from app.media_generation.service import UploadedFile

_SKIP_EVENT = "chat_attachment_fal_upload_skipped"
_LOGGER_NAME = "app.chat.attachment_refs"


def _image(filename: str = "selfie.png") -> ImageAttachmentRef:
    return ImageAttachmentRef(media_type="image/png", filename=filename, data="AAAA")


@pytest.fixture(autouse=True)
def _logger_enabled() -> Iterator[None]:
    """Force-enable the module logger — `caplog` here must not depend on test order.

    Anything in the session that reconfigures logging through `logging.config.fileConfig` /
    `dictConfig` leaves `disabled = True` on every logger it does not name. The suite's alembic
    migration did exactly that (`migrations/env.py` -> `fileConfig`), which is why these tests were
    green when `tests/unit` ran on its own and red in the single-process CI run, where the
    integration/e2e migration runs first. A disabled logger drops records silently, so the
    assertions below failed on an empty `caplog.records` instead of on a real regression. The
    leak itself is fixed at the source (tests/conftest.py::_migrated); this keeps the file
    hermetic against any other logging reconfiguration too.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    prev_disabled = logger.disabled
    logger.disabled = False
    try:
        yield
    finally:
        logger.disabled = prev_disabled


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(MediaGenerationNotConfiguredError("fal off"), id="not-configured"),
        pytest.param(PayloadTooLargeError("too big"), id="payload-too-large"),
        pytest.param(ValidationFailedError("bad image"), id="validation-failed"),
        pytest.param(UpstreamError("fal 502"), id="upstream"),
    ],
)
async def test_upload_failure_is_skipped_softly(
    exc: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    """Каждое из четырёх ловимых исключений: ход НЕ падает, ref просто не появляется."""
    media = AsyncMock()
    media.upload_reference_image = AsyncMock(side_effect=exc)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        ref = await upload_image_ref(media, _image())

    assert ref is None
    # Сообщение уходит СТРУКТУРНЫМ событием: имя файла живёт в `extra_fields`, а не в
    # зарезервированном атрибуте `LogRecord.filename`, из-за которого ветка падала.
    events = [r.getMessage() for r in caplog.records if r.name == _LOGGER_NAME]
    assert _SKIP_EVENT in events
    record = next(r for r in caplog.records if r.getMessage() == _SKIP_EVENT)
    fields = getattr(record, "extra_fields", {})
    assert fields["error"] == type(exc).__name__
    assert fields["fileName"] == "selfie.png"


async def test_upload_success_returns_fal_ref() -> None:
    """Успешная загрузка → ref с fal-url и TTL-сроком."""
    media = AsyncMock()
    media.upload_reference_image = AsyncMock(
        return_value=UploadedFile(
            url="https://fal.media/files/ok.png",
            media_type="image/png",
            size=10,
            expires_at=None,
        )
    )

    ref = await upload_image_ref(media, _image("good.png"))

    assert ref is not None
    assert ref["filename"] == "good.png"
    assert ref["url"] == "https://fal.media/files/ok.png"
    assert ref["expiresAt"].endswith("Z")


async def test_upload_without_media_service_returns_no_ref() -> None:
    """Media на инстансе не сконфигурирована → None и ни одного обращения к fal."""
    assert await upload_image_ref(None, _image()) is None
