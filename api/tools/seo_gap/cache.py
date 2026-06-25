"""竞品 profile 缓存（规格 §0.3）。

以 competitor_url 为 key 缓存抽取结果，TTL 默认 7 天，避免重复花 LLM 成本。
这里给一个进程内 TTL 实现作为骨架；生产可替换为 Redis/SQLite，接口保持不变。
"""

from __future__ import annotations

import time
from typing import Optional

from .config import get_settings
from .models import SinglePageProfile


class ProfileCache:
    def __init__(self, ttl: int | None = None):
        self.ttl = ttl if ttl is not None else get_settings().competitor_cache_ttl
        self._store: dict[str, tuple[float, SinglePageProfile]] = {}

    def get(self, url: str) -> Optional[SinglePageProfile]:
        hit = self._store.get(url)
        if not hit:
            return None
        ts, profile = hit
        if time.time() - ts > self.ttl:
            del self._store[url]
            return None
        return profile

    def set(self, url: str, profile: SinglePageProfile) -> None:
        self._store[url] = (time.time(), profile)


# 进程内单例骨架；替换存储后端时只动这里。
_default_cache = ProfileCache()


def get_cache() -> ProfileCache:
    return _default_cache
