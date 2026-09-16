"""Strict API schemas for avatar speech, saved avatars and virtual makeup."""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.common import StrictModel

_ID_RE = re.compile(r"^[a-z0-9_]+$")

ImageMediaType = Literal["image/jpeg", "image/png", "image/webp"]
FeatureKind = Literal["avatar", "background", "makeup"]
AvatarGender = Literal["male", "female"]
SpeechLanguage = Literal[
    "en-US", "fr-FR", "de-DE", "it-IT", "es-ES", "zh-CN", "ru-RU", "pl-PL", "pt-BR"
]
SpeechMood = Literal["friendly", "professional", "calm", "confident", "excited", "narrative"]


class InlineFeatureImage(StrictModel):
    mediaType: ImageMediaType
    data: str = Field(min_length=1)


class MediaFeaturePresetCreateRequest(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    feature: FeatureKind
    title: str = Field(min_length=1, max_length=120)
    gender: AvatarGender | None = None
    style: str | None = Field(default=None, max_length=64)
    providerValue: str | None = Field(default=None, max_length=120)
    image: InlineFeatureImage
    sortOrder: int | None = None

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("id must match [a-z0-9_]+")
        return value

    @model_validator(mode="after")
    def _feature_fields(self) -> MediaFeaturePresetCreateRequest:
        if self.feature == "avatar" and (self.gender is None or not self.style):
            raise ValueError("avatar presets require gender and style")
        if self.feature == "makeup" and not self.providerValue:
            raise ValueError("makeup presets require providerValue")
        if self.feature != "avatar" and self.gender is not None:
            raise ValueError("gender is only valid for avatar presets")
        return self


class MediaFeaturePresetSchema(StrictModel):
    id: str
    feature: FeatureKind
    title: str
    gender: AvatarGender | None = None
    style: str | None = None
    imageUrl: str
    sortOrder: int


class MediaFeaturePresetDeleteResponse(StrictModel):
    deleted: bool = True


class SystemAvatarsResponse(StrictModel):
    enabled: bool
    genders: list[AvatarGender]
    styles: list[str]
    avatars: list[MediaFeaturePresetSchema]


class MakeupPresetsResponse(StrictModel):
    enabled: bool
    credits: int
    presets: list[MediaFeaturePresetSchema]


class BackgroundPresetsResponse(StrictModel):
    enabled: bool
    presets: list[MediaFeaturePresetSchema]


class SpeechOptionSchema(StrictModel):
    id: str
    name: str


class SpeechVoiceSchema(StrictModel):
    id: str
    name: str
    gender: AvatarGender


class AvatarSpeechOptionsResponse(StrictModel):
    enabled: bool
    configured: bool
    credits: int
    maxTextLength: int
    languages: list[SpeechOptionSchema]
    moods: list[SpeechOptionSchema]
    voices: list[SpeechVoiceSchema]


class UserAvatarCreateRequest(StrictModel):
    title: str | None = Field(default=None, max_length=120)
    image: InlineFeatureImage | None = None
    systemAvatarId: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def _one_source(self) -> UserAvatarCreateRequest:
        if (self.image is None) == (self.systemAvatarId is None):
            raise ValueError("provide exactly one of image or systemAvatarId")
        return self


class UserAvatarSchema(StrictModel):
    id: uuid.UUID
    title: str | None
    sourceSystemAvatarId: str | None
    imageUrl: str
    status: Literal["ready", "preparing"]
    preparationJobId: uuid.UUID | None
    createdAt: datetime.datetime
    updatedAt: datetime.datetime


class UserAvatarsResponse(StrictModel):
    avatars: list[UserAvatarSchema]


class AvatarBackgroundRequest(StrictModel):
    type: Literal["none", "color", "preset", "custom"] = "none"
    color: str | None = Field(default=None, max_length=32)
    presetId: str | None = Field(default=None, max_length=64)
    image: InlineFeatureImage | None = None

    @model_validator(mode="after")
    def _matching_value(self) -> AvatarBackgroundRequest:
        provided = (
            int(self.color is not None)
            + int(self.presetId is not None)
            + int(self.image is not None)
        )
        if self.type == "none" and provided == 0:
            return self
        expected = {
            "color": self.color is not None,
            "preset": self.presetId is not None,
            "custom": self.image is not None,
        }.get(self.type, False)
        if not expected or provided != 1:
            raise ValueError("background type must match exactly one supplied value")
        return self


class UserAvatarPrepareRequest(StrictModel):
    removeBackground: bool = True
    background: AvatarBackgroundRequest = Field(default_factory=AvatarBackgroundRequest)

    @model_validator(mode="after")
    def _background_needs_cutout(self) -> UserAvatarPrepareRequest:
        if not self.removeBackground and self.background.type != "none":
            raise ValueError("a replacement background requires removeBackground=true")
        return self


class AvatarSpeechRequest(StrictModel):
    systemAvatarId: str | None = Field(default=None, max_length=64)
    userAvatarId: uuid.UUID | None = None
    text: str = Field(min_length=1, max_length=300)
    language: SpeechLanguage
    voiceId: str = Field(min_length=1, max_length=64)
    mood: SpeechMood

    @model_validator(mode="after")
    def _one_avatar(self) -> AvatarSpeechRequest:
        if (self.systemAvatarId is None) == (self.userAvatarId is None):
            raise ValueError("provide exactly one of systemAvatarId or userAvatarId")
        return self

    @field_validator("text")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("text must not be blank")
        return stripped


class MakeupCreateRequest(StrictModel):
    presetId: str = Field(min_length=1, max_length=64)
    image: InlineFeatureImage
