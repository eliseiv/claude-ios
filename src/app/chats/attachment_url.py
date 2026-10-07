"""Signed chat-attachment URLs (HMAC-SHA256 + TTL) — ADR-120 §3.

token = base64url(exp) . base64url(HMAC_SHA256(PREVIEW_URL_SECRET,
    "chat-attachment|{attachmentId}|{sessionId}|{ownerUserId}|{exp}"))

The ``chat-attachment|`` prefix keeps a media-asset or preview token from opening an attachment
and vice versa. Verification is constant-time.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass

from app.config import get_settings
from app.website.signed_url import PreviewSecretMissingError

_CANON_PREFIX = "chat-attachment"


@dataclass(frozen=True)
class SignedAttachmentUrl:
    url: str
    expires_at: int  # unix ts


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _secret() -> bytes:
    secret = get_settings().preview_url_secret
    if not secret:
        raise PreviewSecretMissingError("PREVIEW_URL_SECRET is not configured")
    return secret.encode("utf-8")


def _sign(
    *, attachment_id: uuid.UUID, session_id: uuid.UUID, owner_user_id: uuid.UUID, exp: int
) -> bytes:
    canonical = f"{_CANON_PREFIX}|{attachment_id}|{session_id}|{owner_user_id}|{exp}".encode()
    return hmac.new(_secret(), canonical, hashlib.sha256).digest()


def attachment_path(*, session_id: uuid.UUID, attachment_id: uuid.UUID, token: str) -> str:
    return f"/v1/chats/{session_id}/attachments/{attachment_id}/{token}"


def build_signed_url(
    *,
    attachment_id: uuid.UUID,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    now: int | None = None,
) -> SignedAttachmentUrl:
    """Absolute signed URL on SERVICE_DOMAIN (relative path when it is unset).

    Raises ``PreviewSecretMissingError`` when ``PREVIEW_URL_SECRET`` is not configured.
    """
    settings = get_settings()
    issued = now if now is not None else int(time.time())
    exp = issued + settings.chat_attachment_url_ttl()
    mac = _sign(
        attachment_id=attachment_id, session_id=session_id, owner_user_id=owner_user_id, exp=exp
    )
    token = f"{_b64url_encode(str(exp).encode('ascii'))}.{_b64url_encode(mac)}"
    path = attachment_path(session_id=session_id, attachment_id=attachment_id, token=token)
    domain = settings.normalized_service_domain()
    return SignedAttachmentUrl(url=f"https://{domain}{path}" if domain else path, expires_at=exp)


def verify_token(
    *,
    attachment_id: uuid.UUID,
    session_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    token: str,
    now: int | None = None,
) -> int | None:
    """Return the token's ``exp`` when HMAC (constant-time) and TTL hold; ``None`` otherwise."""
    current = now if now is not None else int(time.time())
    parts = token.split(".")
    if len(parts) != 2:
        return None
    exp_part, mac_part = parts
    try:
        exp = int(_b64url_decode(exp_part).decode("ascii"))
        presented_mac = _b64url_decode(mac_part)
    except (ValueError, UnicodeDecodeError):
        return None
    try:
        expected_mac = _sign(
            attachment_id=attachment_id,
            session_id=session_id,
            owner_user_id=owner_user_id,
            exp=exp,
        )
    except PreviewSecretMissingError:
        return None
    if not hmac.compare_digest(presented_mac, expected_mac) or current > exp:
        return None
    return exp


def iso_expires_at(exp: int) -> str:
    return datetime.datetime.fromtimestamp(exp, tz=datetime.UTC).isoformat().replace("+00:00", "Z")
