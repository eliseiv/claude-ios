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
# 32-bit range: what the upstream models accept as a reproducibility seed.
_SEED_MAX = 2**31 - 1


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


class MediaModeSchema(StrictModel):
    """Один режим генерации модели и параметры, которые он принимает.

    Наборы значений различаются между режимами (у Veo в text-to-video нет `auto`, у Kling в
    image-to-video нет `aspectRatio`), поэтому UI должен строить контролы по режиму, а не по модели.
    """

    mode: Literal["textToImage", "imageToImage", "textToVideo", "imageToVideo"] = Field(
        description=(
            "Режим: `textTo…` — когда референсное изображение не передано, `imageTo…` — когда "
            "передано. Выбирается автоматически по наличию `imageUrls`/`imageUrl` в запросе."
        )
    )
    params: list[str] = Field(
        description=(
            "Параметры, которые принимает этот режим (имена полей запроса). Параметр, которого "
            "здесь нет, будет проигнорирован — не показывайте для него контрол."
        )
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
    defaults: dict[str, str | int | bool] = Field(
        default_factory=dict,
        description=(
            "Значения, которые сервер подставит сам, если поле не прислано. Перечислены только "
            "параметры, влияющие на цену, — подставляйте их в свой расчёт стоимости, чтобы он "
            "совпал с `creditsCharged`. Пустой объект — подставлять нечего."
        ),
    )


class MediaModelSchema(StrictModel):
    id: str = Field(description="Идентификатор модели для полей `model` в запросах генерации.")
    title: str = Field(description="Человекочитаемое название модели для UI.")
    kind: Literal["image", "video"] = Field(
        description="Что генерирует модель: `image` → `POST /v1/media/images`, `video` → `/videos`."
    )
    credits: int = Field(
        description=(
            "Базовая цена в кредитах: для image — одно изображение в качестве `1K`; для video — "
            "одна пачка длительности `baseDurationSeconds` при базовом качестве (без "
            "resolution/audio множителей). Не хардкодьте итог: смотрите `resolutionCredits` / "
            "`resolutionMultipliers` / `audioMultiplier`. Фактически списанное — в "
            "`creditsCharged`."
        )
    )
    baseDurationSeconds: int | None = Field(
        default=None,
        description=(
            "Длительность видео, которую покрывает базовая цена. `null` у image-моделей — они "
            "масштабируются по `resolutionCredits` и `numImages`."
        ),
    )
    resolutionCredits: dict[str, int] | None = Field(
        default=None,
        description=(
            "Image: цена **одного** изображения по `resolution` (целые ступени). Итог = "
            "`resolutionCredits[resolution] × numImages`. `null` у video-моделей."
        ),
    )
    resolutionMultipliers: dict[str, int] | None = Field(
        default=None,
        description=(
            "Video: множитель пачки по `resolution` (например Veo `4k` → 2). `null`/пусто — "
            "resolution на цену не влияет."
        ),
    )
    # int|float, not float: Veo's multiplier is a whole 2 and has always gone out as `2`, so
    # widening it to `2.0` would break a client decoding it as an integer. Kling V3's is 1.5.
    audioMultiplier: int | float | None = Field(
        default=None,
        description=(
            "Video: множитель при `generateAudio: true` (Veo → 2, Kling V3 → 1.5). Может быть "
            "дробным — итоговая цена округляется вверх. `null` — звук на цену не влияет (даже "
            "если переключатель в UI есть)."
        ),
    )
    supportsImageInput: bool = Field(
        description=(
            "Принимает ли модель референсные изображения (`imageUrls`/`imageUrl`). Для image-"
            "моделей это режим редактирования, для video — image-to-video. На цену не влияет."
        )
    )
    maxInputImages: int = Field(
        description="Максимум референсных изображений в одном запросе (0 — не поддерживаются)."
    )
    supportsAudio: bool = Field(
        description="Умеет ли модель генерировать звук (параметр `generateAudio`)."
    )
    modes: list[MediaModeSchema] = Field(
        description=(
            "Режимы генерации с их параметрами. Первый — без референсного изображения, второй "
            "(если есть) — с ним. Mode на цену не влияет."
        )
    )


class MediaModelsResponse(StrictModel):
    models: list[MediaModelSchema] = Field(
        description="Каталог доступных моделей генерации в порядке отображения."
    )


class ImageGenerationRequest(StrictModel):
    """Запрос генерации изображения. Наличие `imageUrls` переключает режим на редактирование."""

    model_config = StrictModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "model": "nano-banana-pro",
                    "prompt": "Латунный телескоп на балконе в сумерках, кинематографично",
                    "aspectRatio": "16:9",
                    "resolution": "2K",
                    "numImages": 1,
                    "outputFormat": "png",
                },
                {
                    "model": "nano-banana-2",
                    "prompt": "Добавь рядом чашку чая с паром",
                    "imageUrls": ["https://example.com/teapot.jpg"],
                    "resolution": "1K",
                },
            ]
        }
    }

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
    seed: int | None = Field(
        default=None,
        ge=0,
        le=_SEED_MAX,
        description=(
            "Фиксирует случайность: одинаковый `seed` с теми же параметрами даёт похожий "
            "результат. Опущено — каждый запуск новый."
        ),
    )

    @field_validator("imageUrls")
    @classmethod
    def _check_urls(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_https_urls(value)


class VideoGenerationRequest(StrictModel):
    """Запрос генерации видео. Наличие `imageUrl` переключает режим на image-to-video."""

    model_config = StrictModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "model": "veo-3.1",
                    "prompt": "Чашка кофе на столике кафе, утренний свет, лёгкий наезд камеры",
                    "aspectRatio": "16:9",
                    "resolution": "1080p",
                    "duration": "8s",
                    "generateAudio": True,
                },
                {
                    "model": "kling-video-v3",
                    "prompt": "Медленная панорама вдоль чайника",
                    "imageUrl": "https://example.com/teapot.jpg",
                    "duration": "10",
                    "cfgScale": 0.7,
                    "negativePrompt": "размытие, искажения",
                },
            ]
        }
    }

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
        default=None,
        description=(
            "Генерировать ли звук. Только для моделей с `supportsAudio: true`; у остальных "
            "игнорируется."
        ),
    )
    cfgScale: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "Насколько строго следовать промту (0–1, у провайдера по умолчанию 0.5). Выше — "
            "ближе к описанию, ниже — свободнее. Только у моделей Kling."
        ),
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        le=_SEED_MAX,
        description=(
            "Фиксирует случайность: одинаковый `seed` с теми же параметрами даёт похожий "
            "результат. Опущено — каждый запуск новый."
        ),
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


