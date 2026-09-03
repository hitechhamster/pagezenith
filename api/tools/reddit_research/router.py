"""独立 Reddit 研究 API（前缀 /api/reddit-research）。凭 X-Card-Key 卡密鉴权 + 按点计费。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from billing.deps import Card, charge, require_card

from ..seo_gap.config import get_settings
from .analyzer import RedditResearcher
from .models import RedditResearch, RedditResearchRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/reddit-research", tags=["reddit-research"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


def _settings_for(req: RedditResearchRequest):
    s = get_settings()
    if not s.use_mocks:
        if not s.has_llm_key():
            raise HTTPException(status_code=500, detail="服务端未配置 LLM API Key（GEMINI_API_KEY 或 OPENROUTER_API_KEY）。")
        if not s.serp_key():
            raise HTTPException(status_code=500, detail="服务端未配置 SERPER_KEY。")
    return s


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model_name(),
            "reddit_enabled": s.reddit_enabled}


@router.post("/analyze", response_model=RedditResearch)
async def analyze(req: RedditResearchRequest,
                  card: Card = Depends(require_card)) -> RedditResearch:
    if not req.keyword.strip():
        raise HTTPException(status_code=400, detail="请输入关键词。")
    s = _settings_for(req)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        async with charge(card, "reddit-research", "run") as tx:
            try:
                out = await RedditResearcher(s).research(req)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("reddit research failed")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            tx.set_result(title=f"Reddit 选题：{req.keyword}", summary="",
                          payload={"kind": "reddit-research", **out.model_dump()})
            return out
