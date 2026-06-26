"""站点情报侦察的数据结构。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ReconRequest(BaseModel):
    url: str


class ShopifyInfo(BaseModel):
    is_shopify: bool = False
    is_plus_hint: bool = False           # Shopify Plus 线索
    theme_name: str = ""                 # 主题名（Shopify.theme.name）
    theme_store_id: Optional[int] = None
    theme_known: str = ""                # 由 theme_store_id 推断的官方主题名
    theme_paid_hint: str = ""            # 免费/付费 线索
    products_count: Optional[int] = None  # 来自 /products.json（≥250 时标注）
    products_capped: bool = False
    earliest_product: str = ""           # 最早产品创建时间（开店时间近似）
    latest_product: str = ""             # 最近上新时间（活跃度）
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    vendors: list[str] = Field(default_factory=list)
    product_types: list[str] = Field(default_factory=list)


class AgeInfo(BaseModel):
    domain_created: str = ""             # WHOIS/RDAP 域名注册日期
    domain_age_years: Optional[float] = None
    first_archived: str = ""             # Wayback 首次存档
    first_seen_years: Optional[float] = None


class ReconReport(BaseModel):
    url: str
    final_url: str = ""
    platform: str = ""                   # Shopify / WordPress / Wix / ...
    platform_evidence: list[str] = Field(default_factory=list)
    other_tech: list[str] = Field(default_factory=list)   # WooCommerce/CDN 等附加
    shopify: ShopifyInfo = Field(default_factory=ShopifyInfo)
    age: AgeInfo = Field(default_factory=AgeInfo)
    marketing_stack: list[str] = Field(default_factory=list)  # 像素/分析/营销 App
    pixels: dict = Field(default_factory=dict)               # 具体 ID（GA4/FB/TikTok…）
    emails: list[str] = Field(default_factory=list)
    socials: list[str] = Field(default_factory=list)
    seo: dict = Field(default_factory=dict)                  # title/desc/sitemap页数/语言
    notes: list[str] = Field(default_factory=list)
