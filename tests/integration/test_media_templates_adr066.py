"""Integration: media gallery templates (ADR-066).

Seed list, public cover, admin create/delete, independence from FAL_API_KEY.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from tests.conftest import FakeAnthropicClient, FakeStoreKitVerifier, auth_headers, seed_user

_ADMIN_SECRET = "admin-secret-templates-0123456789abcdef0123456789"
_ADMIN_HEADERS = {"X-Admin-Token": _ADMIN_SECRET}
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
async def templates_client(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fake_storekit: FakeStoreKitVerifier,
) -> AsyncIterator[AsyncClient]:
    settings = get_settings()
    orig_secret = settings.admin_api_secret
    orig_domain = settings.service_domain
    orig_fal = settings.fal_api_key
    settings.admin_api_secret = _ADMIN_SECRET
    settings.service_domain = "templates.test"
    # Ensure catalog works with fal unset (templates must not 503).
    settings.fal_api_key = ""

    from app import deps
    from app.api_gateway.routers import admin_media_templates as admin_tpl
    from app.api_gateway.routers import media_templates as media_tpl
    from app.main import create_app

    async def _override_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _open(**_kwargs: object) -> bool:
        return True

    orig_other = media_tpl.enforce_other_limits
    orig_admin = admin_tpl.enforce_admin_limits
    media_tpl.enforce_other_limits = _open  # type: ignore[assignment]
    admin_tpl.enforce_admin_limits = _open  # type: ignore[assignment]

    app = create_app()
    app.dependency_overrides[deps.get_db] = _override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    media_tpl.enforce_other_limits = orig_other  # type: ignore[assignment]
    admin_tpl.enforce_admin_limits = orig_admin  # type: ignore[assignment]
    settings.admin_api_secret = orig_secret
    settings.service_domain = orig_domain
    settings.fal_api_key = orig_fal


@pytest.mark.asyncio
async def test_seed_lists_and_cover_without_fal(
    templates_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    images = await templates_client.get(
        "/v1/media/templates/images", headers=auth_headers(uid)
    )
    assert images.status_code == 200, images.text
    image_items = images.json()["templates"]
    assert len(image_items) == 5
    assert {t["id"] for t in image_items} >= {
        "smart_resize",
        "bg_removal_change",
        "ecommerce_photos",
        "photo_collage",
        "profile_picture",
    }
    profile = next(t for t in image_items if t["id"] == "profile_picture")
    assert profile["requiredInputImages"] == 1
    assert profile["model"] == "nano-banana-2"
    assert profile["coverUrl"].endswith("/v1/media/templates/profile_picture/cover")
    assert profile["coverUrl"].startswith("https://templates.test/")

    videos = await templates_client.get(
        "/v1/media/templates/videos", headers=auth_headers(uid)
    )
    assert videos.status_code == 200, videos.text
    assert len(videos.json()["templates"]) == 5

    # Models still 503 without fal — templates must not share that gate.
    models = await templates_client.get("/v1/media/models", headers=auth_headers(uid))
    assert models.status_code == 503

    cover = await templates_client.get("/v1/media/templates/profile_picture/cover")
    assert cover.status_code == 200
    assert cover.headers["content-type"].startswith("image/")
    assert cover.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_admin_create_and_delete(
    templates_client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=5)

    created = await templates_client.post(
        "/v1/admin/media/templates",
        headers=_ADMIN_HEADERS,
        json={
            "id": "custom_tile",
            "kind": "image",
            "title": "Custom Tile",
            "prompt": "A custom prompt for the tile",
            "model": "nano-banana-2",
            "requiredInputImages": 0,
            "parameters": {"aspectRatio": "1:1", "resolution": "1K"},
            "cover": {"mediaType": "image/png", "data": _PNG_B64},
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["id"] == "custom_tile"
    assert body["kind"] == "image"

    listing = await templates_client.get(
        "/v1/media/templates/images", headers=auth_headers(uid)
    )
    ids = {t["id"] for t in listing.json()["templates"]}
    assert "custom_tile" in ids

    conflict = await templates_client.post(
        "/v1/admin/media/templates",
        headers=_ADMIN_HEADERS,
        json={
            "id": "custom_tile",
            "kind": "image",
            "title": "Dup",
            "prompt": "x",
            "model": "nano-banana-2",
            "cover": {"mediaType": "image/png", "data": _PNG_B64},
        },
    )
    assert conflict.status_code == 409

    deleted = await templates_client.delete(
        "/v1/admin/media/templates/custom_tile", headers=_ADMIN_HEADERS
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True

    again = await templates_client.delete(
        "/v1/admin/media/templates/custom_tile", headers=_ADMIN_HEADERS
    )
    assert again.status_code == 404

    listing2 = await templates_client.get(
        "/v1/media/templates/images", headers=auth_headers(uid)
    )
    assert "custom_tile" not in {t["id"] for t in listing2.json()["templates"]}


@pytest.mark.asyncio
async def test_list_requires_jwt(templates_client: AsyncClient) -> None:
    resp = await templates_client.get("/v1/media/templates/images")
    assert resp.status_code == 401
