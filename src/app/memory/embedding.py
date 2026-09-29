"""Embedding client for cross-chat memory (OpenAI or deterministic fake for tests)."""

from __future__ import annotations

import hashlib
import math

from openai import AsyncOpenAI

from app.config import Settings, get_settings


def _fake_embedding(text: str, *, dimensions: int) -> list[float]:
    """Deterministic normalized vector — no network, stable in unit tests."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vec = [((digest[i % len(digest)] ^ (i & 0xFF)) / 127.5) - 1.0 for i in range(dimensions)]
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


class EmbeddingClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._fake = settings.memory_embedding_fake
        self._dims = settings.memory_embedding_dimensions
        self._model = settings.memory_embedding_model
        self._client: AsyncOpenAI | None = None
        if not self._fake and settings.openai_api_key:
            self._client = AsyncOpenAI(api_key=settings.openai_api_key)

    @property
    def configured(self) -> bool:
        return self._fake or self._client is not None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._fake or self._client is None:
            return [_fake_embedding(t, dimensions=self._dims) for t in texts]
        response = await self._client.embeddings.create(model=self._model, input=texts)
        ordered = sorted(response.data, key=lambda row: row.index)
        return [row.embedding for row in ordered]


def _embedding_inputs(settings: Settings) -> tuple[object, ...]:
    return (
        settings.openai_api_key,
        settings.memory_embedding_fake,
        settings.memory_embedding_dimensions,
        settings.memory_embedding_model,
    )


class _EmbeddingClientFactory:
    """Процессный клиент эмбеддингов, пересоздаваемый при смене ключа OpenAI из CRM (ADR-116 §5).

    Замена ``lru_cache``: ``cache_clear()`` сохранён — им пользуется изоляция тестов.
    """

    def __init__(self) -> None:
        self._cached: tuple[tuple[object, ...], EmbeddingClient] | None = None

    def __call__(self) -> EmbeddingClient:
        from app.instance_config.effective import effective_settings

        settings = effective_settings(get_settings())
        key = _embedding_inputs(settings)
        if self._cached is not None and self._cached[0] == key:
            return self._cached[1]
        client = EmbeddingClient(settings)
        previous = self._cached
        self._cached = (key, client)
        if previous is not None:
            # Пул прежнего клиента закрывается, когда на нём не останется идущих вызовов.
            from app.instance_config.client_retirement import (
                retire_when_unreferenced,
                sdk_clients_of,
            )

            retire_when_unreferenced(previous[1], sdk_clients_of(previous[1], "_client"))
        return client

    def cache_clear(self) -> None:
        self._cached = None


get_embedding_client = _EmbeddingClientFactory()
