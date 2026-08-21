"""Signed media-asset URLs (HMAC-SHA256 + TTL) — ADR-085.

token = base64url(exp) . base64url(HMAC_SHA256(PREVIEW_URL_SECRET,
    "media-asset|{jobId}|{ownerUserId}|{index}|{exp}"))

Same secret as preview (already on every instance). The ``media-asset|`` prefix keeps a
preview token from unlocking a video and vice versa. Verification is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass

from app.config import get_settings
from app.media_generation.asset_hosts import fal_asset_host_allowed
from app.observability.logging import log_event
from app.website.signed_url import PreviewSecretMissingError

logger = logging.getLogger("app.media_generation.signed_url")

_CANON_PREFIX = "media-asset"


@dataclass(frozen=True)
class SignedMediaAsset:
    token: str
    expires_at: int  # unix ts


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _secret() -> bytes:
    secret = get_settings().preview_url_secret
    if not secret:
        raise PreviewSecretMissingError("PREVIEW_URL_SECRET is not configured")
    return secret.encode("utf-8")


def _canonical(*, job_id: uuid.UUID, owner_user_id: uuid.UUID, index: int, exp: int) -> bytes:
    return f"{_CANON_PREFIX}|{job_id}|{owner_user_id}|{index}|{exp}".encode()


def _sign(*, job_id: uuid.UUID, owner_user_id: uuid.UUID, index: int, exp: int) -> bytes:
    return hmac.new(
        _secret(),
        _canonical(job_id=job_id, owner_user_id=owner_user_id, index=index, exp=exp),
        hashlib.sha256,
    ).digest()


def build_token(
    *,
    job_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    index: int,
    now: int | None = None,
) -> SignedMediaAsset:
    """Build a signed token for one asset of a job owned by ``owner_user_id``."""
    issued = now if now is not None else int(time.time())
    exp = issued + get_settings().media_download_ttl_seconds
    mac = _sign(job_id=job_id, owner_user_id=owner_user_id, index=index, exp=exp)
    token = f"{_b64url_encode(str(exp).encode('ascii'))}.{_b64url_encode(mac)}"
    return SignedMediaAsset(token=token, expires_at=exp)


def verify_token(
    *,
    job_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    index: int,
    token: str,
    now: int | None = None,
) -> bool:
    """Verify HMAC (constant-time) + TTL. False on any mismatch/expiry; never raises."""
    current = now if now is not None else int(time.time())
    parts = token.split(".")
    if len(parts) != 2:
        return False
    exp_part, mac_part = parts
    try:
        exp = int(_b64url_decode(exp_part).decode("ascii"))
        presented_mac = _b64url_decode(mac_part)
    except (ValueError, UnicodeDecodeError):
        return False

    try:
        expected_mac = _sign(job_id=job_id, owner_user_id=owner_user_id, index=index, exp=exp)
    except PreviewSecretMissingError:
        return False
    mac_ok = hmac.compare_digest(presented_mac, expected_mac)
    if not mac_ok:
        return False
    return current <= exp


def asset_path(*, job_id: uuid.UUID, index: int, token: str) -> str:
    return f"/v1/media/jobs/{job_id}/assets/{index}/{token}"


def public_asset_url(
    *,
    job_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    index: int,
    stored_url: str,
) -> str:
    """Client-facing URL: signed path on SERVICE_DOMAIN, or ``stored_url`` if we cannot rewrite.

    Non-fal hosts are left as-is (tests and unexpected providers). A missing preview secret
    must not break job polling — log and hand the stored URL through.
    """
    if not fal_asset_host_allowed(stored_url):
        return stored_url
    try:
        signed = build_token(job_id=job_id, owner_user_id=owner_user_id, index=index)
    except PreviewSecretMissingError:
        log_event(
            logger,
            logging.WARNING,
            "media_asset_url_secret_missing",
            jobId=str(job_id),
        )
        return stored_url
    path = asset_path(job_id=job_id, index=index, token=signed.token)
    domain = get_settings().normalized_service_domain()
    if not domain:
        return path
    return f"https://{domain}{path}"
