"""三步向导之间的会话（进程内，带 TTL）。

为什么需要它：搜索结果（20 条竞品正文）动辄上百 KB，如果每一步都让浏览器把它传回来，
既慢又浪费带宽。于是第一步把参数 + 搜索上下文 + 大纲存在服务器内存里，后两步只带 session_id。

刻意不落库 —— 站点目前整体无状态、无数据库，这里也只是进程内的一个带 TTL 的 dict。
进程重启/会话过期时，前端会把大纲和参数回传，降级为"没有搜索上下文"继续出文，不让用户白填。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from ..seo_gap.config import get_settings


class SessionStore:
    def __init__(self, ttl: int | None = None, max_items: int | None = None):
        s = get_settings()
        self.ttl = ttl if ttl is not None else s.writer_session_ttl
        self.max_items = max_items if max_items is not None else s.writer_max_sessions
        self._store: dict[str, tuple[float, dict[str, Any]]] = {}

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, (ts, _) in self._store.items() if now - ts > self.ttl]:
            del self._store[k]
        # 仍然超量就按最旧淘汰，防内存无上限增长
        while len(self._store) > self.max_items:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest]

    def create(self, data: dict[str, Any]) -> str:
        self._purge()
        sid = uuid.uuid4().hex
        self._store[sid] = (time.time(), data)
        return sid

    def get(self, sid: str) -> Optional[dict[str, Any]]:
        hit = self._store.get(sid or "")
        if not hit:
            return None
        ts, data = hit
        if time.time() - ts > self.ttl:
            del self._store[sid]
            return None
        return data

    def update(self, sid: str, **fields: Any) -> None:
        data = self.get(sid)
        if data is None:
            return
        data.update(fields)
        self._store[sid] = (time.time(), data)   # 续期：用户还在这个会话里操作


_store = SessionStore()


def get_store() -> SessionStore:
    return _store
