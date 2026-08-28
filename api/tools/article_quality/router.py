"""文章质量检测 API（前缀 /api/article-quality）。凭 X-Card-Key 卡密鉴权 + 按点计费。

2026-08：Notion 批量评审整个下架（成本放大器且非核心），只保留单篇检测。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from billing.deps import Card, charge, require_card

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
async def check(req: CheckRequest, card: Card = Depends(require_card)) -> ArticleCheck:
    s = get_settings()
    if not s.use_mocks and not s.openrouter_api_key:
        raise HTTPException(status_code=500, detail="服务端未配置 OPENROUTER_API_KEY。")
    async with charge(card, "article-quality", "check") as tx:
        try:
            out = await ArticleAnalyzer(s).check(req)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("article check failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        tx.set_result(title=(out.detected_title or "文章质量检测"),
                      summary=f"{out.overall_score} 分 · {out.grade}",
                      payload={"kind": "article-quality", **out.model_dump()})
        return out
