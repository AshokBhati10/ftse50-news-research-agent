"""Tiny file-backed JSON cache with per-namespace TTLs.

Design goals: zero dependencies, human-inspectable files, and a stable key
scheme so repeated runs (and the evaluation suite) avoid redundant API calls.
Nothing secret is ever written here — only public web content and metadata.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def _stable_key(*parts: str) -> str:
    digest = hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()
    return digest[:32]


class FileCache:
    def __init__(self, root: str | Path, ttl_seconds: int = 86400, enabled: bool = True):
        self.root = Path(root)
        self.ttl = ttl_seconds
        self.enabled = enabled
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers
    def _path(self, namespace: str, key: str) -> Path:
        ns_dir = self.root / namespace
        ns_dir.mkdir(parents=True, exist_ok=True)
        return ns_dir / f"{_stable_key(key)}.json"

    # ---------------------------------------------------------------------- api
    def get(self, namespace: str, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - payload.get("saved_at", 0) > self.ttl:
            return None
        return payload.get("value")

    def set(self, namespace: str, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        payload = {"saved_at": time.time(), "value": value}
        try:
            path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        except OSError:
            pass  # caching must never break the pipeline

    def invalidate(self, namespace: str, key: str) -> None:
        path = self._path(namespace, key)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class CacheManager:
    """Namespace bundle with sensible TTLs per data type."""

    def __init__(self, root: str | Path, enabled: bool = True):
        root = Path(root)
        self.enabled = enabled
        self.companies = FileCache(root, ttl_seconds=7 * 86400, enabled=enabled)   # FTSE50 list
        self.marketcap = FileCache(root, ttl_seconds=7 * 86400, enabled=enabled)   # yfinance caps
        self.search = FileCache(root, ttl_seconds=24 * 3600, enabled=enabled)      # search results
        self.extraction = FileCache(root, ttl_seconds=7 * 86400, enabled=enabled)  # article text
        self.analysis = FileCache(root, ttl_seconds=24 * 3600, enabled=enabled)    # LLM judgements
