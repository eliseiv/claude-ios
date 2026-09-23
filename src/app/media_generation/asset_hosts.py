"""Allowlist for outgoing fetches of provider-hosted files (ADR-062 §4, ADR-085, ADR-108 §7).

Shared by upload-slot checks, the media download proxy, the client-facing signed URL and the
proxy-webhook asset filter: a URL we did not mint ourselves must live on an operator-approved
host, or we refuse to follow it (SSRF).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.config import get_settings


def fal_asset_host_allowed(url: str, suffixes: tuple[str, ...] | None = None) -> bool:
    """Whether ``url`` is https and its host matches the allowlist.

    ``suffixes=None`` takes the default result-host list
    ``FAL_UPLOAD_HOST_SUFFIXES ∪ MEDIA_RESULT_HOST_SUFFIXES`` (ADR-108 §7); a caller that must stay
    on fal hosts only (``FalClient._upload_host_allowed``) passes the fal list explicitly.

    Empty suffix list fails closed. Comparison is lowercase; a suffix ``.fal.media`` matches
    both ``v3.fal.media`` and the apex ``fal.media``.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    allowed = suffixes if suffixes is not None else get_settings().media_asset_host_suffixes()
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in allowed)
