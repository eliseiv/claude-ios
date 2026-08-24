"""Embedding client for cross-chat memory (OpenAI or deterministic fake for tests)."""

from __future__ import annotations

import hashlib
import math
from functools import lru_cache

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


@lru_cache
def get_embedding_client() -> EmbeddingClient:
    return EmbeddingClient(get_settings())
