"""Response schema for the RU payment webhook (billing-cloudpayments/02-api-contracts.md).

The endpoint reads the RAW request body (no Pydantic body model, ADR-050 §1) so there is no request
schema. This response model only documents the success envelope for OpenAPI / Swagger: ``{"code":
0}`` is returned for every processed callback (the internal outcome lives in logs, not response).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CloudPaymentsWebhookResponse(BaseModel):
    code: int = Field(
        default=0,
        description="Код приёма вебхука. `0` — событие принято (платёж обработан или пропущен).",
        examples=[0],
    )
