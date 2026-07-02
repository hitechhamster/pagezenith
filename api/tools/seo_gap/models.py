"""所有数据结构（Pydantic）。严格对应规格 §1–§5 的 JSON schema。

约定：
  - LLM 抽取层产物 = SinglePageProfile（规格 §1），其中绝不含分数。
  - 评分层产物 = InformationGain / Readability / Eeat（规格 §2–§4），分数由代码计算。
  - 最终报告 = AnalysisReport（规格 §5）。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# §1 单页 Profile（LLM 抽取，禁止给分）
# --------------------------------------------------------------------------- #
ClaimType = Literal["fact", "data_point", "opinion", "how_to_step", "definition"]


class Claim(BaseModel):
    text: str = Field(..., description="归一化后的事实/论点，一句话，去掉修辞")
    type: ClaimType
    has_source: bool = False
    is_firsthand: bool = False
    detected_lang: Optional[str] = None  # 代码侧检测（zh/en/…），非 LLM 输出


class EeatSignals(BaseModel):
    author_named: bool = False
    author_credentials: Optional[str] = None
    firsthand_markers: list[str] = Field(default_factory=list)
    outbound_citations: int = 0
    published_date: Optional[str] = None
    updated_date: Optional[str] = None
    has_about_or_contact: bool = False
    red_flags: list[str] = Field(default_factory=list)


# 页面类型（统一枚举，便于对比匹配度）
PageKind = Literal[
    "article", "product_list", "product_detail", "category",
    "homepage", "forum", "review", "tool", "other",
]
PAGE_KIND_ZH = {
    "article": "信息型文章", "product_list": "商品集合页", "product_detail": "产品详情页",
    "category": "分类页", "homepage": "首页", "forum": "论坛/问答",
    "review": "测评页", "tool": "工具页", "other": "其它",
}


class SinglePageProfile(BaseModel):
    url: str
    page_kind: PageKind = "other"
    claims: list[Claim] = Field(default_factory=list)
    subtopics: list[str] = Field(default_factory=list)
    eeat_signals: EeatSignals = Field(default_factory=EeatSignals)
    readability_notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# §2 信息增益
# --------------------------------------------------------------------------- #
class NovelPoint(BaseModel):
    text: str
    type: ClaimType


class MissingPoint(BaseModel):
    text: str
    covered_by_n_competitors: int


class InformationGain(BaseModel):
    novel_points: list[NovelPoint] = Field(default_factory=list)
    missing_points: list[MissingPoint] = Field(default_factory=list)
    redundancy_ratio: float = 0.0  # shared / 目标页总 claims
    gain_score: Optional[int] = None  # 0-100，代码计算；无关键词(单页模式)时为 None


# --------------------------------------------------------------------------- #
# §3 可读性
# --------------------------------------------------------------------------- #
class ReadabilityMetrics(BaseModel):
    flesch_reading_ease: float = 0.0
    fk_grade: float = 0.0
    avg_sentence_len: float = 0.0
    long_para_ratio: float = 0.0
    long_sentence_ratio: float = 0.0
    passive_ratio: float = 0.0
    heading_density: float = 0.0
    list_ratio: float = 0.0


ReadabilityVerdict = Literal["easier", "aligned", "harder", "n/a"]


class Readability(BaseModel):
    metrics: ReadabilityMetrics = Field(default_factory=ReadabilityMetrics)
    top10_avg: ReadabilityMetrics = Field(default_factory=ReadabilityMetrics)
    verdict: ReadabilityVerdict = "aligned"
    qualitative: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# §4 EEAT
# --------------------------------------------------------------------------- #
class SubScore(BaseModel):
    score: int = 0  # 0-100
    evidence: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class Authoritativeness(BaseModel):
    score: int = 0
    available: bool = True  # False=外链数据不可用，score 无意义
    referring_domains: int = 0
    top10_avg_referring_domains: float = 0.0
    backlinks: int = 0
    domain_rank: Optional[float] = None
    evidence: list[str] = Field(default_factory=list)


class Eeat(BaseModel):
    experience: SubScore = Field(default_factory=SubScore)
    expertise: SubScore = Field(default_factory=SubScore)
    authoritativeness: Authoritativeness = Field(default_factory=Authoritativeness)
    trust: SubScore = Field(default_factory=SubScore)


# --------------------------------------------------------------------------- #
# §5 最终报告
# --------------------------------------------------------------------------- #
class Dimension(str, Enum):
    information_gain = "information_gain"
    readability = "readability"
    eeat = "eeat"


Impact = Literal["high", "medium", "low"]


class PriorityAction(BaseModel):
    action: str
    dimension: Dimension
    impact: Impact


class AnalysisReport(BaseModel):
    keyword: Optional[str] = None
    mode: Literal["keyword", "page"] = "keyword"  # keyword=对比竞品；page=单页直接分析
    target_url: str
    in_top_10: bool = False
    rank: Optional[int] = None
    information_gain: InformationGain = Field(default_factory=InformationGain)
    readability: Readability = Field(default_factory=Readability)
    eeat: Eeat = Field(default_factory=Eeat)
    priority_actions: list[PriorityAction] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    # 调试字段：去重冲突保护统计等（用于评估 embedding 盲区，非对外指标）
    debug: Optional[dict] = None


# --------------------------------------------------------------------------- #
# 内部传递结构（非对外 schema）
# --------------------------------------------------------------------------- #
class SerpItem(BaseModel):
    url: str
    rank: int
    title: Optional[str] = None


class PageContent(BaseModel):
    url: str
    title: Optional[str] = None
    text: str = ""
    raw_html: Optional[str] = None


class BacklinkSummary(BaseModel):
    url: str
    referring_domains: int = 0
    backlinks: int = 0
    domain_rank: Optional[float] = None
    available: bool = True  # False=外链数据拉取失败/未授权，分数应标“不可用”而非 0


class AnalyzeRequest(BaseModel):
    keyword: Optional[str] = None  # 不填=单页直接分析（不对比竞品）
    target_url: str
    location_code: int = 2840  # DataForSEO: 美国
    language_code: str = "en"
    top_n: Optional[int] = None
    # 可选：手动指定竞品 URL，跳过 SERP 发现（用于 SERP 不可用或定向分析）
    competitor_urls: Optional[list[str]] = None
    # 可选：由插件直接传入当前页正文（浏览器已渲染、已过反爬），后端则不再抓目标页。
    # 解决目标站反爬 403（如 wikifx.com）的关键路径。
    target_text: Optional[str] = None
    target_title: Optional[str] = None
    # 抓取方式覆盖：httpx | browser（本地 Chromium 过反爬）。None=用配置默认
    fetch_mode: Optional[str] = None


# =========================================================================== #
# 四部分报告 v2（本地网页应用）
# =========================================================================== #
class ReportRequest(BaseModel):
    keyword: str
    target_url: str
    location_code: int = 2840
    language_code: str = "en"
    target_text: Optional[str] = None    # 网页/插件直传正文（过反爬）
    target_title: Optional[str] = None
    fetch_mode: Optional[str] = None     # 默认 browser（过反爬）
    top_n: Optional[int] = None
    use_tavily: bool = False             # 有 Tavily key 时用它解析竞品正文（更干净）
    # 用户自带 key（前端 localStorage 传入；后端用完即弃，不存不记日志）
    openrouter_key: Optional[str] = None
    serpapi_key: Optional[str] = None
    tavily_key: Optional[str] = None


# 一 · 关键词语义分析
class KeywordSemantics(BaseModel):
    intent_type: str                     # 信息型/商业型/交易型/导航型
    user_wants: list[str] = Field(default_factory=list)   # 用户期望的内容点
    expected_format: str = ""            # 期望内容形态（指南/清单/对比/案例…）
    summary: str = ""


# 二 · 前10逐页分析
class CompetitorPage(BaseModel):
    rank: int
    url: str
    title: Optional[str] = None
    page_kind: str = ""                  # 页面类型（枚举键）
    main_content: list[str] = Field(default_factory=list)  # 主要内容/子话题
    we_lack: list[str] = Field(default_factory=list)       # 它有、我们没有的点
    word_count: int = 0
    image_count: int = 0
    full_text: str = ""                  # 抓取到的竞品正文（供前端「查看原文」）
    fetched: bool = True                 # False=抓取失败/被反爬


# 三 · LSI 语义词分析
LSISource = Literal["autocomplete", "paa", "related"]


class LSITerm(BaseModel):
    term: str
    source: LSISource
    covered: bool = False                # 我们页面是否覆盖该语义


class LSIAnalysis(BaseModel):
    terms: list[LSITerm] = Field(default_factory=list)
    covered_count: int = 0
    missing_count: int = 0


# 四 · 增补段落（可直接粘贴）
class SupplementSection(BaseModel):
    heading: str                         # 建议新增的小标题
    body: str                            # 写好的中文正文段落
    reason: str                          # 为什么补（综合一二三的依据）


class TargetSummary(BaseModel):
    url: str
    title: Optional[str] = None
    page_kind: str = ""
    main_content: list[str] = Field(default_factory=list)
    word_count: int = 0
    image_count: int = 0
    claim_count: int = 0
    lang: str = ""                       # 页面主要语言（zh/en/…）
    visual_summary: str = ""             # 视觉理解（截图分析的中文描述）
    text_adequacy: str = ""              # 文字是否充足：充足/偏少/缺乏


class PageMatch(BaseModel):
    """页面类型/意图匹配分析——这关键词适合什么类型的页面，你的页面对不对路。"""
    target_kind: str = ""
    target_kind_zh: str = ""
    dominant_kind: str = ""              # 前10最主流的页面类型
    dominant_kind_zh: str = ""
    competitor_kinds: dict[str, int] = Field(default_factory=dict)  # 类型->数量
    mismatch: bool = False
    verdict: str = ""                    # 中文结论与建议


# AI 总结（页面质量评测 + 与前10差异）
class AISummary(BaseModel):
    score: int = 0                       # 综合质量分 0-100
    grade: str = ""                      # 优秀/良好/一般/较差
    quality: str = ""                    # 质量评测（一段中文）
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)   # 与前10的主要差异
    verdict: str = ""                    # 结论与最该做的事


# GEO 分析（面向生成式 AI 搜索引擎的优化评测）
class GeoDimension(BaseModel):
    name: str
    score: int = 0
    note: str = ""


class GeoAnalysis(BaseModel):
    score: int = 0
    summary: str = ""
    dimensions: list[GeoDimension] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


# Reddit 真实讨论洞察（融入内容差距分析，权重加强）
class RedditThreadRef(BaseModel):
    title: str = ""
    url: str = ""
    subreddit: str = ""
    score: int = 0
    num_comments: int = 0


class RedditTheme(BaseModel):
    name: str
    summary: str = ""
    pain_points: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)   # 英文原话


class RedditInsights(BaseModel):
    summary: str = ""                # Reddit 整体在这个词上讨论什么（中文）
    themes: list[RedditTheme] = Field(default_factory=list)
    content_angles: list[str] = Field(default_factory=list)  # 页面应覆盖、真实用户想要的角度
    unmet_needs: list[str] = Field(default_factory=list)     # 反复问但现有内容没满足的
    thread_count: int = 0
    comment_count: int = 0
    threads: list[RedditThreadRef] = Field(default_factory=list)


# =========================================================================== #
# 批量模式（关键词簇 vs 目标页）
# =========================================================================== #
class BatchReportRequest(BaseModel):
    keywords: list[str] = Field(default_factory=list)   # 最多 N 个
    target_url: str
    location_code: int = 2840
    language_code: str = "en"
    target_text: Optional[str] = None
    target_title: Optional[str] = None
    fetch_mode: Optional[str] = None
    use_tavily: bool = False
    openrouter_key: Optional[str] = None
    serpapi_key: Optional[str] = None
    tavily_key: Optional[str] = None


class KeywordRank(BaseModel):
    keyword: str
    rank: Optional[int] = None
    in_top_10: bool = False


class BatchCompetitor(BaseModel):
    url: str
    title: Optional[str] = None
    page_kind: str = ""
    ranks_for: list[str] = Field(default_factory=list)   # 命中的关键词
    keyword_count: int = 0                               # 跨词命中数（越高越是核心对手）
    best_rank: int = 99
    we_lack: list[str] = Field(default_factory=list)
    word_count: int = 0
    full_text: str = ""                  # 抓取到的竞品正文（供前端「查看原文」）
    fetched: bool = True


class ConsolidatedGap(BaseModel):
    text: str
    weight: int = 1                                      # 多少竞品覆盖了该缺口


class BatchReport(BaseModel):
    target_url: str
    keywords: list[KeywordRank] = Field(default_factory=list)
    ai_summary: Optional["AISummary"] = None
    competitors: list[BatchCompetitor] = Field(default_factory=list)
    gaps: list[ConsolidatedGap] = Field(default_factory=list)
    lsi: Optional["LSIAnalysis"] = None
    reddit: Optional["RedditInsights"] = None
    supplements: list["SupplementSection"] = Field(default_factory=list)
    target: Optional["TargetSummary"] = None
    debug: Optional[dict] = None


class ReportV2(BaseModel):
    keyword: str
    target_url: str
    rank: Optional[int] = None
    in_top_10: bool = False
    ai_summary: Optional[AISummary] = None
    keyword_semantics: KeywordSemantics
    page_match: Optional[PageMatch] = None
    geo: Optional[GeoAnalysis] = None
    reddit: Optional[RedditInsights] = None
    competitors: list[CompetitorPage] = Field(default_factory=list)
    target: TargetSummary
    lsi: LSIAnalysis
    supplements: list[SupplementSection] = Field(default_factory=list)
    debug: Optional[dict] = None
