"""四部分报告(v2)编排：关键词语义 / 前10分析 / LSI / 增补段落。

复用：SerpApi（SERP+PAA+相关搜索+下拉）、BrowserFetcher（过反爬）、Extractor（抽取）、
SemanticDeduper（覆盖/缺口判定）。所有文本中文。
"""

from __future__ import annotations

import asyncio
import logging
import re

from .clients.browser_fetch import BrowserFetcher
from .clients.fetch import PageFetcher, looks_blocked
from .clients.llm import LLMClient
from .clients.serpapi import SerpApiClient
from .config import Settings, get_settings
from .extraction.extractor import Extractor
from .extraction.prompts_v2 import (
    LSI_SYSTEM, SEMANTICS_SYSTEM, SUPPLEMENT_SYSTEM,
    build_lsi_user, build_semantics_user, build_supplement_user,
)
from .lexical import detect_lang
from .models import (
    PAGE_KIND_ZH, AISummary, CompetitorPage, GeoAnalysis, GeoDimension, KeywordSemantics,
    LSIAnalysis, LSITerm, PageContent, PageMatch, ReportRequest, ReportV2,
    SupplementSection, TargetSummary,
)
from .scoring.semantic import SemanticDeduper

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[A-Za-z0-9]+")


def _norm(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/").lower()


def _word_count(text: str) -> int:
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


def _image_count(html: str | None) -> int:
    return html.lower().count("<img") if html else 0


class ReportV2Builder:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.llm = LLMClient(self.s)
        self.serp = SerpApiClient(self.s)
        self.extractor = Extractor(self.llm, PageFetcher(self.s))
        self.deduper = SemanticDeduper(self.llm)

    def _make_fetcher(self, req: ReportRequest):
        mode = req.fetch_mode or "browser"  # v2 默认浏览器抓取，过反爬
        return BrowserFetcher(self.s) if mode == "browser" else PageFetcher(self.s)

    async def build(self, req: ReportRequest) -> ReportV2:
        """非流式：消费 build_stream 拼成完整 ReportV2（供 /report 和测试用）。"""
        meta, parts = {}, {"competitors": [], "supplements": []}
        async for ev in self.build_stream(req):
            t = ev["type"]
            if t == "meta":
                meta = ev
            elif t == "semantics":
                parts["semantics"] = KeywordSemantics(**ev["data"])
            elif t == "target":
                parts["target"] = TargetSummary(**ev["data"])
            elif t == "competitor":
                parts["competitors"].append(CompetitorPage(**ev["data"]))
            elif t == "page_match":
                parts["page_match"] = PageMatch(**ev["data"])
            elif t == "geo":
                parts["geo"] = GeoAnalysis(**ev["data"])
            elif t == "summary":
                parts["ai_summary"] = AISummary(**ev["data"])
            elif t == "lsi":
                parts["lsi"] = LSIAnalysis(**ev["data"])
            elif t == "supplement":
                parts["supplements"].append(SupplementSection(**ev["data"]))
            elif t == "done":
                parts["debug"] = ev.get("debug")
            elif t == "error":
                raise RuntimeError(ev["message"])
        parts["competitors"].sort(key=lambda c: c.rank)
        return ReportV2(
            keyword=meta["keyword"], target_url=meta["target_url"],
            rank=meta.get("rank"), in_top_10=meta.get("in_top_10", False),
            ai_summary=parts.get("ai_summary"),
            keyword_semantics=parts["semantics"], page_match=parts.get("page_match"),
            geo=parts.get("geo"), competitors=parts["competitors"],
            target=parts["target"], lsi=parts["lsi"], supplements=parts["supplements"],
            debug=parts.get("debug"),
        )

    async def build_stream(self, req: ReportRequest):
        """流式：逐块产出事件（meta/semantics/target/competitor*/lsi/supplement*/done），
        让前端“分析一个显示一个”。"""
        fetcher = self._make_fetcher(req)
        try:
            async for ev in self._build_stream(req, fetcher):
                yield ev
        finally:
            await fetcher.aclose()

    async def _build_stream(self, req: ReportRequest, fetcher):
        top_n = req.top_n or self.s.report_competitors
        target_norm = _norm(req.target_url)

        # SERP + PAA + 相关搜索 + 下拉
        full, autocomplete = await asyncio.gather(
            self.serp.fetch_serp_full(req.keyword, req.location_code, req.language_code, depth=10),
            self.serp.fetch_autocomplete(req.keyword, req.language_code),
        )
        items = full["items"]
        rank = next((it.rank for it in items if _norm(it.url) == target_norm), None)
        comp_items = [it for it in items if _norm(it.url) != target_norm][:top_n]
        yield {"type": "meta", "keyword": req.keyword, "target_url": req.target_url,
               "rank": rank, "in_top_10": rank is not None and rank <= 10,
               "total_competitors": len(comp_items)}

        # 一 关键词语义（仅用标题，尽早产出）
        titles = [it.title or "" for it in comp_items]
        semantics = await self._keyword_semantics(req.keyword, titles, [])
        yield {"type": "semantics", "data": semantics.model_dump()}

        # 目标页（抓正文 + 截图）；we_lack 需要先有目标 claims
        target_page, target_png = await self._get_target(req, fetcher)
        target_profile = await self.extractor.extract_page(target_page)
        target_texts = [c.text for c in target_profile.claims]
        page_lang = detect_lang(target_page.text)
        # 视觉理解页面类型（截图 → 多模态）
        visual = await self._vision(target_png)
        target_kind = visual.get("page_kind") or target_profile.page_kind
        yield {"type": "target", "data": TargetSummary(
            url=req.target_url, title=target_page.title, page_kind=target_kind,
            main_content=target_profile.subtopics[:10],
            word_count=_word_count(target_page.text),
            image_count=_image_count(target_page.raw_html),
            claim_count=len(target_profile.claims), lang=page_lang,
            visual_summary=visual.get("visual_summary", ""),
            text_adequacy=visual.get("text_adequacy", ""),
        ).model_dump()}

        # 二 竞品：抓取成功一个就分析并产出一个
        missing_pool: list[str] = []
        comp_kinds: list[str] = []

        async def analyze(it):
            try:
                pg = await fetcher.fetch(it.url)
                if _word_count(pg.text) < 50 or looks_blocked(pg.text):  # 拦截页/空壳 → 失败
                    raise ValueError("正文过少或被反爬拦截")
                pr = await self.extractor.extract_content(pg, use_cache=True)
            except Exception as exc:
                logger.warning("竞品失败 %s: %s", it.url, exc)
                return CompetitorPage(rank=it.rank, url=it.url, title=it.title, fetched=False), []
            wl = await self._we_lack(pr, target_texts)
            return CompetitorPage(
                rank=it.rank, url=it.url, title=it.title, page_kind=pr.page_kind,
                main_content=pr.subtopics[:8], we_lack=wl[:8],
                word_count=_word_count(pg.text), image_count=_image_count(pg.raw_html),
            ), wl

        ok = 0
        tasks = [asyncio.create_task(analyze(it)) for it in comp_items]
        for fut in asyncio.as_completed(tasks):
            cp, wl = await fut
            if cp.fetched:
                ok += 1
                comp_kinds.append(cp.page_kind)
            missing_pool.extend(wl)
            yield {"type": "competitor", "data": cp.model_dump()}

        # 页面类型匹配（你的页面类型 vs 前10主流）
        pm = self._page_match(target_kind, comp_kinds, target_profile, visual)
        yield {"type": "page_match", "data": pm.model_dump()}

        # 三 LSI
        lsi = await self._lsi(autocomplete, full["paa"], full["related"], target_page.text)
        yield {"type": "lsi", "data": lsi.model_dump()}

        # GEO 分析（面向生成式 AI 搜索引擎）
        geo = await self._geo(req.keyword, target_page, target_profile)
        yield {"type": "geo", "data": geo.model_dump()}

        # 四 增补段落（用页面语言写）
        missing_lsi = [t.term for t in lsi.terms if not t.covered]
        our_main = target_profile.subtopics or [c.text for c in target_profile.claims[:10]]
        supplements = await self._supplements(
            req.keyword, semantics.user_wants, missing_pool, missing_lsi, our_main,
            page_lang, target_page.text,
        )
        for sp in supplements:
            yield {"type": "supplement", "data": sp.model_dump()}

        # AI 总结（最后综合，前端置顶显示）
        summary = await self._ai_summary(
            req.keyword, semantics, pm.verdict,
            {"page_kind": PAGE_KIND_ZH.get(target_kind, target_kind),
             "word_count": _word_count(target_page.text),
             "image_count": _image_count(target_page.raw_html),
             "claim_count": len(target_profile.claims),
             "text_adequacy": visual.get("text_adequacy", "")},
            missing_pool, len(missing_lsi), geo.score,
        )
        yield {"type": "summary", "data": summary.model_dump()}

        yield {"type": "done", "debug": {"fetched_competitors": ok, "total_competitors": len(comp_items)}}

    # ---- 子步骤 ----
    async def _get_target(self, req: ReportRequest, fetcher):
        """返回 (PageContent, png|None)。插件直传正文时无截图。"""
        if req.target_text and req.target_text.strip():
            return PageContent(url=req.target_url, title=req.target_title, text=req.target_text), None
        try:
            page, png = await fetcher.capture(req.target_url)
        except Exception as exc:
            raise RuntimeError(f"目标页抓取失败（{exc}）。可勾选浏览器抓取或确认网址可访问。") from exc
        if looks_blocked(page.text) or _word_count(page.text) < 30:
            raise RuntimeError("目标页被反爬拦截或正文为空。试试勾选浏览器抓取，或换个能正常打开的网址。")
        return page, png

    async def _vision(self, png) -> dict:
        """截图 → 多模态判页面类型/视觉主体/文字是否充足。无截图或失败则返回空。"""
        if not png:
            return {}
        from .extraction.prompts_v2 import VISION_PROMPT
        mock = {"page_kind": "article", "visual_summary": "长文配图", "text_adequacy": "充足"}
        try:
            r = await self.llm.vision_json(VISION_PROMPT, png, mock=mock)
            return r if isinstance(r, dict) else {}
        except Exception as exc:
            logger.warning("视觉分析失败：%s", exc)
            return {}

    def _page_match(self, target_kind, comp_kinds, target_profile, visual) -> PageMatch:
        from collections import Counter
        counts = Counter(k for k in comp_kinds if k)
        dominant = counts.most_common(1)[0][0] if counts else ""
        zh = PAGE_KIND_ZH
        content_kinds = {"article", "review", "forum"}
        # 不匹配：对手主流是内容型，而你是商品/分类/首页型
        mismatch = bool(
            dominant in content_kinds and target_kind not in content_kinds and target_kind
        )
        tkzh = zh.get(target_kind, target_kind or "未知")
        dkzh = zh.get(dominant, dominant or "未知")
        if mismatch:
            adq = visual.get("text_adequacy") or ""
            verdict = (f"⚠️ 页面类型不匹配：你的页面是【{tkzh}】，前10中多数是【{dkzh}】。"
                       f"该关键词的搜索意图偏向内容型页面，{tkzh}很难直接排上去"
                       f"（正文{('确实'+adq) if adq else '通常偏少'}）。"
                       f"建议：单独新建一篇信息型内容页针对该词，或在本页顶部加入实质性的图文内容板块；"
                       f"光补零散文字难以扭转类型层面的差距。")
        elif target_kind and dominant and target_kind == dominant:
            verdict = f"✓ 页面类型匹配：你和前10主流都是【{tkzh}】，方向正确，按下方差距补内容即可。"
        else:
            verdict = f"你的页面类型【{tkzh}】，前10主流【{dkzh}】，请结合搜索意图判断是否需要调整页面形态。"
        return PageMatch(
            target_kind=target_kind, target_kind_zh=tkzh,
            dominant_kind=dominant, dominant_kind_zh=dkzh,
            competitor_kinds={zh.get(k, k): v for k, v in counts.items()},
            mismatch=mismatch, verdict=verdict,
        )

    async def _keyword_semantics(self, keyword, titles, subtopics) -> KeywordSemantics:
        mock = {"intent_type": "信息型", "user_wants": ["识别黑平台", "核验监管", "被骗后处理"],
                "expected_format": "识别清单+案例", "summary": f"用户想了解{keyword}"}
        raw = await self.llm.complete_json(
            SEMANTICS_SYSTEM, build_semantics_user(keyword, titles, subtopics), mock=mock
        )
        return KeywordSemantics(**raw)

    async def _we_lack(self, comp_profile, target_texts: list[str]) -> list[str]:
        """竞品 claims 中，目标页没有覆盖的（语义匹配）。"""
        comp_texts = [c.text for c in comp_profile.claims]
        if not comp_texts or not target_texts:
            return comp_texts[:8]
        matches = await self.deduper.match_many(comp_texts, target_texts)
        return [t for t, m in zip(comp_texts, matches) if not m.is_shared]

    async def _lsi(self, autocomplete, paa, related, page_text) -> LSIAnalysis:
        seen, terms = set(), []
        for src, lst in (("autocomplete", autocomplete), ("paa", paa), ("related", related)):
            for t in lst:
                key = t.strip().lower()
                if t.strip() and key not in seen:
                    seen.add(key)
                    terms.append(LSITerm(term=t.strip(), source=src))
        if not terms:
            return LSIAnalysis()
        mock = [{"term": t.term, "relevant": True, "covered": i % 2 == 0} for i, t in enumerate(terms)]
        cov = await self.llm.complete_json(
            LSI_SYSTEM, build_lsi_user(page_text, [t.term for t in terms]), mock=mock
        )
        info = {c.get("term"): c for c in cov} if isinstance(cov, list) else {}
        # 只保留与本页主题相关的词，过滤掉歧义带来的无关词（如券商页里的 steakhouse）
        kept = []
        for t in terms:
            c = info.get(t.term, {})
            if c and c.get("relevant") is False:
                continue
            t.covered = bool(c.get("covered"))
            kept.append(t)
        covered = sum(1 for t in kept if t.covered)
        return LSIAnalysis(terms=kept, covered_count=covered, missing_count=len(kept) - covered)

    async def _geo(self, keyword, target_page, target_profile):
        from .extraction.prompts_v2 import GEO_SYSTEM, build_geo_user
        sig = target_profile.eeat_signals
        has_schema = bool(target_page.raw_html and "application/ld+json" in target_page.raw_html.lower())
        mock = {"score": 62, "summary": "结构尚可，缺少可引用数据与 Schema",
                "dimensions": [{"name": "可直接提取的答案", "score": 60, "note": "结论不够开门见山"}],
                "recommendations": ["开头加一句直接定义", "补 FAQ Schema"]}
        raw = await self.llm.complete_json(
            GEO_SYSTEM,
            build_geo_user(keyword, target_page.text, has_schema, sig.author_named,
                           sig.outbound_citations, sig.updated_date or sig.published_date or ""),
            mock=mock,
        )
        if not isinstance(raw, dict):
            return GeoAnalysis()
        dims = [GeoDimension(**d) for d in raw.get("dimensions", []) if d.get("name")]
        return GeoAnalysis(score=int(raw.get("score", 0) or 0), summary=raw.get("summary", ""),
                           dimensions=dims, recommendations=raw.get("recommendations", []))

    async def _ai_summary(self, keyword, semantics, page_match_verdict, target_info,
                          missing_pool, lsi_missing, geo_score):
        from .extraction.prompts_v2 import SUMMARY_SYSTEM, build_summary_user
        mock = {"score": 58, "grade": "一般", "quality": "内容方向对路但深度不足。",
                "strengths": ["主题相关"], "gaps": ["字数偏少", "缺权威信号"],
                "verdict": "优先补齐基本盘内容并增加可信信号。"}
        raw = await self.llm.complete_json(
            SUMMARY_SYSTEM,
            build_summary_user(keyword, semantics.model_dump(), page_match_verdict,
                               target_info, missing_pool, lsi_missing, geo_score),
            mock=mock, model=self.s.writer_model or None,
        )
        return AISummary(**raw) if isinstance(raw, dict) else AISummary()

    async def _supplements(self, keyword, user_wants, missing_points, missing_lsi, our_main,
                           page_lang="zh", page_sample=""):
        mock = [{"heading": "如何核验监管牌照", "body": "在 FCA/ASIC/NFA 官网输入牌照号即可核验……",
                 "reason": "竞品覆盖+LSI缺失"}]
        raw = await self.llm.complete_json(
            SUPPLEMENT_SYSTEM,
            build_supplement_user(keyword, user_wants, missing_points, missing_lsi, our_main,
                                  page_lang, page_sample),
            mock=mock,
            model=self.s.writer_model or None,  # 可单独用更强的写作模型
        )
        out = []
        for s in (raw if isinstance(raw, list) else []):
            if s.get("heading") and s.get("body"):
                out.append(SupplementSection(
                    heading=s["heading"], body=s["body"], reason=s.get("reason", "")
                ))
        return out
