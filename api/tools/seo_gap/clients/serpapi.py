"""SerpApi 客户端：只做 SERP（发现竞品 + 判目标页排名）。

与 DataForSEOClient.fetch_serp 同签名，pipeline 按 serp_provider 二选一。
免费额度 250 次/月，无最低充值门槛。不提供 backlinks（外链仍走 DataForSEO 或标不可用）。

端点：GET https://serpapi.com/search.json?engine=google&q=...&api_key=...
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings, get_settings
from ..models import SerpItem

logger = logging.getLogger(__name__)


class SerpApiError(RuntimeError):
    """SerpApi 业务错误（如 key 无效、额度用尽）。"""


# DataForSEO 数字 location_code → SerpApi 的 gl(国家码) 粗映射，未命中默认 us。
_GL_MAP = {2840: "us", 2826: "uk", 2156: "cn", 2344: "hk", 2158: "tw",
           2392: "jp", 2702: "sg", 2036: "au", 2276: "de", 2250: "fr"}


class SerpApiClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    async def _search(self, keyword: str, location_code: int, language_code: str, depth: int) -> dict:
        params = {
            "engine": "google",
            "q": keyword,
            "api_key": self.s.serpapi_key,
            "num": depth,
            "hl": language_code,
            "gl": _GL_MAP.get(location_code, "us"),
        }
        url = f"{self.s.serpapi_base_url}/search.json"
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
        if data.get("error"):
            raise SerpApiError(data["error"])
        return data

    async def fetch_serp(
        self, keyword: str, location_code: int, language_code: str, depth: int = 10
    ) -> list[SerpItem]:
        if self.s.use_mocks:
            return _mock_serp(keyword, depth)
        return _parse(await self._search(keyword, location_code, language_code, depth), depth)

    async def fetch_serp_full(
        self, keyword: str, location_code: int, language_code: str, depth: int = 10
    ) -> dict:
        """一次搜索同时拿 organic + PAA(related_questions) + related_searches，省一次调用。
        返回 {items, paa, related}。"""
        if self.s.use_mocks:
            return {
                "items": _mock_serp(keyword, depth),
                "paa": [f"{keyword} 是什么", f"如何识别{keyword}"],
                "related": [f"{keyword} 案例", f"{keyword} 监管"],
            }
        data = await self._search(keyword, location_code, language_code, depth)
        paa = [q.get("question", "") for q in data.get("related_questions", []) if q.get("question")]
        related = [r.get("query", "") for r in data.get("related_searches", []) if r.get("query")]
        return {"items": _parse(data, depth), "paa": paa, "related": related}

    async def fetch_autocomplete(self, keyword: str, language_code: str = "en") -> list[str]:
        """Google 下拉建议（google_autocomplete 引擎）。"""
        if self.s.use_mocks:
            return [f"{keyword} 出金", f"{keyword} 滑点", f"{keyword} mt4"]
        params = {
            "engine": "google_autocomplete",
            "q": keyword,
            "api_key": self.s.serpapi_key,
            "hl": language_code,
        }
        url = f"{self.s.serpapi_base_url}/search.json"
        try:
            async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
            if data.get("error"):
                logger.warning("autocomplete 失败：%s", data["error"])
                return []
            return [s.get("value", "") for s in data.get("suggestions", []) if s.get("value")]
        except Exception as exc:
            logger.warning("autocomplete 请求失败：%s", exc)
            return []


def _parse(data: dict, depth: int) -> list[SerpItem]:
    items: list[SerpItem] = []
    rank = 0
    for it in data.get("organic_results", []):
        link = it.get("link")
        if not link:
            continue
        rank += 1
        items.append(SerpItem(url=link, rank=it.get("position", rank), title=it.get("title")))
        if rank >= depth:
            break
    return items


def _mock_serp(keyword: str, depth: int) -> list[SerpItem]:
    slug = keyword.replace(" ", "-")
    return [
        SerpItem(url=f"https://competitor{i}.example.com/{slug}", rank=i,
                 title=f"{keyword} — competitor {i}")
        for i in range(1, depth + 1)
    ]
