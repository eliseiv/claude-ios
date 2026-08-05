"""Unit: the reference-image upload contract and its SSRF guard (ADR-062).

The upload exists because ``imageUrls``/``imageUrl`` accept only https URLs — fal fetches the
picture itself — while a phone only ever holds local bytes. Two things are worth pinning at this
level: the request shape (only images, only the allowlisted types, magic bytes checked) and the
host allowlist, because the second step of the upload PUTs a user's file to a URL an upstream
response named.
"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.media_generation.fal_client import FalClient
from app.schemas.media import MediaUploadRequest

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff" + b"\x00" * 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
        "FAL_API_KEY": "k",
    }
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


# --------------------------------- request schema ---------------------------------


def test_a_valid_upload_body_is_accepted() -> None:
    req = MediaUploadRequest(type="image", mediaType="image/png", filename="a.png", data=_b64(_PNG))
    assert req.mediaType == "image/png"


@pytest.mark.parametrize(
    "body",
    [
        # Documents and text are not reference images: nothing generates from them.
        {"type": "document", "mediaType": "application/pdf", "filename": "a.pdf", "data": "eA=="},
        {"type": "text", "mediaType": "text/plain", "filename": "a.txt", "data": "eA=="},
        # mediaType outside the image allowlist.
        {"type": "image", "mediaType": "image/svg+xml", "filename": "a.svg", "data": "eA=="},
        {"type": "image", "mediaType": "application/pdf", "filename": "a.pdf", "data": "eA=="},
        # Empty required values.
        {"type": "image", "mediaType": "image/png", "filename": "", "data": "eA=="},
        {"type": "image", "mediaType": "image/png", "filename": "   ", "data": "eA=="},
        {"type": "image", "mediaType": "image/png", "filename": "a.png", "data": ""},
        # StrictModel: an unknown key is a client mistake, not something to ignore.
        {
            "type": "image",
            "mediaType": "image/png",
            "filename": "a.png",
            "data": "eA==",
            "url": "https://example.com/a.png",
        },
    ],
)
def test_bad_upload_bodies_are_rejected(body: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        MediaUploadRequest(**body)  # type: ignore[arg-type]


def test_the_body_carries_no_url_field() -> None:
    """Uploading is the way to GET a URL; accepting one here would invite an SSRF fetch."""
    assert "url" not in MediaUploadRequest.model_fields


# --------------------------------- host allowlist ---------------------------------


def test_default_allowlist_accepts_the_hosts_fal_actually_serves() -> None:
    client = FalClient(_settings())
    for url in (
        "https://v3b.fal.media/files/b/abc/photo.jpg",
        "https://v3.fal.media/files/b/abc/photo.jpg",
        "https://rest.fal.ai/storage/upload/abc",
        "https://storage.googleapis.com/fal-bucket/abc",
    ):
        assert client._upload_host_allowed(url), url


@pytest.mark.parametrize(
    "url",
    [
        # A host that merely CONTAINS an allowed suffix must not pass.
        "https://fal.media.evil.com/upload",
        "https://evil.com/upload",
        # Suffix matching must not be fooled by a lookalike registrable domain.
        "https://notfal.ai/upload",
        # http, and schemes that would read from us rather than upload.
        "http://v3b.fal.media/files/b/abc",
        "file:///etc/passwd",
        "https:///no-host",
        "not a url at all",
    ],
)
def test_allowlist_rejects_everything_else(url: str) -> None:
    assert not FalClient(_settings())._upload_host_allowed(url)


def test_an_empty_allowlist_trusts_nothing() -> None:
    """Fail closed: an operator who blanks the list gets no uploads, not unrestricted ones."""
    client = FalClient(_settings(FAL_UPLOAD_HOST_SUFFIXES=""))
    assert not client._upload_host_allowed("https://v3b.fal.media/files/b/abc")


def test_operator_formatting_does_not_widen_or_empty_the_allowlist() -> None:
    settings = _settings(FAL_UPLOAD_HOST_SUFFIXES="  .FAL.MEDIA , ,.example.org,  ")
    assert settings.fal_upload_host_suffixes() == (".fal.media", ".example.org")
    client = FalClient(settings)
    assert client._upload_host_allowed("https://v3b.fal.media/x")
    assert not client._upload_host_allowed("https://rest.fal.ai/x")


def test_a_bare_apex_host_matches_its_own_suffix() -> None:
    """`.fal.media` should cover `fal.media` itself — the SDK falls back to the apex host."""
    client = FalClient(_settings(FAL_UPLOAD_HOST_SUFFIXES=".fal.media"))
    assert client._upload_host_allowed("https://fal.media/files/b/abc")


# --------------------------------- limits ---------------------------------


def test_transport_limit_covers_the_base64_inflated_file() -> None:
    """Invariant from ADR-062 §3: a file at the cap must still fit in the body limit."""
    settings = _settings()
    inflated = -(-settings.media_upload_max_bytes * 4 // 3)  # ceil
    assert settings.media_upload_request_body_limit >= inflated + 256 * 1024


def test_defaults_are_the_documented_ones() -> None:
    settings = _settings()
    assert settings.media_upload_max_bytes == 10 * 1024 * 1024
    assert settings.media_upload_request_body_limit == 16 * 1024 * 1024
    assert settings.fal_rest_base == "https://rest.fal.ai"


# --------------------------------- retention ---------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", False),  # no preference: fal's own policy applies
        ("0", None),  # never expires
        ("86400", 86400),
        ("-5", False),  # nonsense degrades to "no preference", never to "delete now"
        ("abc", False),
        ("  3600  ", 3600),
    ],
)
def test_retention_parsing(raw: str, expected: object) -> None:
    assert _settings(FAL_ASSET_RETENTION_SECONDS=raw).fal_asset_retention() == expected


def test_a_configured_retention_reaches_the_upload_as_a_header() -> None:
    client = FalClient(_settings(FAL_ASSET_RETENTION_SECONDS="0"))
    header = client._lifecycle_header()["X-Fal-Object-Lifecycle-Preference"]
    assert header == '{"expiration_duration_seconds": null}'


def test_jpeg_and_png_signatures_are_distinguished() -> None:
    """A renamed file must not pass as an image of the declared type."""
    from app.chat.attachments import _check_magic_bytes
    from app.errors import ValidationFailedError

    _check_magic_bytes("image/png", _PNG)
    _check_magic_bytes("image/jpeg", _JPEG)
    with pytest.raises(ValidationFailedError):
        _check_magic_bytes("image/png", _JPEG)
