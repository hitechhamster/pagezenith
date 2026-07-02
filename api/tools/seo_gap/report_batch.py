"""批量模式：一个目标页 vs 一簇关键词（最多 N 个）。

要点：
  - 每个关键词各拉一次 SERP（并行），算目标页在该词的排名。
  - 跨关键词把竞品 URL **去重**（防同一页在多个词结果里重复抓），按"命中关键词数"
    取前 N 个核心对手，每个只抓取/抽取一次。
  - 汇总跨词的内容缺口、并集 LSI、簇级 Reddit 讨论，产出一份综合增补与总结。

复用 ReportV2Builder 的子步骤（抽取/去重/LSI/Reddit/增补/总结），不重复造轮子。
"""

from __future__ import annotations

import asyncio
import logging

from .clients.reddit import RedditClient
from .config import Settings, get_settings
from .lexical import detect_lang
from .models import (
    PAGE_KIND_ZH, BatchCompetitor, BatchReportRequest, ConsolidatedGap, KeywordRank,
    KeywordSemantics, RedditInsights, RedditTheme, RedditThreadRef, ReportRequest, TargetSummary,
)
from .report_v2 import ReportV2Builder, _clean_text, _image_count, _norm, _word_count

logger = logging.getLogger(__name__)


class BatchReportBuilder:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.rv = ReportV2Builder(self.s)        # 复用其抽取/去重/LSI/Reddit/增补/总结
        self.serp = self.rv.serp
        self.reddit = RedditClient(self.s)

    async def build_stream(self, req: BatchReportRequest):
        fetcher = self.rv._make_fetcher(ReportRequest(keyword="", target_url=req.target_url,
                                                      fetch_mode=req.fetch_mode))
        try:
            async for ev in self._build(req, fetcher):
                yield ev
        finally:
            await fetcher.aclose()

    async def _build(self, req: BatchReportRequest, fetcher):
        kws = [k.strip() for k in req.keywords if k.strip()][: self.s.batch_max_keywords]
        if not kws:
            yield {"type": "error", "message": "请至少输入一个关键词。"}
            return
        target_norm = _norm(req.target_url)

        # 目标页（抓一次）
        treq = ReportRequest(keyword=kws[0], target_url=req.target_url, target_text=req.target_text,
                             target_title=req.target_title, fetch_mode=req.fetch_mode)
        target_page, _png = await self.rv._get_target(treq, fetcher)
        target_profile = await self.rv.extractor.extract_page(target_page)
        target_texts = [c.text for c in target_profile.claims]
        page_lang = detect_lang(target_page.text)
        yield {"type": "target", "data": TargetSummary(
            url=req.target_url, title=target_page.title,
            page_kind=target_profile.page_kind, main_content=target_profile.subtopics[:10],
            word_count=_word_count(target_page.text), image_count=_image_count(target_page.raw_html),
            claim_count=len(target_profile.claims), lang=page_lang,
        ).model_dump()}

        # 每个关键词拉 SERP（并行）
        serps = await asyncio.gather(*[
            self.serp.fetch_serp_full(kw, req.location_code, req.language_code, depth=10) for kw in kws
        ])
        autocompletes = await asyncio.gather(*[
            self.serp.fetch_autocomplete(kw, req.language_code) for kw in kws
        ])

        # 排名 + 跨词竞品去重
        ranks, comp_map = [], {}   # url_norm -> {url,title,ranks_for:set,best_rank}
        for kw, full in zip(kws, serps):
            items = full["items"]
            rk = next((it.rank for it in items if _norm(it.url) == target_norm), None)
            ranks.append(KeywordRank(keyword=kw, rank=rk, in_top_10=rk is not None and rk <= 10))
            for it in items:
                n = _norm(it.url)
                if n == target_norm:
                    continue
                e = comp_map.setdefault(n, {"url": it.url, "title": it.title,
                                            "ranks_for": set(), "best_rank": 99})
                e["ranks_for"].add(kw)
                e["best_rank"] = min(e["best_rank"], it.rank)
        yield {"type": "meta", "target_url": req.target_url,
               "keywords": [r.model_dump() for r in ranks], "unique_competitors": len(comp_map)}

        # 取核心对手：先按命中关键词数、再按最好排名，截前 N
        ordered = sorted(comp_map.values(),
                         key=lambda e: (-len(e["ranks_for"]), e["best_rank"]))[: self.s.batch_max_competitors]

        # 抓取 + 抽取 + we_lack（每个唯一 URL 只做一次，去重核心）
        sem = asyncio.Semaphore(self.s.max_concurrent_runs * 2)
        gap_pool: list[str] = []

        async def analyze(e):
            async with sem:
                try:
                    pg = await self.rv._fetch_content(e["url"], fetcher, req.use_tavily)
                    from .clients.fetch import looks_blocked
                    if _word_count(pg.text) < 50 or looks_blocked(pg.text):
                        raise ValueError("正文过少或被反爬")
                    pr = await self.rv.extractor.extract_content(pg, use_cache=True)
                except Exception as exc:
                    logger.warning("批量竞品失败 %s: %s", e["url"], exc)
                    return BatchCompetitor(url=e["url"], title=e["title"],
                                           ranks_for=sorted(e["ranks_for"]),
                                           keyword_count=len(e["ranks_for"]),
                                           best_rank=e["best_rank"], fetched=False), []
                wl = await self.rv._we_lack(pr, target_texts)
                return BatchCompetitor(
                    url=e["url"], title=e["title"], page_kind=pr.page_kind,
                    ranks_for=sorted(e["ranks_for"]), keyword_count=len(e["ranks_for"]),
                    best_rank=e["best_rank"], we_lack=wl[:8], word_count=_word_count(pg.text),
                    full_text=_clean_text(pg.text)[:20000],
                ), wl

        tasks = [asyncio.create_task(analyze(e)) for e in ordered]
        for fut in asyncio.as_completed(tasks):
            comp, wl = await fut
            gap_pool.extend(wl)
            yield {"type": "competitor", "data": comp.model_dump()}

        # 汇总缺口（按 lower 文本去重 + 计权）
        gaps = _consolidate_gaps(gap_pool)
        yield {"type": "gaps", "data": [g.model_dump() for g in gaps]}

        # 并集 LSI（合并各词的 autocomplete + PAA + related，去重后判覆盖）
        ac = _dedup_flat(autocompletes)
        paa = _dedup_flat([s["paa"] for s in serps])
        related = _dedup_flat([s["related"] for s in serps])
        lsi = await self.rv._lsi(ac, paa, related, target_page.text)
        yield {"type": "lsi", "data": lsi.model_dump()}

        # 簇级 Reddit（仅前 N 个关键词，合并去重帖）
        reddit = await self._cluster_reddit(kws[: self.s.batch_reddit_keywords],
                                            req.location_code, req.language_code, target_page.text)
        reddit_demand = []
        if reddit:
            yield {"type": "reddit", "data": reddit.model_dump()}
            reddit_demand = list(reddit.content_angles) + list(reddit.unmet_needs)

        # 综合增补段落（用页面语言）：缺口 + 缺失 LSI + Reddit 真实需求
        missing_lsi = [t.term for t in lsi.terms if not t.covered]
        gap_texts = reddit_demand + [g.text for g in gaps]
        our_main = target_profile.subtopics or [c.text for c in target_profile.claims[:10]]
        supplements = await self.rv._supplements(
            " / ".join(kws[:5]), [], gap_texts, missing_lsi, our_main,
            page_lang, target_page.text, reddit_demand,
        )
        for sp in supplements:
            yield {"type": "supplement", "data": sp.model_dump()}

        # 簇级总结
        in_top = sum(1 for r in ranks if r.in_top_10)
        summary = await self.rv._ai_summary(
            "、".join(kws),
            KeywordSemantics(intent_type="关键词簇", user_wants=[], expected_format="", summary=""),
            f"目标页在 {len(kws)} 个关键词中有 {in_top} 个进前10。",
            {"page_kind": PAGE_KIND_ZH.get(target_profile.page_kind, target_profile.page_kind),
             "word_count": _word_count(target_page.text),
             "image_count": _image_count(target_page.raw_html),
             "claim_count": len(target_profile.claims), "text_adequacy": ""},
            [g.text for g in gaps], len(missing_lsi), 0,
            reddit.summary if reddit else "", reddit.unmet_needs if reddit else None,
        )
        yield {"type": "summary", "data": summary.model_dump()}
        yield {"type": "done", "debug": {"keywords": len(kws), "unique_competitors": len(comp_map),
                                         "fetched": len(ordered)}}

    async def _cluster_reddit(self, kws, location_code, language_code, page_sample):
        """对前 N 个关键词各 collect 帖子，合并去重，做一次簇级分析。"""
        if not self.s.reddit_enabled or not kws:
            return None
        try:
            batches = await asyncio.gather(*[
                self.reddit.collect(kw, location_code, language_code) for kw in kws
            ], return_exceptions=True)
        except Exception as exc:
            logger.warning("簇级 Reddit 失败：%s", exc)
            return None
        seen, threads = set(), []
        for b in batches:
            if isinstance(b, Exception):
                continue
            for t in b:
                if t.id not in seen:
                    seen.add(t.id)
                    threads.append(t)
        if not threads:
            return None
        threads = sorted(threads, key=lambda t: t.num_comments, reverse=True)[:12]
        per = max(1000, 14000 // max(len(threads), 1))
        corpus = "\n\n---\n\n".join(t.as_text(per) for t in threads)[:14000]
        from .extraction.prompts_v2 import REDDIT_SYSTEM, build_reddit_user
        mock = {"summary": "簇级 Reddit 讨论。", "themes": [], "content_angles": [], "unmet_needs": []}
        try:
            raw = await self.rv.llm.complete_json(
                REDDIT_SYSTEM, build_reddit_user(" / ".join(kws), page_sample, corpus),
                mock=mock, model=self.s.writer_model or None)
        except Exception as exc:
            logger.warning("簇级 Reddit 分析失败：%s", exc)
            return None
        if not isinstance(raw, dict):
            return None
        themes = [RedditTheme(name=t.get("name", ""), summary=t.get("summary", ""),
                              pain_points=t.get("pain_points", []) or [], quotes=t.get("quotes", []) or [])
                  for t in raw.get("themes", []) if t.get("name")]
        return RedditInsights(
            summary=raw.get("summary", ""), themes=themes,
            content_angles=raw.get("content_angles", []) or [],
            unmet_needs=raw.get("unmet_needs", []) or [],
            thread_count=len(threads),
            comment_count=sum(len(t.top_comments) for t in threads),
            threads=[RedditThreadRef(title=t.title, url=t.url, subreddit=t.subreddit,
                                     score=t.score, num_comments=t.num_comments) for t in threads],
        )


def _consolidate_gaps(pool: list[str]) -> list[ConsolidatedGap]:
    """按小写文本归并相同缺口并计权，按权重降序。"""
    by_key: dict[str, ConsolidatedGap] = {}
    for txt in pool:
        t = (txt or "").strip()
        if not t:
            continue
        k = t.lower()
        if k in by_key:
            by_key[k].weight += 1
        else:
            by_key[k] = ConsolidatedGap(text=t, weight=1)
    return sorted(by_key.values(), key=lambda g: g.weight, reverse=True)[:40]


def _dedup_flat(lists: list[list[str]]) -> list[str]:
    seen, out = set(), []
    for lst in lists:
        for x in lst or []:
            k = (x or "").strip().lower()
            if x and k not in seen:
                seen.add(k)
                out.append(x.strip())
    return out
