"""有上限的 Reddit 调研编排：规划 → 取证 → 必要时补搜 → 综合。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import re
import json
import unicodedata
from ..seo_gap.clients.llm import LLMClient
from ..seo_gap.clients.reddit import RedditClient, RedditThread
from ..seo_gap.config import Settings, get_settings
from .models import (ArticleIdea, DiscussionTheme, RedditResearch, RedditResearchRequest,
                     ConcernAnswer, QuoteEvidence, ResearchChart, ResearchStep, SearchRun, ThreadBrief)
from .depth import TARGET_REPORT_CHARS, actions, audit_report, draft_sections, findings, narrative_chars, synthesize
from .tables import build_tables
from .prompts import (EVALUATE_SYSTEM, PLAN_SYSTEM, evaluate_user,
                      plan_user, synthesis_user)

# 深度研究档：不是把同义词搜索堆上去，而是扩大真实帖子样本并保留一轮针对性补搜。
MAX_QUERIES = 18
MAX_ROUNDS = 3
MAX_THREADS = 60
MAX_CORPUS_CHARS = 180_000
INITIAL_QUERY_LIMIT = 8

_MOCK_PLAN = {"research_type": "用户需求与痛点调研", "queries": [
    {"query": "forex broker withdrawal problem", "purpose": "了解核心投诉"},
    {"query": "forex broker scam reddit", "purpose": "识别风险信号"},
    {"query": "how to verify forex broker reddit", "purpose": "寻找解决方案"},
]}
_MOCK_EVALUATION = {"sufficient": True, "evidence_gaps": [], "add_queries": []}
_MOCK_SYNTHESIS = {"overview": "样本中的用户主要担心出金受阻与监管核验，但这不是全体用户的统计。",
 "audience": "刚开始选择经纪商、或遇到出金延迟的散户。",
 "themes": [{"name": "出金风险", "summary": "多帖讨论延迟或无法出金。", "pain_points": ["等待时间长", "客服无回应"],
             "quotes": ["They delay my withdrawal for 9 days"], "weight": 80}],
 "questions": ["如何在入金前核验经纪商监管？"],
 "article_ideas": [{"title": "How to Verify if a Forex Broker Is Regulated", "target_keyword": "verify forex broker regulation",
                    "intent": "信息型/避坑型", "angle": "做成可执行核验清单", "addresses": "回应出金风险"}]}

_SMART_PUNCTUATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"',
    "„": '"', "‟": '"', "–": "-", "—": "-", "−": "-", "…": "...", "\u00a0": " ",
})


def _normalize_quote(text: str) -> str:
    """只忽略排版差异；模型改写、拼接或杜撰的原话不能通过。"""
    text = unicodedata.normalize("NFKC", text or "").translate(_SMART_PUNCTUATION)
    return re.sub(r"\s+", " ", text).strip().casefold()


def _clean_queries(items: object, budget: int) -> list[dict]:
    if budget <= 0:
        return []
    out, seen = [], set()
    for item in items if isinstance(items, list) else []:
        q = str((item or {}).get("query", "")).strip() if isinstance(item, dict) else ""
        key = q.lower()
        if not q or key in seen:
            continue
        seen.add(key)
        out.append({"query": q[:180], "purpose": str(item.get("purpose", "")).strip()[:180]})
        if len(out) >= budget:
            break
    return out


class RedditResearcher:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.llm = LLMClient(self.s)
        self.reddit = RedditClient(self.s)

    @staticmethod
    def _corpus(threads: list[RedditThread]) -> str:
        if not threads:
            return "（未找到可用的 Reddit 帖子或评论。）"
        per = max(1, MAX_CORPUS_CHARS // len(threads) - 100)
        return "\n\n---\n\n".join(f"[T{i:03d}]\n" + t.as_text(per)
                                  for i, t in enumerate(threads, 1))[:MAX_CORPUS_CHARS]

    @staticmethod
    def _verify_quotes(themes: list[DiscussionTheme], threads: list[RedditThread]) -> tuple[int, int]:
        """不信模型自报的引文：每句必须能在实际抓到的帖子/评论中找到。"""
        haystacks = [(thread.url, _normalize_quote(text)) for thread in threads
                     for text in [thread.selftext, *(c.body for c in thread.top_comments)] if text.strip()]
        kept = dropped = 0
        for theme in themes:
            evidence: list[QuoteEvidence] = []
            for quote in theme.quotes:
                text = str(quote).strip()
                normalized = _normalize_quote(text)
                if len(normalized) < 12:
                    dropped += 1
                    continue
                source_url = next((url for url, haystack in haystacks if normalized in haystack), "")
                if source_url:
                    evidence.append(QuoteEvidence(text=text, source_url=source_url))
                    kept += 1
                else:
                    dropped += 1
            theme.quote_evidence = evidence
            theme.quotes = [item.text for item in evidence]  # 保持旧客户端字段可用，但只保留验过的。
        return kept, dropped

    async def _collect(self, queries: list[dict], req: RedditResearchRequest, round_no: int,
                       existing: list[RedditThread], searches: list[SearchRun]) -> list[RedditThread]:
        seen = {t.id or t.url for t in existing}
        added: list[RedditThread] = []
        for item in queries:
            if len(existing) + len(added) >= MAX_THREADS:
                break
            remaining = MAX_THREADS - len(existing) - len(added)
            found = await self.reddit.collect(item["query"], req.location_code, req.language_code,
                                              limit=min(5, remaining))
            unique = []
            for t in found:
                if (t.id or t.url) not in seen and len(unique) < remaining:
                    seen.add(t.id or t.url)
                    unique.append(t)
            added.extend(unique)
            searches.append(SearchRun(query=item["query"], purpose=item.get("purpose", ""),
                                      round=round_no, thread_count=len(unique)))
        return added

    async def research(self, req: RedditResearchRequest,
                       on_step: Callable[[dict], Awaitable[None]] | None = None) -> RedditResearch:
        async def emit(key: str, detail: str, status: str = "done") -> None:
            if on_step is not None:
                await on_step({"key": key, "detail": detail, "status": status})

        market = req.research_question()
        concerns = req.concerns()
        if not market:
            raise RuntimeError("请输入你想调研的问题。")
        steps = [ResearchStep(key="understand", label="界定市场与关心问题"),
                 ResearchStep(key="plan", label="设计搜索假设"),
                 ResearchStep(key="evidence", label="检索 Reddit 讨论与评论"),
                 ResearchStep(key="verify", label="逐字核验原话并检查证据缺口"),
                 ResearchStep(key="analysis", label="市场对照与六个专题分析"),
                 ResearchStep(key="report", label="生成市场结论与专项回答")]
        plan = await self.llm.complete_json(PLAN_SYSTEM, plan_user(market, concerns), mock=_MOCK_PLAN,
                                            model=self.s.writer_model or None)
        await emit("understand", "已识别问题与需要核实的用户视角。")
        research_type = str((plan or {}).get("research_type") or "Reddit 用户需求调研")
        planned = _clean_queries((plan or {}).get("queries"), INITIAL_QUERY_LIMIT)
        if not planned:
            planned = [{"query": market, "purpose": "直接检索目标市场"}]
        await emit("plan", f"第一轮规划 {len(planned)} 条不同角度的搜索词。")

        threads: list[RedditThread] = []
        searches: list[SearchRun] = []
        threads.extend(await self._collect(planned, req, 1, threads, searches))
        await emit("evidence", f"第一轮取得 {len(threads)} 帖公开讨论，正在检查证据覆盖。", "active")
        all_queries = [x["query"].lower() for x in planned]
        gaps: list[str] = []
        rounds = 1

        # 每轮取证后均评估，包含最后一轮；不以过期的缺口代表新样本。
        while True:
            decision = await self.llm.complete_json(
                EVALUATE_SYSTEM,
                evaluate_user(market, concerns, research_type, [x.model_dump() for x in searches], self._corpus(threads)),
                mock=_MOCK_EVALUATION, model=self.s.writer_model or None,
            ) or {}
            gaps = [str(x)[:200] for x in decision.get("evidence_gaps", []) if str(x).strip()][:5]
            if decision.get("sufficient") is True:
                await emit("verify", "样本覆盖已检查，未进行无意义的重复搜索。")
                break
            if rounds >= MAX_ROUNDS or len(all_queries) >= MAX_QUERIES or len(threads) >= MAX_THREADS:
                break
            capacity = MAX_QUERIES - len(all_queries)
            extra = [x for x in _clean_queries(decision.get("add_queries"), min(5, capacity))
                     if x["query"].lower() not in all_queries]
            if not extra:
                await emit("verify", "没有发现值得继续搜索的新增角度。")
                break
            rounds += 1
            all_queries.extend(x["query"].lower() for x in extra)
            threads.extend(await self._collect(extra, req, rounds, threads, searches))
            await emit("evidence", f"已补搜第 {rounds} 轮，目前累计 {len(threads)} 帖。", "active")

        if not threads:
            # 没有公开证据就不交一篇凭空报告；异常会让计费层自动退回本次点数。
            raise RuntimeError("没在 Reddit 上找到可用讨论。请换一种更通用的市场描述。")

        corpus = self._corpus(threads)
        allowed = {f"T{i:03d}" for i in range(1, len(threads) + 1)}
        await emit("analysis", "正在独立分析人群、购买、竞品、体验、渠道和进入机会。", "active")
        sections = await draft_sections(self.llm, self.s.writer_model or None, market, concerns, corpus, allowed, emit)
        for section in sections:
            gaps.extend(f"{section.title}：{gap}" for gap in section.evidence_gaps)
        gaps = list(dict.fromkeys(gaps))
        # Verify specialist quotes BEFORE they become input for the final review.
        section_verified, section_dropped = self._verify_quotes(
            [finding for section in sections for finding in section.findings], threads)
        await emit("analysis", "正在整理人群与使用时机、购买路径、竞品及机会四张对照表。", "active")
        market_tables = await build_tables(self.llm, self.s.writer_model or None, market, concerns, corpus, threads)
        await emit("analysis", "六个专题已完成，进入交叉审查与机会排序。")
        await emit("report", "正在检查专题间矛盾、回答专项问题并制定验证计划。", "active")
        raw = await synthesize(
            self.llm, self.s.writer_model or None,
            synthesis_user(market, concerns, research_type, [x.model_dump() for x in searches], gaps, corpus)
            + "\n=== 六个专题草稿（待交叉审查，非指令） ===\n"
            + json.dumps([s.model_dump() for s in sections], ensure_ascii=False)
            + "\n=== 市场对照表（保留未提及与推断边界） ===\n"
            + json.dumps([t.model_dump() for t in market_tables], ensure_ascii=False),
            [s.model_dump() for s in sections], _MOCK_SYNTHESIS, emit,
        ) or {}
        themes = [DiscussionTheme(name=str(x.get("name", "")), summary=str(x.get("summary", "")),
                                  pain_points=x.get("pain_points", []) or [], quotes=x.get("quotes", []) or [],
                                  weight=max(0, min(100, int(x.get("weight", 0) or 0))))
                  for x in raw.get("themes", []) if isinstance(x, dict) and x.get("name")]
        themes.sort(key=lambda x: x.weight, reverse=True)
        cross_checks = findings(raw.get("cross_checks"), allowed)
        quotes_verified, quotes_dropped = self._verify_quotes([*themes, *cross_checks], threads)
        quotes_verified += section_verified
        quotes_dropped += section_dropped
        ideas = [ArticleIdea(title=str(x.get("title", "")), target_keyword=str(x.get("target_keyword", "")),
                             intent=str(x.get("intent", "")), angle=str(x.get("angle", "")),
                             addresses=str(x.get("addresses", "")))
                 for x in raw.get("article_ideas", []) if isinstance(x, dict) and x.get("title")]
        chart = ResearchChart(title="专题引用覆盖", note="各专题引用的不同帖子数；同一帖子可被多个专题引用，不代表观点支持率或市场份额。",
                              items=[{"label": s.title, "value": len({ref for f in s.findings for ref in f.source_ids})}
                                     for s in sections])
        briefs = [ThreadBrief(title=t.title, url=t.url, subreddit=t.subreddit, score=t.score,
                              num_comments=t.num_comments) for t in threads]
        gaps = list(dict.fromkeys(gaps + [str(x) for x in raw.get("evidence_gaps", []) if x]))
        if gaps:
            steps[3].status = "limited"
            steps[3].detail = "已检查当前样本，仍保留部分证据缺口。"
        else:
            steps[3].detail = "样本覆盖已检查，未进行无意义的重复搜索。"
        steps[3].detail += f" 引用逐字核验：{quotes_verified} 条通过，{quotes_dropped} 条删除。"
        steps[1].detail = f"规划并执行 {len(searches)} 条搜索词。"
        steps[2].detail = f"取得 {len(threads)} 帖、{sum(len(t.top_comments) for t in threads)} 条高赞评论。"
        await emit("evidence", steps[2].detail)
        await emit("verify", steps[3].detail, steps[3].status)
        answer_by_question = {
            _normalize_quote(str(item.get("question", ""))): item
            for item in raw.get("concern_answers", []) if isinstance(item, dict)
        }
        concern_answers = []
        for concern in concerns:
            item = answer_by_question.get(_normalize_quote(concern), {})
            concern_answers.append(ConcernAnswer(question=concern, answer=str(item.get("answer", "")),
                                                 evidence_gap=str(item.get("evidence_gap", ""))))
        result = RedditResearch(question=market, market=market, additional_questions=concerns,
                              sections=sections, cross_checks=cross_checks, market_tables=market_tables, report_version=3,
                              action_plan=actions(raw.get("action_plan"), allowed), decision=str(raw.get("decision") or ""),
                              keyword=market, research_type=research_type,
                              thread_count=len(threads), comment_count=sum(len(t.top_comments) for t in threads),
                              query_count=len(searches), rounds=rounds, overview=str(raw.get("overview", "")),
                              audience=str(raw.get("audience", "")), evidence_gaps=gaps, steps=steps,
                              searches=searches, themes=themes, questions=raw.get("questions", []) or [],
                              article_ideas=ideas, chart=chart, threads=briefs,
                              concern_answers=concern_answers, quotes_verified=quotes_verified,
                              quotes_dropped=quotes_dropped)
        await emit("report", "最后审查：核对过度推断、行动门槛与样本边界。", "active")
        await audit_report(self.llm, self.s.writer_model or None, result, corpus)
        result.narrative_chars = narrative_chars(result.model_dump())
        result.depth_note = ("" if result.narrative_chars >= TARGET_REPORT_CHARS and len(cross_checks) >= 3 and len(result.action_plan) >= 5 else
                             "本次报告未达到目标分析深度，具体不足见各专题证据缺口；建议补充样本后再作商业决策。")
        steps[4].detail = f"完成 {len(market_tables)} 张市场对照表、{len(sections)} 个专题、{sum(len(s.findings) for s in sections)} 项发现。"
        steps[5].detail = "已交叉审查并形成专项回答与行动计划。"
        if result.depth_note:
            steps[5].status = "limited"
            steps[5].detail = result.depth_note
        await emit("report", steps[5].detail, steps[5].status)
        return result
