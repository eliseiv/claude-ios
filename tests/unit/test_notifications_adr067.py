"""Unit: deviceId resolve, APNs payload, media-ready push skip/claim (ADR-067)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.errors import ValidationFailedError
from app.notifications.apns_client import ApnsClient, MediaReadyPush, media_ready_copy
from app.notifications.push_service import MediaPushService
from app.notifications.service import NotificationsService
from app.preferences.service import PreferencesView


def test_resolve_device_id_prefers_body_then_jwt_then_header() -> None:
    assert (
        NotificationsService.resolve_device_id(
            body_device_id=" body ",
            jwt_device_id="jwt",
            header_device_id="hdr",
        )
        == "body"
    )
    assert (
        NotificationsService.resolve_device_id(
            body_device_id=None,
            jwt_device_id="jwt",
            header_device_id="hdr",
        )
        == "jwt"
    )
    assert (
        NotificationsService.resolve_device_id(
            body_device_id="  ",
            jwt_device_id=None,
            header_device_id="hdr",
        )
        == "hdr"
    )


def test_resolve_device_id_missing_raises() -> None:
    with pytest.raises(ValidationFailedError):
        NotificationsService.resolve_device_id(
            body_device_id=None, jwt_device_id=None, header_device_id=None
        )


def test_media_ready_payload_shape() -> None:
    client = ApnsClient(MagicMock())
    title, body = media_ready_copy(kind="video")
    payload = client.build_media_ready_payload(
        MediaReadyPush(
            job_id="j1",
            kind="video",
            media_url="https://cdn.example/out.mp4",
            title=title,
            body=body,
        )
    )
    assert payload["aps"]["mutable-content"] == 1
    assert payload["aps"]["alert"]["body"] == "Your video is ready"
    assert payload["jobId"] == "j1"
    assert payload["kind"] == "video"
    assert payload["mediaUrl"] == "https://cdn.example/out.mp4"


def test_apns_not_configured_send_skipped() -> None:
    settings = MagicMock()
    settings.apns_key_id = ""
    settings.apns_team_id = ""
    settings.apns_topic = ""
    settings.resolve_apns_auth_key.return_value = ""
    client = ApnsClient(settings)
    assert client.configured is False


@pytest.mark.asyncio
async def test_push_skips_when_notifications_disabled() -> None:
    session = AsyncMock()
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = uuid.uuid4()
    session.execute = AsyncMock(return_value=claim_result)

    prefs = AsyncMock()
    prefs.get = AsyncMock(
        return_value=PreferencesView(
            default_assistant_mode="chat",
            notifications_enabled=False,
            code_defaults={},
        )
    )
    apns = MagicMock()
    apns.configured = True
    apns.send = AsyncMock()
    tokens = AsyncMock()
    tokens.list_for_user = AsyncMock(return_value=[MagicMock(push_token="tok")])

    svc = MediaPushService(session, apns=apns, tokens=tokens, preferences=prefs)
    await svc.notify_media_ready(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="image",
        media_url="https://x/a.png",
    )
    apns.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_skips_when_already_claimed() -> None:
    session = AsyncMock()
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=claim_result)

    prefs = AsyncMock()
    apns = MagicMock()
    apns.send = AsyncMock()
    tokens = AsyncMock()

    svc = MediaPushService(session, apns=apns, tokens=tokens, preferences=prefs)
    await svc.notify_media_ready(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="image",
        media_url="https://x/a.png",
    )
    prefs.get.assert_not_awaited()
    apns.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_sends_and_drops_unregistered_token() -> None:
    session = AsyncMock()
    claim_result = MagicMock()
    claim_result.scalar_one_or_none.return_value = uuid.uuid4()
    session.execute = AsyncMock(return_value=claim_result)

    prefs = AsyncMock()
    prefs.get = AsyncMock(
        return_value=PreferencesView(
            default_assistant_mode="chat",
            notifications_enabled=True,
            code_defaults={},
        )
    )
    apns = MagicMock()
    apns.configured = True
    apns.build_media_ready_payload = ApnsClient(MagicMock()).build_media_ready_payload
    apns.send = AsyncMock(return_value="unregistered")

    row = MagicMock(push_token="dead-token")
    tokens = AsyncMock()
    tokens.list_for_user = AsyncMock(return_value=[row])
    tokens.delete_by_push_token = AsyncMock()

    svc = MediaPushService(session, apns=apns, tokens=tokens, preferences=prefs)
    await svc.notify_media_ready(
        job_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        kind="image",
        media_url="https://x/a.png",
    )
    apns.send.assert_awaited_once()
    tokens.delete_by_push_token.assert_awaited_once_with(push_token="dead-token")
