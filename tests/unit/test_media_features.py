"""Unit coverage for the opt-in avatar speech and makeup backend surfaces."""

from __future__ import annotations

import uuid
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import Settings
from app.media_generation.avatar_images import compose_avatar
from app.media_generation.fal_client import FalSubmission
from app.media_generation.features_service import MediaFeaturesService
from app.media_generation.service import MediaGenerationService
from app.schemas.media_features import (
    AvatarBackgroundRequest,
    AvatarSpeechRequest,
    MediaFeaturePresetCreateRequest,
    UserAvatarCreateRequest,
    UserAvatarPrepareRequest,
)


def _png(*, color: tuple[int, int, int, int] = (255, 0, 0, 255)) -> bytes:
    output = BytesIO()
    Image.new("RGBA", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def test_new_features_are_disabled_and_cost_ten_by_default() -> None:
    settings = Settings()
    assert settings.avatar_speech_enabled is False
    assert settings.makeup_enabled is False
    assert settings.avatar_speech_credit_cost == 10
    assert settings.makeup_credit_cost == 10


def test_bad_feature_prices_fall_back_to_ten_without_changing_tts_fallback() -> None:
    settings = Settings(
        AVATAR_SPEECH_CREDIT_COST=0,
        MAKEUP_CREDIT_COST=-1,
        TTS_CREDIT_COST=0,
    )
    assert settings.avatar_speech_credit_cost == 10
    assert settings.makeup_credit_cost == 10
    assert settings.tts_credit_cost == 1


def test_avatar_speech_requires_exactly_one_avatar_source() -> None:
    common = {
        "text": "Hello",
        "language": "en-US",
        "voiceId": "default_female",
        "mood": "friendly",
    }
    with pytest.raises(ValidationError):
        AvatarSpeechRequest.model_validate(common)
    with pytest.raises(ValidationError):
        AvatarSpeechRequest.model_validate(
            {**common, "systemAvatarId": "ava", "userAvatarId": str(uuid.uuid4())}
        )


def test_saved_avatar_requires_upload_or_system_avatar_but_not_both() -> None:
    with pytest.raises(ValidationError):
        UserAvatarCreateRequest.model_validate({})
    with pytest.raises(ValidationError):
        UserAvatarCreateRequest.model_validate(
            {
                "systemAvatarId": "ava",
                "image": {"mediaType": "image/png", "data": "AA=="},
            }
        )


def test_replacement_background_requires_removal() -> None:
    with pytest.raises(ValidationError):
        UserAvatarPrepareRequest(
            removeBackground=False,
            background=AvatarBackgroundRequest(type="color", color="#123456"),
        )


def test_makeup_preset_requires_provider_value() -> None:
    with pytest.raises(ValidationError):
        MediaFeaturePresetCreateRequest.model_validate(
            {
                "id": "no_makeup",
                "feature": "makeup",
                "title": "No makeup",
                "image": {"mediaType": "image/png", "data": "AA=="},
            }
        )


def test_compose_avatar_keeps_transparency_without_background_and_applies_color() -> None:
    transparent_subject = _png(color=(255, 0, 0, 128))
    without_background = Image.open(
        BytesIO(
            compose_avatar(
                transparent_subject,
                background_bytes=None,
                background_color=None,
            )
        )
    )
    with_background = Image.open(
        BytesIO(
            compose_avatar(
                transparent_subject,
                background_bytes=None,
                background_color="#0000ff",
            )
        )
    )
    assert without_background.convert("RGBA").getpixel((0, 0))[3] == 128
    assert with_background.convert("RGBA").getpixel((0, 0))[3] == 255


class _Repo:
    def __init__(self, preset: object | None = None) -> None:
        self.preset = preset

    async def get_preset(self, _preset_id: str, *, active_only: bool = True) -> object | None:
        del active_only
        return self.preset


class _Fal:
    configured = True

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []

    async def upload(self, *, content: bytes, media_type: str, file_name: str) -> str:
        del content
        self.uploaded.append((media_type, file_name))
        return f"https://cdn.example/{file_name}"


class _Speech:
    configured = True

    def __init__(self) -> None:
        self.text: str | None = None
        self.instructions: str | None = None

    async def synthesize(self, *, text: str, voice: object) -> bytes:
        self.text = text
        self.instructions = str(cast(Any, voice).instructions)
        return b"audio"


class _Media:
    def __init__(self) -> None:
        self.submission: dict[str, Any] | None = None

    async def submit_custom(self, **kwargs: Any) -> object:
        self.submission = kwargs
        return SimpleNamespace(job=SimpleNamespace(id=uuid.uuid4()))

    async def validate_custom_input(self, *, prompt: str, image_urls: list[str]) -> None:
        del prompt, image_urls


@pytest.mark.asyncio
async def test_language_controls_pronunciation_without_translating_text() -> None:
    preset = SimpleNamespace(
        id="business_woman",
        feature="avatar",
        image_bytes=_png(),
        image_media_type="image/png",
    )
    fal = _Fal()
    speech = _Speech()
    media = _Media()
    service = MediaFeaturesService(
        repo=_Repo(preset),  # type: ignore[arg-type]
        media=media,  # type: ignore[arg-type]
        fal=fal,  # type: ignore[arg-type]
        speech=speech,  # type: ignore[arg-type]
        settings=Settings(
            AVATAR_SPEECH_ENABLED=True,
            AVATAR_SPEECH_CREDIT_COST=10,
            FAL_API_KEY="fal",
            OPENAI_API_KEY="openai",
        ),
        moderation=None,
    )

    await service.create_avatar_speech(
        user_id=uuid.uuid4(),
        system_avatar_id="business_woman",
        user_avatar_id=None,
        text="Привет, world",
        language="ru-RU",
        voice_id="default_female",
        mood="calm",
    )

    assert speech.text == "Привет, world"
    assert speech.instructions is not None
    assert "Russian pronunciation" in speech.instructions
    assert "do not translate" in speech.instructions.lower()
    assert media.submission is not None
    assert media.submission["credits"] == 10
    assert media.submission["payload"] == {
        "image_url": "https://cdn.example/avatar.png",
        "audio_url": "https://cdn.example/speech.mp3",
    }


def test_disabled_speech_options_do_not_advertise_catalogs() -> None:
    service = MediaFeaturesService(
        repo=SimpleNamespace(),  # type: ignore[arg-type]
        media=SimpleNamespace(),  # type: ignore[arg-type]
        fal=SimpleNamespace(configured=True),  # type: ignore[arg-type]
        speech=SimpleNamespace(configured=True),  # type: ignore[arg-type]
        settings=Settings(AVATAR_SPEECH_ENABLED=False),
        moderation=None,
    )
    options = service.speech_options("en")
    assert options["enabled"] is False
    assert options["languages"] == []
    assert options["moods"] == []
    assert options["voices"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("credits", "visible", "expected_debits"),
    [(0, False, []), (10, True, [10])],
)
async def test_custom_jobs_reuse_billing_and_can_be_hidden(
    credits: int, visible: bool, expected_debits: list[int]
) -> None:
    created: dict[str, Any] = {}

    class Repo:
        async def create(self, **kwargs: Any) -> object:
            created.update(kwargs)
            return SimpleNamespace(**kwargs, credits_refunded=False, result=None, error=None)

    class Fal:
        async def submit(self, *, endpoint: str, payload: dict[str, Any]) -> FalSubmission:
            assert endpoint == "provider/endpoint"
            assert payload == {"input": "fixed"}
            return FalSubmission(
                request_id="request",
                status="IN_QUEUE",
                status_url="https://queue/status",
                response_url="https://queue/result",
                queue_position=1,
            )

    class Wallet:
        def __init__(self) -> None:
            self.debits: list[int] = []

        async def consume(self, **kwargs: Any) -> None:
            self.debits.append(int(kwargs["amount"]))

    wallet = Wallet()
    service = MediaGenerationService(
        repo=Repo(),  # type: ignore[arg-type]
        fal=Fal(),  # type: ignore[arg-type]
        wallet=wallet,  # type: ignore[arg-type]
        settings=Settings(),
        moderation=None,
    )
    await service.submit_custom(
        user_id=uuid.uuid4(),
        kind="image",
        model_id="feature",
        endpoint="provider/endpoint",
        payload={"input": "fixed"},
        prompt="server owned",
        image_urls=[],
        credits=credits,
        operation="feature_operation",
        operation_input={"presetId": "preset"},
        visible_in_history=visible,
    )

    assert wallet.debits == expected_debits
    assert created["credits_charged"] == credits
    assert created["operation"] == "feature_operation"
    assert created["visible_in_history"] is visible
