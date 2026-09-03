"""SEO 文章生成工作流（线下 Colab 工作流 V11 的 async 版）。

流程：判字数 → 搜索红海 → 主题分类 → 大纲 →（人工审批/反复改）→ 写文 →
SEO 元数据 →（可选）配图。大纲和正文都是流式产出，router 直接转成 SSE。

这里只负责编排和解析，prompt 在 prompts.py，供应商差异在 providers.py。
"""

from __future__ import annotations

import logging

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Optional

from ..seo_gap.config import Settings
from . import prompts as P
from . import prose_audit
from .providers import LLM, generate_image, search
from .voices import get_voice

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


#: 给大纲/正文的写作目标 = 用户目标 × 这个系数。
#: 润色（尤其全量档）必然会压缩篇幅 —— 实测三篇分别掉到 78%/87%/100%，
#: 按用户目标写就一定偏少。多写两成，润色完刚好落在目标附近。
#: **只放大给写作用**，`wordcount_status` 的验收仍按用户原始目标算。
WRITING_TARGET_RATIO = 1.2


def writing_target(wordcounts: int) -> int:
    """写作阶段用的字数目标（比用户目标高两成，留给润色压缩）。"""
    return int(round((wordcounts or 0) * WRITING_TARGET_RATIO)) or wordcounts


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
def _product_instructions(ctx: dict[str, Any]) -> str:
    """正文里的产品推荐指令。没填产品就返回空串（prompt 里那一段直接消失）。"""
    url = (ctx.get("product_url") or "").strip()
    if not url:
        return ""
    return P.PRODUCT_INSTRUCTIONS.format(
        product_title=ctx.get("product_title") or "the product",
        product_url=url,
        level=ctx.get("product_level") or P.DEFAULT_PRODUCT_LEVEL,
    )


