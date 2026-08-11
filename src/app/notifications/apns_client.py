"""Apple Push Notification service client (token-based JWT auth, ADR-067 / TD-011).

Best-effort outbound HTTP/2 to APNs. Misconfiguration (empty credentials) makes
``configured`` false and ``send`` a no-op — registration of device tokens still works.
Push failures never raise into the media completion path; callers treat the return value.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt

from app.config import Settings
from app.observability.logging import log_event

logger = logging.getLogger("app.notifications.apns")

ApnsResult = Literal["sent", "unregistered", "failed", "skipped"]


@dataclass(frozen=True)
class MediaReadyPush:
    """Payload for a media-generation-completed alert (mutable-content for NSE)."""

    job_id: str
    kind: str  # image | video
    media_url: str
    title: str
    body: str


class ApnsClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cached_jwt: str | None = None
        self._cached_jwt_exp: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(
            self._settings.apns_key_id.strip()
            and self._settings.apns_team_id.strip()
            and self._settings.apns_topic.strip()
            and self._settings.resolve_apns_auth_key()
        )

    def _host(self) -> str:
        env = self._settings.apns_environment.strip().lower()
        if env == "production":
            return "https://api.push.apple.com"
        return "https://api.sandbox.push.apple.com"

    def _bearer(self) -> str:
        now = time.time()
        # APNs JWTs may live up to 60 minutes; refresh a bit earlier.
        if self._cached_jwt and now < self._cached_jwt_exp - 60:
            return self._cached_jwt
        issued = int(now)
        token = jwt.encode(
            {"iss": self._settings.apns_team_id.strip(), "iat": issued},
            self._settings.resolve_apns_auth_key(),
            algorithm="ES256",
            headers={"alg": "ES256", "kid": self._settings.apns_key_id.strip()},
        )
        self._cached_jwt = token
        self._cached_jwt_exp = issued + 50 * 60
        return token

    def build_media_ready_payload(self, push: MediaReadyPush) -> dict[str, Any]:
        return {
            "aps": {
                "alert": {"title": push.title, "body": push.body},
                "mutable-content": 1,
                "sound": "default",
            },
            "jobId": push.job_id,
            "kind": push.kind,
            "mediaUrl": push.media_url,
        }

    async def send(self, *, device_token: str, payload: dict[str, Any]) -> ApnsResult:
        if not self.configured:
            return "skipped"
        token = device_token.strip().replace(" ", "")
        if not token:
            return "failed"
        url = f"{self._host()}/3/device/{token}"
        headers = {
            "authorization": f"bearer {self._bearer()}",
            "apns-topic": self._settings.apns_topic.strip(),
            "apns-push-type": "alert",
            "apns-priority": "10",
            "content-type": "application/json",
        }
        timeout = self._settings.apns_timeout_seconds
        try:
            async with httpx.AsyncClient(http2=True, timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            log_event(
                logger,
                logging.WARNING,
                "apns_send_transport_error",
                errorType=type(exc).__name__,
            )
            return "failed"

        if response.status_code == 200:
            return "sent"
        if response.status_code == 410:
            log_event(logger, logging.INFO, "apns_unregistered")
            return "unregistered"

        # Do not log the device token or full APNs body (may echo the token).
        log_event(
            logger,
            logging.WARNING,
            "apns_send_rejected",
            status=response.status_code,
            reason=(response.headers.get("apns-id") or "")[:64],
        )
        return "failed"


def media_ready_copy(*, kind: str) -> tuple[str, str]:
    """Default alert copy (English; iOS may refine via Notification Service Extension)."""
    if kind == "video":
        return "Ready", "Your video is ready"
    return "Ready", "Your photo is ready"
