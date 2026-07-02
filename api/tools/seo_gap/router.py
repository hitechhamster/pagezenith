"""seo-gap 工具的 API 路由（前缀 /api/seo-gap）。

- key 按请求传入，后端用完即弃（不存、不记日志）。
- 并发上限保护服务器资源（每个分析开浏览器 + 抓多页）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException

from .config import get_settings
from .models import BatchReportRequest, ReportRequest, ReportV2
from .report_batch import BatchReportBuilder
from .report_v2 import ReportV2Builder

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/seo-gap", tags=["seo-gap"])

# 同时进行的分析数上限（公开部署防资源/账单失控）
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


def _settings_for(req: ReportRequest):
    """合并用户 key，并校验有了能跑。"""
    s = get_settings().with_keys(req.openrouter_key, req.serpapi_key, req.tavily_key)
    if not s.use_mocks:
        if not s.openrouter_api_key:
            raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
        if s.serp_provider == "serpapi" and not s.serpapi_key:
            raise HTTPException(status_code=400, detail="缺少 SerpApi Key，请在设置里填写。")
    return s


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model,
            "free_slots": _sema._value}


@router.post("/report", response_model=ReportV2)
async def report(req: ReportRequest) -> ReportV2:
    s = _settings_for(req)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        try:
            return await ReportV2Builder(s).build(req)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("report failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/report_stream")
async def report_stream(req: ReportRequest):
    """流式四部分报告（SSE）：逐块产出，前端分析一个显示一个。"""
    s = _settings_for(req)

    async def gen():
        if _sema.locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '服务繁忙，请稍后重试'}, ensure_ascii=False)}\n\n"
            return
        async with _sema:
            try:
                async for ev in ReportV2Builder(s).build_stream(req):
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("report_stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _settings_for_batch(req: BatchReportRequest):
    s = get_settings().with_keys(req.openrouter_key, req.serpapi_key, req.tavily_key)
    if not s.use_mocks:
        if not s.openrouter_api_key:
            raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
        if s.serp_provider == "serpapi" and not s.serpapi_key:
            raise HTTPException(status_code=400, detail="缺少 SerpApi Key，请在设置里填写。")
    return s


@router.post("/batch_stream")
async def batch_stream(req: BatchReportRequest):
    """批量模式（关键词簇 vs 目标页）流式报告（SSE）。"""
    s = _settings_for_batch(req)

    async def gen():
        if _sema.locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '服务繁忙，请稍后重试'}, ensure_ascii=False)}\n\n"
            return
        async with _sema:
            try:
                async for ev in BatchReportBuilder(s).build_stream(req):
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("batch_stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
