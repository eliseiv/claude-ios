"""Presets-catalog schema for GET /v1/presets (chat-orchestrator/02, ADR-035, ADR-049, ADR-080).

Provider-agnostic, read-only contract: the static prompt-preset registry as a list of
``{id, title, icon, prompt, category}`` items plus the resolved ``locale``. No state, no DB;
identical on every instance for a given locale (ADR-033). ``title``/``prompt`` are localized
(ADR-049); ``id``/``icon``/``category`` are stable across locales. ``category`` is additive
(ADR-080): ``null`` on home-screen chips, a genre slug on Agents-screen cards.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.schemas.common import StrictModel

PresetCategory = Literal["work", "life", "entertainment"]


class PresetInfo(StrictModel):
    id: str = Field(
        description=(
            "Стабильный slug пресета (snake_case, `[a-z0-9_]`), уникален в наборе. Стабилен "
            "между релизами; пригоден для аналитики/кэша на клиенте."
        )
    )
    title: str = Field(description="Отображаемое имя чипа (например `Plan Week`).")
    icon: str = Field(
        description=(
            "Имя SF Symbol (например `calendar`); рисуется на iOS через `Image(systemName:)`."
        )
    )
    prompt: str = Field(
        description="Текст промта, подставляемый в композер при тапе по чипу (plain text)."
    )
    category: PresetCategory | None = Field(
        default=None,
        description=(
            "Жанр карточки на экране агентов: `work` (работа), `life` (жизнь), "
            "`entertainment` (развлечения). `null` — чип главного экрана, не агент. "
            "Стабильный slug, не зависит от языка ответа."
        ),
    )


class PresetsResponse(StrictModel):
    locale: str = Field(
        description=(
            "Язык каталога, фактически применённый к текстам `title` и `prompt` "
            "(из числа поддерживаемых, например `en` или `ru`)."
        ),
        examples=["en"],
    )
    presets: list[PresetInfo] = Field(
        description=(
            "Каталог пресетов промтов для чипов на главном экране чата (порядок = порядок чипов)."
        )
    )
