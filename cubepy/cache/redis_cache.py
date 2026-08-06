"""Async Redis result cache. Wraps any ``redis.asyncio``-compatible client
(real Redis in prod, ``fakeredis.FakeAsyncRedis`` in tests)."""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis


class RedisCache:
    def __init__(self, client: Redis) -> None:
        self.client = client

    async def get(self, key: str) -> dict[str, Any] | None:
        raw = await self.client.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw)

    async def setex(self, key: str, ttl_seconds: int, value: dict[str, Any]) -> None:
        payload = json.dumps(value, default=str)
        await self.client.setex(key, ttl_seconds, payload)

    async def delete(self, key: str) -> None:
        await self.client.delete(key)
