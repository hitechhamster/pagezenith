"""目标页 + 竞品页正文抓取。

用 selectolax 去掉 script/style/nav 等噪声，提取可读正文。
mock 模式下返回构造的占位正文。
"""

from __future__ import annotations

import httpx
from selectolax.parser import HTMLParser

from ..config import Settings, get_settings
from ..models import PageContent
from ..security import assert_safe_url

_DROP_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form")

# 反爬/拦截页特征短语：正文很短 + 命中任一 → 判抓取失败（别把拦截页当内容）
_BLOCK_PHRASES = (
    "just a moment", "attention required", "cloudflare", "access denied",
    "access to this page is forbidden", "verify you are human", "are you a robot",
    "checking your browser", "enable javascript", "unusual traffic", "403 forbidden",
    "请稍候", "网站访问限制", "触发了安全规则", "访问被拒绝", "人机验证",
)


def looks_blocked(text: str) -> bool:
    """短文本里命中拦截特征短语 → 视为被反爬拦截。"""
    t = text.lower()
    return len(text) < 700 and any(p in t for p in _BLOCK_PHRASES)

# 较完整的浏览器头，降低被 bot 管理器拦截的概率（生产可用）
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}


class PageFetcher:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    async def fetch(self, url: str) -> PageContent:
        if self.s.use_mocks:
            return _mock_page(url)
        if self.s.block_private_urls:
            assert_safe_url(url)
        async with httpx.AsyncClient(
            timeout=self.s.fetch_timeout, follow_redirects=True, headers=_BROWSER_HEADERS,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            html = resp.text
        return _extract(url, html)

    async def capture(self, url: str):
        """httpx 无法截图：返回 (PageContent, None)，接口与 BrowserFetcher 一致。"""
        return await self.fetch(url), None

    async def aclose(self) -> None:
        """与 BrowserFetcher 统一接口；httpx 每次请求自管理，无需关闭。"""
        return None


def _extract(url: str, html: str) -> PageContent:
    tree = HTMLParser(html)
    title = tree.css_first("title")
    for sel in _DROP_TAGS:
        for node in tree.css(sel):
            node.decompose()
    body = tree.body or tree.root
    text = body.text(separator="\n", strip=True) if body else ""
    return PageContent(
        url=url,
        title=title.text(strip=True) if title else None,
        text=text,
        raw_html=html,
    )


def _mock_page(url: str) -> PageContent:
    if "competitor" in url:
        text = (
            "What is forex regulation. Forex brokers are licensed by authorities such as "
            "the FCA, ASIC and CySEC. A regulated broker segregates client funds. "
            "Leverage in the EU is capped at 30:1 for retail clients. "
            "Always verify a broker's license number on the regulator's public register. "
            "This guide explains how to check a license step by step."
        )
    else:
        text = (
            "How to spot a forex scam. We tested 12 brokers ourselves and recorded the "
            "withdrawal times. Our original data shows that unregulated brokers delay "
            "withdrawals by an average of 9 days. We also attach screenshots of each "
            "support chat. Leverage in the EU is capped at 30:1 for retail clients."
        )
    return PageContent(url=url, title=f"Mock page for {url}", text=text, raw_html=f"<html>{text}</html>")
