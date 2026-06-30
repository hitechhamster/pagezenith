"""独立 Reddit 研究编排：collect 帖子 → 拼语料 → LLM 分析 → 结构化结果。"""

from __future__ import annotations

import logging

from ..seo_gap.clients.llm import LLMClient
from ..seo_gap.clients.reddit import RedditClient
from ..seo_gap.config import Settings, get_settings
from .models import (
    ArticleIdea, DiscussionTheme, RedditResearch, RedditResearchRequest, ThreadBrief,
)
from .prompts import RESEARCH_SYSTEM, build_research_user

logger = logging.getLogger(__name__)

# 喂给 LLM 的总语料上限（控 token + 提速；按帖均摊）
_CORPUS_MAX = 16000

_MOCK = {
    "overview": "Reddit 用户主要在讨论如何识别外汇黑平台、出金被拖延后怎么办。",
    "audience": "刚入门的散户交易者，遭遇过或担心遇到不出金的经纪商。",
    "themes": [
        {"name": "出金困难", "summary": "大量用户反映入金容易出金难。",
         "pain_points": ["出金被拖延数周", "客服已读不回"],
         "quotes": ["They delay my withdrawal for 9 days"], "weight": 80},
    ],
    "questions": ["怎么核验经纪商是否受监管？", "被黑平台骗了还能追回吗？"],
    "article_ideas": [
        {"title": "How to Verify if a Forex Broker Is Regulated (Step by Step)",
         "target_keyword": "how to verify forex broker regulation",
         "intent": "信息型/避坑型", "angle": "把核验监管牌照做成可照做的清单",
         "addresses": "回应‘出金困难’主题里反复出现的‘怎么提前识别’"},
    ],
}


class RedditResearcher:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.llm = LLMClient(self.s)
        self.reddit = RedditClient(self.s)

    async def research(self, req: RedditResearchRequest) -> RedditResearch:
        threads = await self.reddit.collect(
            req.keyword, req.location_code, req.language_code, req.max_threads
        )
        if not threads:
            raise RuntimeError("没在 Reddit 上找到相关讨论。换个更通用的英文关键词试试。")

        per = max(1500, _CORPUS_MAX // max(len(threads), 1))
        corpus = "\n\n---\n\n".join(t.as_text(per) for t in threads)[:_CORPUS_MAX]
        comment_count = sum(len(t.top_comments) for t in threads)

        raw = await self.llm.complete_json(
            RESEARCH_SYSTEM, build_research_user(req.keyword, corpus),
            mock=_MOCK, model=self.s.writer_model or None,
        )
        if not isinstance(raw, dict):
            raw = {}

        themes = [
            DiscussionTheme(
                name=t.get("name", ""), summary=t.get("summary", ""),
                pain_points=t.get("pain_points", []) or [],
                quotes=t.get("quotes", []) or [], weight=int(t.get("weight", 0) or 0),
            )
            for t in raw.get("themes", []) if t.get("name")
        ]
        themes.sort(key=lambda x: x.weight, reverse=True)
        ideas = [
            ArticleIdea(
                title=i.get("title", ""), target_keyword=i.get("target_keyword", ""),
                intent=i.get("intent", ""), angle=i.get("angle", ""),
                addresses=i.get("addresses", ""),
            )
            for i in raw.get("article_ideas", []) if i.get("title")
        ]
        briefs = [
            ThreadBrief(title=t.title, url=t.url, subreddit=t.subreddit,
                        score=t.score, num_comments=t.num_comments)
            for t in threads
        ]
        return RedditResearch(
            keyword=req.keyword, thread_count=len(threads), comment_count=comment_count,
            overview=raw.get("overview", ""), audience=raw.get("audience", ""),
            themes=themes, questions=raw.get("questions", []) or [],
            article_ideas=ideas, threads=briefs,
        )
