"""Unit: пост-модерация крупного результата (ADR-086 §7).

`400 file_too_large` провайдера → ассет скачивается (внешняя граница — respx), уменьшается и
проверяется повторно; чистый повтор → `completed`. Любой другой `400`, повторный `400` или
неподготовимый ассет → `failed` с возвратом кредитов СРАЗУ, без ожидания дедлайна.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
import respx

from app.config import Settings
from app.media_generation.catalog import KIND_IMAGE
from app.media_generation.service import (
    UNCHECKABLE_RESULT_ERROR,
    MediaAsset,
    MediaGenerationService,
)
from app.moderation.service import ModerationService
from tests.images import image_bytes

_ASSET_URL = "https://v3.fal.media/files/result.png"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        MODERATION_ENABLED="true", MODERATION_API_KEY="sk-test-moderation"
    )


def _bad_request(code: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://api.openai.com/v1/moderations")
    body = {"error": {"message": "bad", "code": code}}
    return openai.BadRequestError(
        "bad", response=httpx.Response(400, request=request, json=body), body=body["error"]
    )


class _ScriptedModerations:
    """Провайдер модерации: исходы по очереди (исключение или ответ)."""

    def __init__(self, *outcomes: Any) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_CLEAN = SimpleNamespace(results=[SimpleNamespace(flagged=False, categories={})])


class _Wallet:
    def __init__(self) -> None:
        self.granted: list[int] = []

    async def grant(self, **kwargs: Any) -> None:
        self.granted.append(kwargs["amount"])


class _Repo:
    def __init__(self) -> None:
        self.completed: dict[str, Any] | None = None
        self.failed: dict[str, Any] | None = None

    async def mark_completed(self, job: Any, **kwargs: Any) -> None:
        self.completed = kwargs

    async def mark_failed(self, job: Any, **kwargs: Any) -> None:
        self.failed = kwargs


def _build(*outcomes: Any) -> tuple[MediaGenerationService, _ScriptedModerations, _Repo, _Wallet]:
    settings = _settings()
    moderation = ModerationService(settings=settings)
    fake = _ScriptedModerations(*outcomes)
    moderation._client = SimpleNamespace(moderations=fake)  # noqa: SLF001 — исходящий клиент
    repo, wallet = _Repo(), _Wallet()
    service = MediaGenerationService(
        repo=repo,  # type: ignore[arg-type]
        fal=SimpleNamespace(),  # type: ignore[arg-type]
        wallet=wallet,  # type: ignore[arg-type]
        settings=settings,
        moderation=moderation,
    )
    return service, fake, repo, wallet


def _job() -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        model_id="fal-ai/x",
        kind=KIND_IMAGE,
        credits_charged=7,
        credits_refunded=False,
        visible_in_history=False,
        provider="",
    )


async def _complete(service: MediaGenerationService) -> Any:
    asset = MediaAsset(url=_ASSET_URL, content_type="image/png", file_name="result.png")
    return await service._complete_run(  # noqa: SLF001
        _job(), result={"assets": [{"url": _ASSET_URL}]}, assets=[asset]
    )


@pytest.mark.asyncio
@respx.mock
async def test_file_too_large_is_downscaled_rechecked_and_completed() -> None:
    respx.get(_ASSET_URL).mock(
        return_value=httpx.Response(200, content=image_bytes("image/png", (3000, 1000)))
    )
    service, fake, repo, wallet = _build(_bad_request("file_too_large"), _CLEAN)

    await _complete(service)

    assert repo.completed is not None and repo.failed is None
    assert repo.completed["moderation"]["status"] == "passed"
    assert wallet.granted == []
    assert len(fake.calls) == 2
    rechecked = str(fake.calls[1]["input"])
    assert "data:image/jpeg;base64," in rechecked and _ASSET_URL not in rechecked


@pytest.mark.asyncio
@respx.mock
async def test_second_rejection_after_downscale_fails_with_refund_at_once() -> None:
    respx.get(_ASSET_URL).mock(return_value=httpx.Response(200, content=image_bytes()))
    service, _, repo, wallet = _build(
        _bad_request("file_too_large"), _bad_request("file_too_large")
    )

    await _complete(service)

    assert repo.completed is None
    assert repo.failed is not None and repo.failed["error"] == UNCHECKABLE_RESULT_ERROR
    assert repo.failed["refunded"] is True and wallet.granted == [7]


@pytest.mark.asyncio
@respx.mock
async def test_undecodable_asset_fails_with_refund_without_recheck() -> None:
    respx.get(_ASSET_URL).mock(return_value=httpx.Response(200, content=b"not an image"))
    service, fake, repo, wallet = _build(_bad_request("file_too_large"))

    await _complete(service)

    assert repo.failed is not None and repo.failed["error"] == UNCHECKABLE_RESULT_ERROR
    assert wallet.granted == [7]
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_other_bad_request_fails_with_refund_without_download() -> None:
    service, fake, repo, wallet = _build(_bad_request("invalid_image"))

    with respx.mock(assert_all_called=False) as router:
        await _complete(service)
        assert not router.calls, "иной 400 — без скачивания"

    assert repo.failed is not None and repo.failed["error"] == UNCHECKABLE_RESULT_ERROR
    assert wallet.granted == [7]
    assert len(fake.calls) == 1
