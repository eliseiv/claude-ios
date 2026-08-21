"""Unit: media-asset signed URL (HMAC-SHA256 + TTL) — ADR-085."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.media_generation.signed_url import (
    build_token,
    public_asset_url,
    verify_token,
)
from app.website.signed_url import (
    PreviewSecretMissingError,
)
from app.website.signed_url import (
    build_token as build_preview_token,
)

_SECRET = "preview-secret-unit-0123456789abcdef0123456789abcdef"
_FAL = "https://v3.fal.media/files/b/out.mp4"


@pytest.fixture
def media_secret() -> Iterator[None]:
    settings = get_settings()
    orig_secret = settings.preview_url_secret
    orig_ttl = settings.media_download_ttl_seconds
    orig_domain = settings.service_domain
    settings.preview_url_secret = _SECRET
    settings.media_download_ttl_seconds = 86400
    settings.service_domain = "zenquelo.shop"
    yield
    settings.preview_url_secret = orig_secret
    settings.media_download_ttl_seconds = orig_ttl
    settings.service_domain = orig_domain


def test_build_verify_roundtrip_ok(media_secret: None) -> None:
    job_id = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(job_id=job_id, owner_user_id=owner, index=0, now=1000)
    assert (
        verify_token(job_id=job_id, owner_user_id=owner, index=0, token=signed.token, now=1001)
        is True
    )
    assert signed.expires_at == 1000 + 86400


def test_tamper_job_id_rejected(media_secret: None) -> None:
    owner = uuid.uuid4()
    signed = build_token(job_id=uuid.uuid4(), owner_user_id=owner, index=0, now=1000)
    assert (
        verify_token(
            job_id=uuid.uuid4(), owner_user_id=owner, index=0, token=signed.token, now=1001
        )
        is False
    )


def test_tamper_owner_rejected(media_secret: None) -> None:
    job_id = uuid.uuid4()
    signed = build_token(job_id=job_id, owner_user_id=uuid.uuid4(), index=0, now=1000)
    assert (
        verify_token(
            job_id=job_id, owner_user_id=uuid.uuid4(), index=0, token=signed.token, now=1001
        )
        is False
    )


def test_tamper_index_rejected(media_secret: None) -> None:
    job_id = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(job_id=job_id, owner_user_id=owner, index=0, now=1000)
    assert (
        verify_token(job_id=job_id, owner_user_id=owner, index=1, token=signed.token, now=1001)
        is False
    )


def test_expired_token_rejected(media_secret: None) -> None:
    job_id = uuid.uuid4()
    owner = uuid.uuid4()
    signed = build_token(job_id=job_id, owner_user_id=owner, index=0, now=1000)
    assert (
        verify_token(
            job_id=job_id, owner_user_id=owner, index=0, token=signed.token, now=1000 + 86401
        )
        is False
    )


def test_preview_token_cannot_unlock_media(media_secret: None) -> None:
    job_id = uuid.uuid4()
    owner = uuid.uuid4()
    preview = build_preview_token(project_id=job_id, owner_user_id=owner, now=1000)
    assert (
        verify_token(job_id=job_id, owner_user_id=owner, index=0, token=preview.token, now=1001)
        is False
    )


def test_public_url_rewrites_fal_host(media_secret: None) -> None:
    job_id = uuid.uuid4()
    owner = uuid.uuid4()
    url = public_asset_url(job_id=job_id, owner_user_id=owner, index=0, stored_url=_FAL)
    assert url.startswith(f"https://zenquelo.shop/v1/media/jobs/{job_id}/assets/0/")
    assert "fal.media" not in url


def test_public_url_leaves_non_fal_host(media_secret: None) -> None:
    stored = "https://cdn.example/a.png"
    url = public_asset_url(
        job_id=uuid.uuid4(), owner_user_id=uuid.uuid4(), index=0, stored_url=stored
    )
    assert url == stored


def test_public_url_passthrough_without_secret() -> None:
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = ""
    try:
        url = public_asset_url(
            job_id=uuid.uuid4(), owner_user_id=uuid.uuid4(), index=0, stored_url=_FAL
        )
        assert url == _FAL
    finally:
        settings.preview_url_secret = orig


def test_missing_secret_raises_on_build() -> None:
    settings = get_settings()
    orig = settings.preview_url_secret
    settings.preview_url_secret = ""
    try:
        with pytest.raises(PreviewSecretMissingError):
            build_token(job_id=uuid.uuid4(), owner_user_id=uuid.uuid4(), index=0)
    finally:
        settings.preview_url_secret = orig
