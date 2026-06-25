"""文章质量检测的数据结构。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CheckRequest(BaseModel):
    text: str = ""                      # 粘贴的正文（可含首行/# 标题）
    title: Optional[str] = None         # 可选：手动指定/确认的标题
    url: Optional[str] = None           # 可选：抓取该网址正文
    language_code: Optional[str] = None  # 留空=自动识别
    fetch_mode: Optional[str] = None
    # 用户自带 key（用完即弃，不存不记日志）
    openrouter_key: Optional[str] = None


# ---- 可读性（后端代码计算，用于综合分；前端另有实时高亮）----
class ReadabilityStat(BaseModel):
    language: str = "en"
    grade: Optional[float] = None       # 英文 FK 年级
    verdict: str = ""                   # 易读/适中/偏难
    reading_minutes: float = 0.0
    word_count: int = 0
    sentence_count: int = 0
    long_sentences: int = 0             # 长句
    very_long_sentences: int = 0        # 超长句
    passive: int = 0
    adverbs: int = 0


class HardSentenceRewrite(BaseModel):
    original: str
    rewrite: str


# ---- 信息密度 ----
class WaterySegment(BaseModel):
    quote: str                          # 注水原文（节选）
    issue: str                          # 问题
    suggestion: str                     # 精简建议


class DensityResult(BaseModel):
    score: int = 0                      # 0-100
    info_points: int = 0                # 实质信息点数
    per_100w: float = 0.0               # 每百词信息点
    summary: str = ""
    watery: list[WaterySegment] = Field(default_factory=list)


# ---- 标题吸引力 ----
class TitleDim(BaseModel):
    name: str
    score: int = 0
    note: str = ""


class TitleSuggestion(BaseModel):
    title: str
    why: str


class TitleResult(BaseModel):
    title: str = ""
    detected: bool = True               # 是否自动识别到标题
    score: int = 0
    dims: list[TitleDim] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    suggestions: list[TitleSuggestion] = Field(default_factory=list)


class ArticleCheck(BaseModel):
    language: str = "en"
    detected_title: str = ""
    overall_score: int = 0
    grade: str = ""                     # 优秀/良好/一般/较差
    verdict: str = ""
    priority_actions: list[str] = Field(default_factory=list)
    readability: ReadabilityStat = Field(default_factory=ReadabilityStat)
    rewrites: list[HardSentenceRewrite] = Field(default_factory=list)
    density: DensityResult = Field(default_factory=DensityResult)
    title: TitleResult = Field(default_factory=TitleResult)
