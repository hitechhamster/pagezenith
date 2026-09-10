"""有上限的 Reddit 调研 Agent 数据结构。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RedditResearchRequest(BaseModel):
    # keyword 保留兼容旧标签页/API；新页面只发 question。
    question: str = ""
    keyword: str = ""
    location_code: int = 2840
    language_code: str = "en"
    max_threads: Optional[int] = None

    def research_question(self) -> str:
        return (self.question or self.keyword).strip()


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
    # 样本中的相对讨论强度，不是 Reddit 全站统计比例。
    weight: int = 0


class ArticleIdea(BaseModel):
    title: str = ""
    target_keyword: str = ""
    intent: str = ""
    angle: str = ""
    addresses: str = ""


class ResearchChart(BaseModel):
    title: str = "样本讨论强度"
    note: str = "基于本次抓取样本，不代表 Reddit 全站比例。"
    items: list[dict] = Field(default_factory=list)


class RedditResearch(BaseModel):
    question: str = ""
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
    steps: list[ResearchStep] = Field(default_factory=list)
    searches: list[SearchRun] = Field(default_factory=list)
    themes: list[DiscussionTheme] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    article_ideas: list[ArticleIdea] = Field(default_factory=list)
    chart: ResearchChart = Field(default_factory=ResearchChart)
    threads: list[ThreadBrief] = Field(default_factory=list)
