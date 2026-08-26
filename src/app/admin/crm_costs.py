"""Периодные расходы на провайдеров: день × провайдер (расширение контракта CRM v1.3).

Отвечает на вопрос страницы CRM «Расход API»: сколько бизнес заплатил провайдерам за каждый
день выбранного периода и в чей счёт. Считает по тем же закупочным прайсам и тому же правилу
«модель → вендор», что колонка «Себестоимость» и блок «Доход и провайдеры» карточки (ADR-079),
— иначе две страницы одного продукта показали бы под одной подписью два разных числа.

**Но не «ровно те же деньги»: дневная сумма здесь ≥ суммы колонки «Себестоимость» за тот же
день.** Прайс, правило «модель → вендор» и правило `None ≠ 0` — общие; расходится ГРАНУЛЯРНОСТЬ
отбрасывания неоценимого: колонка обнуляет весь ХОД, если неоценим хоть один его вызов
(ADR-079 §5), здесь же отбрасывается только сам ВЫЗОВ (ADR-092 §2). Утверждать равенство
запрещено — оператору расхождение придётся объяснять, но объяснимо оно только так.

**Гранулярность — вызов провайдера, а не ход пользователя.** Все три величины клетки описывают
ОДНО: что провайдер нам выставил. `requests` — число оплаченных обращений (шаг чата с `usage`
= один вызов LLM; строка `media_jobs` = одна оплаченная генерация), `tokens` — сколько токенов
он посчитал, `spend_usd` — во сколько это обошлось. Ход (`message_step_id`) — единица истории
пользователя, и она отвечает на другой вопрос («за что списали кредиты»), поэтому здесь не
применяется.

**Прайс в SQL не переезжает.** SQL отдаёт СЫРЫЕ суммы счётчиков по (день, модель), а цену к ним
применяет `app.pricing.provider_prices` — единственный дом цен и правила «модель → вендор».
Переписать таблицу цен и правило `claude*` в SQL-выражение означало бы завести вторую копию,
которая молча разойдётся с первой. Стоимость выбора: агрегация в БД (десятки строк на выходе
вместо сотен тысяч), а не выгрузка `usage` каждого шага в Python.

**`null` ≠ `0`.** Клетка без единого оценённого вызова отдаёт `spend_usd = null`, а не `0.0`:
ноль утверждал бы измеренную бесплатность. Клетки за день без трафика не существует вовсе —
её отсутствие и означает измеренный ноль (контракт v1.3, три случая отсутствия).

**Занижение и завышение — оба честные, и оба унаследованы от ADR-079.** Вызов, который не по
чему оценить (модель без цены, шаг без счётчиков), в сумму дня не входит: подставить ноль за
неизвестную цену — записать факт, которого нет. Наоборот, media-строки, снятые до появления
`media_jobs.provider_cost_usd`, восстанавливаются из тарифной пачки и у посекундных моделей
дают оценку СВЕРХУ. Пометить это в ответе нечем — поля `estimated` в v1.3 не существует, —
поэтому оговорка живёт здесь: расход по `Fal` на исторической глубине читается как верхняя
граница, а не как замер.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.pricing.provider_prices import (
    PROVIDER_FAL,
    PROVIDER_UNKNOWN,
    ChatUsageTotals,
    chat_billed_tokens,
    chat_cost_usd_of_totals,
    provider_of_chat_model,
    round_usd,
)
from app.schemas.crm_admin import CrmDailyCostItem

# Себестоимость исторической media-строки по её кредитам: (model_id, credits, asset_count) → USD.
# Передаётся снаружи, чтобы восстановление шло тем же путём, что колонка «Себестоимость»
# (`CrmAdminService._provider_cost`), а не второй копией правил ADR-079 §2.
RecoveredMediaUsd = Callable[[str, int, int | None], float | None]

# Строка агрегата: `RowMapping` из БД либо обычный `Mapping` — та же пара, что у
# `CrmAdminService._provider_cost`, чтобы агрегатор оставался проверяемым без сессии.
_Row = Mapping[str, Any] | RowMapping

# Суммы счётчиков по (день, модель). `measured` повторяет `_has_token_counts` прайс-модуля:
# «хотя бы один счётчик присутствует И является числом». Шаг без счётчиков нечем умножать на
# цену, поэтому в денежные и токенные суммы он не входит — но обращением к провайдеру он был,
# и в `calls` попадает. `->>` под `jsonb_typeof(...) = 'number'` безопасен: нечисловое значение
# до приведения к numeric не доходит.
_CHAT_DAILY_SQL = """
    WITH calls AS (
        SELECT
            (s.created_at AT TIME ZONE 'UTC')::date AS day,
            s.usage ->> 'model' AS model,
            (
                jsonb_typeof(s.usage -> 'inputTokens') = 'number'
                OR jsonb_typeof(s.usage -> 'outputTokens') = 'number'
                OR jsonb_typeof(s.usage -> 'cacheReadTokens') = 'number'
                OR jsonb_typeof(s.usage -> 'cacheWriteTokens') = 'number'
            ) AS measured,
            GREATEST(0, trunc(CASE
                WHEN jsonb_typeof(s.usage -> 'inputTokens') = 'number'
                THEN (s.usage ->> 'inputTokens')::numeric ELSE 0
            END))::bigint AS input_tokens,
            GREATEST(0, trunc(CASE
                WHEN jsonb_typeof(s.usage -> 'outputTokens') = 'number'
                THEN (s.usage ->> 'outputTokens')::numeric ELSE 0
            END))::bigint AS output_tokens,
            GREATEST(0, trunc(CASE
                WHEN jsonb_typeof(s.usage -> 'cacheReadTokens') = 'number'
                THEN (s.usage ->> 'cacheReadTokens')::numeric ELSE 0
            END))::bigint AS cache_read_tokens,
            GREATEST(0, trunc(CASE
                WHEN jsonb_typeof(s.usage -> 'cacheWriteTokens') = 'number'
                THEN (s.usage ->> 'cacheWriteTokens')::numeric ELSE 0
            END))::bigint AS cache_write_tokens,
            GREATEST(0, trunc(CASE
                WHEN jsonb_typeof(s.usage -> 'webSearchRequests') = 'number'
                THEN (s.usage ->> 'webSearchRequests')::numeric ELSE 0
            END))::bigint AS web_search_requests
          FROM chat_steps s
         WHERE s.created_at >= :ts_from
           AND s.created_at < :ts_to
           AND s.role = 'assistant'
           AND s.usage IS NOT NULL
    )
    SELECT day,
           model,
           count(*)::bigint AS calls,
           count(*) FILTER (WHERE measured)::bigint AS measured_calls,
           COALESCE(sum(input_tokens) FILTER (WHERE measured), 0)::bigint
             AS input_tokens,
           -- Вычитание кэша у OpenAI пофакторное и обрезано нулём снизу: разность ИТОГОВ
           -- разошлась бы с ответом по вызовам на любом вызове, где кэша больше входа.
           COALESCE(sum(GREATEST(0, input_tokens - cache_read_tokens))
                    FILTER (WHERE measured), 0)::bigint
             AS input_excl_cache_read_tokens,
           COALESCE(sum(output_tokens) FILTER (WHERE measured), 0)::bigint
             AS output_tokens,
           COALESCE(sum(cache_read_tokens) FILTER (WHERE measured), 0)::bigint
             AS cache_read_tokens,
           COALESCE(sum(cache_write_tokens) FILTER (WHERE measured), 0)::bigint
             AS cache_write_tokens,
           COALESCE(sum(web_search_requests) FILTER (WHERE measured), 0)::bigint
             AS web_search_requests
      FROM calls
     GROUP BY day, model
