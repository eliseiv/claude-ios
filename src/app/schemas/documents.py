"""Схемы документов чата (ADR-090)."""

from __future__ import annotations

import datetime
import uuid
from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel

DocumentMediaType = Literal["text/markdown", "text/plain", "text/csv", "application/json"]


class DocumentCreateRequest(StrictModel):
    filename: str = Field(
        min_length=1,
        max_length=200,
        description="Имя файла. Разделители пути вырезаются, расширение приводится к `mediaType`.",
    )
    mediaType: DocumentMediaType = Field(
        description="Тип содержимого. Только текстовые форматы — бинарные не поддерживаются."
    )
    content: str = Field(
        description="Полное содержимое документа в base64 (UTF-8 после декодирования)."
    )


class DocumentUpdateRequest(StrictModel):
    content: str = Field(
        description=(
            "Новое содержимое ЦЕЛИКОМ, в base64. Частичного обновления нет: документ заменяется "
            "полностью, а `version` увеличивается на единицу."
        )
    )


class DocumentSchema(StrictModel):
    documentId: uuid.UUID = Field(description="Идентификатор документа в пределах чата.")
    filename: str = Field(description="Имя файла для показа и скачивания.")
    mediaType: DocumentMediaType = Field(description="Тип содержимого.")
    size: int = Field(description="Размер содержимого в байтах (UTF-8).")
    version: int = Field(
        description=(
            "Версия. Начинается с 1 и увеличивается при каждом обновлении — по ней клиент "
            "понимает, что документ изменился."
        )
    )
    createdBy: Literal["user", "assistant"] = Field(
        description="Кто создал документ. Влияет только на отображение, не на права."
    )
    createdAt: datetime.datetime = Field(description="Момент создания.")
    updatedAt: datetime.datetime = Field(description="Момент последнего изменения.")
    content: str | None = Field(
        default=None,
        description=(
            "Содержимое в base64. Присутствует только в ответе на запрос одного документа; "
            "в списке — `null`."
        ),
    )


class DocumentsListResponse(StrictModel):
    documents: list[DocumentSchema] = Field(
        description="Документы этого чата, старые первыми. Удаляются вместе с чатом."
    )


class DocumentDeleteResponse(StrictModel):
    deleted: bool = Field(description="Всегда `true`; повторное удаление даёт `404`.")
