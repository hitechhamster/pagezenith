"""文章质量检测编排。复用 seo_gap 的 LLM/抓取/语言识别（公共层后续再抽）。"""

from __future__ import annotations

import logging
import re

import textstat

from ..seo_gap.clients.browser_fetch import BrowserFetcher
from ..seo_gap.clients.fetch import PageFetcher, looks_blocked
from ..seo_gap.config import Settings, get_settings
from ..seo_gap.clients.llm import LLMClient
from ..seo_gap.lexical import detect_lang
from ..seo_gap.models import PageContent
from .models import (
    ArticleCheck, CheckRequest, DensityResult, HardSentenceRewrite, ReadabilityStat,
    TitleDim, TitleResult, TitleSuggestion, WaterySegment,
)
from .prompts import (
    DENSITY_SYSTEM, REWRITE_SYSTEM, TITLE_SYSTEM,
    build_density_user, build_rewrite_user, build_title_user,
)

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[一-鿿]")
_EN_SENT = re.compile(r"[^.!?]+[.!?]+|\S[^.!?]*$")
_ZH_SENT = re.compile(r"[^。！？；\n]+[。！？；]?")
_PASSIVE = re.compile(r"\b(is|are|was|were|be|been|being)\b\s+\w+ed\b", re.I)
_ADVERB = re.compile(r"\b\w{3,}ly\b", re.I)
_END_PUNCT = "。.!?！？；;"


# --------------------------------------------------------------------------- #
# 标题识别 + 正文切分
# --------------------------------------------------------------------------- #
def detect_title(text: str, explicit: str | None) -> tuple[str, str, bool]:
    """返回 (title, body, detected)。优先 explicit → markdown # → 首行启发式。"""
    if explicit and explicit.strip():
        return explicit.strip(), text.strip(), True
    lines = text.strip().splitlines()
    if not lines:
        return "", "", False
    first = lines[0].strip()
    rest = "\n".join(lines[1:]).strip()
    if first.startswith("#"):
        return first.lstrip("#").strip(), rest, True
    is_zh = bool(_CJK.search(first))
    short = (len(first) <= 30) if is_zh else (len(first.split()) <= 12)
    if first and short and first[-1] not in _END_PUNCT:
        return first, rest, True
    return "", text.strip(), False


def _sentences(text: str, lang: str) -> list[str]:
    pat = _ZH_SENT if lang == "zh" else _EN_SENT
    return [s.strip() for s in pat.findall(text) if s.strip()]


def _word_count(text: str, lang: str) -> int:
    return len(_CJK.findall(text)) if lang == "zh" else len(re.findall(r"[A-Za-z0-9']+", text))


# --------------------------------------------------------------------------- #
# 可读性（代码）
# --------------------------------------------------------------------------- #
def readability(body: str, lang: str) -> tuple[ReadabilityStat, list[str]]:
    sents = _sentences(body, lang)
    wc = _word_count(body, lang)
    if lang == "zh":
        lens = [len(s) for s in sents]
        long_n = sum(1 for n in lens if n > 40)
        vlong_n = sum(1 for n in lens if n > 60)
        verdict = "偏难" if (vlong_n or (lens and sum(lens) / len(lens) > 45)) else \
                  ("适中" if long_n else "易读")
        grade = None
        mins = round(wc / 300, 1)
    else:
        lens = [len(s.split()) for s in sents]
        long_n = sum(1 for n in lens if n > 20)
        vlong_n = sum(1 for n in lens if n > 30)
        try:
            grade = round(textstat.flesch_kincaid_grade(body), 1)
        except Exception:
            grade = None
        verdict = "偏难" if (grade and grade >= 12) else ("适中" if (grade and grade >= 9) else "易读")
        mins = round(wc / 200, 1)
    stat = ReadabilityStat(
        language=lang, grade=grade, verdict=verdict, reading_minutes=mins,
        word_count=wc, sentence_count=len(sents),
        long_sentences=long_n, very_long_sentences=vlong_n,
        passive=len(_PASSIVE.findall(body)) if lang == "en" else 0,
        adverbs=len(_ADVERB.findall(body)) if lang == "en" else 0,
    )
    # 最难读的若干句（最长）拿去 LLM 改写
    hard = sorted(sents, key=lambda s: (len(s) if lang == "zh" else len(s.split())), reverse=True)
    threshold = 60 if lang == "zh" else 30
    hard = [s for s in hard if (len(s) if lang == "zh" else len(s.split())) > (40 if lang == "zh" else 20)]
    return stat, hard[:5]


def _readability_score(stat: ReadabilityStat) -> int:
    return {"易读": 88, "适中": 68, "偏难": 45}.get(stat.verdict, 65)


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
async def fetch_article(url: str, fetch_mode: str | None, s: Settings) -> tuple[str, str]:
    """抓取网址正文，返回 (title, text)。供「粘贴链接」用。"""
    fetcher = BrowserFetcher(s) if (fetch_mode or "browser") == "browser" else PageFetcher(s)
    try:
        page = await fetcher.fetch(url)
    finally:
        await fetcher.aclose()
    if looks_blocked(page.text) or len(page.text) < 50:
        raise RuntimeError("该网址被反爬拦截或正文为空，请直接粘贴正文。")
    return (page.title or ""), page.text