class MediaUploadRequest(StrictModel):
    """Загрузка референсного изображения (inline base64) — ADR-062.

    Форма тела совпадает с загрузкой файлов рабочего пространства, чтобы клиенту не понадобился
    второй нормалайзер. Принимаются только изображения: и режим редактирования, и image-to-video
    берут на вход картинку.
    """

    model_config = StrictModel.model_config | {
        "json_schema_extra": {
            "examples": [
                {
                    "type": "image",
                    "mediaType": "image/jpeg",
                    "filename": "photo.jpg",
                    "data": "/9j/4AAQSkZJRgABAQAAAQABAAD…",
                }
            ]
        }
    }

    type: Literal["image"] = Field(
        description="Класс файла. Поддерживается только `image` — референс генерации."
    )
    mediaType: Literal["image/jpeg", "image/png", "image/gif", "image/webp"] = Field(
        description="MIME-тип изображения из allowlist. Вне списка → `422`."
    )
    filename: str = Field(
        min_length=1,
        max_length=512,
        description="Имя файла. Попадает в имя объекта у провайдера.",
    )
    data: str = Field(
        min_length=1,
        description=(
            "Содержимое файла в base64. Только inline base64 — ссылки здесь не принимаются. "
            "Размер после декодирования ограничен (превышение → `413`)."
        ),
    )

    @field_validator("filename")
    @classmethod
    def _check_filename(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("filename must be a non-empty string")
        return value


class MediaUploadResponse(StrictModel):
    url: str = Field(
        description=(
            "https-ссылка на загруженный файл. Подставляйте её в `imageUrls` "
            "(`POST /v1/media/images`) или `imageUrl` (`POST /v1/media/videos`)."
        )
    )
    mediaType: str = Field(description="MIME-тип загруженного файла.")
    size: int = Field(description="Размер файла в байтах после декодирования.")
    expiresAt: datetime.datetime | None = Field(
        default=None,
        description=(
            "Когда ссылка перестанет работать, если срок задан на инстансе. `null` — срок не "
            "ограничен либо определяется политикой провайдера; в этом случае не рассчитывайте на "
            "бессрочность и сохраняйте нужный файл локально."
        ),
    )


class MediaJobsListResponse(StrictModel):
    jobs: list[MediaJobResponse] = Field(
        description=(
            "Задачи пользователя, новые сверху. Список не опрашивает провайдера: у незавершённых "
            "задач отдаётся последнее известное состояние."
        )
    )
