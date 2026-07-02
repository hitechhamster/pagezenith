"""外链拓客工具的数据结构。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class OutreachRequest(BaseModel):
    keyword: str = ""                    # 领域/主题词；留空则从 your_url 推断
    your_url: str = ""                   # 你想要外链的页面/网站（用于邮件个性化）
    your_brief: str = ""                 # 一句话：你能提供什么/为何值得链接
    location_code: int = 2840
    language_code: str = "en"
    generate_emails: bool = True         # 是否为有邮箱的站生成外联邮件草稿
    breadth: str = "standard"            # 搜索广度：standard | wide | max
    max_prospects: Optional[int] = None  # 显式覆盖上限（一般由 breadth 决定）
    openrouter_key: Optional[str] = None
    serpapi_key: Optional[str] = None


class ProspectEmail(BaseModel):
    address: str
    confidence: str = "中"               # 高/中/低


class Prospect(BaseModel):
    domain: str
    url: str = ""                        # 找到该站的页面
    title: str = ""
    site_type: str = ""                  # 博客/资源页/新闻/目录/论坛/公司站/其它
    relevance: int = 0                   # 0-100 与主题相关度
    opportunity: str = ""                # 投稿/资源位加链/合作/断链替换/跳过
    reason: str = ""                     # 中文一句：为什么是机会/为什么跳过
    emails: list[ProspectEmail] = Field(default_factory=list)
    has_form: bool = False               # 只有联系表单、没抓到邮箱
    email_subject: str = ""              # 生成的外联邮件主题（英文）
    email_body: str = ""                 # 生成的外联邮件正文（英文）
    fetched: bool = True
