"""CRM admin read/write service (broad-crm «Пользователи бэков», v1).

Aggregates users, wallets, subscriptions and payment webhook events for the CRM panel. Optional
blocks (revenue, media_stats) are returned as null when not tracked.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.service import AdminService
from app.audit.service import EVENT_CRM_SUBSCRIPTION_GRANT, AuditEvent, AuditService
from app.config import Settings, get_settings
from app.errors import InsufficientCreditsError, UserNotFoundError
from app.schemas.crm_admin import (
    CrmPaymentItem,
    CrmPaymentListResponse,
    CrmProductItem,
    CrmProductListResponse,
    CrmRequestItem,
    CrmRequestListResponse,
    CrmStatsResponse,
    CrmSubscriptionGrantResponse,
    CrmTokensAdjustResponse,
    CrmUserBalance,
    CrmUserDetailResponse,
    CrmUserListItem,
    CrmUserListResponse,
    CrmUserSubscription,
)
from app.wallet.service import WalletService

_ADAPTY_PAYMENT_EVENTS = (
    "subscription_started",
    "subscription_renewed",
    "non_subscription_purchase",
    "access_level_updated",
)
_ADAPTY_RENEWAL_EVENTS = ("subscription_renewed",)

# Порог «медленного» запроса в секундах — тот же, что в 232 (`_SLOW_THRESHOLD_SEC`): CRM
# раскрашивает строку по `status`, и разъезд порогов между бэками дал бы операторам две
# несравнимые шкалы «медленно» на одной странице.
_SLOW_THRESHOLD_SEC = 30.0
_PROMPT_PREVIEW_MAX = 200

# Единица истории для чата — ХОД (`message_step_id`), а не шаг: в server-side tool-loop один ход
# пишет несколько строк `chat_steps` (`assistant` с `tool_use`, затем финальный `assistant`), а
# списание по нему РОВНО ОДНО — `orchestrator._debit` идемпотентен по `message_step_id`. Строка
# на шаг показала бы одно и то же списание два-три раза (проверено на проде: 18 assistant-шагов
# против 15 оплаченных ходов).
#
# `request_logs` подключается к ходу ЛЕВЫМ соединением ради двух величин, которых в доменных
# таблицах нет, — кода ответа и длительности; агрегат по `message_step_id` обязателен, потому что
# продолжение через `/tool-result` — отдельный HTTP-запрос с тем же `message_step_id`, и без
# группировки соединение размножило бы ход.
_REQUEST_HISTORY_SQL = """
    WITH turns AS (
        SELECT s.message_step_id AS message_step_id,
               min(s.created_at) AS sent_at,
               (array_remove(array_agg(s.usage->>'model' ORDER BY s.seq DESC), NULL))[1] AS model
          FROM chat_steps s
          JOIN chat_sessions cs ON cs.id = s.session_id
         WHERE cs.user_id = :uid AND s.role = 'assistant'
         GROUP BY s.message_step_id
    ), logged AS (
        SELECT message_step_id,
               max(status_code) AS status_code,
               sum(EXTRACT(EPOCH FROM (completed_at - started_at))) AS duration_sec
          FROM request_logs
         WHERE user_id = :uid AND message_step_id IS NOT NULL
         GROUP BY message_step_id
    )
    SELECT CASE WHEN t.model IS NULL THEN 'chat' ELSE 'chat:' || t.model END AS endpoint,
           NULL::text AS prompt_preview,
           COALESCE(l.status_code, 200) AS status_code,
           CASE WHEN l.duration_sec > :slow THEN 'slow' ELSE 'ok' END AS status,
           l.duration_sec::float AS duration_sec,
           t.sent_at AS sent_at,
           lt.amount::float AS tokens_spent,
           NULL::float AS provider_cost_usd,
           false AS refunded
      FROM turns t
      LEFT JOIN logged l ON l.message_step_id = t.message_step_id
      LEFT JOIN ledger_transactions lt
             ON lt.user_id = :uid
            AND lt.idempotency_key = t.message_step_id::text
            AND lt.type = 'debit'
    UNION ALL
    SELECT j.kind || ':' || j.model_id AS endpoint,
           left(j.prompt, :preview) AS prompt_preview,
           CASE j.status
             WHEN 'completed' THEN 200
             WHEN 'failed' THEN 500
             ELSE 202
           END AS status_code,
           CASE
             WHEN j.status = 'failed' THEN 'error'
             WHEN j.status <> 'completed' THEN 'slow'
             WHEN EXTRACT(EPOCH FROM (j.updated_at - j.created_at)) > :slow THEN 'slow'
             ELSE 'ok'
           END AS status,
           CASE
             WHEN j.status = 'completed'
             THEN EXTRACT(EPOCH FROM (j.updated_at - j.created_at))
           END::float AS duration_sec,
           j.created_at AS sent_at,
           j.credits_charged::float AS tokens_spent,
           NULL::float AS provider_cost_usd,
           j.credits_refunded AS refunded
      FROM media_jobs j
     WHERE j.user_id = :uid
    UNION ALL
    SELECT r.endpoint AS endpoint,
           r.prompt_preview AS prompt_preview,
           CASE
             WHEN r.status IN ('started', 'queued') THEN 202
             ELSE r.status_code
           END AS status_code,
           CASE
             WHEN r.status = 'failed' THEN 'error'
             WHEN r.status IN ('started', 'queued') THEN 'slow'
             WHEN EXTRACT(EPOCH FROM (r.completed_at - r.started_at)) > :slow THEN 'slow'
             ELSE 'ok'
           END AS status,
           CASE
             WHEN r.status = 'completed'
             THEN EXTRACT(EPOCH FROM (r.completed_at - r.started_at))
           END::float AS duration_sec,
           r.started_at AS sent_at,
           r.tokens_spent::float AS tokens_spent,
           r.provider_cost_usd AS provider_cost_usd,
           r.refunded AS refunded
      FROM request_logs r
     WHERE r.user_id = :uid
       AND r.message_step_id IS NULL
       AND r.media_job_id IS NULL
