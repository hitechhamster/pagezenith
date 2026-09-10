"""有上限的 Reddit 调研 Agent 数据结构。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class RedditResearchRequest(BaseModel):
    # market + additional_questions 是新版市场调研入口。question / keyword 保留兼容旧客户端。
    market: str = ""
    additional_questions: list[str] = Field(default_factory=list)
    question: str = ""
    keyword: str = ""
    location_code: int = 2840
    language_code: str = "en"
    max_threads: Optional[int] = None

    def research_question(self) -> str:
        return (self.market or self.question or self.keyword).strip()

    def concerns(self) -> list[str]:
        """去掉空项和重复项，避免同一关切被重复检索。"""
        out, seen = [], set()
        for item in self.additional_questions:
            text = str(item).strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text[:300])
        return out[:5]


class ThreadBrief(BaseModel):
    title: str = ""
    url: str = ""
    subreddit: str = ""
    score: int = 0
    num_comments: int = 0


class ResearchStep(BaseModel):
    key: str
    label: str
    detail: str = ""
    status: str = "done"  # done | limited


class SearchRun(BaseModel):
    query: str
    purpose: str = ""
    round: int = 1
    thread_count: int = 0


class DiscussionTheme(BaseModel):
    name: str
    summary: str = ""
    pain_points: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    quote_evidence: list["QuoteEvidence"] = Field(default_factory=list)
    # 样本中的相对讨论强度，不是 Reddit 全站统计比例。
    weight: int = 0


class ArticleIdea(BaseModel):
    title: str = ""
    target_keyword: str = ""
    intent: str = ""
    angle: str = ""
    addresses: str = ""


class QuoteEvidence(BaseModel):
    """经机器逐字核验过的原话及其实际来源。"""
    text: str
    source_url: str


class ConcernAnswer(BaseModel):
    question: str
    answer: str = ""
    evidence_gap: str = ""


class ResearchChart(BaseModel):
    title: str = "样本讨论强度"
    note: str = "基于本次抓取样本，不代表 Reddit 全站比例。"
    items: list[dict] = Field(default_factory=list)


class ResearchFinding(BaseModel):
    title: str
    observation: str = ""
    interpretation: str = ""
    action: str = ""
    caveat: str = ""
    source_ids: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    quote_evidence: list[QuoteEvidence] = Field(default_factory=list)


class ResearchSection(BaseModel):
    key: str
    title: str
    introduction: str = ""
    findings: list[ResearchFinding] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)


class ResearchAction(BaseModel):
    priority: str = ""
    task: str = ""
    rationale: str = ""
    test: str = ""
    success_signal: str = ""
    stop_signal: str = ""
    source_ids: list[str] = Field(default_factory=list)


class MarketCell(BaseModel):
    text: str = "样本未提及"
    basis: Literal["sample", "inference", "unknown"] = "unknown"
    source_ids: list[str] = Field(default_factory=list)
    quote_evidence: list[QuoteEvidence] = Field(default_factory=list)


class MarketRow(BaseModel):
    cells: dict[str, MarketCell] = Field(default_factory=dict)


class MarketTable(BaseModel):
    key: str
    title: str
    columns: dict[str, str] = Field(default_factory=dict)
    rows: list[MarketRow] = Field(default_factory=list)
    evidence_gap: str = ""


class RedditResearch(BaseModel):
    report_version: int = 2
    market_tables: list[MarketTable] = Field(default_factory=list)
    sections: list[ResearchSection] = Field(default_factory=list)
    cross_checks: list[ResearchFinding] = Field(default_factory=list)
    action_plan: list[ResearchAction] = Field(default_factory=list)
    decision: str = ""
    depth_note: str = ""
    narrative_chars: int = 0
    question: str = ""
    market: str = ""
    additional_questions: list[str] = Field(default_factory=list)
    # 兼容旧客户端与历史结果。
    keyword: str = ""
    research_type: str = "用户需求与痛点调研"
    thread_count: int = 0
    comment_count: int = 0
    query_count: int = 0
    rounds: int = 1
    overview: str = ""
    audience: str = ""
    evidence_gaps: list[str] = Field(default_factory=list)
    concern_answers: list[ConcernAnswer] = Field(default_factory=list)
    quotes_verified: int = 0
    quotes_dropped: int = 0
    steps: list[ResearchStep] = Field(default_factory=list)
    searches: list[SearchRun] = Field(default_factory=list)
    themes: list[DiscussionTheme] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    article_ideas: list[ArticleIdea] = Field(default_factory=list)
    chart: ResearchChart = Field(default_factory=ResearchChart)
    threads: list[ThreadBrief] = Field(default_factory=list)
