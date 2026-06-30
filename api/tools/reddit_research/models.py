"""独立 Reddit 研究工具的数据结构。

输入一个关键词 → 自动搜 Reddit → 分析在讨论什么 / 痛点 / 常见问题，
并据此产出"写什么文章对应什么关键词"的选题建议。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RedditResearchRequest(BaseModel):
    keyword: str
    location_code: int = 2840
    language_code: str = "en"
    max_threads: Optional[int] = None
    # 用户自带 key（用完即弃）
    openrouter_key: Optional[str] = None
    serpapi_key: Optional[str] = None


class ThreadBrief(BaseModel):
    title: str = ""
    url: str = ""
    subreddit: str = ""
    score: int = 0
    num_comments: int = 0


class DiscussionTheme(BaseModel):
    name: str                                   # 讨论主题（中文）
    summary: str = ""                           # 大家在说什么（中文）
    pain_points: list[str] = Field(default_factory=list)   # 痛点/抱怨（中文）
    quotes: list[str] = Field(default_factory=list)        # 代表性原话（英文原文）
    weight: int = 0                             # 讨论热度 1-100（出现频率×互动）


class ArticleIdea(BaseModel):
    title: str = ""                             # 建议文章标题（英文，用于排名）
    target_keyword: str = ""                    # 对应目标关键词（英文）
    intent: str = ""                            # 搜索意图：信息型/对比型/导购型…
    angle: str = ""                             # 切入角度 / 为什么能打中（中文）
    addresses: str = ""                         # 回应了哪个讨论/痛点（中文）


class RedditResearch(BaseModel):
    keyword: str = ""
    thread_count: int = 0
    comment_count: int = 0
    overview: str = ""                          # 一段总览：Reddit 在这个词上整体在聊什么
    audience: str = ""                          # 人群画像/他们的处境（中文）
    themes: list[DiscussionTheme] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)     # 高频问题（适合做 FAQ/选题）
    article_ideas: list[ArticleIdea] = Field(default_factory=list)
    threads: list[ThreadBrief] = Field(default_factory=list)  # 来源帖
