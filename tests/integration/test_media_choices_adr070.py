"""Integration: media.ask_params → mediaChoices → mediaSelection → mediaJobs (ADR-070)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
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
        rid = "req_media_choices_01"
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
async def test_media_choices_wizard_to_job(
    client: AsyncClient,
    db_sessionmaker: async_sessionmaker[AsyncSession],
    fake_anthropic: FakeAnthropicClient,
    fal_ready: None,
) -> None:
    async with db_sessionmaker() as s:
        uid = await seed_user(s, subscription="active", balance=50)

    fake_anthropic.responses = [
        fake_anthropic.tool_result(
            "media.ask_params",
            {"kind": "image", "prompt": "a fluffy cat"},
            tool_id="toolu_askparams01",
        ),
        fake_anthropic.text_result("Pick a model to continue."),
    ]

    r1 = await client.post(
        "/v1/chat/v2/run",
        json={"userId": str(uid), "message": "make a photo of a cat", "mode": "credits"},
        headers=auth_headers(uid),
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    choices = body1.get("mediaChoices")
    assert choices is not None
    assert choices["kind"] == "image"
    assert choices["step"] == "model"
    assert "prompt" not in choices
    assert body1.get("mediaJobs") is None
    selection_id = choices["selectionId"]
    session_id = body1["sessionId"]
    model_values = {o["value"] for o in choices["questions"][0]["options"]}
    assert "nano-banana-2" in model_values

    r2 = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "sessionId": session_id,
            "message": "",
            "mode": "credits",
            "mediaSelection": {
                "selectionId": selection_id,
                "answers": {"model": "nano-banana-2"},
            },
        },
        headers=auth_headers(uid),
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    choices2 = body2["mediaChoices"]
    assert choices2["step"] == "resolution"
    assert body2.get("mediaJobs") is None
    res_q = choices2["questions"][0]
    res_opts = {o["value"]: o for o in res_q["options"]}
    assert "1K" in res_opts and "2K" in res_opts
    assert "cr." in res_opts["2K"]["label"]
    assert isinstance(res_opts["2K"].get("credits"), int)
    assert "2K:" in res_q["question"] and "cr." in res_q["question"]

    # Intermediate taps must NOT add chat bubbles (progress is patched into ask_params result).
    hist_mid = await client.get(f"/v1/chats/{session_id}", headers=auth_headers(uid))
    assert hist_mid.status_code == 200, hist_mid.text
    mid_steps = hist_mid.json()["steps"]
    mid_user_texts = [
        b.get("text", "")
        for s in mid_steps
        if s["role"] == "user"
        for b in (s.get("payload") or {}).get("content") or []
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    assert not any(t.startswith("Media:") for t in mid_user_texts)
    assert not any("media selection" in t for t in mid_user_texts)

    # Complete priced params + aspectRatio in one cumulative answers payload by walking steps.
    answers = {"model": "nano-banana-2", "resolution": "2K"}
    # aspectRatio is next after resolution for nano-banana-2
    r3 = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "sessionId": session_id,
            "message": "",
            "mode": "credits",
            "mediaSelection": {"selectionId": selection_id, "answers": answers},
        },
        headers=auth_headers(uid),
    )
    assert r3.status_code == 200, r3.text
    body3 = r3.json()
    assert body3["mediaChoices"]["step"] == "aspectRatio"

    answers["aspectRatio"] = "1:1"
    r4 = await client.post(
        "/v1/chat/v2/run",
        json={
            "userId": str(uid),
            "sessionId": session_id,
            "message": "",
            "mode": "credits",
            "mediaSelection": {"selectionId": selection_id, "answers": answers},
        },
        headers=auth_headers(uid),
    )
    assert r4.status_code == 200, r4.text
    body4 = r4.json()
    assert body4.get("mediaChoices") is None
    jobs = body4.get("mediaJobs")
    assert isinstance(jobs, list) and len(jobs) == 1
    assert jobs[0]["kind"] == "image"
    assert jobs[0]["model"] == "nano-banana-2"
    assert jobs[0]["status"] == "queued"
    job_id = jobs[0]["jobId"]

    hist = await client.get(f"/v1/chats/{session_id}", headers=auth_headers(uid))
    assert hist.status_code == 200, hist.text
    steps = hist.json()["steps"]
    summary_users = [
        s
        for s in steps
        if s["role"] == "user"
        and any(
            isinstance(b, dict)
            and b.get("type") == "text"
            and str(b.get("text", "")).startswith("Media:")
            for b in (s.get("payload") or {}).get("content") or []
        )
    ]
    assert len(summary_users) == 1
    summary_text = next(
        b["text"]
        for b in summary_users[0]["payload"]["content"]
        if isinstance(b, dict) and b.get("type") == "text"
    )
    assert "nano-banana" in summary_text.lower() or "Nano Banana" in summary_text
    assert "2K" in summary_text and "cr." in summary_text
    assert "fluffy cat" not in summary_text
    assert summary_users[0]["payload"].get("mediaWizard", {}).get("jobId") == job_id

    assistant_with_jobs = [
        s for s in steps if s["role"] == "assistant" and (s.get("payload") or {}).get("mediaJobs")
    ]
    assert len(assistant_with_jobs) == 1
    assert assistant_with_jobs[0]["payload"]["mediaJobs"][0]["jobId"] == job_id
