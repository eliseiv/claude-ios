"""Policy schemas for /v1/policy/effective (policy-engine/02)."""

from __future__ import annotations

import datetime

from pydantic import Field

from app.schemas.common import StrictModel


class EffectivePolicyResponse(StrictModel):
    isSubscribed: bool = Field(description="Есть ли активная подписка.")
    subscriptionExpiresAt: datetime.datetime | None = Field(
        default=None,
        description=(
            "Момент окончания активной подписки (ISO8601), или `null` если подписки нет/истекла. "
            "После него подписка лениво считается истёкшей и `isSubscribed` станет `false`."
        ),
    )
    plan: str | None = Field(
        default=None,
        description="Код тарифа активной подписки (например `week_6.99_nottrial`), или `null`.",
    )
    willRenew: bool | None = Field(
        default=None,
        description=(
            "Активно ли автопродление активной подписки: `true` — продлится, `false` — отменена "
            "(доступ до `subscriptionExpiresAt`), `null` — неизвестно (нет подписки, либо оплата "
            "прошла по каналу без сведений об автопродлении, напр. RU-платёж)."
        ),
    )
    trialRemaining: int = Field(description="Остаток бесплатных пробных генераций (trial).")
    creditsBalance: int = Field(description="Текущий баланс кредитов (1 кредит = 1 сообщение).")
    byokEnabled: bool = Field(
        description="Включён ли пользователем собственный ключ Anthropic (BYOK)."
    )
    canGenerateCreditsMode: bool = Field(description="Доступна ли генерация в режиме `credits`.")
    canGenerateByokMode: bool = Field(description="Доступна ли генерация в режиме `byok`.")
    reasons: list[str] = Field(
        description=(
            "Причины недоступности генерации (подмножество значений `blockReason`: "
            "`trial_used`, `subscription_required`, `subscription_expired`, `credits_empty`, "
            "`byok_disabled`, `byok_invalid`, `rate_limited`, `policy_denied`). Те же значения "
            "использует `blockReason` в Chat."
        ),
    )
