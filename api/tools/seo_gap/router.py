"""seo-gap 工具的 API 路由（前缀 /api/seo-gap）。

- 凭 X-Card-Key 卡密鉴权 + 按点计费；key 由服务端统一出。
- 并发上限保护服务器资源（每个分析要抓多页竞品）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from billing.deps import Card, charge, require_card
from billing.pricing import price as _price

from .config import get_settings
from .models import BatchReportRequest, ReportRequest, ReportV2
from .report_batch import BatchReportBuilder
from .report_v2 import ReportV2Builder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/seo-gap", tags=["seo-gap"])

# 同时进行的分析数上限（公开部署防资源/账单失控）
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


def _settings_for(req: ReportRequest):
    """服务端统一出 key；缺 key 是站长的配置问题。"""
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
            "free_slots": _sema._value}


@router.post("/report", response_model=ReportV2)
async def report(req: ReportRequest, card: Card = Depends(require_card)) -> ReportV2:
    s = _settings_for(req)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        async with charge(card, "seo-gap", "analyze") as tx:
            try:
                out = await ReportV2Builder(s).build(req)
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception("report failed")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            tx.set_result(title=f"内容差距：{getattr(req, 'keyword', '') or ''}", summary="",
                          payload={"kind": "seo-gap", **out.model_dump()})
            return out


@router.post("/report_stream")
async def report_stream(req: ReportRequest, card: Card = Depends(require_card)):
    """流式四部分报告（SSE）：逐块产出，前端分析一个显示一个。"""
    s = _settings_for(req)

    async def gen():
        if _sema.locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '服务繁忙，请稍后重试'}, ensure_ascii=False)}\n\n"
            return
        async with _sema:
            async with charge(card, "seo-gap", "analyze") as tx:
                events = []
                try:
                    async for ev in ReportV2Builder(s).build_stream(req):
                        events.append(ev)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except Exception as exc:
                    logger.exception("report_stream failed")
                    yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
                    raise
                tx.set_result(title=f"内容差距：{getattr(req, 'keyword', '') or ''}",
                              summary=f"{len(events)} 条事件",
                              payload={"kind": "seo-gap", "events": events[-200:]})

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _settings_for_batch(req: BatchReportRequest):
    s = get_settings()
    if not s.use_mocks:
        if not s.has_llm_key():
            raise HTTPException(status_code=500, detail="服务端未配置 LLM API Key（GEMINI_API_KEY 或 OPENROUTER_API_KEY）。")
        if not s.serp_key():
            raise HTTPException(status_code=500, detail="服务端未配置 SERPER_KEY。")
    return s


@router.post("/batch_stream")
async def batch_stream(req: BatchReportRequest, card: Card = Depends(require_card)):
    """批量模式（关键词簇 vs 目标页）流式报告（SSE）。按关键词数计点。"""
    s = _settings_for_batch(req)
    n_kw = max(1, len(getattr(req, "keywords", []) or []))
    cost = _price("seo-gap", "analyze") * n_kw

    async def gen():
        if _sema.locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '服务繁忙，请稍后重试'}, ensure_ascii=False)}\n\n"
            return
        async with _sema:
            async with charge(card, "seo-gap", "analyze", credits=cost) as tx:
                events = []
                try:
                    async for ev in BatchReportBuilder(s).build_stream(req):
                        events.append(ev)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except Exception as exc:
                    logger.exception("batch_stream failed")
                    yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
                    raise
                tx.set_result(title=f"批量内容差距（{n_kw} 词）", summary=f"{len(events)} 条事件",
                              payload={"kind": "seo-gap-batch", "events": events[-300:]})

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
