"""站点情报侦察 API（前缀 /api/site-recon）。纯检测，无需 key。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from ..seo_gap.config import get_settings
from .analyzer import recon
from .models import ReconReport, ReconRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/site-recon", tags=["site-recon"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


@router.post("/check", response_model=ReconReport)
async def check(req: ReconRequest) -> ReconReport:
    if not req.url or not req.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="请输入 http/https 网址。")
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        try:
            return await recon(req.url)
        except Exception as exc:
            logger.exception("site recon failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
