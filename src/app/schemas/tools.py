"""Tools-catalog schema for GET /v1/tools (chat-orchestrator/02, ADR-019)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.schemas.common import StrictModel


class ToolDescriptor(StrictModel):
    name: str = Field(description="Доменное имя инструмента с точкой (например `files.read`).")
    description: str = Field(description="Назначение инструмента.")
    mutating: bool = Field(description="Меняет ли инструмент данные.")
    execution: Literal["client", "server"] = Field(
        description=(
            "Где исполняется: `client` (на устройстве пользователя) или `server` (на бэкенде)."
        )
    )
    inputSchema: dict[str, Any] = Field(description="JSON Schema аргументов инструмента.")
    requiresConfirmation: bool = Field(
        default=False,
        description=(
            "Нужно ли спросить пользователя перед исполнением вызова. Признак задаёт бэкенд; "
            "выводить его из имени инструмента нельзя."
        ),
    )


class ToolsResponse(StrictModel):
    tools: list[ToolDescriptor] = Field(description="Полный каталог поддерживаемых инструментов.")