class ArticleAnalyzer:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.llm = LLMClient(self.s)

    async def _get_text(self, req: CheckRequest) -> str:
        if req.text and req.text.strip():
            return req.text
        if req.url:
            fetcher = BrowserFetcher(self.s) if (req.fetch_mode or "browser") == "browser" else PageFetcher(self.s)
            try:
                page: PageContent = await fetcher.fetch(req.url)
            finally:
                await fetcher.aclose()
            if looks_blocked(page.text) or len(page.text) < 50:
                raise RuntimeError("URL 抓取被反爬拦截或正文为空，请改为直接粘贴正文。")
            return (page.title + "\n" + page.text) if page.title else page.text
        raise RuntimeError("请粘贴文章正文，或填写可抓取的 URL。")

    async def check(self, req: CheckRequest) -> ArticleCheck:
        text = await self._get_text(req)
        title, body, detected = detect_title(text, req.title)
        lang = req.language_code or detect_lang(body or text)
        lang = "zh" if lang == "zh" else "en"

        stat, hard = readability(body, lang)

        title_res = await self._title(title, body, lang, detected)
        density_res = await self._density(body, lang)
        rewrites = await self._rewrites(hard, lang)

        # 综合分（代码）
        r_score = _readability_score(stat)
        overall = round(r_score * 0.4 + density_res.score * 0.35 + title_res.score * 0.25)
        grade = "优秀" if overall >= 80 else "良好" if overall >= 65 else "一般" if overall >= 50 else "较差"
        actions = self._priority(stat, density_res, title_res, r_score)
        verdict = f"综合 {overall} 分（{grade}）：可读性{stat.verdict}、信息密度{density_res.score}、标题{title_res.score}。"

        return ArticleCheck(
            language=lang, detected_title=title, overall_score=overall, grade=grade,
            verdict=verdict, priority_actions=actions, readability=stat,
            rewrites=rewrites, density=density_res, title=title_res,
        )

    async def _title(self, title, body, lang, detected) -> TitleResult:
        mock = {"score": 60, "dims": [{"name": "吸引力", "score": 55, "note": "偏平淡"}],
                "issues": ["缺少具体利益点"], "suggestions": [{"title": "更好的标题示例", "why": "更具体"}]}
        raw = await self.llm.complete_json(TITLE_SYSTEM, build_title_user(title, body, lang), mock=mock)
        if not isinstance(raw, dict):
            return TitleResult(title=title, detected=detected)
        return TitleResult(
            title=title, detected=detected, score=int(raw.get("score", 0) or 0),
            dims=[TitleDim(**d) for d in raw.get("dims", []) if d.get("name")],
            issues=raw.get("issues", []),
            suggestions=[TitleSuggestion(**s) for s in raw.get("suggestions", []) if s.get("title")],
        )

    async def _density(self, body, lang) -> DensityResult:
        mock = {"score": 58, "info_points": 6, "summary": "信息尚可但有铺垫",
                "watery": [{"quote": "众所周知……", "issue": "空泛", "suggestion": "删去"}]}
        raw = await self.llm.complete_json(DENSITY_SYSTEM, build_density_user(body, lang), mock=mock)
        if not isinstance(raw, dict):
            return DensityResult()
        wc = max(_word_count(body, lang), 1)
        pts = int(raw.get("info_points", 0) or 0)
        return DensityResult(
            score=int(raw.get("score", 0) or 0), info_points=pts,
            per_100w=round(pts / wc * 100, 1), summary=raw.get("summary", ""),
            watery=[WaterySegment(**w) for w in raw.get("watery", []) if w.get("quote")],
        )

    async def _rewrites(self, hard, lang) -> list[HardSentenceRewrite]:
        if not hard:
            return []
        mock = [{"original": s, "rewrite": s} for s in hard]
        raw = await self.llm.complete_json(REWRITE_SYSTEM, build_rewrite_user(hard, lang), mock=mock)
        out = []
        for r in (raw if isinstance(raw, list) else []):
            if r.get("original") and r.get("rewrite"):
                out.append(HardSentenceRewrite(original=r["original"], rewrite=r["rewrite"]))
        return out

    def _priority(self, stat, density, title, r_score) -> list[str]:
        items = []
        scored = sorted([("可读性", r_score), ("信息密度", density.score), ("标题", title.score)],
                        key=lambda x: x[1])
        for name, sc in scored:
            if sc >= 75:
                continue
            if name == "可读性":
                items.append(f"可读性{stat.verdict}：拆分 {stat.very_long_sentences} 个超长句"
                             + ("、减少被动语态与副词" if stat.language == "en" else ""))
            elif name == "信息密度":
                items.append(f"信息密度偏低（{density.score}）：精简注水段落，补充具体数据/案例")
            else:
                items.append(f"标题吸引力偏弱（{title.score}）：从下方改写建议里挑一个更具体的")
        return items[:3]