"""


def _iso_z(dt: datetime.datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _subscription_active(status: str | None, expires_at: datetime.datetime | None) -> bool:
    if status != "active":
        return False
    if expires_at is None:
        return True
    exp = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=datetime.UTC)
    return exp > datetime.datetime.now(tz=datetime.UTC)


class CrmAdminService:
    def __init__(
        self,
        session: AsyncSession,
        wallet: WalletService,
        audit: AuditService,
        admin: AdminService,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._wallet = wallet
        self._audit = audit
        self._admin = admin
        self._settings = settings or get_settings()

    async def _external_id(self, user_id: uuid.UUID) -> str | None:
        row = await self._session.scalar(
            text(
                "SELECT device_id FROM auth_devices WHERE user_id = :uid "
                "ORDER BY last_seen_at DESC NULLS LAST, device_id LIMIT 1"
            ),
            {"uid": str(user_id)},
        )
        return str(row) if row is not None else None

    async def _payment_counts(self, user_id: uuid.UUID) -> tuple[int, int]:
        cp = await self._session.scalar(
            text("SELECT COUNT(*)::int FROM cloudpayments_webhook_events WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        adapty_pay = await self._session.scalar(
            text(
                "SELECT COUNT(*)::int FROM adapty_webhook_events "
                "WHERE user_id = :uid AND event_type = ANY(:types)"
            ),
            {"uid": str(user_id), "types": list(_ADAPTY_PAYMENT_EVENTS)},
        )
        renewals = await self._session.scalar(
            text(
                "SELECT COUNT(*)::int FROM adapty_webhook_events "
                "WHERE user_id = :uid AND event_type = ANY(:types)"
            ),
            {"uid": str(user_id), "types": list(_ADAPTY_RENEWAL_EVENTS)},
        )
        cp_sub = await self._session.scalar(
            text(
                "SELECT COUNT(*)::int FROM cloudpayments_webhook_events "
                "WHERE user_id = :uid AND kind = 'subscription'"
            ),
            {"uid": str(user_id)},
        )
        cp_count = int(cp or 0)
        adapty_count = int(adapty_pay or 0)
        payments = cp_count + adapty_count
        renewal_total = int(renewals or 0) + max(0, int(cp_sub or 0) - 1)
        return payments, renewal_total

    async def list_users(
        self,
        *,
        limit: int,
        offset: int,
        search: str | None,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
        is_paid: bool | None,
    ) -> CrmUserListResponse:
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)
        clauses = ["1=1"]
        params: dict[str, Any] = {"lim": limit, "off": offset}
        if search:
            clauses.append(
                "(CAST(u.id AS text) ILIKE :search OR EXISTS ("
                "SELECT 1 FROM auth_devices ad WHERE ad.user_id = u.id "
                "AND ad.device_id ILIKE :search))"
            )
            params["search"] = f"%{search.strip()}%"
        if date_from is not None:
            clauses.append("u.created_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("u.created_at <= :date_to")
            params["date_to"] = date_to
        if is_paid is True:
            clauses.append(
                """(
                  EXISTS (
                    SELECT 1 FROM cloudpayments_webhook_events cp WHERE cp.user_id = u.id
                  ) OR EXISTS (
                    SELECT 1 FROM adapty_webhook_events aw
                    WHERE aw.user_id = u.id AND aw.event_type = ANY(:adapty_types)
                  )
                )"""
            )
            params["adapty_types"] = list(_ADAPTY_PAYMENT_EVENTS)
        elif is_paid is False:
            clauses.append(
                """NOT (
                  EXISTS (
                    SELECT 1 FROM cloudpayments_webhook_events cp WHERE cp.user_id = u.id
                  ) OR EXISTS (
                    SELECT 1 FROM adapty_webhook_events aw
                    WHERE aw.user_id = u.id AND aw.event_type = ANY(:adapty_types)
                  )
                )"""
            )
            params["adapty_types"] = list(_ADAPTY_PAYMENT_EVENTS)
        where_sql = " AND ".join(clauses)

        count_sql = f"SELECT COUNT(*)::int FROM users u WHERE {where_sql}"
        total = int(await self._session.scalar(text(count_sql), params) or 0)

        rows = (
            (
                await self._session.execute(
                    text(
                        f"""
                    SELECT
                      u.id,
                      u.created_at,
                      COALESCE(w.balance, 0) AS balance,
                      s.status,
                      s.expires_at,
                      s.plan,
                      (
                        SELECT COUNT(*)::int FROM cloudpayments_webhook_events cp
                        WHERE cp.user_id = u.id
                      ) + (
                        SELECT COUNT(*)::int FROM adapty_webhook_events aw
                        WHERE aw.user_id = u.id
                          AND aw.event_type = ANY(:adapty_types)
                      ) AS payments_count,
                      (
                        SELECT COUNT(*)::int FROM adapty_webhook_events aw
                        WHERE aw.user_id = u.id
                          AND aw.event_type = ANY(:renewal_types)
                      ) + GREATEST(0, (
                        SELECT COUNT(*)::int FROM cloudpayments_webhook_events cp
                        WHERE cp.user_id = u.id AND cp.kind = 'subscription'
                      ) - 1) AS renewals_count,
                      (
                        SELECT ad.device_id FROM auth_devices ad
                        WHERE ad.user_id = u.id
                        ORDER BY ad.last_seen_at DESC NULLS LAST, ad.device_id
                        LIMIT 1
                      ) AS external_id
                    FROM users u
                    LEFT JOIN wallets w ON w.user_id = u.id
                    LEFT JOIN subscriptions s ON s.user_id = u.id
                    WHERE {where_sql}
                    ORDER BY u.created_at DESC
                    LIMIT :lim OFFSET :off
                    """
                    ),
                    {
                        **params,
                        "adapty_types": list(_ADAPTY_PAYMENT_EVENTS),
                        "renewal_types": list(_ADAPTY_RENEWAL_EVENTS),
                    },
                )
            )
            .mappings()
            .all()
        )

        items: list[CrmUserListItem] = []
        for row in rows:
            payments_count = int(row["payments_count"] or 0)
            items.append(
                CrmUserListItem(
                    id=str(row["id"]),
                    external_id=row["external_id"],
                    is_paid=payments_count > 0,
                    payments_count=payments_count,
                    renewals_count=int(row["renewals_count"] or 0),
                    tokens=float(row["balance"] or 0),
                    subscription_active=_subscription_active(row["status"], row["expires_at"]),
                    subscription_expires_at=_iso_z(row["expires_at"]),
                    plan_id=row["plan"],
                    registered_at=_iso_z(row["created_at"]) or "",
                )
            )

        return CrmUserListResponse(total=total, items=items)

    async def get_user(self, user_id: uuid.UUID) -> CrmUserDetailResponse:
        row = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT u.id, u.created_at,
                           COALESCE(w.balance, 0) AS balance,
                           s.status, s.plan, s.expires_at
                    FROM users u
                    LEFT JOIN wallets w ON w.user_id = u.id
                    LEFT JOIN subscriptions s ON s.user_id = u.id
                    WHERE u.id = :uid
                    """
                    ),
                    {"uid": str(user_id)},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise UserNotFoundError("user not found")

        credited = await self._session.scalar(
            text(
                "SELECT COALESCE(SUM(amount), 0)::bigint FROM ledger_transactions "
                "WHERE user_id = :uid AND type = 'credit'"
            ),
            {"uid": str(user_id)},
        )
        spent = await self._session.scalar(
            text(
                "SELECT COALESCE(SUM(amount), 0)::bigint FROM ledger_transactions "
                "WHERE user_id = :uid AND type = 'debit'"
            ),
            {"uid": str(user_id)},
        )
        last_payment = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT processed_at, product_id, payload
                    FROM cloudpayments_webhook_events
                    WHERE user_id = :uid
                    ORDER BY processed_at DESC
                    LIMIT 1
                    """
                    ),
                    {"uid": str(user_id)},
                )
            )
            .mappings()
            .first()
        )

        external_id = await self._external_id(user_id)
        plan_id = row["plan"]
        return CrmUserDetailResponse(
            id=str(row["id"]),
            external_id=external_id,
            registered_at=_iso_z(row["created_at"]) or "",
            balance=CrmUserBalance(
                tokens=float(row["balance"] or 0),
                credited_total=float(credited or 0),
                spent_total=float(spent or 0),
            ),
            subscription=CrmUserSubscription(
                plan_id=plan_id,
                plan_name=plan_id,
                price=None,
                active=_subscription_active(row["status"], row["expires_at"]),
                expires_at=_iso_z(row["expires_at"]),
                last_payment_at=_iso_z(last_payment["processed_at"]) if last_payment else None,
                last_payment_method=None,
            ),
            revenue=None,
            media_stats=None,
        )

    async def list_payments(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> CrmPaymentListResponse:
        await self._admin._require_user_exists(user_id)
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)

        total_cp = int(
            await self._session.scalar(
                text("SELECT COUNT(*)::int FROM cloudpayments_webhook_events WHERE user_id = :uid"),
                {"uid": str(user_id)},
            )
            or 0
        )
        total_ad = int(
            await self._session.scalar(
                text("SELECT COUNT(*)::int FROM adapty_webhook_events WHERE user_id = :uid"),
                {"uid": str(user_id)},
            )
            or 0
        )
        total = total_cp + total_ad

        rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT title, description, amount, currency, status, occurred_at FROM (
                      SELECT
                        product_id AS title,
                        kind AS description,
                        COALESCE((payload->>'amount')::float, 0) AS amount,
                        COALESCE(payload->>'currency', 'RUB') AS currency,
                        'success' AS status,
                        processed_at AS occurred_at
                      FROM cloudpayments_webhook_events
                      WHERE user_id = :uid
                      UNION ALL
                      SELECT
                        event_type AS title,
                        NULL AS description,
                        0 AS amount,
                        'USD' AS currency,
                        'success' AS status,
                        processed_at AS occurred_at
                      FROM adapty_webhook_events
                      WHERE user_id = :uid
                    ) p
                    ORDER BY occurred_at DESC
                    LIMIT :lim OFFSET :off
                    """
                    ),
                    {"uid": str(user_id), "lim": limit, "off": offset},
                )
            )
            .mappings()
            .all()
        )

        items = [
            CrmPaymentItem(
                title=str(r["title"]),
                description=r["description"],
                amount=float(r["amount"] or 0),
                currency=str(r["currency"] or "USD"),
                status=r["status"],
                occurred_at=_iso_z(r["occurred_at"]) or "",
            )
            for r in rows
        ]
        return CrmPaymentListResponse(total=total, items=items)

    async def list_requests(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> CrmRequestListResponse:
        """История запросов, ВЫВЕДЕННАЯ из доменных таблиц (как в 232 — из `generations`).

        Читать один журнал (`request_logs`) оказалось нельзя: журнал заполняется только с
        момента своего появления, поэтому у всех существующих пользователей история
        обнулилась бы — а именно она и нужна оператору. Доменные таблицы хранят те же факты
        с самого начала работы сервиса, поэтому история здесь РЕТРОАКТИВНА:

        * `chat_steps` (`role='assistant'`) — один ответ модели = один запрос; модель берётся
          из `usage`, а списанные кредиты — из `ledger_transactions` по `idempotency_key`,
          равному `message_step_id` (`chat/orchestrator._debit`), то есть это ТОЧНАЯ сумма,
          а не оценка;
        * `media_jobs` — сумма и возврат лежат в самой строке (`credits_charged`,
          `credits_refunded`), длительность считается по `created_at`/`updated_at`;
        * `request_logs` — ТОЛЬКО те запросы, которых в доменных таблицах нет физически:
          упавшие и ещё выполняющиеся. Успешный запрос всегда проставляет
          `message_step_id`/`media_job_id`, поэтому фильтр по двум `IS NULL` исключает
          двойной показ БЕЗ дедупликации по ключам.

        `provider_cost_usd` остаётся `NULL` = «не измерено» (правило `NULL ≠ 0`): себестоимость
        провайдера в этом сервисе не сохраняется, и `0` был бы выдумкой под видом факта.
        """
        await self._admin._require_user_exists(user_id)
        limit = min(max(limit, 1), 100)
        offset = max(offset, 0)

        params = {
            "uid": str(user_id),
            "preview": _PROMPT_PREVIEW_MAX,
            "slow": _SLOW_THRESHOLD_SEC,
        }
        total = int(
            await self._session.scalar(
                text(f"WITH history AS ({_REQUEST_HISTORY_SQL}) SELECT count(*) FROM history"),
                params,
            )
            or 0
        )
        rows = (
            (
                await self._session.execute(
                    text(
                        f"WITH history AS ({_REQUEST_HISTORY_SQL}) "
                        "SELECT * FROM history ORDER BY sent_at DESC LIMIT :lim OFFSET :off"
                    ),
                    {**params, "lim": limit, "off": offset},
                )
            )
            .mappings()
            .all()
        )

        items = [
            CrmRequestItem(
                endpoint=str(r["endpoint"]),
                prompt_preview=r["prompt_preview"],
                status_code=int(r["status_code"]),
                status=str(r["status"]),
                duration_sec=None if r["duration_sec"] is None else float(r["duration_sec"]),
                sent_at=_iso_z(r["sent_at"]) or "",
                tokens_spent=None if r["tokens_spent"] is None else float(r["tokens_spent"]),
                provider_cost_usd=(
                    None if r["provider_cost_usd"] is None else float(r["provider_cost_usd"])
                ),
                refunded=bool(r["refunded"]),
            )
            for r in rows
        ]
        return CrmRequestListResponse(total=total, items=items)

    async def stats(
        self,
        *,
        date_from: datetime.datetime | None,
        date_to: datetime.datetime | None,
    ) -> CrmStatsResponse:
        clauses = ["1=1"]
        params: dict[str, Any] = {}
        if date_from is not None:
            clauses.append("u.created_at >= :date_from")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("u.created_at <= :date_to")
            params["date_to"] = date_to
        where_sql = " AND ".join(clauses)

        users_total = int(
            await self._session.scalar(
                text(f"SELECT COUNT(*)::int FROM users u WHERE {where_sql}"), params
            )
            or 0
        )
        paid_users = int(
            await self._session.scalar(
                text(
                    f"""
                    SELECT COUNT(*)::int FROM users u
                    WHERE {where_sql}
                      AND (
                        EXISTS (
                          SELECT 1 FROM cloudpayments_webhook_events cp
                          WHERE cp.user_id = u.id
                        )
                        OR EXISTS (
                          SELECT 1 FROM adapty_webhook_events aw
                          WHERE aw.user_id = u.id
                            AND aw.event_type = ANY(:adapty_types)
                        )
                      )
                    """
                ),
                {**params, "adapty_types": list(_ADAPTY_PAYMENT_EVENTS)},
            )
            or 0
        )
        return CrmStatsResponse(
            users_total=users_total,
            paid_users=paid_users,
            payments_sum_usd=0.0,
        )

    def list_products(self) -> CrmProductListResponse:
        items: list[CrmProductItem] = []
        seen: set[str] = set()
        for product_id, credits in self._settings.token_products().items():
            if product_id in seen:
                continue
            seen.add(product_id)
            items.append(
                CrmProductItem(
                    product_id=product_id,
                    name=f"{credits} tokens",
                    price=None,
                    period=None,
                )
            )
        for product_id in self._settings.cloudpayments_product_tokens():
            if product_id in seen:
                continue
            seen.add(product_id)
            items.append(
                CrmProductItem(
                    product_id=product_id,
                    name=product_id,
                    price=None,
                    period="subscription",
                )
            )
        for entry in self._settings.products_catalog():
            pid = entry.get("productId") or entry.get("product_id")
            if not isinstance(pid, str) or pid in seen:
                continue
            seen.add(pid)
            items.append(
                CrmProductItem(
                    product_id=pid,
                    name=str(entry.get("title") or entry.get("name") or pid),
                    price=str(entry.get("price")) if entry.get("price") is not None else None,
                    period=str(entry.get("period")) if entry.get("period") is not None else None,
                )
            )
        return CrmProductListResponse(items=items)

    async def adjust_tokens(self, user_id: uuid.UUID, amount: int) -> CrmTokensAdjustResponse:
        await self._admin._require_user_exists(user_id)
        if amount == 0:
            raise HTTPException(status_code=400, detail="amount must not be zero")
        if amount > 0:
            grant = await self._wallet.grant(
                user_id=user_id,
                amount=amount,
                idempotency_key=f"crm-tokens:{uuid.uuid4()}",
                meta={"source": "crm_admin"},
                reason="crm_admin_tokens",
            )
            balance = grant.new_balance
        else:
            try:
                debit = await self._wallet.consume(
                    user_id=user_id,
                    amount=-amount,
                    idempotency_key=f"crm-tokens:{uuid.uuid4()}",
                    meta={"source": "crm_admin"},
                )
                balance = debit.new_balance
            except InsufficientCreditsError as exc:
                raise HTTPException(status_code=400, detail="insufficient balance") from exc
        return CrmTokensAdjustResponse(id=str(user_id), tokens=float(balance))

    async def grant_subscription(
        self,
        user_id: uuid.UUID,
        *,
        product_id: str,
        expires_in_days: int,
        grant_id: str,
    ) -> CrmSubscriptionGrantResponse:
        await self._admin._require_user_exists(user_id)
        known_products = {
            *self._settings.token_products().keys(),
            *self._settings.cloudpayments_product_tokens().keys(),
        }
        for entry in self._settings.products_catalog():
            pid = entry.get("productId") or entry.get("product_id")
            if isinstance(pid, str):
                known_products.add(pid)
        if product_id not in known_products:
            raise HTTPException(status_code=400, detail="unknown product_id")

        existing = await self._session.scalar(
            text(
                "SELECT 1 FROM audit_logs WHERE user_id = :uid AND event_type = :etype "
                "AND payload->>'grantId' = :grant_id LIMIT 1"
            ),
            {
                "uid": str(user_id),
                "etype": EVENT_CRM_SUBSCRIPTION_GRANT,
                "grant_id": grant_id,
            },
        )
        if existing is not None:
            balance, sub = await self._current_balance_and_subscription(user_id)
            return CrmSubscriptionGrantResponse(
                id=str(user_id),
                tokens=float(balance),
                subscription_active=_subscription_active(sub["status"], sub["expires_at"]),
                subscription_expires_at=_iso_z(sub["expires_at"]),
                applied=False,
            )

        now = datetime.datetime.now(tz=datetime.UTC)
        sub_row = (
            (
                await self._session.execute(
                    text("SELECT status, expires_at FROM subscriptions WHERE user_id = :uid"),
                    {"uid": str(user_id)},
                )
            )
            .mappings()
            .first()
        )
        base = now
        if sub_row and sub_row["status"] == "active" and sub_row["expires_at"]:
            exp = sub_row["expires_at"]
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.UTC)
            if exp > now:
                base = exp
        new_expires = base + datetime.timedelta(days=expires_in_days)

        credits_map = self._settings.cloudpayments_product_tokens()
        credits = credits_map.get(product_id, self._settings.subscription_credits_per_period)

        await self._session.execute(
            text(
                "INSERT INTO subscriptions (user_id, status, plan, expires_at, updated_at) "
                "VALUES (:uid, 'active', :plan, :expires_at, now()) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "status = 'active', plan = EXCLUDED.plan, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()"
            ),
            {"uid": str(user_id), "plan": product_id, "expires_at": new_expires},
        )
        await self._session.flush()

        if credits > 0:
            await self._wallet.grant(
                user_id=user_id,
                amount=credits,
                idempotency_key=f"crm-sub-grant:{grant_id}",
                meta={"source": "crm_admin", "productId": product_id},
                reason="crm_admin_subscription",
            )

        await self._audit.record(
            AuditEvent(
                user_id=user_id,
                event_type=EVENT_CRM_SUBSCRIPTION_GRANT,
                payload={
                    "grantId": grant_id,
                    "productId": product_id,
                    "expiresInDays": expires_in_days,
                    "expiresAt": new_expires.isoformat(),
                },
            )
        )

        balance, _ = await self._current_balance_and_subscription(user_id)
        return CrmSubscriptionGrantResponse(
            id=str(user_id),
            tokens=float(balance),
            subscription_active=True,
            subscription_expires_at=_iso_z(new_expires),
            applied=True,
        )

    async def _current_balance_and_subscription(
        self, user_id: uuid.UUID
    ) -> tuple[int, dict[str, Any]]:
        row = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT COALESCE(w.balance, 0) AS balance,
                           s.status, s.expires_at
                    FROM users u
                    LEFT JOIN wallets w ON w.user_id = u.id
                    LEFT JOIN subscriptions s ON s.user_id = u.id
                    WHERE u.id = :uid
                    """
                    ),
                    {"uid": str(user_id)},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise UserNotFoundError("user not found")
        return int(row["balance"] or 0), {"status": row["status"], "expires_at": row["expires_at"]}
