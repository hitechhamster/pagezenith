"""单页抽取：抓正文 → LLM → SinglePageProfile。竞品走缓存。"""

from __future__ import annotations

from ..cache import ProfileCache, get_cache
from ..clients.fetch import PageFetcher
from ..clients.llm import LLMClient
from ..lexical import detect_lang
from ..models import PageContent, SinglePageProfile
from .prompts import EXTRACTION_SYSTEM, build_extraction_user


class Extractor:
    def __init__(
        self,
        llm: LLMClient | None = None,
        fetcher: PageFetcher | None = None,
        cache: ProfileCache | None = None,
    ):
        self.llm = llm or LLMClient()
        self.fetcher = fetcher or PageFetcher()
        self.cache = cache or get_cache()

    async def extract_page(self, content: PageContent) -> SinglePageProfile:
        from ..config import get_settings
        user = build_extraction_user(
            content.url, content.title, content.text, output_lang=get_settings().output_lang
        )
        raw = await self.llm.chat_json(EXTRACTION_SYSTEM, user)
        raw.setdefault("url", content.url)
        profile = SinglePageProfile.model_validate(raw)
        # 代码侧补语言（非 LLM 输出，确定性）
        for claim in profile.claims:
            claim.detected_lang = detect_lang(claim.text)
        return profile

    async def extract_content(self, content: PageContent, use_cache: bool = False) -> SinglePageProfile:
        """从已抓取的 PageContent 抽取（避免重复抓取），按 url 缓存。"""
        if use_cache:
            cached = self.cache.get(content.url)
            if cached is not None:
                return cached
        profile = await self.extract_page(content)
        if use_cache:
            self.cache.set(content.url, profile)
        return profile

    async def extract_url(self, url: str, use_cache: bool = False) -> SinglePageProfile:
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached
        content = await self.fetcher.fetch(url)
        profile = await self.extract_content(content, use_cache=use_cache)
        return profile
