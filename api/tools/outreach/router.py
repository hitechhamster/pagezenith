"""外链拓客 API（前缀 /api/outreach）。凭 X-Card-Key 卡密鉴权 + 按点计费。"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from billing.deps import Card, charge, require_card

from ..seo_gap.config import get_settings
from .analyzer import OutreachFinder
from .models import OutreachRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/outreach", tags=["outreach"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


def _settings_for(req: OutreachRequest):
    """服务端统一出 key。缺 key 是站长的配置问题，报 500 而不是 400。"""
    s = get_settings()
    if not s.use_mocks:
        if not s.openrouter_api_key:
            raise HTTPException(status_code=500, detail="服务端未配置 OPENROUTER_API_KEY。")
        if not s.serp_key():
            raise HTTPException(status_code=500, detail="服务端未配置 SERPER_KEY。")
    return s


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model,
            "max_prospects": s.outreach_max_prospects}


@router.post("/find_stream")
async def find_stream(req: OutreachRequest, card: Card = Depends(require_card)):
    if not req.keyword.strip() and not req.your_url.strip():
        raise HTTPException(status_code=400, detail="请填写主题关键词或目标网址。")
    s = _settings_for(req)

    async def gen():
        if _sema.locked():
            yield f"data: {json.dumps({'type': 'error', 'message': '服务繁忙，请稍后重试'}, ensure_ascii=False)}\n\n"
            return
        async with _sema:
            # charge 的异常路径会自动退点：失败或中途断开，用户不该付这笔钱
            async with charge(card, "outreach", "run") as tx:
                events = []
                try:
                    async for ev in OutreachFinder(s).stream(req):
                        events.append(ev)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except Exception as exc:
                    logger.exception("outreach find failed")
                    yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
                    raise
                tx.set_result(title=f"外链拓客：{req.keyword or req.your_url}",
                              summary=f"{len(events)} 条事件",
                              payload={"kind": "outreach", "events": events[-200:]})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
