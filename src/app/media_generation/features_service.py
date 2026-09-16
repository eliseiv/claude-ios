"""Use-cases for avatar speech, durable user avatars and virtual makeup."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, cast

from app.chat.attachments import _check_magic_bytes, _decode_base64, _decoded_len_from_base64
from app.chat.speech import SpeechClient
from app.chat.voices import get_voice, is_selectable_voice, voice_catalog
from app.config import Settings
from app.errors import (
    AvatarSpeechDisabledError,
    ConflictError,
    ContentPolicyViolationError,
    MakeupDisabledError,
    MediaGenerationNotConfiguredError,
    NotFoundError,
    PayloadTooLargeError,
    UnknownVoiceError,
    ValidationFailedError,
    VoiceOutputNotConfiguredError,
)
from app.media_generation.avatar_images import compose_avatar, validate_avatar_image
from app.media_generation.catalog import KIND_IMAGE, KIND_VIDEO
from app.media_generation.fal_client import FalClient
from app.media_generation.features_repository import MediaFeaturesRepository
from app.media_generation.service import MediaAsset, MediaGenerationService, MediaJobView
from app.models import MediaFeaturePreset, MediaJob, UserAvatar
from app.moderation import ModerationService
from app.moderation.service import STAGE_INPUT, SURFACE_MEDIA_UPLOAD

FEATURE_AVATAR = "avatar"
FEATURE_BACKGROUND = "background"
FEATURE_MAKEUP = "makeup"

OPERATION_AVATAR_PREPARE = "avatar_prepare"
OPERATION_AVATAR_SPEECH = "avatar_speech"
OPERATION_MAKEUP = "makeup"

_REMOVE_BACKGROUND_ENDPOINT = "fal-ai/imageutils/rembg"
_LIP_SYNC_ENDPOINT = "fal-ai/sync-lipsync/v3/image-to-video"
_MAKEUP_ENDPOINT = "fal-ai/image-apps-v2/makeup-application"

_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en-US", "English"),
    ("fr-FR", "French"),
    ("de-DE", "German"),
    ("it-IT", "Italian"),
    ("es-ES", "Spanish"),
    ("zh-CN", "Chinese"),
    ("ru-RU", "Russian"),
    ("pl-PL", "Polish"),
    ("pt-BR", "Portuguese"),
)
_LANGUAGE_INSTRUCTION = dict(_LANGUAGES)
_MOODS: tuple[tuple[str, str], ...] = (
    ("friendly", "Friendly"),
    ("professional", "Professional"),
    ("calm", "Calm"),
    ("confident", "Confident"),
    ("excited", "Excited"),
    ("narrative", "Narrative"),
)
_MOOD_INSTRUCTION = {
    "friendly": "Use a warm, friendly delivery.",
    "professional": "Use a clear, professional delivery.",
    "calm": "Use a calm, unhurried delivery.",
    "confident": "Use a confident, assured delivery.",
    "excited": "Use an energetic, excited delivery without shouting.",
    "narrative": "Use an engaging storytelling delivery.",
}


@dataclass(frozen=True)
class FeaturePresetView:
    id: str
    feature: str
    title: str
    gender: str | None
    style: str | None
    image_url: str
    sort_order: int


@dataclass(frozen=True)
class UserAvatarView:
    row: UserAvatar
    image_url: str


class MediaFeaturesService:
    """Feature orchestration over the existing media queue, billing and moderation path."""

    def __init__(
        self,
        *,
        repo: MediaFeaturesRepository,
        media: MediaGenerationService,
        fal: FalClient,
        speech: SpeechClient,
        settings: Settings,
        moderation: ModerationService | None,
    ) -> None:
        self._repo = repo
        self._media = media
        self._fal = fal
        self._speech = speech
        self._settings = settings
        self._moderation = moderation

    def preset_image_url(self, preset_id: str) -> str:
        return self._absolute_url(f"/v1/media/features/presets/{preset_id}/image")

    def user_avatar_image_url(self, avatar_id: uuid.UUID) -> str:
        return self._absolute_url(f"/v1/media/user-avatars/{avatar_id}/image")

    def _absolute_url(self, path: str) -> str:
        domain = self._settings.normalized_service_domain()
        return f"https://{domain}{path}" if domain else path

    def _preset_view(self, row: MediaFeaturePreset) -> FeaturePresetView:
        return FeaturePresetView(
            id=row.id,
            feature=row.feature,
            title=row.title,
            gender=row.gender,
            style=row.style,
            image_url=self.preset_image_url(row.id),
            sort_order=row.sort_order,
        )

    def _avatar_view(self, row: UserAvatar) -> UserAvatarView:
        return UserAvatarView(row=row, image_url=self.user_avatar_image_url(row.id))

    def speech_options(self, locale: str) -> dict[str, object]:
        """Return a stable server-owned catalog; language changes pronunciation, not text."""
        enabled = self._settings.avatar_speech_enabled
        return {
            "enabled": enabled,
            "configured": self._fal.configured and self._speech.configured,
            "credits": self._settings.avatar_speech_credit_cost,
            "maxTextLength": 300,
            "languages": (
                [{"id": code, "name": name} for code, name in _LANGUAGES] if enabled else []
            ),
            "moods": ([{"id": mood, "name": name} for mood, name in _MOODS] if enabled else []),
            "voices": voice_catalog(locale) if enabled else [],
        }

    async def list_system_avatars(
        self, *, gender: str | None, style: str | None
    ) -> list[FeaturePresetView]:
        if not self._settings.avatar_speech_enabled:
            return []
        rows = await self._repo.list_presets(feature=FEATURE_AVATAR, gender=gender, style=style)
        return [self._preset_view(row) for row in rows]

    async def list_makeup_presets(self) -> list[FeaturePresetView]:
        if not self._settings.makeup_enabled:
            return []
        rows = await self._repo.list_presets(feature=FEATURE_MAKEUP)
        return [self._preset_view(row) for row in rows]

    async def list_background_presets(self) -> list[FeaturePresetView]:
        if not self._settings.avatar_speech_enabled:
            return []
        rows = await self._repo.list_presets(feature=FEATURE_BACKGROUND)
        return [self._preset_view(row) for row in rows]

    async def get_preset_image(self, preset_id: str) -> tuple[str, bytes]:
        row = await self._repo.get_preset(preset_id)
        if row is None:
            raise NotFoundError("media feature preset not found")
        return row.image_media_type, bytes(row.image_bytes)

    async def create_preset(
        self,
        *,
        preset_id: str,
        feature: str,
        title: str,
        gender: str | None,
        style: str | None,
        provider_value: str | None,
        image_media_type: str,
        image_data: str,
        sort_order: int | None,
    ) -> FeaturePresetView:
        if await self._repo.get_preset(preset_id, active_only=False) is not None:
            raise ConflictError("media feature preset id already exists")
        image_bytes = self._decode_image(image_media_type, image_data)
        order = (
            sort_order
            if sort_order is not None
            else await self._repo.next_preset_sort_order(feature)
        )
        row = await self._repo.create_preset(
            preset_id=preset_id,
            feature=feature,
            title=title,
            gender=gender,
            style=style,
            provider_value=provider_value,
            image_bytes=image_bytes,
            image_media_type=image_media_type,
            sort_order=order,
        )
        return self._preset_view(row)

    async def delete_preset(self, preset_id: str) -> None:
        if not await self._repo.delete_preset(preset_id):
            raise NotFoundError("media feature preset not found")

    async def create_user_avatar(
        self,
        *,
        user_id: uuid.UUID,
        title: str | None,
        image_media_type: str | None,
        image_data: str | None,
        system_avatar_id: str | None,
    ) -> UserAvatarView:
        self._require_avatar_enabled()
        if await self._repo.count_user_avatars(user_id) >= self._settings.user_avatar_max_count:
            raise ConflictError("user avatar limit reached")
        source_preset_id: str | None = None
        if system_avatar_id is not None:
            preset = await self._repo.get_preset(system_avatar_id)
            if preset is None or preset.feature != FEATURE_AVATAR:
                raise NotFoundError("system avatar not found")
            content = bytes(preset.image_bytes)
            media_type = preset.image_media_type
            source_preset_id = preset.id
        else:
            if image_media_type is None or image_data is None:
                raise ValidationFailedError("image is required")
            content = self._decode_image(image_media_type, image_data)
            await self._moderate_inline(image_media_type, image_data)
            media_type = image_media_type
        row = await self._repo.create_user_avatar(
            user_id=user_id,
            title=title,
            source_preset_id=source_preset_id,
            image_bytes=content,
            media_type=media_type,
        )
        return self._avatar_view(row)

    async def list_user_avatars(self, user_id: uuid.UUID) -> list[UserAvatarView]:
        self._require_avatar_enabled()
        return [self._avatar_view(row) for row in await self._repo.list_user_avatars(user_id)]

    async def get_user_avatar_image(
        self, *, user_id: uuid.UUID, avatar_id: uuid.UUID
    ) -> tuple[str, bytes]:
        row = await self._get_user_avatar(user_id=user_id, avatar_id=avatar_id)
        if row.prepared_image_bytes is not None and row.prepared_media_type is not None:
            return row.prepared_media_type, bytes(row.prepared_image_bytes)
        return row.original_media_type, bytes(row.original_image_bytes)

    async def delete_user_avatar(self, *, user_id: uuid.UUID, avatar_id: uuid.UUID) -> None:
        self._require_avatar_enabled()
        row = await self._get_user_avatar(user_id=user_id, avatar_id=avatar_id)
        await self._repo.delete_user_avatar(row)

    async def prepare_user_avatar(
        self,
        *,
        user_id: uuid.UUID,
        avatar_id: uuid.UUID,
        remove_background: bool,
        background_type: str,
        background_color: str | None,
        background_preset_id: str | None,
        background_media_type: str | None,
        background_data: str | None,
    ) -> UserAvatarView:
        self._require_avatar_ready()
        row = await self._get_user_avatar(user_id=user_id, avatar_id=avatar_id)
        if not remove_background:
            await self._repo.use_original(row)
            return self._avatar_view(row)

        background_bytes: bytes | None = None
        resolved_media_type: str | None = None
        if background_type == "preset":
            preset = await self._repo.get_preset(background_preset_id or "")
            if preset is None or preset.feature != FEATURE_BACKGROUND:
                raise NotFoundError("background preset not found")
            background_bytes = bytes(preset.image_bytes)
            resolved_media_type = preset.image_media_type
        elif background_type == "custom":
            if background_media_type is None or background_data is None:
                raise ValidationFailedError("custom background image is required")
            background_bytes = self._decode_image(background_media_type, background_data)
            await self._moderate_inline(background_media_type, background_data)
            resolved_media_type = background_media_type
        elif background_type == "color":
            # Parse now so invalid colors fail before an upstream job is created.
            try:
                compose_avatar(
                    bytes(row.original_image_bytes),
                    background_bytes=None,
                    background_color=background_color,
                )
            except ValueError as exc:
                raise ValidationFailedError(str(exc)) from exc
        image_url = await self._fal.upload(
            content=bytes(row.original_image_bytes),
            media_type=row.original_media_type,
            file_name=f"avatar-{row.id}.{_extension(row.original_media_type)}",
        )
        job = await self._media.submit_custom(
            user_id=user_id,
            kind=KIND_IMAGE,
            model_id="avatar-remove-background",
            endpoint=_REMOVE_BACKGROUND_ENDPOINT,
            payload={"image_url": image_url},
            prompt="Remove the image background",
            image_urls=[image_url],
            credits=0,
            operation=OPERATION_AVATAR_PREPARE,
            operation_input={"avatarId": str(row.id)},
            visible_in_history=False,
        )
        await self._repo.begin_preparation(
            row,
            job_id=job.job.id,
            background_image_bytes=background_bytes,
            background_media_type=resolved_media_type,
            background_color=background_color if background_type == "color" else None,
        )
        return self._avatar_view(row)

    async def create_avatar_speech(
        self,
        *,
        user_id: uuid.UUID,
        system_avatar_id: str | None,
        user_avatar_id: uuid.UUID | None,
        text: str,
        language: str,
        voice_id: str,
        mood: str,
    ) -> MediaJobView:
        self._require_avatar_ready(require_speech=True)
        image_bytes, image_media_type = await self._speech_avatar_bytes(
            user_id=user_id,
            system_avatar_id=system_avatar_id,
            user_avatar_id=user_avatar_id,
        )
        if not is_selectable_voice(voice_id):
            raise UnknownVoiceError("unknown voice")
        voice = get_voice(voice_id)
        if voice is None:  # pragma: no cover - is_selectable_voice guarantees this
            raise UnknownVoiceError("unknown voice")
        language_name = _LANGUAGE_INSTRUCTION.get(language)
        mood_instruction = _MOOD_INSTRUCTION.get(mood)
        if language_name is None or mood_instruction is None:
            raise ValidationFailedError("unsupported speech option")
        # TTS is itself a paid provider call. Reject unsafe text before synthesizing it; the
        # later submit_custom check also covers the hosted avatar URL and persists the verdict.
        await self._media.validate_custom_input(prompt=text, image_urls=[])
        instructed_voice = voice._replace(
            instructions=(
                f"Speak clearly using {language_name} pronunciation. "
                "Preserve the supplied words exactly: do not translate or rewrite them. "
                f"{mood_instruction} Keep the pacing natural and the articulation clear."
            )
        )
        audio = await self._speech.synthesize(text=text, voice=instructed_voice)
        image_url = await self._fal.upload(
            content=image_bytes,
            media_type=image_media_type,
            file_name=f"avatar.{_extension(image_media_type)}",
        )
        audio_url = await self._fal.upload(
            content=audio,
            media_type=self._settings.tts_media_type(),
            file_name=f"speech.{self._settings.resolved_tts_audio_format()}",
        )
        return await self._media.submit_custom(
            user_id=user_id,
            kind=KIND_VIDEO,
            model_id="avatar-speech",
            endpoint=_LIP_SYNC_ENDPOINT,
            payload={"image_url": image_url, "audio_url": audio_url},
            prompt=text,
            image_urls=[image_url],
            credits=self._settings.avatar_speech_credit_cost,
            operation=OPERATION_AVATAR_SPEECH,
            operation_input={
                "language": language,
                "voiceId": voice_id,
                "mood": mood,
            },
            visible_in_history=True,
        )

    async def create_makeup(
        self,
        *,
        user_id: uuid.UUID,
        preset_id: str,
        image_media_type: str,
        image_data: str,
    ) -> MediaJobView:
        self._require_makeup_ready()
        preset = await self._repo.get_preset(preset_id)
        if preset is None or preset.feature != FEATURE_MAKEUP or not preset.provider_value:
            raise NotFoundError("makeup preset not found")
        uploaded = await self._media.upload_reference_image(
            media_type=image_media_type,
            file_name=f"makeup-source.{_extension(image_media_type)}",
            data=image_data,
        )
        return await self._media.submit_custom(
            user_id=user_id,
            kind=KIND_IMAGE,
            model_id="makeup",
            endpoint=_MAKEUP_ENDPOINT,
            payload={"image_url": uploaded.url, "makeup_style": preset.provider_value},
            prompt=f"Apply makeup preset: {preset.title}",
            image_urls=[uploaded.url],
            credits=self._settings.makeup_credit_cost,
            operation=OPERATION_MAKEUP,
            operation_input={"presetId": preset.id},
            visible_in_history=True,
        )

    async def _speech_avatar_bytes(
        self,
        *,
        user_id: uuid.UUID,
        system_avatar_id: str | None,
        user_avatar_id: uuid.UUID | None,
    ) -> tuple[bytes, str]:
        if system_avatar_id is not None:
            preset = await self._repo.get_preset(system_avatar_id)
            if preset is None or preset.feature != FEATURE_AVATAR:
                raise NotFoundError("system avatar not found")
            return bytes(preset.image_bytes), preset.image_media_type
        if user_avatar_id is None:
            raise ValidationFailedError("avatar source is required")
        row = await self._get_user_avatar(user_id=user_id, avatar_id=user_avatar_id)
        if row.prepared_image_bytes is not None and row.prepared_media_type is not None:
            return bytes(row.prepared_image_bytes), row.prepared_media_type
        return bytes(row.original_image_bytes), row.original_media_type

    async def _get_user_avatar(self, *, user_id: uuid.UUID, avatar_id: uuid.UUID) -> UserAvatar:
        row = await self._repo.get_user_avatar(user_id=user_id, avatar_id=avatar_id)
        if row is None:
            raise NotFoundError("user avatar not found")
        return row

    def _decode_image(self, media_type: str, data: str) -> bytes:
        if _decoded_len_from_base64(data) > self._settings.media_upload_max_bytes:
            raise PayloadTooLargeError("image exceeds the maximum allowed size")
        raw = _decode_base64(data)
        if len(raw) > self._settings.media_upload_max_bytes:
            raise PayloadTooLargeError("image exceeds the maximum allowed size")
        _check_magic_bytes(media_type, raw)
        try:
            validate_avatar_image(raw)
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        return raw

    async def _moderate_inline(self, media_type: str, data: str) -> None:
        if self._moderation is None:
            return
        verdict = await self._moderation.check(
            surface=SURFACE_MEDIA_UPLOAD,
            stage=STAGE_INPUT,
            image_urls=[f"data:{media_type};base64,{data}"],
        )
        if verdict.blocked:
            raise ContentPolicyViolationError("изображение отклонено правилами контента")

    def _require_avatar_enabled(self) -> None:
        if not self._settings.avatar_speech_enabled:
            raise AvatarSpeechDisabledError("avatar speech is disabled")

    def _require_avatar_ready(self, *, require_speech: bool = False) -> None:
        self._require_avatar_enabled()
        if not self._fal.configured:
            raise MediaGenerationNotConfiguredError("media generation is not configured")
        if require_speech and not self._speech.configured:
            raise VoiceOutputNotConfiguredError("speech synthesis is not configured")

    def _require_makeup_ready(self) -> None:
        if not self._settings.makeup_enabled:
            raise MakeupDisabledError("makeup is disabled")
        if not self._fal.configured:
            raise MediaGenerationNotConfiguredError("media generation is not configured")


class AvatarPreparationCompleter:
    """Completion hook that turns a provider cutout into the durable prepared avatar."""

    def __init__(self, *, repo: MediaFeaturesRepository, fal: FalClient) -> None:
        self._repo = repo
        self._fal = fal

    async def complete(self, job: MediaJob, assets: list[MediaAsset]) -> None:
        if job.operation != OPERATION_AVATAR_PREPARE:
            return
        raw_id = (job.operation_input or {}).get("avatarId")
        if not isinstance(raw_id, str) or not assets:
            raise ValidationFailedError("avatar preparation metadata is incomplete")
        try:
            avatar_id = uuid.UUID(raw_id)
        except ValueError as exc:
            raise ValidationFailedError("avatar preparation metadata is invalid") from exc
        row = await self._repo.get_user_avatar(user_id=job.user_id, avatar_id=avatar_id)
        if row is None or row.preparation_job_id != job.id:
            return
        cutout = await self._fal.download_asset(assets[0].url)
        try:
            prepared = compose_avatar(
                cutout,
                background_bytes=(
                    bytes(row.background_image_bytes)
                    if row.background_image_bytes is not None
                    else None
                ),
                background_color=row.background_color,
            )
        except ValueError as exc:
            raise ValidationFailedError(str(exc)) from exc
        await self._repo.complete_preparation(
            row,
            job_id=job.id,
            image_bytes=prepared,
            media_type="image/png",
        )

    async def fail(self, job: MediaJob) -> None:
        if job.operation != OPERATION_AVATAR_PREPARE:
            return
        raw_id = (job.operation_input or {}).get("avatarId")
        if not isinstance(raw_id, str):
            return
        try:
            avatar_id = uuid.UUID(raw_id)
        except ValueError:
            return
        row = await self._repo.get_user_avatar(user_id=job.user_id, avatar_id=avatar_id)
        if row is not None:
            await self._repo.cancel_preparation(row, job_id=job.id)


def _extension(media_type: str) -> Literal["jpg", "png", "webp"]:
    return cast(
        Literal["jpg", "png", "webp"],
        {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[media_type],
    )
