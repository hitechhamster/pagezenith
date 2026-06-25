"""DataForSEO 客户端：SERP（前 10）+ Backlinks（引荐域名/外链/域名 rank）。

真实端点已接好结构（你已有凭证）。settings.use_mocks=True 时返回桩数据，
便于无凭证跑通流程。把 use_mocks 关掉即走真实 API。

端点参考：
  - SERP:      POST /v3/serp/google/organic/live/advanced
  - Backlinks: POST /v3/backlinks/summary/live
"""

from __future__ import annotations

import base64
import logging

import httpx

from ..config import Settings, get_settings
from ..models import BacklinkSummary, SerpItem

logger = logging.getLogger(__name__)


class DataForSEOError(RuntimeError):
    """DataForSEO 业务错误（如账号未验证）。SERP 失败属硬错误，需上层处理。"""


class DataForSEOClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(
            f"{self.s.dataforseo_login}:{self.s.dataforseo_password}".encode()
        ).decode()
        return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    # ------------------------------------------------------------------ SERP
    async def fetch_serp(
        self, keyword: str, location_code: int, language_code: str, depth: int = 10
    ) -> list[SerpItem]:
        if self.s.use_mocks:
            return _mock_serp(keyword, depth)

        payload = [
            {
                "keyword": keyword,
                "location_code": location_code,
                "language_code": language_code,
                "depth": depth,
            }
        ]
        url = f"{self.s.dataforseo_base_url}/v3/serp/google/organic/live/advanced"
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.post(url, headers=self._auth_header(), json=payload)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status_code", 0) >= 40000:
            raise DataForSEOError(f"SERP 失败：{data.get('status_message')}")
        return _parse_serp(data, depth)

    # -------------------------------------------------------------- Backlinks
    async def fetch_backlinks(self, target: str) -> BacklinkSummary:
        if self.s.use_mocks:
            return _mock_backlinks(target)

        payload = [{"target": _strip_scheme(target), "internal_list_limit": 1}]
        url = f"{self.s.dataforseo_base_url}/v3/backlinks/summary/live"
        try:
            async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
                resp = await client.post(url, headers=self._auth_header(), json=payload)
                resp.raise_for_status()
                data = resp.json()
            # DataForSEO 业务错误（如账号未验证）体现在 status_code 而非 HTTP 码
            if data.get("status_code", 0) >= 40000:
                logger.warning("backlinks unavailable for %s: %s", target, data.get("status_message"))
                return BacklinkSummary(url=target, available=False)
            return _parse_backlinks(data, target)
        except httpx.HTTPError as exc:
            logger.warning("backlinks request failed for %s: %s", target, exc)
            return BacklinkSummary(url=target, available=False)


# --------------------------------------------------------------------------- #
# 解析（真实响应）
# --------------------------------------------------------------------------- #
def _parse_serp(data: dict, depth: int) -> list[SerpItem]:
    items: list[SerpItem] = []
    try:
        results = data["tasks"][0]["result"][0]["items"]
    except (KeyError, IndexError, TypeError):
        return items
    rank = 0
    for it in results:
        if it.get("type") != "organic":
            continue
        rank += 1
        items.append(
            SerpItem(url=it.get("url", ""), rank=rank, title=it.get("title"))
        )
        if rank >= depth:
            break
    return items


def _parse_backlinks(data: dict, target: str) -> BacklinkSummary:
    try:
        r = data["tasks"][0]["result"][0]
    except (KeyError, IndexError, TypeError):
        return BacklinkSummary(url=target)
    return BacklinkSummary(
        url=target,
        referring_domains=r.get("referring_domains", 0) or 0,
        backlinks=r.get("backlinks", 0) or 0,
        domain_rank=r.get("rank"),
    )


def _strip_scheme(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/")


# --------------------------------------------------------------------------- #
# Mock
# --------------------------------------------------------------------------- #
def _mock_serp(keyword: str, depth: int) -> list[SerpItem]:
    return [
        SerpItem(url=f"https://competitor{i}.example.com/{keyword.replace(' ', '-')}",
                 rank=i, title=f"{keyword} — competitor {i}")
        for i in range(1, depth + 1)
    ]


def _mock_backlinks(target: str) -> BacklinkSummary:
    # 竞品给高外链，目标页给低外链，制造"权威性差距"便于演示。
    is_competitor = "competitor" in target
    return BacklinkSummary(
        url=target,
        referring_domains=120 if is_competitor else 8,
        backlinks=4200 if is_competitor else 40,
        domain_rank=78.0 if is_competitor else 31.0,
    )
