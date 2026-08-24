"""Обе ветки чата собираются одинаково полно (детектор пропущенной зависимости).

Поводом послужил реальный дефект (2026-08-24): сервис документов (ADR-090) был проброшен только
в `get_orchestrator`, а `/v1/chat/v2/run` — которым и ходит приложение — обслуживается
`get_v2_orchestrator`, где его не оказалось. Инструмент `document.create` отвечал
`documents_not_available`, и модель сообщала пользователю, что файл создать нельзя.

Это тот же класс, что и пропущенный `/v1/chat/v2/run/stream` в карте transport-лимитов: перечень,
поддерживаемый вручную, с забытой записью. Тест сравнивает ветки МЕЖДУ СОБОЙ, а не со списком имён,
поэтому ловит и следующую зависимость, добавленную только в одну из них.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from app.deps import get_orchestrator, get_v2_orchestrator


def _fake_session() -> Any:
    """Сборка графа трогает сессию (например `.bind`), но запросов не делает — mock достаточно."""
    return MagicMock(name="AsyncSession")


def _deps_of(orchestrator: Any) -> Any:
    return orchestrator._deps  # noqa: SLF001 — тест намеренно смотрит на собранный граф


def test_both_chat_branches_get_the_same_dependencies() -> None:
    session = _fake_session()
    legacy = _deps_of(get_orchestrator(session))  # type: ignore[arg-type]
    v2 = _deps_of(get_v2_orchestrator(session))  # type: ignore[arg-type]

    missing = [
        name
        for name in vars(legacy)
        if getattr(legacy, name) is not None and getattr(v2, name, None) is None
    ]
    assert not missing, (
        f"эти зависимости есть в legacy-ветке, но не в v2: {missing}. "
        "Инструмент, опирающийся на них, будет молча отвечать 'not available' именно на v2 — "
        "той ветке, которой ходит приложение."
    )


def test_document_tools_are_wired_on_both_branches() -> None:
    """Точечная проверка ADR-090: без неё общий тест выше проходил бы и на двух пустых ветках."""
    session = _fake_session()
    for factory in (get_orchestrator, get_v2_orchestrator):
        deps = _deps_of(factory(session))  # type: ignore[arg-type]
        assert deps.documents is not None, f"{factory.__name__}: сервис документов не проброшен"
        tools = deps.global_tools
        assert tools._documents is not None, (  # noqa: SLF001
            f"{factory.__name__}: GlobalToolHandlers собран без документов — "
            "document.* вернёт documents_not_available"
        )
