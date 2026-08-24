"""Документы чата — доменный слой (ADR-090).

Один и тот же объект создаётся и пользователем (`POST /v1/chats/{id}/documents`), и моделью
(`document.create`); дальше оба одинаково читаются, правятся и скачиваются. Отдельного «класса
загруженных» нет — иначе половина инструментов работала бы с одним классом, половина с другим
(ADR-090 §5). ``created_by`` сохраняется как факт происхождения и на права НЕ влияет.

Изоляция владельца проверяется в КАЖДОМ методе по паре (user_id, session_id): чужой документ и
несуществующий неотличимы — оба дают ``DocumentNotFoundError`` (404), чтобы существование чужого
объекта не раскрывалось.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import (
    DocumentNotFoundError,
    DocumentsTotalTooLargeError,
    DocumentTooLargeError,
    SessionNotFoundError,
    TooManyDocumentsError,
    UnsupportedMediaTypeError,
)
from app.models import ChatDocument, ChatSession

# Формат — только текстовый (ADR-090 §1). Хранение TEXT, не BYTEA: модель порождает текст
# напрямую, поэтому такой документ она умеет и создавать, и править.
ALLOWED_MEDIA_TYPES = frozenset({"text/markdown", "text/plain", "text/csv", "application/json"})

CREATED_BY_USER = "user"
CREATED_BY_ASSISTANT = "assistant"

_EXTENSION_BY_TYPE = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/json": ".json",
}


@dataclass(frozen=True)
class DocumentView:
    """Проекция документа для API и инструментов (ORM наружу не уходит)."""

    id: uuid.UUID
    filename: str
    media_type: str
    size_bytes: int
    version: int
    created_by: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    content: str | None = None

    @classmethod
    def of(cls, row: ChatDocument, *, with_content: bool = False) -> DocumentView:
        return cls(
            id=row.id,
            filename=row.filename,
            media_type=row.media_type,
            size_bytes=row.size_bytes,
            version=row.version,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
            content=row.content if with_content else None,
        )


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class DocumentsService:
    """CRUD документов сессии + сборка строки для системного промта."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def assert_session(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
        """Сессия существует и принадлежит вызывающему, иначе 404.

        Проверка нужна ИМЕННО на создании: у документов её обеспечивает пара (user_id, session_id)
        в самом запросе, а новый документ такой пары ещё не имеет — без этой проверки его можно
        было бы положить в чужую сессию.
        """
        row = await self._session.scalar(
            select(ChatSession.id).where(
                ChatSession.id == session_id, ChatSession.user_id == user_id
            )
        )
        if row is None:
            raise SessionNotFoundError("session not found")

    # ---- запись ----

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        filename: str,
        media_type: str,
        content: str,
        created_by: str = CREATED_BY_USER,
    ) -> DocumentView:
        self._check_media_type(media_type)
        size = len(content.encode("utf-8"))
        self._check_single_size(size)
        await self._check_session_budget(
            user_id=user_id, session_id=session_id, incoming=size, replacing=None
        )
        row = ChatDocument(
            user_id=user_id,
            session_id=session_id,
            filename=_normalize_filename(filename, media_type),
            media_type=media_type,
            content=content,
            size_bytes=size,
            version=1,
            created_by=created_by,
        )
        self._session.add(row)
        await self._session.flush()
        return DocumentView.of(row)

    async def update(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, document_id: uuid.UUID, content: str
    ) -> DocumentView:
        """Полная замена содержимого (ADR-090 §3).

        Патч-семантики нет намеренно: она требует от модели точного знания текущего текста и молча
        портит документ при расхождении. Полная замена делает контракт проверяемым — модель обязана
        сначала ``read``, затем ``update``.
        """
        row = await self._row(user_id=user_id, session_id=session_id, document_id=document_id)
        size = len(content.encode("utf-8"))
        self._check_single_size(size)
        await self._check_session_budget(
            user_id=user_id, session_id=session_id, incoming=size, replacing=row.size_bytes
        )
        row.content = content
        row.size_bytes = size
        row.version += 1
        row.updated_at = _now()
        await self._session.flush()
        return DocumentView.of(row)

    async def delete(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        row = await self._row(user_id=user_id, session_id=session_id, document_id=document_id)
        await self._session.delete(row)
        await self._session.flush()

    # ---- чтение ----

    async def list(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> list[DocumentView]:
        stmt = (
            select(ChatDocument)
            .where(ChatDocument.user_id == user_id, ChatDocument.session_id == session_id)
            .order_by(ChatDocument.created_at.asc())
        )
        rows = list((await self._session.scalars(stmt)).all())
        return [DocumentView.of(r) for r in rows]

    async def get(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, document_id: uuid.UUID
    ) -> DocumentView:
        row = await self._row(user_id=user_id, session_id=session_id, document_id=document_id)
        return DocumentView.of(row, with_content=True)

    async def context_line(self, *, user_id: uuid.UUID, session_id: uuid.UUID) -> str | None:
        """Строка для системного промта: какие документы есть в сессии (ADR-090 §6).

        Содержимое НЕ включается — оно большое и меняется; для чтения есть ``document.read``.
        Без этой строки модель не знает, что документы существуют, и не вызовет ``document.list``.
        """
        docs = await self.list(user_id=user_id, session_id=session_id)
        if not docs:
            return None
        listed = "; ".join(f"{d.filename} (id={d.id})" for d in docs)
        line = (
            "This chat has stored documents you can read and update with the document.* tools: "
            f"{listed}."
        )
        return line[: self._settings.document_context_max_chars]

    # ---- внутреннее ----

    async def _row(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, document_id: uuid.UUID
    ) -> ChatDocument:
        stmt = select(ChatDocument).where(
            ChatDocument.id == document_id,
            ChatDocument.user_id == user_id,
            ChatDocument.session_id == session_id,
        )
        row = await self._session.scalar(stmt)
        if row is None:
            raise DocumentNotFoundError("document not found")
        return row

    def _check_media_type(self, media_type: str) -> None:
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise UnsupportedMediaTypeError(f"unsupported_media_type: {media_type}")

    def _check_single_size(self, size: int) -> None:
        if size > self._settings.document_max_bytes:
            raise DocumentTooLargeError("document exceeds the maximum size")

    async def _check_session_budget(
        self, *, user_id: uuid.UUID, session_id: uuid.UUID, incoming: int, replacing: int | None
    ) -> None:
        """Потолки заданы НА СЕССИЮ, потому что скоуп документа — сессия (ADR-090 §2, §7).

        ``replacing`` — размер документа, который перезаписывается: при update он выбывает из
        суммы, иначе правка на один байт упиралась бы в потолок, который сама же и занимает.
        """
        stmt = select(
            func.count(ChatDocument.id), func.coalesce(func.sum(ChatDocument.size_bytes), 0)
        ).where(ChatDocument.user_id == user_id, ChatDocument.session_id == session_id)
        count, total = (await self._session.execute(stmt)).one()
        if replacing is None and count >= self._settings.document_max_count:
            raise TooManyDocumentsError("too many documents in this chat")
        occupied = int(total) - (replacing or 0)
        if occupied + incoming > self._settings.document_total_bytes:
            raise DocumentsTotalTooLargeError("documents exceed the total size limit for this chat")


def _normalize_filename(filename: str, media_type: str) -> str:
    """Имя без путей и с расширением, соответствующим типу.

    Разделители пути вырезаются: имя уходит в ``Content-Disposition``, и `../` там — это обход
    каталога на стороне клиента, сохраняющего файл.
    """
    name = (filename or "document").replace("\\", "/").split("/")[-1].strip() or "document"
    expected = _EXTENSION_BY_TYPE.get(media_type)
    if expected and not name.lower().endswith(expected):
        # Сначала снимаем ЧУЖОЕ текстовое расширение, иначе модель, назвавшая файл «список.txt»
        # при mediaType=text/markdown, получала «список.txt.md» — так и вышло на проде.
        stem, dot, ext = name.rpartition(".")
        if dot and f".{ext.lower()}" in _EXTENSION_BY_TYPE.values():
            name = stem or name
        name = f"{name}{expected}"
    return name[:200]
