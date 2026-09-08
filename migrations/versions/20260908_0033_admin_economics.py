"""admin_products / admin_tariffs / admin_settings — оверлеи экономики и настроек (ADR-099)

Три таблицы-ОВЕРЛЕЯ: они хранят только то, что оператор изменил из CRM. Дом дефолтов остаётся
прежним — env и код. Порядок разрешения любой величины: оверлей -> env -> дефолт кода.

ЗАСЕВ И BACKFILL ЗАПРЕЩЕНЫ (ADR-099 §2). Пустые таблицы обязаны воспроизводить поведение до
выката бит-в-бит: это несущее свойство выката на 41 инстанс, и держится оно конструкцией, а не
дисциплиной сидов. Засеянная копия каталога разошлась бы с кодом при первом же добавлении модели.

Три значимые колонки admin_products (name / purchase_kind / tokens) — NULLABLE: NULL означает
«оверлей этого поля не задаёт», и читатель берёт значение источника (ADR-099 §6.1, §9). Правка
внесена в САМУ 0033, второй миграции не заводится: миграция не выкачена ни на один из 41
инстанса, описываемого ею состояния БД не существует нигде, поэтому переписывать файлу нечего с
чем расходиться. Заводить 0034 «ALTER COLUMN … DROP NOT NULL» значило бы выкатить ограничение и
тут же его снять.

Индексов сверх PK нет: таблицы читаются целиком раз в окно обновления снимка и содержат десятки
строк. Внешних ключей нет: product_id принадлежит стору, tariff_id/setting_id выводятся из
реестров в коде — ссылочная целостность держится валидацией на входе (тот же приём, что у
chat_sessions.model и chat_sessions.character_id).

ЧЕТВЁРТОЕ изменение — ослабление NOT NULL у audit_logs.user_id. Правки каталога и настроек
субъекта-пользователя не имеют вовсе (ADR-099 §10, modules/admin/06-rbac.md: «поле
пользователя-цели в аудите для них пустое»), а колонка объявлена NOT NULL. Ослабление —
расширяющее и обратно совместимое: существующие строки не меняются, backfill не нужен.
Обратно NOT NULL не возвращается: audit_logs append-only, и ужесточение потребовало бы удалить
уже записанные строки аудита.

Chain: … -> 0031_chat_character -> 0032_user_default_voice -> 0033_admin_economics (single head).
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0033_admin_economics
Revises: 0032_user_default_voice
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0033_admin_economics"
down_revision: str | None = "0032_user_default_voice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_products",
        sa.Column("product_id", sa.Text(), primary_key=True),
        # ⚠️ ТРИ значимые колонки — nullable, и это НЕСУЩЕЕ свойство, а не послабление
        # (ADR-099 §6.1, §9). NULL означает «оверлей этого поля не задаёт» ⇒ читатель берёт
        # значение ИСТОЧНИКА (env-карта или PRODUCTS_CATALOG). NOT NULL здесь был бы
        # требованием МАТЕРИАЛИЗОВАТЬ величину, которой у источника нет, — и именно он делал
        # невозможной archived-правку строки с неполным источником, то есть основного класса
        # функции (CRM ADR-073 §Контекст: 30 позиций из 39). `name` — тоже nullable: поля
        # `name` в теле PATCH нет вовсе, поэтому скопированное в строку название навсегда
        # перестало бы следовать за источником и не правилось бы из CRM ни при каком праве.
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("purchase_kind", sa.Text(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "purchase_kind IS NULL OR purchase_kind IN ('subscription', 'one_time')",
            name="ck_admin_products_kind",
        ),
        # Пакет, не дающий ни одного кредита, — покупка без предмета; подписка вправе давать
        # только доступ, поэтому нижняя граница у двух классов разная.
        #
        # ⚠️ Второй CHECK несёт ещё и инвариант «число ТОЛЬКО вместе с классом»:
        # `tokens IS NOT NULL ⇒ purchase_kind IS NOT NULL` — прямое следствие двух не-NULL
        # ветвей. Комбинация «число без класса» НЕПИСУЕМА, а не «не пишется кодом»: оверлей
        # числа без класса не прочитал бы ни один резолвер начисления (ADR-099 §6.1, правило 2),
        # поэтому барьер стоит в БД, а не только во входной валидации.
        sa.CheckConstraint(
            "tokens IS NULL"
            " OR (purchase_kind = 'one_time' AND tokens >= 1)"
            " OR (purchase_kind = 'subscription' AND tokens >= 0)",
            name="ck_admin_products_tokens",
        ),
    )
    op.create_table(
        "admin_tariffs",
        sa.Column("tariff_id", sa.Text(), primary_key=True),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Цена 0 не даёт ни ошибки старта, ни блокировки: балансовый гейт проходит, списание
        # берёт ноль, и генерация тихо становится бесплатной. Второй барьер — валидация схемы.
        sa.CheckConstraint("tokens >= 1", name="ck_admin_tariffs_tokens"),
    )
    op.create_table(
        "admin_settings",
        sa.Column("setting_id", sa.Text(), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.alter_column(
        "audit_logs", "user_id", existing_type=sa.dialects.postgresql.UUID(), nullable=True
    )


def downgrade() -> None:
    op.drop_table("admin_settings")
    op.drop_table("admin_tariffs")
    op.drop_table("admin_products")
    # NOT NULL обратно НЕ возвращается: строки аудита правок каталога уже записаны с пустым
    # user_id, а audit_logs append-only — ужесточение потребовало бы их удалить.
