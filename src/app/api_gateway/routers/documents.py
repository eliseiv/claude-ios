"""Документы чата: /v1/chats/{sessionId}/documents (ADR-090 §4).

Скачивание идёт по JWT, а НЕ по подписанному URL: подпись введена для медиа (ADR-085) потому, что
ассет тянет webview без заголовков авторизации, — документ забирает само приложение, у которого
токен уже есть. Правило «ассет отдаётся по подписи» сюда не переносится.

Изоляция владельца на каждом пути: чужая сессия и чужой документ дают 404, а не 403 —
существование чужого объекта не раскрывается.
"""

from __future__ import annotations

import base64
import binascii
import urllib.parse
import uuid
from typing import Annotated

from fastapi import APIRouter, Path
from starlette.responses import Response

from app.api_gateway.rate_limit import enforce_other_limits
from app.deps import CurrentUser, get_documents_service_dep
from app.documents import DocumentsService, DocumentView
from app.documents.service import CREATED_BY_USER
from app.errors import RateLimitedError, ValidationFailedError
from app.schemas.documents import (
    DocumentCreateRequest,
    DocumentDeleteResponse,
    DocumentSchema,
    DocumentsListResponse,
    DocumentUpdateRequest,
)

router = APIRouter(prefix="/v1/chats/{session_id}/documents", tags=["Documents"])

SessionId = Annotated[
    uuid.UUID, Path(description="Идентификатор чата, которому принадлежат документы.")
]
DocumentId = Annotated[uuid.UUID, Path(description="Идентификатор документа.")]


async def _rate_limit(user_id: uuid.UUID) -> None:
    if not await enforce_other_limits(user_id=user_id):
        raise RateLimitedError("rate limit exceeded")


def _decode(data: str) -> str:
    """base64 → UTF-8. Битый вход — 422, никогда 500 (та же политика, что у вложений)."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationFailedError("document data is not valid base64") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailedError("document content is not valid UTF-8") from exc


def _content_disposition(filename: str) -> str:
    """Заголовок для скачивания, устойчивый к не-ASCII имени (RFC 5987).

    HTTP-заголовки кодируются latin-1, поэтому кириллическое имя — а для этой аудитории оно
    обычное — падало бы UnicodeEncodeError прямо в Starlette. Отдаём ДВА параметра: `filename` с
    ASCII-остатком для старых клиентов и `filename*` с percent-encoded UTF-8, который современные
    предпочитают. Разделители пути вырезаны раньше, в сервисе.
    """
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").replace('"', "").strip()
    fallback = ascii_name or "document"
    quoted = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def _schema(view: DocumentView, *, with_content: bool = False) -> DocumentSchema:
    content = None
    if with_content and view.content is not None:
        content = base64.b64encode(view.content.encode("utf-8")).decode("ascii")
    return DocumentSchema(
        documentId=view.id,
        filename=view.filename,
        mediaType=view.media_type,
        size=view.size_bytes,
        version=view.version,
        createdBy=view.created_by,
        createdAt=view.created_at,
        updatedAt=view.updated_at,
        content=content,
    )


@router.post(
    "",
    response_model=DocumentSchema,
    status_code=201,
    summary="Загрузить документ в чат",
    description=(
        "Создаёт текстовый документ в этом чате. Тот же объект создаёт и модель инструментом "
        "`document.create` — дальше оба читаются, правятся и скачиваются одинаково. Документ "
        "удаляется вместе с чатом."
    ),
)
async def create_document(
    session_id: SessionId,
    body: DocumentCreateRequest,
    current: CurrentUser,
    documents: get_documents_service_dep,
) -> DocumentSchema:
    await _rate_limit(current.user_id)
    await documents.assert_session(user_id=current.user_id, session_id=session_id)
    view = await documents.create(
        user_id=current.user_id,
        session_id=session_id,
        filename=body.filename,
        media_type=body.mediaType,
        content=_decode(body.content),
        created_by=CREATED_BY_USER,
    )
    return _schema(view)


@router.get(
    "",
    response_model=DocumentsListResponse,
    summary="Документы чата",
    description="Список документов этого чата без содержимого — содержимое запрашивается отдельно.",
)
async def list_documents(
    session_id: SessionId,
    current: CurrentUser,
    documents: get_documents_service_dep,
) -> DocumentsListResponse:
    await _rate_limit(current.user_id)
    await documents.assert_session(user_id=current.user_id, session_id=session_id)
    views = await documents.list(user_id=current.user_id, session_id=session_id)
    return DocumentsListResponse(documents=[_schema(v) for v in views])


@router.get(
    "/{document_id}",
    response_model=DocumentSchema,
    summary="Документ с содержимым",
    description="Метаданные и содержимое в base64. Для сохранения файла используйте `/download`.",
)
async def get_document(
    session_id: SessionId,
    document_id: DocumentId,
    current: CurrentUser,
    documents: get_documents_service_dep,
) -> DocumentSchema:
    await _rate_limit(current.user_id)
    view = await documents.get(
        user_id=current.user_id, session_id=session_id, document_id=document_id
    )
    return _schema(view, with_content=True)


@router.patch(
    "/{document_id}",
    response_model=DocumentSchema,
    summary="Заменить содержимое документа",
    description=(
        "Заменяет содержимое ЦЕЛИКОМ и увеличивает `version`. Частичного обновления нет: "
        "присылайте полный новый текст."
    ),
)
async def update_document(
    session_id: SessionId,
    document_id: DocumentId,
    body: DocumentUpdateRequest,
    current: CurrentUser,
    documents: get_documents_service_dep,
) -> DocumentSchema:
    await _rate_limit(current.user_id)
    view = await documents.update(
        user_id=current.user_id,
        session_id=session_id,
        document_id=document_id,
        content=_decode(body.content),
    )
    return _schema(view)


@router.get(
    "/{document_id}/download",
    summary="Скачать документ файлом",
    description=(
        "Отдаёт содержимое как файл с `Content-Disposition: attachment`. Требует того же "
        "`Authorization`, что и остальные `/v1/*` — подписанный URL здесь не используется."
    ),
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_document(
    session_id: SessionId,
    document_id: DocumentId,
    current: CurrentUser,
    documents: get_documents_service_dep,
) -> Response:
    await _rate_limit(current.user_id)
    view = await documents.get(
        user_id=current.user_id, session_id=session_id, document_id=document_id
    )
    payload = (view.content or "").encode("utf-8")
    return Response(
        content=payload,
        media_type=view.media_type,
        headers={
            "Content-Disposition": _content_disposition(view.filename),
            "Cache-Control": "private, no-store",
        },
    )


@router.delete(
    "/{document_id}",
    response_model=DocumentDeleteResponse,
    summary="Удалить документ",
)
async def delete_document(
    session_id: SessionId,
    document_id: DocumentId,
    current: CurrentUser,
    documents: get_documents_service_dep,
) -> DocumentDeleteResponse:
    await _rate_limit(current.user_id)
    await documents.delete(user_id=current.user_id, session_id=session_id, document_id=document_id)
    return DocumentDeleteResponse(deleted=True)


__all__ = ["DocumentsService", "router"]
