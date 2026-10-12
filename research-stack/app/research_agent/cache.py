from __future__ import annotations

import hashlib
import json
from typing import Any


class Cache:
    """App-level cache on Valkey (separate DB from SearXNG's own DB 0).

    Keys:
      sx:<sha256>   -> SearXNG JSON response for (query, categories, engines, time_range, page, lang)
      page:<sha256> -> extracted document (title/text/date) for a canonical URL
      neg:<sha256>  -> negative cache: URL failed to fetch/extract recently
    """

    def __init__(self, url: str) -> None:
        # redis-py speaks the same protocol as Valkey. Imported lazily so this module
        # (and the package) can be imported without the client library installed.
        import redis.asyncio as redis

        self._r = redis.from_url(url, decode_responses=True)

    @staticmethod
    def key(prefix: str, payload: dict[str, Any] | str) -> str:
        raw = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    async def get_json(self, key: str) -> Any | None:
        raw = await self._r.get(key)
        return json.loads(raw) if raw else None

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        await self._r.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)

    async def mark_failed(self, url: str, ttl: int = 6 * 3600) -> None:
        await self._r.set(self.key("neg", url), "1", ex=ttl)

    async def recently_failed(self, url: str) -> bool:
        return bool(await self._r.exists(self.key("neg", url)))

    async def ping(self) -> bool:
        return bool(await self._r.ping())

    async def close(self) -> None:
        await self._r.aclose()
