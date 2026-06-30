"""独立 Reddit 研究 API（前缀 /api/reddit-research）。key 按请求传，用完即弃。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..seo_gap.config import get_settings
from .analyzer import RedditResearcher
from .models import RedditResearch, RedditResearchRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reddit-research", tags=["reddit-research"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


def _settings_for(req: RedditResearchRequest):
    s = get_settings().with_keys(req.openrouter_key, req.serpapi_key)
    if not s.use_mocks:
        if not s.openrouter_api_key:
            raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
        if not s.serpapi_key:
            raise HTTPException(status_code=400, detail="缺少 SerpApi Key（用于发现 Reddit 帖子），请在设置里填写。")
    return s


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model,
            "reddit_enabled": s.reddit_enabled}


@router.post("/analyze", response_model=RedditResearch)
async def analyze(req: RedditResearchRequest) -> RedditResearch:
    if not req.keyword.strip():
        raise HTTPException(status_code=400, detail="请输入关键词。")
    s = _settings_for(req)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        try:
            return await RedditResearcher(s).research(req)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("reddit research failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
