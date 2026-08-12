"""Unit: chat attachmentRefs TTL + recent-image resolution."""

from __future__ import annotations

import datetime

from app.chat.attachment_refs import (
    build_attachment_ref,
    is_ref_alive,
    latest_alive_image_urls,
    recent_image_available,
    user_step_has_image_signal,
)


def test_ref_alive_within_ttl() -> None:
    now = datetime.datetime(2026, 8, 12, 12, 0, tzinfo=datetime.UTC)
    ref = build_attachment_ref(
        media_type="image/png",
        filename="a.png",
        url="https://fal.media/files/a.png",
        expires_at=now + datetime.timedelta(hours=1),
    )
    assert is_ref_alive(ref, now=now)


def test_ref_expired() -> None:
    now = datetime.datetime(2026, 8, 12, 12, 0, tzinfo=datetime.UTC)
    ref = build_attachment_ref(
        media_type="image/png",
        filename="a.png",
        url="https://fal.media/files/a.png",
        expires_at=now - datetime.timedelta(seconds=1),
    )
    assert not is_ref_alive(ref, now=now)


def test_latest_alive_picks_newest() -> None:
    now = datetime.datetime(2026, 8, 12, 12, 0, tzinfo=datetime.UTC)
    older = {
        "attachmentRefs": [
            build_attachment_ref(
                media_type="image/png",
                filename="old.png",
                url="https://fal.media/files/old.png",
                expires_at=now + datetime.timedelta(hours=2),
            )
        ]
    }
    newer = {
        "attachmentRefs": [
            build_attachment_ref(
                media_type="image/png",
                filename="new.png",
                url="https://fal.media/files/new.png",
                expires_at=now + datetime.timedelta(hours=2),
            )
        ]
    }
    # newest-first scan order
    urls = latest_alive_image_urls([newer, older], now=now, max_urls=1)
    assert urls == ["https://fal.media/files/new.png"]


def test_placeholder_counts_as_image_signal() -> None:
    payload = {
        "content": [
            {
                "type": "text",
                "text": (
                    '[attachment: image/jpeg "x.jpg", 12B '
                    "— отправлено в первом обращении к модели]"
                ),
            }
        ]
    }
    assert user_step_has_image_signal(payload)
    assert recent_image_available([payload])
