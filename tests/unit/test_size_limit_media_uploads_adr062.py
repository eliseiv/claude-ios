"""Unit: the per-path body limit for the reference-image upload (ADR-062 §3).

`POST /v1/media/uploads` carries a base64 image, so it needs a raised transport limit — but only
it. Everything else under /v1/media/* posts small JSON and must keep the general cap, otherwise a
single feature widens the gateway for the whole prefix. These pin `_limit_for` path matching
directly plus the config invariant that ties the two limits together. No I/O.
"""

from __future__ import annotations

import math
import uuid

import pytest

from app.api_gateway.middleware import SizeLimitMiddleware
from app.config import Settings

_UPLOADS = "/v1/media/uploads"


async def _noop_app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
    return None


@pytest.fixture
def middleware() -> SizeLimitMiddleware:
    return SizeLimitMiddleware(_noop_app)


@pytest.fixture
def settings() -> Settings:
    return Settings()


def test_invariant_body_limit_covers_the_base64_inflated_file(settings: Settings) -> None:
    """Source of truth is MEDIA_UPLOAD_MAX_BYTES; the transport limit is derived from it.

    base64 inflates a file by 4/3, and the JSON envelope adds field names, quoting and escaping on
    top. If this invariant breaks, a file the service would happily accept dies at the gateway with
    a 413 that names no reason the client can act on.
    """
    inflated = math.ceil(settings.media_upload_max_bytes * 4 / 3)
    assert settings.media_upload_request_body_limit >= inflated
    assert settings.media_upload_request_body_limit - inflated >= 256 * 1024
    assert settings.media_upload_max_bytes == 10 * 1024 * 1024
    assert settings.media_upload_request_body_limit == 16 * 1024 * 1024


def test_the_upload_path_gets_the_raised_limit(
    middleware: SizeLimitMiddleware, settings: Settings
) -> None:
    assert middleware._limit_for(_UPLOADS) == settings.media_upload_request_body_limit


@pytest.mark.parametrize(
    "path",
    [
        "/v1/media/models",
        "/v1/media/images",
        "/v1/media/videos",
        "/v1/media/jobs",
        f"/v1/media/jobs/{uuid.uuid4()}",
        # Near-misses: the rule is an exact path, not a prefix.
        "/v1/media/uploads/",
        "/v1/media/uploads/abc",
        "/v1/media/uploadsx",
        "/v1/mediauploads",
    ],
)
def test_every_other_media_path_keeps_the_general_limit(
    middleware: SizeLimitMiddleware, settings: Settings, path: str
) -> None:
    assert middleware._limit_for(path) == settings.size_limit_body


def test_the_other_raised_paths_are_unaffected(
    middleware: SizeLimitMiddleware, settings: Settings
) -> None:
    """The three raises are independent; adding one must not shadow the others."""
    assert middleware._limit_for("/v1/chat/run") == settings.attachment_request_body_limit
    assert (
        middleware._limit_for(f"/v1/workspaces/{uuid.uuid4()}/files")
        == settings.workspace_request_body_limit
    )
