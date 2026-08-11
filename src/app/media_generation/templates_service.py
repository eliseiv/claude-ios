"""Gallery templates use-cases (ADR-066)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.chat.attachments import _check_magic_bytes, _decode_base64, _decoded_len_from_base64
from app.config import Settings, get_settings
from app.errors import ConflictError, NotFoundError, PayloadTooLargeError, ValidationFailedError
from app.media_generation.catalog import KIND_IMAGE, KIND_VIDEO, find_model
from app.media_generation.templates_repository import MediaTemplatesRepository
from app.models import MediaTemplate


@dataclass(frozen=True)
class TemplateListItem:
    id: str
    title: str
    cover_url: str
    prompt: str
    model: str
    required_input_images: int
    parameters: dict[str, Any]


@dataclass(frozen=True)
class TemplateCover:
    media_type: str
    data: bytes


@dataclass(frozen=True)
class TemplateAdminItem:
    id: str
    kind: str
    title: str
    cover_url: str
    prompt: str
    model: str
    required_input_images: int
    parameters: dict[str, Any]
    sort_order: int


class MediaTemplatesService:
    def __init__(
        self,
        repo: MediaTemplatesRepository,
        settings: Settings | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings or get_settings()

    def cover_url_for(self, template_id: str) -> str:
        path = f"/v1/media/templates/{template_id}/cover"
        domain = self._settings.normalized_service_domain()
        if not domain:
            return path
        return f"https://{domain}{path}"

    def _to_list_item(self, row: MediaTemplate) -> TemplateListItem:
        return TemplateListItem(
            id=row.id,
            title=row.title,
            cover_url=self.cover_url_for(row.id),
            prompt=row.prompt,
            model=row.model,
            required_input_images=row.required_input_images,
            parameters=dict(row.parameters or {}),
        )

    async def list_kind(self, kind: str) -> list[TemplateListItem]:
        if kind not in (KIND_IMAGE, KIND_VIDEO):
            raise ValidationFailedError("kind must be image or video")
        rows = await self._repo.list_by_kind(kind)
        return [self._to_list_item(row) for row in rows]

    async def get_cover(self, template_id: str) -> TemplateCover:
        row = await self._repo.get(template_id)
        if row is None:
            raise NotFoundError("template not found")
        return TemplateCover(media_type=row.cover_media_type, data=bytes(row.cover_bytes))

    async def create(
        self,
        *,
        template_id: str,
        kind: str,
        title: str,
        prompt: str,
        model_id: str,
        required_input_images: int,
        parameters: dict[str, Any],
        cover_media_type: str,
        cover_data_b64: str,
        sort_order: int | None,
    ) -> TemplateAdminItem:
        self._validate_model_and_params(
            kind=kind,
            model_id=model_id,
            required_input_images=required_input_images,
            parameters=parameters,
        )
        cover_bytes = self._decode_cover(cover_media_type, cover_data_b64)

        existing = await self._repo.get(template_id)
        if existing is not None:
            raise ConflictError("template id already exists")

        order = sort_order if sort_order is not None else await self._repo.next_sort_order(kind)
        row = await self._repo.create(
            template_id=template_id,
            kind=kind,
            title=title,
            prompt=prompt,
            model=model_id,
            required_input_images=required_input_images,
            parameters=parameters,
            cover_bytes=cover_bytes,
            cover_media_type=cover_media_type,
            sort_order=order,
        )
        item = self._to_list_item(row)
        return TemplateAdminItem(
            id=item.id,
            kind=kind,
            title=item.title,
            cover_url=item.cover_url,
            prompt=item.prompt,
            model=item.model,
            required_input_images=item.required_input_images,
            parameters=item.parameters,
            sort_order=row.sort_order,
        )

    async def delete(self, template_id: str) -> None:
        deleted = await self._repo.delete(template_id)
        if not deleted:
            raise NotFoundError("template not found")

    def _validate_model_and_params(
        self,
        *,
        kind: str,
        model_id: str,
        required_input_images: int,
        parameters: dict[str, Any],
    ) -> None:
        model = find_model(model_id)
        if model is None:
            raise ValidationFailedError(f"unknown model: {model_id}")
        if model.kind != kind:
            raise ValidationFailedError(f"model {model_id} is {model.kind}, not {kind}")

        with_image = required_input_images > 0
        if with_image and model.image_variant is None:
            raise ValidationFailedError(f"model {model_id} does not accept reference images")
        if with_image and required_input_images > model.max_input_images:
            raise ValidationFailedError(
                f"requiredInputImages exceeds model max ({model.max_input_images})"
            )

        variant = model.variant_for(with_image=with_image)
        if variant is None:
            raise ValidationFailedError(f"model {model_id} has no variant for this mode")

        # prompt is always accepted upstream but lives outside parameters.
        allowed_fields = set(variant.fields) - {"prompt"}
        for key, value in parameters.items():
            if key not in allowed_fields:
                raise ValidationFailedError(
                    f"parameter {key!r} is not accepted by {model.id} in this mode"
                )
            if key in ("aspectRatio", "resolution", "duration"):
                allowed = variant.allowed(key)
                if allowed and value not in allowed:
                    raise ValidationFailedError(f"invalid {key} for {model.id}: {value!r}")
            if key == "numImages" and (not isinstance(value, int) or value < 1 or value > 4):
                raise ValidationFailedError("numImages must be an integer 1..4")
            if key == "generateAudio" and not isinstance(value, bool):
                raise ValidationFailedError("generateAudio must be a boolean")
            if key == "cfgScale" and not isinstance(value, int | float):
                raise ValidationFailedError("cfgScale must be a number")
            if key == "seed" and (not isinstance(value, int) or value < 0):
                raise ValidationFailedError("seed must be a non-negative integer")
            if key == "outputFormat" and value not in ("jpeg", "png", "webp"):
                raise ValidationFailedError("outputFormat must be jpeg|png|webp")
            if key == "negativePrompt" and not isinstance(value, str):
                raise ValidationFailedError("negativePrompt must be a string")

    def _decode_cover(self, media_type: str, data_b64: str) -> bytes:
        max_bytes = self._settings.media_template_cover_max_bytes
        decoded_len = _decoded_len_from_base64(data_b64)
        if decoded_len > max_bytes:
            raise PayloadTooLargeError("cover exceeds size limit")
        raw = _decode_base64(data_b64)
        if len(raw) > max_bytes:
            raise PayloadTooLargeError("cover exceeds size limit")
        _check_magic_bytes(media_type, raw)
        return raw
