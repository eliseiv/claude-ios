"""ADR-112: the task-result read (avatar preparation cutout) uses the result-host union.

``FalClient.download_asset`` checks the url against ``FAL_UPLOAD_HOST_SUFFIXES ∪
MEDIA_RESULT_HOST_SUFFIXES``; ``upload`` / ``rehost_reference_image`` stay fal-only. The provider
HTTP boundary is replaced by an ``httpx.MockTransport`` that records every outgoing request, so
"no outgoing request" is observed, not assumed. The error reason lives only in the
``fal_call_outcome`` log record, so the handler is attached to the emitting logger directly.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from PIL import Image

from app.config import get_settings
from app.errors import UpstreamError
from app.media_generation import fal_client as fal_mod
from app.media_generation.fal_client import FalClient
from app.media_generation.features_service import (
    OPERATION_AVATAR_PREPARE,
    AvatarPreparationCompleter,
)
from app.media_generation.service import MediaAsset

_RELAY_SUFFIX = ".relay-adr112.example"
_RELAY_ASSET = "https://cdn.relay-adr112.example/files/cutout.png"
_FOREIGN_ASSET = "https://evil-adr112.example/files/cutout.png"
_FAL_ASSET = "https://v3.fal.media/files/cutout.png"


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


class _Net:
    """Records every outgoing request; answers GET with a PNG and the upload slot with JSON."""

    def __init__(self, slot: dict[str, str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.slot = slot or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and "/storage/upload/initiate" in str(request.url):
            return httpx.Response(200, json=self.slot)
        if request.method == "GET":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        return httpx.Response(200, json={})


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Net]:
    recorder = _Net()
    real_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(recorder.handler), **kwargs)

    monkeypatch.setattr(fal_mod.httpx, "AsyncClient", _factory)
    yield recorder


@pytest.fixture
def fal(monkeypatch: pytest.MonkeyPatch) -> Iterator[FalClient]:
    monkeypatch.setenv("FAL_API_KEY", "placeholder-adr112")
    monkeypatch.setenv("MEDIA_RESULT_HOST_SUFFIXES", _RELAY_SUFFIX)
    get_settings.cache_clear()
    yield FalClient(get_settings())
    get_settings.cache_clear()


@contextmanager
def _capture(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    target = logging.getLogger(fal_mod.logger.name)
    was_disabled, was_level = target.disabled, target.level
    target.disabled = False
    target.setLevel(logging.INFO)
    target.addHandler(caplog.handler)
    try:
        yield
    finally:
        target.removeHandler(caplog.handler)
        target.disabled, target.level = was_disabled, was_level


def _reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    out: list[str] = []
    # The same record can reach caplog twice (emitter handler + root propagation): dedupe by id.
    for rec in {id(r): r for r in caplog.records}.values():
        fields = getattr(rec, "extra_fields", {}) or {}
        if rec.getMessage() == "fal_call_outcome" and "reason" in fields:
            out.append(fields["reason"])
    return out


class _Repo:
    def __init__(self, job_id: uuid.UUID) -> None:
        self.row = SimpleNamespace(
            preparation_job_id=job_id, background_image_bytes=None, background_color=None
        )
        self.completed: list[dict[str, Any]] = []

    async def get_user_avatar(self, *, user_id: uuid.UUID, avatar_id: uuid.UUID) -> Any:
        return self.row

    async def complete_preparation(self, row: Any, **kwargs: Any) -> None:
        self.completed.append(kwargs)

    async def cancel_preparation(self, row: Any, *, job_id: uuid.UUID) -> None:  # pragma: no cover
        raise AssertionError("not expected")


def _job() -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        operation=OPERATION_AVATAR_PREPARE,
        operation_input={"avatarId": str(uuid.uuid4())},
    )


# ---- case 1: relay-host result completes via the completion hook (inv: union for result reads) --


async def test_avatar_preparation_completes_with_relay_host_result_via_completion_hook(
    fal: FalClient, net: _Net
) -> None:
    job = _job()
    repo = _Repo(job.id)
    completer = AvatarPreparationCompleter(repo=repo, fal=fal)  # type: ignore[arg-type]

    await completer.complete(
        job, [MediaAsset(url=_RELAY_ASSET, content_type="image/png", file_name=None)]
    )

    assert [str(r.url) for r in net.requests] == [_RELAY_ASSET]
    assert "authorization" not in {k.lower() for k in net.requests[0].headers}
    assert len(repo.completed) == 1
    done = repo.completed[0]
    assert done["job_id"] == job.id and done["media_type"] == "image/png"
    assert done["image_bytes"].startswith(b"\x89PNG")


# ---- case 2: host outside the union -> untrusted_asset_url, no request (inv: host check first) --


async def test_download_asset_foreign_host_rejected_without_request(
    fal: FalClient, net: _Net, caplog: pytest.LogCaptureFixture
) -> None:
    with _capture(caplog), pytest.raises(UpstreamError):
        await fal.download_asset(_FOREIGN_ASSET)
    assert net.requests == []
    assert _reasons(caplog) == ["untrusted_asset_url"]


# ---- case 3: http:// on an allowed host -> rejected, no request (inv: https required) ----------


@pytest.mark.parametrize(
    "url",
    ["http://cdn.relay-adr112.example/files/cutout.png", "http://v3.fal.media/files/cutout.png"],
)
async def test_download_asset_plain_http_on_allowed_host_rejected(
    fal: FalClient, net: _Net, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    with _capture(caplog), pytest.raises(UpstreamError):
        await fal.download_asset(url)
    assert net.requests == []
    assert _reasons(caplog) == ["untrusted_asset_url"]


# ---- case 4: upload slot on the relay host -> still untrusted_upload_url (inv: upload fal-only) -


async def test_upload_slot_on_relay_host_still_untrusted(
    fal: FalClient, net: _Net, caplog: pytest.LogCaptureFixture
) -> None:
    net.slot = {"upload_url": _RELAY_ASSET, "file_url": _RELAY_ASSET}
    with _capture(caplog), pytest.raises(UpstreamError):
        await fal.upload(content=b"x", media_type="image/png", file_name="a.png")
    assert [r.method for r in net.requests] == ["POST"], "no PUT to the relay slot"
    assert _reasons(caplog) == ["untrusted_upload_url"]


# ---- case 5: rehost_reference_image stays fal-only (inv: relay url returned untouched) ---------


async def test_rehost_reference_image_relay_url_returned_unchanged_without_fetch(
    fal: FalClient, net: _Net
) -> None:
    assert await fal.rehost_reference_image(_RELAY_ASSET) == _RELAY_ASSET
    assert net.requests == []


# ---- control: fal host still downloads (the union keeps the fal list) --------------------------


async def test_download_asset_fal_host_still_allowed(fal: FalClient, net: _Net) -> None:
    assert (await fal.download_asset(_FAL_ASSET)).startswith(b"\x89PNG")
    assert [str(r.url) for r in net.requests] == [_FAL_ASSET]