"""

# Медиа за день, сгруппированные до ступени тарифа. Ключ (модель, кредиты, число ассетов) —
# ровно те три значения, из которых ADR-079 §2 восстанавливает цену старой строки, поэтому
# группировка не теряет ничего, а строк на выходе — единицы на день. Отдельно считается,
# сколько строк уже несут точную цену сабмита: их сумма берётся как есть.
#
# Возвращённые пользователю кредиты цену НЕ отменяют: fal нам денег не вернул (ADR-079 §5),
# поэтому `credits_refunded` в отборе не участвует.
_MEDIA_DAILY_SQL = """
    SELECT (j.created_at AT TIME ZONE 'UTC')::date AS day,
           j.model_id AS model_id,
           j.credits_charged AS credits,
           CASE
             WHEN jsonb_typeof(j.result -> 'assets') = 'array'
             THEN jsonb_array_length(j.result -> 'assets')
           END AS asset_count,
           count(*)::bigint AS jobs,
           count(*) FILTER (WHERE j.provider_cost_usd IS NOT NULL)::bigint AS stored_jobs,
           COALESCE(sum(j.provider_cost_usd), 0)::float AS stored_usd
      FROM media_jobs j
     WHERE j.created_at >= :ts_from
       AND j.created_at < :ts_to
     GROUP BY 1, 2, 3, 4
