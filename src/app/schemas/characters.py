"""Characters-catalog schema for GET /v1/characters (ADR-097).

Read-only, provider-agnostic contract: the static character registry as a list of
``{id, name, tagline, icon}`` items, plus the resolved ``locale`` and the instance flag
``enabled``. ``name``/``tagline`` are localized (EN canon and per-field fallback);
``id``/``icon`` are stable across locales. The character's system-prompt text is never part
of this contract.
"""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import StrictModel


class CharacterInfo(StrictModel):
    id: str = Field(
        description=(
            "Стабильный slug персонажа (snake_case, `[a-z0-9_]`). Передаётся в `characterId` "
            "при создании чата; не зависит от языка — пригоден для аналитики и графики "
            "на клиенте."
        )
    )
    name: str = Field(description="Отображаемое имя персонажа (на языке ответа).")
    tagline: str = Field(description="Короткая подпись карточки в одну строку (на языке ответа).")
    icon: str = Field(
        description=(
            "Имя SF Symbol (например `sparkles`); рисуется на iOS через `Image(systemName:)`. "
            "Не зависит от языка."
        )
    )


class CharactersResponse(StrictModel):
    enabled: bool = Field(
        description=(
            "Включён ли выбор персонажа на этом инстансе. При `false` список пуст, а "
            "`characterId` при создании чата отклоняется с ошибкой 422. Прячьте вход в выбор "
            "персонажа по этому полю."
        )
    )
    locale: str = Field(
        description=(
            "Язык, фактически применённый к текстам `name` и `tagline` (из числа "
            "поддерживаемых, например `en` или `ru`)."
        ),
        examples=["en"],
    )
    characters: list[CharacterInfo] = Field(
        description="Каталог персонажей; порядок элементов = порядок на экране выбора."
    )
