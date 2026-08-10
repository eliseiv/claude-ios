"""Chat Orchestrator: Anthropic calls, tool-loop, billing (CO phases).

Eager imports of ``anthropic_client`` are avoided here: ``app.schemas.chat`` imports
``Quiz`` from ``app.chat.tools``, and loading this package must not pull the client
stack (which imports schemas) or unit collection hits a circular import.
"""

from __future__ import annotations

from typing import Any

__all__ = ["AnthropicClient", "AnthropicResult", "get_anthropic_client"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from app.chat import anthropic_client as _mod

        return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
