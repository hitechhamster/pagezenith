"""文章质量检测 API（前缀 /api/article-quality）。key 按请求传，用完即弃。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..seo_gap.config import get_settings
from .analyzer import ArticleAnalyzer, fetch_article
from .models import ArticleCheck, CheckRequest, FetchRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/article-quality", tags=["article-quality"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


@router.post("/fetch")
async def fetch(req: FetchRequest) -> dict:
    """抓取网址正文供编辑器载入（无需 key）。"""
    s = get_settings()
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        try:
            title, text = await fetch_article(req.url, req.fetch_mode, s)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"title": title, "text": (title + "\n\n" + text) if title else text}


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model}


@router.post("/check", response_model=ArticleCheck)
async def check(req: CheckRequest) -> ArticleCheck:
    s = get_settings().with_keys(req.openrouter_key, None)
    if not s.use_mocks and not s.openrouter_api_key:
        raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
    try:
        return await ArticleAnalyzer(s).check(req)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("article check failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
