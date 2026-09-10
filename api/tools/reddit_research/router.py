"""独立 Reddit 研究 API（前缀 /api/reddit-research）。凭 X-Card-Key 卡密鉴权 + 按点计费。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

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


async def _research(req: RedditResearchRequest, card: Card,
                    on_step: Callable[[dict], Awaitable[None]] | None = None) -> RedditResearch:
    if not req.research_question():
        raise HTTPException(status_code=400, detail="请输入想调研的问题。")
    s = _settings_for(req)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        async with charge(card, "reddit-research", "run") as tx:
            try:
                out = await RedditResearcher(s).research(req, on_step=on_step)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("reddit research failed")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            tx.set_result(title=f"Reddit 调研：{req.research_question()}", summary="",
                          payload={"kind": "reddit-research", **out.model_dump()})
            return out


@router.post("/analyze", response_model=RedditResearch)
async def analyze(req: RedditResearchRequest,
                  card: Card = Depends(require_card)) -> RedditResearch:
    """兼容已有客户端的非流式接口。"""
    return await _research(req, card)


@router.post("/analyze_stream")
async def analyze_stream(req: RedditResearchRequest,
                         card: Card = Depends(require_card)):
    """给左右栏 Agent UI 的真实阶段事件流。"""
    async def events():
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def on_step(event: dict) -> None:
            await queue.put({"type": "step", **event})

        async def work() -> None:
            try:
                out = await _research(req, card, on_step=on_step)
                await queue.put({"type": "result", "data": out.model_dump()})
            except Exception as exc:  # 错误也经 SSE 正常交给页面，计费层已自动退款。
                logger.exception("reddit research stream failed")
                await queue.put({"type": "error", "message": str(exc)})
            finally:
                await queue.put(None)

        task = asyncio.create_task(work())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    return
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
