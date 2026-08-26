"""chat_steps.created_at index — периодная разбивка расходов (контракт CRM v1.3)

``GET /v1/admin/costs/daily`` агрегирует шаги чата по календарным дням, то есть отбирает их
единственным предикатом ``created_at >= :from AND created_at < :to``. Обоих действующих
индексов таблицы для этого недостаточно: ``ix_steps_session_seq`` ведёт по ``(session_id, seq)``,
``ix_steps_message_step`` — по ходу, и ни один не начинается с ``created_at``. Без этого индекса
запрос за 30 дней читает ВСЮ историю чатов инстанса, а она растёт быстрее любой другой таблицы
продукта — по строке на каждый вызов LLM.

Индекс обычный, а не ``CONCURRENTLY``: миграции здесь выполняются перед стартом приложения
(entrypoint), сборка btree по одной колонке занимает секунды, и держать ради этого отдельный
autocommit-блок дороже, чем короткая пауза записи в момент выката. Тот же приём, что у
``0015_devid_lower_idx``.

Данные не трогаются: ``CREATE INDEX`` — expand-only, отката к потере не ведёт.

NOTE: ``revision`` обязан быть <= 32 символов (``alembic_version.version_num`` — VARCHAR(32)).

Revision ID: 0029_steps_created_idx
Revises: 0028_memory_enabled_default_true
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0029_steps_created_idx"
down_revision: str | None = "0028_memory_enabled_default_true"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_steps_created_at ON chat_steps (created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_steps_created_at")
