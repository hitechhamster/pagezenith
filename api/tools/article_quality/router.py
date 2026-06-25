"""文章质量检测 API（前缀 /api/article-quality）。key 按请求传，用完即弃。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..seo_gap.config import get_settings
from .analyzer import ArticleAnalyzer
from .models import ArticleCheck, CheckRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/article-quality", tags=["article-quality"])


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
