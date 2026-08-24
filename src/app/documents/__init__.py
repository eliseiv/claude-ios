"""Документы чата (ADR-090): создание, чтение, обновление моделью и отдача клиенту."""

from app.documents.service import DocumentsService, DocumentView

__all__ = ["DocumentsService", "DocumentView"]
