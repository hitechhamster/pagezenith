"""SEO 文章生成的数据结构。

三步向导：outline（第一步）→ revise（可反复）→ article（第三步）。
参数在第一步落进服务端会话（session.py），后两步只带 session_id，
搜索结果这种大块上下文不用在浏览器和服务器之间来回搬。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

LLMProvider = Literal["openrouter", "deepseek"]
SearchProvider = Literal["tavily", "exa", "none"]

LANGUAGES = [
    "English", "Indonesian", "Spanish", "French", "German", "Japanese",
    "Portuguese", "Chinese (Simplified)", "Chinese (Traditional)", "Korean", "Italian",
]


class Keys(BaseModel):
    """用户自带 key（用完即弃，不存储、不打日志）。"""

    openrouter_key: Optional[str] = None
    deepseek_key: Optional[str] = None
    tavily_key: Optional[str] = None
    exa_key: Optional[str] = None
    serpapi_key: Optional[str] = None      # 本工具用不到，前端统一带上，这里忽略


class Providers(BaseModel):
    llm_provider: LLMProvider = "openrouter"
    llm_model: Optional[str] = None        # 留空 = 用该供应商的默认写作模型
    search_provider: SearchProvider = "tavily"


class OutlineRequest(Keys, Providers):
    """第一步：文章参数 → 搜索 → 判字数 → 分类 → 出大纲。"""

    main_keyword: str
    secondary_keyword: str
    topic: str
    specific: str = ""                     # 特殊要求（最高优先级）
    wordcounts: int = 0                    # 0 = 让 AI 判断（800-3000）
    language: str = "English"
    enable_images: bool = False
    images_per_article: int = 2
    links: list[dict] = Field(default_factory=list)   # [{url, title}]，来自 /links/parse


class ReviseRequest(Keys, Providers):
    """第二步：根据修改意见重出大纲，可反复调。"""

    session_id: str
    feedback: str
    outline: Optional[str] = None          # 会话丢失时前端回传，降级继续


class ArticleRequest(Keys, Providers):
    """第三步：大纲通过 → 选外链 → 写文 → SEO 元数据 →（可选）配图 → Word。"""

    session_id: str
    outline: Optional[str] = None          # 会话丢失时的降级入口
    # 会话丢失且前端回传时，下面这些也一并回传，避免用户白填
    main_keyword: Optional[str] = None
    secondary_keyword: Optional[str] = None
    topic: Optional[str] = None
    specific: Optional[str] = None
    wordcounts: Optional[int] = None
    language: Optional[str] = None
    enable_images: Optional[bool] = None
    images_per_article: Optional[int] = None


class LinkRow(BaseModel):
    url: str
    title: str = ""


class SelectedLink(BaseModel):
    url: str = ""
    title: str = ""
    anchor_text: str = ""
    suggested_section: str = ""
