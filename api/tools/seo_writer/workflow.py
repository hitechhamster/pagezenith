"""SEO 文章生成工作流（线下 Colab 工作流 V11 的 async 版）。

流程：判字数 → 搜索红海 → 主题分类 → 大纲 →（人工审批/反复改）→ 写文 →
SEO 元数据 →（可选）配图。大纲和正文都是流式产出，router 直接转成 SSE。

这里只负责编排和解析，prompt 在 prompts.py，供应商差异在 providers.py。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Optional

from ..seo_gap.config import Settings
from . import prompts as P
from .providers import LLM, generate_image, search

logger = logging.getLogger(__name__)

IMAGE_TAG = re.compile(r"\[IMAGE:\s*([^\]]+?)\s*\]")


# --------------------------------------------------------------------------- #
# 文本辅助
# --------------------------------------------------------------------------- #
def extract_h1(text: str) -> str:
    m = re.search(r"^#\s+(.+?)$", text or "", re.MULTILINE)
    return m.group(1).strip() if m else ""


def strip_markdown_inline(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\*{1,3}([^*]*)\*{1,3}", r"\1", text)
    text = text.replace("*", "").replace("`", "")
    text = re.sub(r"(?<![A-Za-z0-9])_([^_]+)_(?![A-Za-z0-9])", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def extract_image_prompts(article: str) -> list[dict[str, str]]:
    """按出现顺序取出去重后的 [IMAGE: ...] 占位符。"""
    results, seen = [], set()
    for m in IMAGE_TAG.finditer(article or ""):
        ph = m.group(0).strip()
        if ph not in seen:
            seen.add(ph)
            results.append({"placeholder": ph, "prompt": m.group(1).strip()})
    return results


def count_words(article: str) -> int:
    """英文按空格计词；中日韩按字符数计（split 对中文毫无意义）。"""
    text = IMAGE_TAG.sub("", article or "")
    cjk = len(re.findall(r"[一-鿿぀-ヿ가-힯]", text))
    latin = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", text))
    return cjk + latin


def wordcount_status(actual: int, target: int) -> tuple[str, str]:
    """返回 (等级, 说明)。等级用于前端上色：ok / warn / bad。"""
    if not target:
        return "ok", f"{actual} 词"
    ratio = actual / target
    if ratio < 0.7:
        return "bad", f"严重不足（{actual}/{target}，{ratio:.0%}）"
    if ratio < 0.85:
        return "warn", f"偏少（{actual}/{target}，{ratio:.0%}）"
    if ratio > 1.4:
        return "warn", f"偏多（{actual}/{target}，{ratio:.0%}）"
    return "ok", f"合格（{actual}/{target}，{ratio:.0%}）"


def reading_grade(article: str, language: str) -> Optional[float]:
    """Flesch-Kincaid 阅读年级。只对英文有意义，其他语言返回 None。

    与「文章质量检测」工具用的是同一个指标（textstat.flesch_kincaid_grade），
    方便两边的数字对得上。
    """
    if not (language or "").lower().startswith("english"):
        return None
    body = IMAGE_TAG.sub("", article or "")
    body = re.sub(r"^#{1,6}\s+", "", body, flags=re.MULTILINE)   # 标题不参与计算
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)          # 链接只留锚文本
    body = body.replace("**", "").replace("|", " ")
    try:
        import textstat
        return round(textstat.flesch_kincaid_grade(body), 1)
    except Exception:
        return None


def grade_verdict(grade: Optional[float]) -> tuple[str, str]:
    """(等级, 说明)。目标是美国 12 年级学生能读懂 → FK 9-12。"""
    if grade is None:
        return "ok", "非英文，不计算阅读年级"
    if grade <= 8:
        return "warn", f"FK {grade} 年级 · 偏浅，专业感可能不足"
    if grade <= 12:
        return "ok", f"FK {grade} 年级 · 12 年级学生能读懂 ✓"
    if grade <= 14:
        return "warn", f"FK {grade} 年级 · 偏难，建议润色"
    return "bad", f"FK {grade} 年级 · 太难，12 年级学生读不下来"


def _h2_count(wordcounts: int) -> str:
    if wordcounts < 1500:
        return "3-4"
    if wordcounts < 2500:
        return "4-5"
    if wordcounts < 4000:
        return "5-6"
    return "6-8"


# --------------------------------------------------------------------------- #
# 工作流
# --------------------------------------------------------------------------- #
class SEOWriter:
    def __init__(self, settings: Settings, llm: LLM, search_provider: str = "tavily"):
        self.s = settings
        self.llm = llm
        self.search_provider = search_provider

    # ------------------------------------------------------------ 字数判断
    async def infer_wordcount(self, main_keyword: str, secondary_keyword: str,
                              topic: str, specific: str, language: str) -> int:
        raw = await self.llm.complete(
            P.WORDCOUNT_PROMPT.format(
                main_keyword=main_keyword, secondary_keyword=secondary_keyword,
                topic=topic, specific=specific or "无", language=language),
            task="wordcount", temperature=0.3)
        m = re.search(r"\b(\d{3,4})\b", raw.strip())
        if m:
            return max(800, min(3000, int(m.group(1))))
        return 1800

    # ------------------------------------------------------------ 搜索红海
    async def search_context(self, main_keyword: str, secondary_keyword: str) -> tuple[str, str]:
        """两个关键词并发搜索。失败的那个会返回提示串，不阻断流程。"""
        if self.search_provider == "none":
            return "", ""
        main, sec = await asyncio.gather(
            search(self.s, self.search_provider, main_keyword),
            search(self.s, self.search_provider, secondary_keyword),
        )
        return main, sec

    # ------------------------------------------------------------ 主题分类
    async def classify_topic_type(self, main_keyword: str, topic: str) -> str:
        raw = await self.llm.complete(
            P.CLASSIFY_PROMPT.format(topic=topic, main_keyword=main_keyword),
            task="classify", temperature=0.2)
        low = raw.strip().lower()
        for t in ("realistic_product", "comparison_data", "strategy_experience",
                  "how_to", "conceptual"):
            if t in low:
                return t
        return "conceptual"

    # -------------------------------------------------------------- 大纲
    def outline_prompt(self, ctx: dict[str, Any]) -> str:
        wc = ctx["wordcounts"]
        specific = (ctx.get("specific") or "").strip()
        image_context = ""
        if ctx.get("enable_images"):
            image_context = P.IMAGE_CONTEXT.format(
                images_per_article=ctx.get("images_per_article", 2),
                image_style_hint=P.get_type_profile(ctx["topic_type"])["image_style_hint"])
        return P.OUTLINE_PROMPT.format(
            language=ctx["language"],
            specific_block=P.SPECIFIC_BLOCK.format(specific=specific) if specific else "",
            specific_reminder=P.SPECIFIC_REMINDER_OUTLINE.format(specific=specific) if specific else "",
            main_keyword=ctx["main_keyword"], secondary_keyword=ctx["secondary_keyword"],
            topic=ctx["topic"], wordcounts=wc,
            wc_min=int(wc * 0.85), wc_max=int(wc * 1.15), h2_count=_h2_count(wc),
            image_context=image_context,
            main_search_results=ctx.get("main_search", ""),
            secondary_search_results=ctx.get("sec_search", ""),
        )

    def stream_outline(self, ctx: dict[str, Any]) -> AsyncIterator[str]:
        return self.llm.stream(self.outline_prompt(ctx), task="outline")

    def stream_revise(self, ctx: dict[str, Any], feedback: str) -> AsyncIterator[str]:
        specific = (ctx.get("specific") or "").strip()
        prompt = P.REVISE_PROMPT.format(
            specific_block=P.REVISE_SPECIFIC_BLOCK.format(specific=specific) if specific else "",
            main_keyword=ctx["main_keyword"], secondary_keyword=ctx["secondary_keyword"],
            topic=ctx["topic"], wordcounts=ctx["wordcounts"], language=ctx["language"],
            current_outline=ctx.get("outline", ""), user_feedback=feedback)
        return self.llm.stream(prompt, task="revise_outline")

    # -------------------------------------------------------------- 正文
    def article_prompt(self, ctx: dict[str, Any]) -> str:
        wc = ctx["wordcounts"]
        specific = (ctx.get("specific") or "").strip()
        topic_type = ctx.get("topic_type", "conceptual")

        image_instruction = ""
        if ctx.get("enable_images"):
            image_instruction = P.IMAGE_INSTRUCTION.format(
                images_per_article=ctx.get("images_per_article", 2),
                image_style_hint=P.get_type_profile(topic_type)["image_style_hint"],
                realism_note=P.REALISM_NOTE if topic_type == "realistic_product" else "")

        return P.ARTICLE_PROMPT.format(
            language=ctx["language"],
            specific_block=P.SPECIFIC_BLOCK.format(specific=specific) if specific else "",
            specific_reminder=P.SPECIFIC_REMINDER_ARTICLE.format(specific=specific) if specific else "",
            main_keyword=ctx["main_keyword"], secondary_keyword=ctx["secondary_keyword"],
            topic=ctx["topic"], wordcounts=wc, wc_min=int(wc * 0.9), wc_max=int(wc * 1.4),
            outline=ctx.get("outline", ""),
            main_search_results=ctx.get("main_search", ""),
            secondary_search_results=ctx.get("sec_search", ""),
            image_instruction=image_instruction,
        )

    def stream_article(self, ctx: dict[str, Any]) -> AsyncIterator[str]:
        return self.llm.stream(self.article_prompt(ctx), task="article")

    # -------------------------------------------------------------- 润色
    def stream_polish(self, ctx: dict[str, Any], article: str) -> AsyncIterator[str]:
        """独立环节：整篇改写到「美国 12 年级学生能读懂」，结构一律不动。"""
        actual = count_words(article)
        prompt = P.POLISH_PROMPT.format(
            language=ctx.get("language", "English"), article=article, actual=actual,
            wc_min=int(actual * 0.9), wc_max=int(actual * 1.15))
        return self.llm.stream(prompt, task="polish")

    # ----------------------------------------------------------- SEO 元数据
    async def generate_seo(self, article: str, main_keyword: str, language: str) -> dict[str, str]:
        h1 = extract_h1(article)
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", article or "")
        clean = IMAGE_TAG.sub("", clean)[:1000]
        prompt = P.SEO_PROMPT.format(
            language=language, h1=h1 or "(未提取到)", main_keyword=main_keyword, excerpt=clean)

        seo: dict[str, str] = {}
        for _ in range(2):
            seo = _parse_seo(await self.llm.complete(prompt, task="seo"))
            if seo.get("seo_title") and seo.get("seo_description"):
                break

        for k in ("seo_title", "seo_description"):
            if seo.get(k):
                seo[k] = re.sub(r"\b(19|20)\d{2}\b", "", seo[k]).replace("  ", " ").strip()
        desc = seo.get("seo_description", "")
        for w in P.BANNED_DESC_STARTS:
            if desc.lower().startswith(w):
                desc = desc[len(w):].strip().lstrip(",.:;-").strip()
                seo["seo_description"] = (desc[0].upper() + desc[1:]) if desc else desc
                break
        if not seo.get("seo_title"):
            seo["seo_title"] = h1 or main_keyword
        if not seo.get("seo_description"):
            seo["seo_description"] = _fallback_description(article)
        return seo

    # -------------------------------------------------------------- 配图
    async def generate_images(self, article: str, topic_type: str,
                              limit: int) -> dict[str, bytes]:
        """按占位符逐张生成（串行，避免把图片模型的限流打爆）。失败的那张跳过。"""
        out: dict[str, bytes] = {}
        style = P.image_style_suffix(topic_type)
        for item in extract_image_prompts(article)[:limit]:
            png = await generate_image(self.s, item["prompt"], style)
            if png:
                out[item["placeholder"]] = png
            await asyncio.sleep(1)
        return out


# --------------------------------------------------------------------------- #
# SEO 响应解析
# --------------------------------------------------------------------------- #
def _parse_seo(response: Optional[str]) -> dict[str, str]:
    if not response:
        return {}
    text = re.sub(r"^`{3,}[a-zA-Z]*\s*", "", str(response).strip())
    text = re.sub(r"\s*`{3,}$", "", text)
    seo: dict[str, str] = {}
    m = re.search(r"\*{0,2}\s*Title\s*\*{0,2}\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        seo["seo_title"] = strip_markdown_inline(m.group(1).strip().strip("\"'"))
    m = re.search(r"\*{0,2}\s*(?:Meta\s+)?Description\s*\*{0,2}\s*[:：]\s*(.+)", text, re.IGNORECASE)
    if m:
        seo["seo_description"] = strip_markdown_inline(m.group(1).strip().strip("\"'"))
    return seo


def _fallback_description(content: str) -> str:
    text = IMAGE_TAG.sub("", content or "")
    for para in text.split("\n"):
        p = re.sub(r"[#*>`_]", "", para).strip()
        if len(p) >= 40:
            if len(p) <= 168:
                return p
            cut = p[:165]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            return cut.rstrip(",.;:") + "..."
    return ""
