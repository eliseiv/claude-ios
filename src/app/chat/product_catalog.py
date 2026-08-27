"""Built-in chat product catalog (ADR-076).

Shown for every enabled credits provider. Env ``OPENAI_MODELS`` / ``ANTHROPIC_MODELS`` add extra
ids and may override display names; they no longer hide these rows. Instance default is always
present and emitted first by ``allowed_models_for``.
"""

from __future__ import annotations

from types import MappingProxyType

# Insertion order is the catalog order after the instance default is prepended.
OPENAI_PRODUCT_MODELS: MappingProxyType[str, str] = MappingProxyType(
    {
        "gpt-5.1": "GPT-5.1",
        "gpt-5": "GPT-5",
        "gpt-5-mini": "GPT-5 mini",
        "gpt-4.1": "GPT-4.1",
        "gpt-4o": "GPT-4o",
    }
)

ANTHROPIC_PRODUCT_MODELS: MappingProxyType[str, str] = MappingProxyType(
    {
        "claude-opus-5": "Claude Opus 5",
        # Модель была ЗАТАРИФИЦИРОВАНА (pricing/provider_prices.py), но в каталог не попала —
        # то есть сервер умел считать за неё деньги, а клиенту её не показывал. Приложение
        # держит её моделью по умолчанию, и её отсутствие в /v1/models выглядело как пропажа.
        "claude-sonnet-5": "Claude Sonnet 5",
        "claude-fable-5": "Claude Fable 5",
        "claude-opus-4-7": "Claude Opus 4.7",
        "claude-sonnet-4-6": "Claude Sonnet 4.6",
        "claude-opus-4-6": "Claude Opus 4.6",
        "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
        "claude-sonnet-4-5": "Claude Sonnet 4.5",
    }
)


def product_models_for(provider: str) -> MappingProxyType[str, str]:
    """Built-in id→name map for ``openai``; any other value uses the Anthropic catalog."""
    if provider.strip().lower() == "openai":
        return OPENAI_PRODUCT_MODELS
    return ANTHROPIC_PRODUCT_MODELS
