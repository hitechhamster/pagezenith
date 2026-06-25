"""浏览器抓取（Playwright 本地 Chromium）：执行 JS + 真实指纹，绕过多数反爬 403。

慢但抓得到。与 PageFetcher 同接口（fetch / aclose），pipeline 按 fetch_mode 二选一。
一个 run 内复用同一个浏览器实例，结束时 aclose() 关闭。

依赖：pip install playwright && python -m playwright install chromium
"""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from ..models import PageContent
from ..security import assert_safe_url
from .fetch import _extract, _mock_page

logger = logging.getLogger(__name__)


class BrowserFetcher:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self._pw = None
        self._browser = None

    async def _ensure(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            kwargs = {"headless": self.s.browser_headless}
            if self.s.browser_channel:  # 用系统已装浏览器，免下载自带 Chromium
                kwargs["channel"] = self.s.browser_channel
            self._browser = await self._pw.chromium.launch(**kwargs)
        return self._browser

    async def fetch(self, url: str) -> PageContent:
        if self.s.use_mocks:
            return _mock_page(url)
        if self.s.block_private_urls:
            assert_safe_url(url)

        browser = await self._ensure()
        # 关键：不要覆盖 user_agent。用 channel=chrome 时若强行写死 UA，会和真实 Chrome
        # 的 sec-ch-ua 客户端提示头不一致 → 被 WAF 当机器人 403。用浏览器自带的一致指纹。
        ctx = await browser.new_context(
            locale="zh-CN",
            viewport={"width": 1366, "height": 900},
        )
        page = await ctx.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=self.s.browser_nav_timeout_ms)
            await page.wait_for_timeout(self.s.browser_wait_ms)  # 等 JS 渲染
            html = await page.content()
            # Cloudflare/Akamai 挑战页（“请稍候/just a moment”）：正文极短，
            # 再多等几秒让 JS 自动通过跳转，然后重读。
            if len(_extract(url, html).text) < 200:
                await page.wait_for_timeout(5000)
                html = await page.content()
            status = resp.status if resp else 0
        finally:
            await ctx.close()
        content = _extract(url, html)
        # 真实浏览器也拿到 4xx/5xx（站点按代理 IP 拦截，如 403 拦截页）→ 当抓取失败，
        # 让上层标“被反爬/已跳过”，而不是把拦截页的几个词当成正文。
        if status >= 400 and len(content.text) < 500:
            raise RuntimeError(f"HTTP {status} 拦截页（站点封禁代理 IP）")
        return content

    async def capture(self, url: str):
        """抓正文 + 首屏截图（用于视觉理解页面类型）。返回 (PageContent, png_bytes|None)。"""
        if self.s.use_mocks:
            return _mock_page(url), None
        if self.s.block_private_urls:
            assert_safe_url(url)
        browser = await self._ensure()
        ctx = await browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900})
        page = await ctx.new_page()
        png = None
        try:
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=self.s.browser_nav_timeout_ms)
            await page.wait_for_timeout(self.s.browser_wait_ms)
            html = await page.content()
            if len(_extract(url, html).text) < 200:
                await page.wait_for_timeout(5000)
                html = await page.content()
            status = resp.status if resp else 0
            try:
                png = await page.screenshot(full_page=False)  # 首屏
            except Exception:
                png = None
        finally:
            await ctx.close()
        content = _extract(url, html)
        if status >= 400 and len(content.text) < 500:
            raise RuntimeError(f"HTTP {status} 拦截页（站点封禁代理 IP）")
        return content, png

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
