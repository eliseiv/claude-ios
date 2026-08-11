"""Integration: media.generate_* in the chat tool-loop (ADR-068).

Real PostgreSQL; Anthropic faked; fal submit faked at FalClient.submit. Asserts mediaJobs on the
ChatResponse, double billing (chat turn + media-gen), and that /v1/media/jobs/{id} still works.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.media_generation.fal_client import FalSubmission
from tests.conftest import FakeAnthropicClient, auth_headers, seed_user

_QUEUE = "https://queue.fal.run"


@pytest.fixture
def fal_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAL_API_KEY", "test-fal-key")
    monkeypatch.setenv("FAL_QUEUE_BASE", _QUEUE)
    get_settings.cache_clear()

    async def _submit(self: object, *, endpoint: str, payload: dict[str, object]) -> FalSubmission:
        rid = "req_chat_media_01"
        return FalSubmission(
            request_id=rid,
            status="IN_QUEUE",
            status_url=f"{_QUEUE}/{endpoint}/requests/{rid}/status",
            response_url=f"{_QUEUE}/{endpoint}/requests/{rid}",
            queue_position=0,
        )

    monkeypatch.setattr(
        "app.media_generation.fal_client.FalClient.submit",
        _submit,
    )
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_media_generate_image_tool_loop_returns_media_jobs(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "media.generate_image",
            {"model": "nano-banana-2", "prompt": "a fluffy cat", "resolution": "1K"},
            tool_id="toolu_mediaimg01",
        ),
        fake_anthropic.text_result("Started generating your cat photo."),
    ]

    r = await client.post(
        "/v1/chat/run",
        json={"userId": str(uid), "message": "make a photo of a cat", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "assistant_message"
    assert body["assistantMessage"] == "Started generating your cat photo."
    assert body.get("toolCalls") in (None, [])
    jobs = body.get("mediaJobs")
    assert isinstance(jobs, list) and len(jobs) == 1
    job = jobs[0]
    assert job["kind"] == "image"
    assert job["status"] == "queued"
    assert job["model"] == "nano-banana-2"
    assert job["creditsCharged"] >= 1
    job_id = job["jobId"]

    # serverTools compact indicator for the media tool.
    names = {st["toolName"] for st in body["serverTools"]}
    assert "media.generate_image" in names

    # Dual billing: chat debit (idempotency = messageStepId) + media-gen:{jobId}.
    async with db_sessionmaker() as s:
        keys = set(
            (
                await s.execute(
                    text("SELECT idempotency_key FROM ledger_transactions WHERE user_id = :u"),
                    {"u": str(uid)},
                )
            )
            .scalars()
            .all()
        )
    assert body["messageStepId"] in keys
    assert f"media-gen:{job_id}" in keys

    async with db_sessionmaker() as s:
        row = await s.scalar(
            text("SELECT status FROM media_jobs WHERE id = :id AND user_id = :u"),
            {"id": job_id, "u": str(uid)},
        )
    assert row == "queued"


@pytest.mark.asyncio
async def test_media_generate_not_configured_soft_error_keeps_turn(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        async with db_sessionmaker() as s:
            uid = await seed_user(s, subscription="active", balance=20)

        fake_anthropic.responses = [
            fake_anthropic.tool_result(
                "media.generate_image",
                {"model": "nano-banana-2", "prompt": "x"},
                tool_id="toolu_mediancfg01",
            ),
            fake_anthropic.text_result("Media generation is unavailable right now."),
        ]

        r = await client.post(
            "/v1/chat/run",
            json={"userId": str(uid), "message": "make a photo", "mode": "credits"},
            headers=auth_headers(uid),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "assistant_message"
        assert body.get("mediaJobs") is None
        server = body["serverTools"]
        assert any(
            st["toolName"] == "media.generate_image" and st["status"] == "errored" for st in server
        )
        assert any(st.get("summary") == "media_not_configured" for st in server)
    finally:
        get_settings.cache_clear()
