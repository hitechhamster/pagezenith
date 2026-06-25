"""编排流程（规格 §0.4）：

  输入关键词 + 目标 URL
    → DataForSEO 拉前 10 SERP，判断目标 URL 是否在内、排第几
    → 抓取 目标页 + 前 N 竞品页正文
    → LLM 抽取每页 profile（竞品走缓存）
    → 代码聚合竞品 profile 成知识并集
    → 代码计算三维度分数与差距
    → DataForSEO backlinks 补充目标页 + 竞品外链/引荐域名
    → 汇总成报告
"""

from __future__ import annotations

import asyncio
import logging

from .clients.browser_fetch import BrowserFetcher
from .clients.dataforseo import DataForSEOClient
from .clients.fetch import PageFetcher
from .clients.llm import LLMClient
from .clients.serpapi import SerpApiClient
from .config import Settings, get_settings
from .extraction.extractor import Extractor
from .models import (
    AnalysisReport,
    AnalyzeRequest,
    BacklinkSummary,
    InformationGain,
    NovelPoint,
    PageContent,
    ReadabilityMetrics,
    SerpItem,
)
from .report import build_report
from .scoring.eeat import compute_eeat
from .scoring.information_gain import compute_information_gain
from .scoring.readability import compute_readability
from .scoring.semantic import SemanticDeduper


logger = logging.getLogger(__name__)


def _norm(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/").lower()


class AnalysisPipeline:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.dfs = DataForSEOClient(self.s)  # backlinks（SERP 可被 SerpApi 替代）
        # SERP 数据源按配置选择；backlinks 仍走 DataForSEO（无替代源，失败则标不可用）
        self.serp = SerpApiClient(self.s) if self.s.serp_provider == "serpapi" else self.dfs
        self.fetcher = PageFetcher(self.s)
        self.llm = LLMClient(self.s)
        self.extractor = Extractor(self.llm, self.fetcher)
        self.deduper = SemanticDeduper(self.llm)

    def _make_fetcher(self, req: AnalyzeRequest):
        """按生效抓取方式建抓取器（每个 run 一个，结束时关闭）。"""
        mode = req.fetch_mode or self.s.fetch_mode
        return BrowserFetcher(self.s) if mode == "browser" else PageFetcher(self.s)

    async def run(self, req: AnalyzeRequest) -> AnalysisReport:
        fetcher = self._make_fetcher(req)
        try:
            return await self._run(req, fetcher)
        finally:
            await fetcher.aclose()  # browser 模式下关闭 Chromium

    async def _run(self, req: AnalyzeRequest, fetcher) -> AnalysisReport:
        has_keyword = bool(req.keyword and req.keyword.strip())
        target_norm = _norm(req.target_url)

        # 目标页正文：插件已传入则直接用（浏览器已渲染、过反爬）；否则服务端抓取。
        target_page = await self._get_target_page(req, fetcher)

        rank: int | None = None
        if has_keyword:
            # ===== 关键词模式：发现竞品并对比 =====
            effective_n = min(req.top_n or self.s.top_n, self.s.max_competitors)
            if req.competitor_urls:
                competitors = [
                    SerpItem(url=u, rank=i + 1)
                    for i, u in enumerate(req.competitor_urls)
                    if _norm(u) != target_norm
                ][:effective_n]
            else:
                serp = await self.serp.fetch_serp(
                    req.keyword, req.location_code, req.language_code, depth=self.s.top_n
                )
                rank = next((it.rank for it in serp if _norm(it.url) == target_norm), None)
                competitors = [it for it in serp if _norm(it.url) != target_norm][:effective_n]

            live = await self._fetch_competitor_pages(competitors, fetcher)
            competitor_pages = [p for _, p in live]
            competitor_profiles = await asyncio.gather(
                *[self.extractor.extract_content(p, use_cache=True) for _, p in live]
            )
            target_profile = await self.extractor.extract_page(target_page)
            target_bl, competitor_bls = await self._fetch_backlinks(
                req.target_url, [it for it, _ in live]
            )

            gain = await compute_information_gain(
                target_profile, list(competitor_profiles), self.deduper
            )
            readability = compute_readability(
                target_page, competitor_pages, qualitative_notes=target_profile.readability_notes
            )
            eeat = compute_eeat(target_profile, target_bl, list(competitor_bls))
            mode = "keyword"
        else:
            # ===== 单页模式：不发现竞品、不对比，只评本页 =====
            target_profile = await self.extractor.extract_page(target_page)
            # 单页模式无竞品对比：gain_score=None，但把本页 claims 列出来供查看
            gain = InformationGain(
                novel_points=[NovelPoint(text=c.text, type=c.type) for c in target_profile.claims]
            )
            readability = compute_readability(
                target_page, [], qualitative_notes=target_profile.readability_notes
            )
            readability.verdict = "n/a"  # 无前10可比
            readability.top10_avg = ReadabilityMetrics()
            # 无竞品外链对比 → 权威性标不可用；经验/专业/可信仍可评
            eeat = compute_eeat(
                target_profile, BacklinkSummary(url=req.target_url, available=False), []
            )
            mode = "page"

        in_top_10 = rank is not None and rank <= 10
        guard = self.deduper.stats
        logger.info(
            "mode=%s | dedup guard: enabled=%s high_cosine=%d demoted=%d "
            "demoted_judged_different=%d (ratio=%.3f)",
            mode, guard.guard_enabled, guard.high_cosine_pairs, guard.demoted_pairs,
            guard.demoted_judged_different, guard.demoted_different_ratio,
        )
        report = build_report(
            keyword=req.keyword, target_url=req.target_url,
            in_top_10=in_top_10, rank=rank,
            gain=gain, readability=readability, eeat=eeat,
        )
        report.mode = mode
        report.debug = {"dedup_guard": guard.as_dict()}
        return report

    async def _get_target_page(self, req: AnalyzeRequest, fetcher) -> PageContent:
        if req.target_text and req.target_text.strip():
            return PageContent(
                url=req.target_url, title=req.target_title, text=req.target_text, raw_html=None
            )
        try:
            return await fetcher.fetch(req.target_url)
        except Exception as exc:
            raise RuntimeError(
                f"目标页抓取失败（{exc}）。该站可能反爬；请用浏览器插件分析"
                f"（插件会直接读取你已打开的页面正文，绕过反爬）。"
            ) from exc

    async def _fetch_competitor_pages(
        self, competitors: list[SerpItem], fetcher
    ) -> list[tuple[SerpItem, PageContent]]:
        """并发单次抓取，失败/空正文的竞品丢弃不中断。"""
        fetched = await asyncio.gather(
            *[fetcher.fetch(c.url) for c in competitors], return_exceptions=True
        )
        live: list[tuple[SerpItem, PageContent]] = []
        for item, page in zip(competitors, fetched):
            if isinstance(page, Exception) or not page.text.strip():
                logger.warning("竞品抓取失败/空，跳过 %s: %s", item.url,
                               page if isinstance(page, Exception) else "empty")
            else:
                live.append((item, page))
        return live

    async def _fetch_backlinks(self, target_url: str, competitors: list[SerpItem]):
        results = await asyncio.gather(
            self.dfs.fetch_backlinks(target_url),
            *[self.dfs.fetch_backlinks(c.url) for c in competitors],
        )
        return results[0], list(results[1:])
