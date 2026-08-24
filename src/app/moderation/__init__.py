"""Модерация UGC (ADR-086): пре-модерация входа и пост-модерация результата.

Публичная поверхность модуля — сервис ``ModerationService`` и типы вердикта. Вызывающие стороны
(chat-orchestrator, media-generation) получают его через ``app.deps``.
"""

from app.moderation.service import (
    STATUS_BLOCKED,
    STATUS_FLAGGED,
    STATUS_PASSED,
    STATUS_UNCHECKED,
    ModerationService,
    ModerationVerdict,
    unchecked_verdict,
)

__all__ = [
    "STATUS_BLOCKED",
    "STATUS_FLAGGED",
    "STATUS_PASSED",
    "STATUS_UNCHECKED",
    "ModerationService",
    "ModerationVerdict",
    "unchecked_verdict",
]