def _voice_instructions(ctx: dict[str, Any], stage: str) -> str:
    """写手文风块。stage ∈ outline/article/polish。

    三个阶段都要注入，缺一不可：大纲不给就按通用结构规划、正文不给就没文风、
    **润色不给会把文风整篇抹平**（润色是整篇重写）。

    没选写手时：outline/polish 返回空串（那两处的 prompt 片段直接消失），
    article 返回改造前那句硬编码文风 —— 保证"不选写手"与老版本产物一致。
    """
    v = get_voice(ctx.get("voice"))
    # 规则为空也算"没选" —— clark（平衡档）三段规则就是空的，它 ≡ 原版文风。
    # 不这样兜底的话会拼出一个「写作声音」标题下面什么都没有的空块。
    rules = (v or {}).get(stage, "").strip() if v else ""
    if not rules:
        return P.ARTICLE_VOICE_DEFAULT if stage == "article" else ""
    if stage == "outline":
        return P.OUTLINE_VOICE_BLOCK.format(voice_outline_rules=rules)
    if stage == "polish":
        return P.POLISH_VOICE_BLOCK.format(voice_polish_rules=rules)
    return P.ARTICLE_VOICE_BLOCK.format(voice_article_rules=rules)


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

    # ------------------------------------------------------- 社媒（Reddit）
    async def reddit_context(self, main_keyword: str, limit: int | None = None) -> str:
        """抓 Reddit 上关于这个关键词的真实讨论（帖子 + 高赞评论）。

        为什么值得单独跑一路：全网搜索拿到的是**已经写好的竞品文章**，
        它们彼此高度同质；Reddit 拿到的是**真人原话** —— 高频痛点、
        被反复问却没人好好回答的问题，正是"独特价值点"的来源。
        成本极低（搜索侧不计费），失败也不阻断流程。
        """
        try:
            from ..seo_gap.clients.reddit import RedditClient
            threads = await RedditClient(self.s).collect(main_keyword, limit=limit)
        except Exception as exc:  # noqa: BLE001  社媒拿不到不该拖垮整篇文章
            logger.warning("Reddit 抓取失败（已跳过）: %s", exc)
            return ""
        if not threads:
            return ""
        per = max(800, self.s.reddit_max_chars_per_thread // 2)
        blocks = [t.as_text(per) for t in threads[: self.s.reddit_max_threads]]
        return "\n\n---\n\n".join(b for b in blocks if b.strip())

    # ------------------------------------------------------------ 产品信息
    async def product_info(self, url: str) -> dict[str, str]:
        """抓推荐产品页正文（原版用 Tavily extract，这里改 Serper /scrape）。"""
        url = (url or "").strip()
        if not url:
            return {}
        if self.s.use_mocks:
            return {"title": "Mock Product", "content": "（mock 产品正文）", "url": url}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=self.s.request_timeout, trust_env=False,
                                         proxy=self.s.proxy_for("serper")) as c:
                r = await c.post(f"{self.s.serper_base_url}/scrape",
                                 headers={"X-API-KEY": self.s.serper_key,
                                          "Content-Type": "application/json"},
                                 json={"url": url})
                r.raise_for_status()
                j = r.json()
            text = (j.get("text") or "")[:1000]
            title = ((j.get("metadata") or {}).get("title")
                     or (j.get("metadata") or {}).get("og:title") or "")
            if not text:
                return {}
            return {"title": title, "url": url, "content": text}
        except Exception as exc:  # noqa: BLE001
            logger.warning("产品页抓取失败（已跳过）: %s", exc)
            return {}

    # ------------------------------------------------------------ 事实清单
    async def extract_facts(self, ctx: dict[str, Any]) -> str:
        """从搜索/社媒资料里抽出带出处的可核实事实。失败返回空串，不阻断流程。

        放在大纲之前：大纲和正文都拿这份清单当唯一数字来源，避免同一系统对同一事实
        每次给不同答案（实测同主题三版里 Mailchimp 分群门槛 2000→1000、
        「Shopify Email」被写成不存在的「Shopify Messaging」）。

        用 utility 槽位（flash-lite）—— 这是抽取不是创作，不值得用贵模型。
        """
        corpus_len = len(ctx.get("main_search") or "") + len(ctx.get("sec_search") or "")
        if corpus_len < 500:                      # 没资料就没清单，别花这次调用
            return ""
        try:
            raw = await self.llm.complete(
                P.FACTS_PROMPT.format(
                    topic=ctx.get("topic", ""),
                    main_keyword=ctx.get("main_keyword", ""),
                    secondary_keyword=ctx.get("secondary_keyword", ""),
                    main_search_results=ctx.get("main_search", ""),
                    secondary_search_results=ctx.get("sec_search", ""),
                    reddit_context=ctx.get("reddit_context", "") or ""),
                task="facts", temperature=0.1)
        except Exception:  # noqa: BLE001  抽不出事实不该拖垮整篇文章
            logger.warning("事实清单抽取失败（已跳过）", exc_info=True)
            return ""
        return (raw or "").strip()

    @staticmethod
    def facts_block(ctx: dict[str, Any]) -> str:
        """事实清单的 prompt 片段；没有清单就返回空串（那一段直接消失）。"""
        facts = (ctx.get("facts") or "").strip()
        return P.FACTS_BLOCK.format(facts=facts) if facts else ""

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
        """组装大纲 prompt（2026-08 换成用户实际在用的 EEAT 版）。

        与旧版的差异：{specific} 直接注入（空则 prompt 内部自行忽略）、
        新增 {product_context}/{reddit_context}、不再需要 wc_min/h2_count。
        """
        specific = (ctx.get("specific") or "").strip()
        reddit = (ctx.get("reddit_context") or "").strip()
        product_block = ""
        if (ctx.get("product_url") or "").strip():
            lvl = ctx.get("product_level") or P.DEFAULT_PRODUCT_LEVEL
            product_block = P.PRODUCT_CONTEXT.format(
                product_title=ctx.get("product_title") or "Unknown",
                product_url=ctx["product_url"],
                product_content=(ctx.get("product_content") or "")[:500],
                level=lvl,
                level_instruction=P.PRODUCT_DETAIL_LEVELS.get(lvl, P.PRODUCT_DETAIL_LEVELS[P.DEFAULT_PRODUCT_LEVEL]),
            )
        image_context = ""
        if ctx.get("enable_images"):
            image_context = P.IMAGE_CONTEXT.format(
                images_per_article=ctx.get("images_per_article", 2),
                image_style_hint=P.get_type_profile(ctx["topic_type"])["image_style_hint"])
        return P.OUTLINE_PROMPT.format(
            language=ctx["language"],
            specific=specific,
            product_context=product_block,
            reddit_context=P.REDDIT_CONTEXT.format(reddit=reddit) if reddit else "",
            facts_block=self.facts_block(ctx),
            image_context=image_context,
            voice_outline=_voice_instructions(ctx, "outline"),
            main_keyword=ctx["main_keyword"], secondary_keyword=ctx["secondary_keyword"],
            topic=ctx["topic"], wordcounts=writing_target(ctx["wordcounts"]),
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
        # 写作目标比用户目标高两成，留给润色压缩（见 WRITING_TARGET_RATIO）
        wc = writing_target(ctx["wordcounts"])
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
            specific=specific or "（无）",
            product_instructions=_product_instructions(ctx),
            facts_block=self.facts_block(ctx),
            main_keyword=ctx["main_keyword"], secondary_keyword=ctx["secondary_keyword"],
            wordcounts=wc,
            outline=ctx.get("outline", ""),
            main_search_results=ctx.get("main_search", ""),
            secondary_search_results=ctx.get("sec_search", ""),
            image_instruction=image_instruction,
            voice_article=_voice_instructions(ctx, "article"),
        )

    def stream_article(self, ctx: dict[str, Any]) -> AsyncIterator[str]:
        return self.llm.stream(self.article_prompt(ctx), task="article")

    # -------------------------------------------------------------- 润色
    @staticmethod
    def outline_section_diff(old: str, new: str) -> dict:
        """改大纲前后 H2/H3 的增删。给用户看「这次改掉了哪些节」。

        为什么要有它：修订是整篇重生成，不是编辑。实测连续三版里，
        Etsy 最有用的「按数据诊断」节、Klaviyo 的三个具体 flow 都在下一版无声消失。
        在「锁定节」功能做出来之前，至少让用户看到删了什么，能拒绝。
        """
        def heads(t: str) -> list[str]:
            return [h.strip() for h in re.findall(r"^#{2,3}\s+(.+?)\s*$", t or "", re.M)]
        o, n = heads(old), heads(new)
        norm = lambda h: re.sub(r"[^a-z0-9一-鿿]+", " ", h.lower()).strip()
        on, nn = {norm(h): h for h in o}, {norm(h): h for h in n}
        return {
            "removed": [on[k] for k in on if k not in nn],
            "added": [nn[k] for k in nn if k not in on],
            "kept": sum(1 for k in on if k in nn),
        }

    @staticmethod
    def polish_added_numbers(before: str, after: str) -> list[str]:
        """润色**新增**了哪些数字。空列表 = 合规。

        润色的职责是删/拆/重排/换说法，**不该往里加信息**。给它"加"的权限一定出事：
        2026-09-02 实测，一条「具体压过抽象」的润色规则催生出
        「That 3 to 4 months window」「that 20 characters limit」这类把数字硬塞进
        指代短语的病句（正文 0 处 → 润色 4 处）。

        prompt 是软约束，这里是硬闸门 —— 以后任何润色规则想再往里塞数字都会被这条拦下。
        只查带信息量的数字（金额/百分比/四位以上），序数和小整数是行文自然会用的。
        """
        pat = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b\d{4,}\b")
        norm = lambda s: re.sub(r"[\s$%,]", "", s)
        had = {norm(x) for x in pat.findall(before or "")}
        out, seen = [], set()
        for m in pat.findall(after or ""):
            n = norm(m)
            if n and n not in had and n not in seen:
                seen.add(n)
                out.append(m.strip())
        return out

    @staticmethod
    def polish_broke_structure(before: str, after: str) -> str:
        """润色是否破坏了结构？返回空串=没问题，否则返回人话原因。

        只查**可数且必须守恒**的东西 —— 标题层级数、表格、链接、图片占位符。
        字数和句子当然会变，那是润色的本职。"""
        def counts(md: str) -> dict[str, int]:
            return {
                "H1": len(re.findall(r"^#\s", md, re.M)),
                "H2": len(re.findall(r"^##\s", md, re.M)),
                "H3": len(re.findall(r"^###\s", md, re.M)),
                "表格行": md.count("\n|"),
                "链接": len(re.findall(r"\[[^\]]+\]\(https?://", md)),
                "图片占位符": len(IMAGE_TAG.findall(md)) if IMAGE_TAG else 0,
            }
        b, a = counts(before), counts(after)
        bad = []
        for k in ("H1", "H2", "H3"):
            if a[k] < b[k]:                      # 只罚"丢失"，多了不算错
                bad.append(f"{k} 从 {b[k]} 个变成 {a[k]} 个")
        for k in ("表格行", "链接", "图片占位符"):
            if b[k] and a[k] < b[k] * 0.8:       # 允许小幅波动（表格行会因换行差一两行）
                bad.append(f"{k} 从 {b[k]} 掉到 {a[k]}")
        return "；".join(bad)


    @staticmethod
    def polish_mode(article: str, language: str) -> tuple[str, str]:
        """决定这篇要「全量重写」还是「轻润色」。返回 (mode, 人话理由)。

        全量润色是把文章往「12 年级能读懂」压，输入本来就达标时它只会帮倒忙 ——
        2026-09-02 实测：原文 FK 10.6（已在 9-12 带内）被压到 7.4「偏浅」，
        字数掉 27% 触发「偏少」告警。所以先看输入水平再决定力度。

        英文用 FK；中文没有 FK，用平均句长代替（阈值与 article_quality 的 zh 分支一致）。
        判不出来就走 full —— 保持改造前的行为，不确定时不要少干活。
        """
        # 太短就别判了：FK 在几十个词上没有意义，空文本还会算出 0.0 被误判成「已达标」。
        if count_words(article) < 200:
            return "full", "正文过短，判不了"
        if (language or "").lower().startswith("english"):
            g = reading_grade(article, language)
            if g is None:
                return "full", "拿不到阅读年级"
            # >12 = grade_verdict 的「偏难」起点，只有这时才值得整篇重写
            return ("full", f"FK {g} 偏难") if g > 12 else ("light", f"FK {g} 已达标")
        if prose_audit.is_cjk(article):
            sents = prose_audit.split_sentences(article, True)
            if not sents:
                return "full", "切不出句子"
            avg = sum(len(s) for s in sents) / len(sents)
            vlong = sum(1 for s in sents if len(s) > 60)
            if avg > 45 or vlong > len(sents) * 0.1:
                return "full", f"平均句长 {avg:.0f} 字 偏难"
            return "light", f"平均句长 {avg:.0f} 字 已达标"
        return "full", "非中英文"

    def stream_polish(self, ctx: dict[str, Any], article: str,
                      strict: bool = False) -> AsyncIterator[str]:
        """润色。两档力度：

        - **full**：整篇改写到「12 年级能读懂」（输入偏难时）
        - **light**：只修体检点名的地方，篇幅和措辞尽量不动（输入已达标时）

        两档都先跑确定性文风体检（prose_audit，零成本），把**这篇文章的具体违规**
        拼进 prompt。规则留在 prompt 里，体检负责指出位置 —— 模型不用自己在两千词里
        找哪句超长、哪几句同一个词开头。

        strict=True（结构被破坏后的重试）一律走 full 模板 —— 那条重试路径的护栏
        写在 POLISH_STRICT_RETRY 里，换模板会把它丢掉。
        """
        audit_block = ""
        try:
            body = prose_audit.to_prompt_block(prose_audit.audit(article))
            if body:
                audit_block = P.POLISH_AUDIT_BLOCK.format(audit_findings=body)
        except Exception:  # noqa: BLE001  体检挂了不能拖垮润色
            logger.warning("文风体检失败（已跳过）", exc_info=True)

        language = ctx.get("language", "English")
        mode, why = ("full", "结构重试") if strict else self.polish_mode(article, language)
        logger.info("润色力度=%s（%s）", mode, why)

        template = P.POLISH_PROMPT if mode == "full" else P.POLISH_LIGHT_PROMPT
        prompt = template.format(
            language=language, article=article,
            main_keyword=ctx.get("main_keyword", ""),
            secondary_keyword=ctx.get("secondary_keyword", ""),
            audit_findings=audit_block,
            preserve_instructions=(P.POLISH_PRESERVE_LINKS
                                   if (ctx.get("product_url") or "").strip() else ""),
            voice_polish=_voice_instructions(ctx, "polish"))
        if strict:
            prompt += P.POLISH_STRICT_RETRY
        return self.llm.stream(prompt, task="polish")

    # ----------------------------------------------------------- SEO 元数据
    async def generate_seo(self, article: str, main_keyword: str, language: str) -> dict[str, str]:
        h1 = extract_h1(article)
        clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", article or "")
        clean = IMAGE_TAG.sub("", clean)[:1000]
        prompt = P.SEO_PROMPT.format(
            language=language, main_keyword=main_keyword, excerpt=clean)

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
    async def generate_images(self, article: str, topic_type: str, limit: int,
                              image_style: str = "auto") -> dict[str, bytes]:
        """按占位符逐张生成（串行，避免把图片模型的限流打爆）。失败的那张跳过。

        image_style 由用户选；"auto" 走按 topic_type 自动判断的老逻辑。
        """
        out: dict[str, bytes] = {}
        style = P.image_style_suffix(topic_type, image_style)
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
