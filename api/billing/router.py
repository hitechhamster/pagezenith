"""卡密自助接口（前缀 /api/billing）。

卡密即身份：查余额、查流水、取回历史生成结果、重连未完成的任务。
不需要注册登录 —— 换台电脑输入同一张卡，东西都在。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import jobs, store
from .deps import Card, require_card
from .pricing import signup_credits, REVISE_FREE, TIERS, price_table

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/pricing")
async def pricing() -> dict:
    """公开：给首页/工具页展示价目与档位，不需要卡密。"""
    return {
        "tiers": [{"key": k, **v} for k, v in TIERS.items()],
        "prices": price_table(),
        "revise_free": REVISE_FREE,
        # 注册赠送额度也从这里出：以前首页把它手抄成"注册送 9 点"，
        # 后端改成 11 之后前端没跟着动，又出现一次数字不一致。
        "signup_credits": signup_credits(),
    }


@router.get("/balance")
async def balance(card: Card = Depends(require_card)) -> dict:
    st = store.card_state(card.key_hash)
    if st is None:
        raise HTTPException(status_code=401, detail="卡密无效。")
    return {"remaining": st["remaining"], "total": st["total"], "used": st["used"],
            "label": st["label"], "spent_today": store.card_spent_today(card.key_hash)}


@router.get("/usage")
async def usage(limit: int = Query(50, ge=1, le=200),
                card: Card = Depends(require_card)) -> dict:
    return {"items": store.list_usage(card.key_hash, limit)}


@router.get("/history")
async def history(limit: int = Query(50, ge=1, le=100),
                  card: Card = Depends(require_card)) -> dict:
    """我的记录：最近的生成结果（标题/时间/工具），点进去再取全文。"""
    return {"items": store.list_results(card.key_hash, limit)}


@router.get("/result/{rid}")
async def result(rid: str, card: Card = Depends(require_card)) -> dict:
    r = store.get_result(rid, card.key_hash)
    if r is None:
        raise HTTPException(status_code=404, detail="找不到这条记录（或不属于当前卡密）。")
    return r


@router.get("/job/{job_id}")
async def job_stream(job_id: str, from_index: int = Query(0, ge=0),
                     card: Card = Depends(require_card)):
    """重连正在跑的任务：断线后带上已收到的事件数续订。"""
    job = jobs.get(job_id, card.key_hash)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期（完成的结果请去「我的记录」）。")
    return StreamingResponse(
        jobs.stream(job, from_index), media_type="text/event-stream",
        # X-Accel-Buffering: 让任何一层反代（nginx / 未来可能加的 CDN）别攒着缓冲，
        # 否则流式生成会憋到最后一次性吐出，用户看着像卡死。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
