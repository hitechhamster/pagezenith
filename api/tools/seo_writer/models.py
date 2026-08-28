"""SEO 文章生成的数据结构。

三步向导：outline（第一步）→ revise（可反复）→ article（第三步）。
参数在第一步落进服务端会话（session.py），后两步只带 session_id，
搜索结果这种大块上下文不用在浏览器和服务器之间来回搬。

2026-08 卡密化改造：
- 用户自带 key 的字段**全部移除**（服务端统一出 key，凭 X-Card-Key 头鉴权计费）
- 模型自选 → `tier`（basic / pro），具体模型由 billing.pricing 在服务端映射
- 搜索源固定 Exa（Tavily 已下线，见 providers.search）
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Tier = Literal["basic", "pro"]

LANGUAGES = [
    "English", "Indonesian", "Spanish", "French", "German", "Japanese",
    "Portuguese", "Chinese (Simplified)", "Chinese (Traditional)", "Korean", "Italian",
]


class Tiered(BaseModel):
    """所有花钱的请求都带档位；价目与模型都由服务端按它决定。"""

    tier: Tier = "basic"


class OutlineRequest(Tiered):
    """第一步：文章参数 → 搜索 → 判字数 → 分类 → 出大纲。"""

    main_keyword: str
    secondary_keyword: str
    topic: str
    specific: str = ""                     # 特殊要求（最高优先级）
    wordcounts: int = 0                    # 0 = 让 AI 判断（800-3000）
    language: str = "English"
    enable_images: bool = False
    images_per_article: int = 2
    # 推荐产品（可选）：填了就抓产品页，大纲规划推荐位、正文写成锚文本链接
    product_url: str = ""
    product_level: str = "中等介绍"     # 简短提及 | 中等介绍 | 详细介绍


class ReviseRequest(Tiered):
    """第二步：根据修改意见重出大纲。前 N 次免费，超出按点扣（见 pricing.REVISE_FREE）。"""

    session_id: str
    feedback: str
    outline: Optional[str] = None          # 会话丢失时前端回传，降级继续


class ArticleRequest(Tiered):
    """第三步：大纲通过 → 写文 → SEO 元数据 →（可选）配图 → Word。"""

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


class PolishRequest(Tiered):
    """独立润色：把已生成的文章整篇改写到「美国 12 年级学生能读懂」。

    不进第三步自动跑 —— 这是一次完整长文调用，成本和写一篇差不多，由用户自己决定要不要花。
    """

    article: str
    language: str = "English"
    session_id: Optional[str] = None
    main_keyword: Optional[str] = None      # 只用于 Word 文件名