"""


@dataclass
class _Slot:
    """Накопитель одной клетки (день × провайдер).

    `spend_usd` / `tokens` стартуют с `None` — «ещё ничего не измерено», и остаются `None`, если
    измерить не удалось ничего. `requests` стартует с нуля: число обращений известно всегда,
    считать его нечем не может быть.
    """

    requests: int = 0
    spend_usd: float | None = None
    tokens: float | None = None

    def add_usd(self, value: float) -> None:
        self.spend_usd = (self.spend_usd or 0.0) + value

    def add_tokens(self, value: float) -> None:
        self.tokens = (self.tokens or 0.0) + value


def _chat_totals(row: _Row) -> ChatUsageTotals:
    return ChatUsageTotals(
        input_tokens=int(row["input_tokens"]),
        input_excl_cache_read_tokens=int(row["input_excl_cache_read_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cache_read_tokens=int(row["cache_read_tokens"]),
        cache_write_tokens=int(row["cache_write_tokens"]),
        web_search_requests=int(row["web_search_requests"]),
    )


def _collect_chat(
    slots: dict[tuple[datetime.date, str], _Slot],
    rows: Sequence[_Row],
) -> None:
    for row in rows:
        model = row["model"]
        if not isinstance(model, str) or not model.strip():
            # Оплаченный вызов LLM, которому нечего приписать: счётчики есть, имени вендора нет.
            # Существующему вендору он не приписывается — придуманный ключ был бы вымышленным
            # счётом и испортил бы уже верную клетку. Но и исчезнуть он не имеет права: «нет
            # строки» по контракту означает ИЗМЕРЕННЫЙ НОЛЬ, и день, весь трафик которого
            # неатрибутируем, прочитался бы как `$0.00` там, где расход был. Поэтому — отдельная
            # клетка `Unknown`: `requests = N` при `spend_usd = null` говорит «трафик был,
            # оценить нечем», потребитель сводит незнакомый ключ в `other` и по `spend_usd IS
            # NULL` поднимает пометку неполноты вместо молча заниженной суммы (ADR-092 §6).
            # `spend_usd` / `tokens` намеренно не трогаем: они остаются `null` = «не измерено».
            # Те же вызовы слышны оператору как `chat_unpriced_steps_total{reason="no_model"}`.
            slots.setdefault((row["day"], PROVIDER_UNKNOWN), _Slot()).requests += int(row["calls"])
            continue
        slot = slots.setdefault((row["day"], provider_of_chat_model(model)), _Slot())
        slot.requests += int(row["calls"])
        if int(row["measured_calls"]) == 0:
            continue
        totals = _chat_totals(row)
        cost = chat_cost_usd_of_totals(model, totals)
        if cost is not None:
            slot.add_usd(cost)
        tokens = chat_billed_tokens(model, totals)
        if tokens is not None:
            slot.add_tokens(float(tokens))


def _collect_media(
    slots: dict[tuple[datetime.date, str], _Slot],
    rows: Sequence[_Row],
    recovered_media_usd: RecoveredMediaUsd,
) -> None:
    for row in rows:
        slot = slots.setdefault((row["day"], PROVIDER_FAL), _Slot())
        jobs = int(row["jobs"])
        stored_jobs = int(row["stored_jobs"])
        slot.requests += jobs
        # Ноль здесь — ИЗМЕРЕНИЕ, а не пробел: fal берёт деньги за кадры и секунды, токенов не
        # считает вовсе. `null` объявил бы величину неизмеряемой и навсегда пометил бы сводку
        # CRM неполной у любого бэка, где есть генерации.
        slot.add_tokens(0.0)
        if stored_jobs > 0:
            slot.add_usd(float(row["stored_usd"]))
        historic_jobs = jobs - stored_jobs
        if historic_jobs <= 0:
            continue
        asset_count = None if row["asset_count"] is None else int(row["asset_count"])
        recovered = recovered_media_usd(str(row["model_id"]), int(row["credits"]), asset_count)
        if recovered is not None:
            slot.add_usd(recovered * historic_jobs)


async def daily_cost_items(
    session: AsyncSession,
    *,
    date_from: datetime.date,
    date_to: datetime.date,
    recovered_media_usd: RecoveredMediaUsd,
) -> list[CrmDailyCostItem]:
    """Клетки (день × провайдер) за период, обе границы включительно, календарь — UTC.

    Порядок — `date ASC, provider ASC`; пара (день, провайдер) уникальна, поэтому порядок полный
    и постраничная нарезка вызывающим стабильна без дополнительного tie-break.
    """
    params = {
        # Полуинтервал `[ts_from, ts_to)` вместо `<= конец дня`: он не зависит от точности
        # `timestamptz` и ложится на btree-индекс по `created_at` как диапазонный поиск.
        "ts_from": datetime.datetime.combine(date_from, datetime.time.min, tzinfo=datetime.UTC),
        "ts_to": datetime.datetime.combine(
            date_to + datetime.timedelta(days=1), datetime.time.min, tzinfo=datetime.UTC
        ),
    }
    chat_rows = (await session.execute(text(_CHAT_DAILY_SQL), params)).mappings().all()
    media_rows = (await session.execute(text(_MEDIA_DAILY_SQL), params)).mappings().all()

    slots: dict[tuple[datetime.date, str], _Slot] = {}
    _collect_chat(slots, chat_rows)
    _collect_media(slots, media_rows, recovered_media_usd)

    return [
        CrmDailyCostItem(
            date=day.isoformat(),
            provider=provider,
            spend_usd=round_usd(slot.spend_usd),
            requests=slot.requests,
            tokens=slot.tokens,
        )
        for (day, provider), slot in sorted(slots.items())
    ]
