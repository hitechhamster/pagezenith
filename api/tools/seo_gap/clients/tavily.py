"""Tavily Extract 客户端（可选）：用户填了 Tavily key 时，用它把网页解析成更干净的正文。

比 selectolax 更擅长剥离导航/广告/模板，拿到主体内容。失败自动回退到常规抓取。
端点：POST https://api.tavily.com/extract  Header: Authorization: Bearer tvly-...
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


class TavilyClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    @property
    def available(self) -> bool:
        return bool(self.s.tavily_key)

    async def extract(self, url: str) -> str | None:
        """返回该 URL 的干净正文；失败/无 key 返回 None（上层回退常规抓取）。"""
        if not self.s.tavily_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.s.tavily_timeout) as client:
                resp = await client.post(
                    f"{self.s.tavily_base_url}/extract",
                    headers={"Authorization": f"Bearer {self.s.tavily_key}",
                             "Content-Type": "application/json"},
                    json={"urls": [url], "extract_depth": "basic"},
                )
            if resp.status_code != 200:
                logger.warning("Tavily extract %s → HTTP %s", url, resp.status_code)
                return None
            data = resp.json()
            results = data.get("results") or []
            if results:
                content = (results[0].get("raw_content") or "").strip()
                return content or None
        except Exception as exc:
            logger.warning("Tavily extract 失败 %s: %s", url, exc)
        return None
