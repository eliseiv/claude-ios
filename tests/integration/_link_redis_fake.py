"""In-memory Redis double for the checkout link reuse (``cp:link:*``): hget/set NX/delete/pipeline.

Patched over ``app.billing_cloudpayments.checkout.get_redis`` so checkout tests never touch a real
Redis. ``broken=True`` makes every call raise ``RedisError`` (fail-open path).
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis


class _Pipeline:
    def __init__(self, owner: FakeLinkRedis) -> None:
        self._owner = owner
        self._ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _Pipeline:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def hset(self, key: str, field: str, value: str) -> None:
        self._ops.append(("hset", (key, field, value)))

    def expire(self, key: str, seconds: int) -> None:
        self._ops.append(("expire", (key, seconds)))

    async def execute(self) -> list[Any]:
        self._owner._check()
        for op, args in self._ops:
            if op == "hset":
                key, field, value = args
                self._owner.hashes.setdefault(key, {})[field] = value
            else:
                self._owner.expires[args[0]] = args[1]
        return [True] * len(self._ops)


class FakeLinkRedis:
    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.hashes: dict[str, dict[str, str]] = {}
        self.expires: dict[str, int] = {}
        self.locks: set[str] = set()
        self.lock_attempts: list[tuple[str, bool]] = []

    def _check(self) -> None:
        if self.broken:
            raise aioredis.ConnectionError("redis down")

    async def hget(self, key: str, field: str) -> str | None:
        self._check()
        return self.hashes.get(key, {}).get(field)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self._check()
        taken = not (nx and key in self.locks)
        self.lock_attempts.append((key, taken))
        if taken:
            self.locks.add(key)
        return taken

    async def delete(self, key: str) -> int:
        self._check()
        existed = key in self.hashes or key in self.locks
        self.hashes.pop(key, None)
        self.locks.discard(key)
        return int(existed)

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        self._check()
        return _Pipeline(self)
