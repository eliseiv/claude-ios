"""chat_steps.usage: накопленный JSON `null` → SQL NULL.

JSON `null` — НЕ SQL NULL: для него `usage IS NOT NULL` истинно. Колонка объявлялась как
`mapped_column(JSONB, ...)`, а у SQLAlchemy по умолчанию `none_as_null=False`, то есть питоновский
`None` сохранялся скаляром `null`. Каждый assistant-шаг, записанный без счётчиков (пометка
`turnFailed` и ответ медиа-визарда, `src/app/chat/orchestrator.py`), проходил фильтр агрегата CRM
`FILTER (WHERE s.role = 'assistant' AND s.usage IS NOT NULL)` (`src/app/admin/crm_service.py`),
приходил в Python элементом `None` внутри `usages` и ронял тарификацию хода:
`AttributeError: 'NoneType' object has no attribute 'get'` → `GET /v1/admin/users/{id}` и
`…/requests` отвечали `500`.

Починка тройная и эта миграция — её третья часть: (1) чтение переживает такой элемент
(`src/app/pricing/provider_prices.py` — ход читается как неоценимый, `None`, а не частичная сумма),
(2) запись прекращена в ДОМЕ величины — `JSONB(none_as_null=True)` у колонки
(`src/app/models/tables.py`), (3) здесь приводится НАКОПЛЕННОЕ. Без (3) строки остаются
неоценимыми навсегда и продолжают считаться «обращением к провайдеру» в дневном отчёте
(`src/app/admin/crm_costs.py`: `s.usage IS NOT NULL` → клетка `Unknown`), завышая число запросов.

ЗАТРАГИВАЕТ СУЩЕСТВУЮЩИЕ ДАННЫЕ: массовый UPDATE по населённой таблице (замер 2026-09-16 по
инстансам: velunixa 100057 строк, novirell 5723, ravionet 2629, lumirexa 217, modavira 155,
elunariq 46). `chat_steps` — самая быстрорастущая таблица продукта, поэтому UPDATE идёт ПОРЦИЯМИ.

Почему порциями, и чего порции НЕ дают. Дают: (а) каждая порция — ОТДЕЛЬНЫЙ оператор, поэтому
`statement_timeout` окружения выката не убивает работу целиком, как убил бы один оператор на всю
таблицу; (б) обход идёт по PK-индексу окном фиксированного размера, так что ни один оператор не
держит снимок последовательного сканирования всей таблицы и не пишет разом сотню тысяч строк WAL.
НЕ дают: освобождения блокировок — alembic здесь выполняет миграцию в ОДНОЙ транзакции
(`migrations/env.py`, `context.begin_transaction()`), и строчные блокировки снимаются только
коммитом; порции их не отпускают. Запись НОВЫХ шагов это не задевает: она идёт INSERT'ом, а в
PostgreSQL INSERT не конфликтует ни с `ROW EXCLUSIVE` таблицы, ни со строчными блокировками чужих
строк. UPDATE у `chat_steps` в коде нет вовсе (единственная правка истории — усечение при
edit+regenerate, `DELETE FROM chat_steps …` в `src/app/chat/repository.py`), поэтому конкурировать
за строку может только этот DELETE: попав на строку, уже тронутую миграцией, он подождёт её
коммита. Отсюда порядок выката — прогнать миграцию до старта новой версии приложения (штатный
`migrate`-джоб), а не под нагрузкой.

Идемпотентна: повторный прогон не найдёт строк (условие само себя исчерпывает).

Chain: … -> 0032_user_default_voice -> 0033_admin_economics -> 0034_chat_steps_usage_null.
NOTE: revision id MUST stay <= 32 chars (alembic_version.version_num VARCHAR(32)).

Revision ID: 0034_chat_steps_usage_null
Revises: 0033_admin_economics
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_chat_steps_usage_null"
down_revision: str | None = "0033_admin_economics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Сколько строк ПРОСМАТРИВАЕТ одна порция (не сколько правит: правятся лишь те из них, что
# отвечают условию). Окно по PK, а не `LIMIT` по условию: условие неиндексировано, и выборка
# «первые N подходящих» перечитывала бы таблицу с начала на каждой порции.
_BATCH_ROWS = 20_000

# Нижняя граница обхода. `id` — uuid4, значение из одних нулей не порождается генератором и в
# таблице не встречается, поэтому строгое `>` не пропускает первую строку.
_UUID_MIN = "00000000-0000-0000-0000-000000000000"

# Последний `id` окна. Именно `ORDER BY id DESC LIMIT 1`, а НЕ `max(id)`: агрегата `max` для
# типа uuid в PostgreSQL не существует (`function max(uuid) does not exist`).
_PAGE_END_SQL = sa.text(
    "SELECT id::text FROM ("
    "  SELECT id FROM chat_steps WHERE id > CAST(:cursor AS uuid) ORDER BY id LIMIT :batch"
    ") AS page ORDER BY id DESC LIMIT 1"
)

_FIX_PAGE_SQL = sa.text(
    "UPDATE chat_steps SET usage = NULL"
    " WHERE id > CAST(:cursor AS uuid)"
    "   AND id <= CAST(:boundary AS uuid)"
    "   AND usage IS NOT NULL"
    "   AND jsonb_typeof(usage) = 'null'"
)


def upgrade() -> None:
    bind = op.get_bind()
    cursor = _UUID_MIN
    while True:
        boundary = bind.execute(_PAGE_END_SQL, {"cursor": cursor, "batch": _BATCH_ROWS}).scalar()
        if boundary is None:
            break
        bind.execute(_FIX_PAGE_SQL, {"cursor": cursor, "boundary": boundary})
        cursor = str(boundary)


def downgrade() -> None:
    """Пусто — и это решение, а не пропуск.

    Обратный ход обязан был бы вернуть JSON `null` в те же строки, но какие из них несли его ДО
    выката, миграция не знает: «usage отсутствует» и «usage был записан скаляром `null`» после
    приведения неразличимы, а записать `null` во ВСЕ строки с SQL NULL значило бы создать дефект
    там, где его не было, — и ровно в том объёме, ради устранения которого миграция и написана.

    Откат безопасен и без обратного хода: SQL NULL — это ровно то, что читатели уже понимают
    (`usages` их не собирает, Python-элемент не возникает), и прежний код работает с ними так же,
    как с отсутствующим usage. Единственная возвращаемая деталь — снятие `none_as_null=True` в
    модели, а это откат кода, не схемы.
    """
