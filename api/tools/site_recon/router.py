"""站点情报侦察 API（前缀 /api/site-recon）。凭 X-Card-Key 卡密鉴权 + 按点计费。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException

from billing.deps import Card, charge, require_card

from ..seo_gap.config import get_settings
from .analyzer import recon
from .models import ReconReport, ReconRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/site-recon", tags=["site-recon"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


@router.post("/check", response_model=ReconReport)
async def check(req: ReconRequest, card: Card = Depends(require_card)) -> ReconReport:
    if not req.url or not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="请输入 http/https 网址。")
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        async with charge(card, "site-recon", "run") as tx:
            try:
                out = await recon(req.url)
            except Exception as exc:
                logger.exception("site recon failed")
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            tx.set_result(title=f"站点侦察：{req.url}", summary="",
                          payload={"kind": "site-recon", **out.model_dump()})
            return out
