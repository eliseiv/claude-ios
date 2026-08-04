"""Schemas for /v1/media/* — image & video generation (media-generation/02-api-contracts.md).

JWT-protected, owner-scoped; every model forbids extra fields (StrictModel). Requests carry only
*generation intent* — the model id, the prompt and a small set of common knobs. The price, the fal
endpoint and the upstream field names are server-side (catalog.py), so the client can never
influence what a run costs.

Reference images are passed as **URLs**, not inline base64: fal fetches them itself, and a 4K
reference image would blow past SIZE_LIMIT_BODY. Only https URLs are accepted.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import Field, field_validator

from app.schemas.common import StrictModel

_PROMPT_MAX = 5000
_NEGATIVE_PROMPT_MAX = 2000
_URL_MAX = 2048
_MAX_IMAGE_URLS = 14


def _validate_https_urls(values: list[str]) -> list[str]:
    """Reference images must be plain https URLs.

    The URL is handed to fal, which fetches it server-side, so anything but https (``file:``,
    ``data:``, an internal ``http://10.x``) is refused up front rather than forwarded.
    """
    for value in values:
        if not value.startswith("https://"):
            raise ValueError("image URLs must start with https://")
        if len(value) > _URL_MAX:
            raise ValueError(f"image URL must be at most {_URL_MAX} characters")
    return values


class MediaAssetSchema(StrictModel):
    url: str = Field(description="Прямая ссылка на сгенерированный файл (CDN fal).")
    contentType: str | None = Field(
        default=None,
        description="MIME-тип файла, если провайдер его вернул (например `image/png`).",
    )
    fileName: str | None = Field(
        default=None, description="Имя файла, если провайдер его вернул, иначе null."
    )


class MediaModelSchema(StrictModel):
    id: str = Field(description="Идентификатор модели для полей `model` в запросах генерации.")
    title: str = Field(description="Человекочитаемое название модели для UI.")
    kind: Literal["image", "video"] = Field(
        description="Что генерирует модель: `image` → `POST /v1/media/images`, `video` → `/videos`."
    )
    credits: int = Field(description="Сколько кредитов списывается за одну генерацию.")
    supportsImageInput: bool = Field(
        description=(
            "Принимает ли модель референсные изображения (`imageUrls`/`imageUrl`). Для image-"
            "моделей это режим редактирования, для video — image-to-video."
        )
    )
    maxInputImages: int = Field(
        description="Максимум референсных изображений в одном запросе (0 — не поддерживаются)."
    )
    aspectRatios: list[str] = Field(
        description="Допустимые значения `aspectRatio`. Пустой список — параметр не поддерживается."
    )
    resolutions: list[str] = Field(
        description="Допустимые значения `resolution`. Пустой список — параметр не поддерживается."
    )
    durations: list[str] = Field(
        description="Допустимые значения `duration`. Пустой список — параметр не поддерживается."
    )


class MediaModelsResponse(StrictModel):
    models: list[MediaModelSchema] = Field(
        description="Каталог доступных моделей генерации в порядке отображения."
    )


class ImageGenerationRequest(StrictModel):
    model: str = Field(
        min_length=1,
        description=(
            "Идентификатор image-модели из `GET /v1/media/models` (например `nano-banana-2`)."
        ),
    )
    prompt: str = Field(
        min_length=1,
        max_length=_PROMPT_MAX,
        description="Текстовое описание желаемого изображения.",
    )
    imageUrls: list[str] | None = Field(
        default=None,
        max_length=_MAX_IMAGE_URLS,
        description=(
            "Референсные изображения (https-URL) — включают режим редактирования. Опущено или "
            "null — генерация с нуля по промту."
        ),
    )
    aspectRatio: str | None = Field(
        default=None, description="Соотношение сторон из `aspectRatios` выбранной модели."
    )
    resolution: str | None = Field(
        default=None, description="Разрешение из `resolutions` выбранной модели (например `2K`)."
    )
    numImages: int | None = Field(
        default=None, ge=1, le=4, description="Сколько изображений сгенерировать (1–4)."
    )
    outputFormat: Literal["jpeg", "png", "webp"] | None = Field(
        default=None, description="Формат файла результата."
    )

    @field_validator("imageUrls")
    @classmethod
    def _check_urls(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_https_urls(value)


class VideoGenerationRequest(StrictModel):
    model: str = Field(
        min_length=1,
        description="Идентификатор video-модели из `GET /v1/media/models` (например `veo-3.1`).",
    )
    prompt: str = Field(
        min_length=1,
        max_length=_PROMPT_MAX,
        description="Текстовое описание сцены. Для моделей со звуком может содержать реплики.",
    )
    imageUrl: str | None = Field(
        default=None,
        max_length=_URL_MAX,
        description=(
            "Стартовый кадр (https-URL) — включает режим image-to-video. Опущено или null — "
            "генерация из текста."
        ),
    )
    negativePrompt: str | None = Field(
        default=None,
        max_length=_NEGATIVE_PROMPT_MAX,
        description="Что не должно попасть в кадр. Поддерживается не всеми моделями.",
    )
    aspectRatio: str | None = Field(
        default=None,
        description=(
            "Соотношение сторон из `aspectRatios` модели. В режиме image-to-video игнорируется — "
            "берётся из стартового кадра."
        ),
    )
    resolution: str | None = Field(
        default=None, description="Разрешение из `resolutions` модели (например `720p`)."
    )
    duration: str | None = Field(
        default=None, description="Длительность из `durations` модели (например `8s` или `5`)."
    )
    generateAudio: bool | None = Field(
        default=None, description="Генерировать ли звук. Поддерживается не всеми моделями."
    )

    @field_validator("imageUrl")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        return None if value is None else _validate_https_urls([value])[0]


class MediaJobResponse(StrictModel):
    jobId: uuid.UUID = Field(description="Идентификатор задачи для `GET /v1/media/jobs/{jobId}`.")
    status: Literal["queued", "running", "completed", "failed"] = Field(
        description=(
            "Состояние задачи. `queued`/`running` — опрашивайте эндпоинт задачи; `completed` — "
            "результат в `assets`; `failed` — причина в `error`, списанные кредиты возвращены."
        )
    )
    kind: Literal["image", "video"] = Field(description="Тип генерации.")
    model: str = Field(description="Идентификатор модели, которой выполнена генерация.")
    prompt: str = Field(description="Промт, с которым была отправлена задача.")
    creditsCharged: int = Field(description="Сколько кредитов списано при постановке в очередь.")
    creditsRefunded: bool = Field(description="Возвращены ли кредиты (true только для `failed`).")
    assets: list[MediaAssetSchema] = Field(
        description="Сгенерированные файлы. Непустой список только при `status = completed`."
    )
    error: str | None = Field(
        default=None, description="Причина неудачи при `status = failed`, иначе null."
    )
    createdAt: datetime.datetime = Field(description="Когда задача была поставлена в очередь.")
    updatedAt: datetime.datetime = Field(
        description="Когда состояние задачи менялось последний раз."
    )


class MediaJobsListResponse(StrictModel):
    jobs: list[MediaJobResponse] = Field(
        description=(
            "Задачи пользователя, новые сверху. Список не опрашивает провайдера: у незавершённых "
            "задач отдаётся последнее известное состояние."
        )
    )
