"""Serper.dev 客户端：SERP + PAA + 相关搜索 + 下拉建议。

与 SerpApiClient / DataForSEOClient 同签名，pipeline 按 `serp_provider` 三选一。
2026-08 起是**默认且唯一在用**的 SERP 源 —— 同样的数据比 SerpApi 便宜约一个数量级
（$50/5万次 vs $75/5千次），对"服务端出钱"的卡密模式来说是决定性的差别。

端点：POST https://google.serper.dev/search   Header: X-API-KEY
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings, get_settings
from ..models import SerpItem

logger = logging.getLogger(__name__)


class SerperError(RuntimeError):
    """Serper 业务错误（key 无效、额度用尽）。"""


# DataForSEO 数字 location_code → gl(国家码) 粗映射，未命中默认 us。
_GL_MAP = {2840: "us", 2826: "uk", 2156: "cn", 2344: "hk", 2158: "tw",
           2392: "jp", 2702: "sg", 2036: "au", 2276: "de", 2250: "fr",
           # 2026-09 补：外链拓客的语言下拉里有西/葡/意，之前未命中悄悄回落 gl=us
           2724: "es", 2076: "br", 2380: "it", 2528: "nl", 2643: "ru",
           2410: "kr", 2616: "pl", 2792: "tr", 2124: "ca", 2356: "in",
           2484: "mx", 2032: "ar", 2360: "id", 2764: "th", 2704: "vn",
           2620: "pt", 2752: "se"}


class SerperClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    async def _post(self, path: str, body: dict) -> dict:
        if not self.s.serper_key:
            raise SerperError("服务端未配置 SERPER_KEY")
        async with httpx.AsyncClient(timeout=self.s.request_timeout, trust_env=False,
                                     proxy=self.s.proxy_for("serper")) as client:
            resp = await client.post(
                f"{self.s.serper_base_url}{path}",
                headers={"X-API-KEY": self.s.serper_key, "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code >= 400:
                raise SerperError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()

    async def _search(self, keyword: str, location_code: int,
                      language_code: str, depth: int) -> dict:
        return await self._post("/search", {
            "q": keyword,
            "gl": _GL_MAP.get(location_code, "us"),
            "hl": language_code or "en",
            "num": max(10, min(100, depth)),
        })

    async def fetch_serp(self, keyword: str, location_code: int,
                         language_code: str, depth: int = 10) -> list[SerpItem]:
        if self.s.use_mocks:
            return _mock_serp(keyword, depth)
        return _parse(await self._search(keyword, location_code, language_code, depth), depth)

    async def fetch_serp_full(self, keyword: str, location_code: int,
                              language_code: str, depth: int = 10) -> dict:
        """一次搜索同时拿 organic + PAA + 相关搜索，省调用次数。"""
        if self.s.use_mocks:
            return {"items": _mock_serp(keyword, depth),
                    "paa": [f"{keyword} 是什么", f"如何识别{keyword}"],
                    "related": [f"{keyword} 案例", f"{keyword} 监管"]}
        data = await self._search(keyword, location_code, language_code, depth)
        paa = [q.get("question", "") for q in data.get("peopleAlsoAsk", []) if q.get("question")]
        related = [r.get("query", "") for r in data.get("relatedSearches", []) if r.get("query")]
        return {"items": _parse(data, depth), "paa": paa, "related": related}

    async def fetch_autocomplete(self, keyword: str, language_code: str = "en") -> list[str]:
        if self.s.use_mocks:
            return [f"{keyword} 出金", f"{keyword} 滑点", f"{keyword} mt4"]
        try:
            data = await self._post("/autocomplete", {"q": keyword, "hl": language_code or "en"})
            return [s.get("value", "") for s in data.get("suggestions", []) if s.get("value")]
        except Exception as exc:  # noqa: BLE001  建议词拿不到不该拖垮分析
            logger.warning("serper autocomplete 失败：%s", exc)
            return []


def _parse(data: dict, depth: int) -> list[SerpItem]:
    items: list[SerpItem] = []
    rank = 0
    for it in data.get("organic", []):
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
    return [SerpItem(url=f"https://competitor{i}.example.com/{slug}", rank=i,
                     title=f"{keyword} — competitor {i}")
            for i in range(1, depth + 1)]
