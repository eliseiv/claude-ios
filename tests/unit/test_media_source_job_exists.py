"""Unit: выдуманный sourceJobId отбивается в ходе, а не 422 после нажатия (прод lunexoro).

Модель присылала идентификатор, которого нет ни у одной задачи. `media.ask_params` проверял
только форму UUID, поэтому выдумка доезжала до карточки выбора и падала уже при отправке формы —
422 прилетал человеку ПОСЛЕ нажатия, когда исправлять некому: модели в том шаге нет.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.chat.global_tools import MEDIA_INVALID_ERROR_CODE, GlobalToolHandlers
from app.chat.tools import TOOL_MEDIA_ASK_PARAMS


def _media(*, exists: bool) -> AsyncMock:
    media = AsyncMock()
    media.credits_for = lambda m: m.default_credits
    media.job_exists = AsyncMock(return_value=exists)
    return media


@pytest.mark.asyncio
async def test_invented_source_job_id_degrades_to_tool_error() -> None:
    media = _media(exists=False)
    out = await GlobalToolHandlers(media=media).execute(
        tool_name=TOOL_MEDIA_ASK_PARAMS,
        args={"kind": "image", "prompt": "то же, но зимой", "sourceJobId": str(uuid.uuid4())},
        user_id=uuid.uuid4(),
    )
    # Ошибка инструмента, а не исключение: модель ещё в ходе и может повторить без sourceJobId.
    assert out.is_error is True
    assert out.error_code == MEDIA_INVALID_ERROR_CODE
    assert "not found" in (out.error_message or "")
    media.job_exists.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_source_job_id_opens_the_wizard() -> None:
    media = _media(exists=True)
    out = await GlobalToolHandlers(media=media).execute(
        tool_name=TOOL_MEDIA_ASK_PARAMS,
        args={"kind": "image", "prompt": "то же, но зимой", "sourceJobId": str(uuid.uuid4())},
        user_id=uuid.uuid4(),
    )
    assert out.is_error is False


@pytest.mark.asyncio
async def test_malformed_source_job_id_is_not_probed() -> None:
    # Разбор формы идёт первым: несуществующая форма не должна ходить в базу.
    media = _media(exists=True)
    out = await GlobalToolHandlers(media=media).execute(
        tool_name=TOOL_MEDIA_ASK_PARAMS,
        args={"kind": "image", "prompt": "то же, но зимой", "sourceJobId": "job-12"},
        user_id=uuid.uuid4(),
    )
    assert out.is_error is True
    assert "must be a UUID" in (out.error_message or "")
    media.job_exists.assert_not_awaited()
