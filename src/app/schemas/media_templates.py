"""Schemas for /v1/media/templates/* and /v1/admin/media/templates (ADR-066)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.schemas.common import StrictModel

_ID_RE = re.compile(r"^[a-z0-9_]+$")
_PROMPT_MAX = 5000
_TITLE_MAX = 120
_IMAGE_PARAM_KEYS = frozenset(
    {"aspectRatio", "resolution", "numImages", "outputFormat", "seed"}
)
_VIDEO_PARAM_KEYS = frozenset(
    {
        "aspectRatio",
        "resolution",
        "duration",
        "generateAudio",
        "cfgScale",
        "seed",
        "negativePrompt",
    }
)


class MediaTemplateCoverIn(StrictModel):
    mediaType: Literal["image/jpeg", "image/png", "image/webp"] = Field(
        description="MIME-тип обложки."
    )
    data: str = Field(
        min_length=1,
        description="Обложка в base64 (без data:-префикса).",
    )


class MediaTemplateCreateRequest(StrictModel):
    """Admin create: metadata + base64 cover."""

    id: str = Field(
        min_length=1,
        max_length=64,
        description="Стабильный slug `[a-z0-9_]` — также путь обложки.",
    )
    kind: Literal["image", "video"] = Field(
        description="`image` → list `/templates/images`; `video` → `/templates/videos`."
    )
    title: str = Field(min_length=1, max_length=_TITLE_MAX)
    prompt: str = Field(min_length=1, max_length=_PROMPT_MAX)
    model: str = Field(
        min_length=1,
        description="Id модели из `GET /v1/media/models`, того же `kind`.",
    )
    requiredInputImages: int = Field(
        default=0,
        ge=0,
        le=14,
        description="Сколько фото попросить у юзера перед генерацией.",
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Knobs create-запроса (`aspectRatio`, `resolution`, …) без prompt/model.",
    )
    cover: MediaTemplateCoverIn
    sortOrder: int | None = Field(
        default=None,
        description="Порядок в list; опущено — ставится в конец kind-группы.",
    )

    @field_validator("id")
    @classmethod
    def _id_slug(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("id must match [a-z0-9_]+")
        return value

    @model_validator(mode="after")
    def _param_keys_for_kind(self) -> MediaTemplateCreateRequest:
        allowed = _IMAGE_PARAM_KEYS if self.kind == "image" else _VIDEO_PARAM_KEYS
        unknown = set(self.parameters) - allowed
        if unknown:
            raise ValueError(
                f"parameters contain unsupported keys for {self.kind}: "
                f"{', '.join(sorted(unknown))}"
            )
        if self.kind == "video" and self.requiredInputImages > 1:
            raise ValueError("video templates accept at most 1 requiredInputImages")
        return self


class MediaTemplateItemSchema(StrictModel):
    id: str
    title: str
    coverUrl: str
    prompt: str
    model: str
    requiredInputImages: int
    parameters: dict[str, Any]


class MediaTemplatesResponse(StrictModel):
    templates: list[MediaTemplateItemSchema]


class MediaTemplateDeleteResponse(StrictModel):
    deleted: bool = True


class MediaTemplateAdminItemSchema(StrictModel):
    """Create response — same fields as list item (coverUrl absolute when domain set)."""

    id: str
    kind: Literal["image", "video"]
    title: str
    coverUrl: str
    prompt: str
    model: str
    requiredInputImages: int
    parameters: dict[str, Any]
    sortOrder: int
