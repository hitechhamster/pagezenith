"""外链拓客 API（前缀 /api/outreach）。key 按请求传，用完即弃。"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..seo_gap.config import get_settings
from .analyzer import OutreachFinder
from .models import OutreachRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/outreach", tags=["outreach"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


def _settings_for(req: OutreachRequest):
    s = get_settings().with_keys(req.openrouter_key, req.serpapi_key)
    if not s.use_mocks:
        if not s.openrouter_api_key:
            raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
        if not s.serpapi_key:
            raise HTTPException(status_code=400, detail="缺少 SerpApi Key（用于找站），请在设置里填写。")
    return s


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model,
            "max_prospects": s.outreach_max_prospects}


@router.post("/find_stream")
async def find_stream(req: OutreachRequest):
    if not req.keyword.strip() and not req.your_url.strip():
        raise HTTPException(status_code=400, detail="请填写主题关键词或目标网址。")
    s = _settings_for(req)

    async def gen():
        if _sema.locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '服务繁忙，请稍后重试'}, ensure_ascii=False)}\n\n"
            return
        async with _sema:
            try:
                async for ev in OutreachFinder(s).stream(req):
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except Exception as exc:
                logger.exception("outreach find failed")
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
