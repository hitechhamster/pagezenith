"""免签支付 API（前缀 /api/pay）。机制说明见 payorders.py 顶部。

买家侧三个端点全公开（订单号本身就是取卡凭证）；
店主侧（notify / pending / confirm）用 PAY_NOTIFY_TOKEN 守着 ——
这个 token 同时发给手机上的通知转发工具和 /payadmin 管理页。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tools.seo_gap.config import get_settings

from . import payorders as P

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pay", tags=["pay"])


def _check_token(request: Request) -> None:
    want = get_settings().pay_notify_token
    got = request.headers.get("X-Pay-Token", "")
    # token 没配置 = 功能未开启，全部拒掉，别裸奔
    if not want or not hmac.compare_digest(want, got):
        raise HTTPException(status_code=401, detail="token 不对或未配置。")


# ── 买家侧 ──────────────────────────────────────────────────────────
@router.get("/products")
async def products() -> list[dict[str, Any]]:
    return [{"id": pid, "name": n, "price": f"{cents // 100}.{cents % 100:02d}",
             "credits": cr} for pid, (n, cents, cr) in P.PRODUCTS.items()]


class OrderReq(BaseModel):
    product: str


@router.post("/order")
async def create_order(req: OrderReq, request: Request):
    ip = (request.client.host if request.client else "") or ""
    try:
        return P.create_order(req.product, ip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/order/{oid}")
async def order_status(oid: str):
    out = P.get_order(oid)
    if out is None:
        raise HTTPException(status_code=404, detail="没有这个订单。")
    return out


# ── 店主侧 ──────────────────────────────────────────────────────────
class NotifyReq(BaseModel):
    text: Optional[str] = None       # 到账通知原文，如 "支付宝到账48.83元"
    amount: Optional[str] = None     # 或者直接给金额 "48.83"


@router.post("/notify")
async def notify(req: NotifyReq, request: Request):
    """手机通知转发工具打这里。匹配到唯一待付订单就自动发卡。"""
    _check_token(request)
    cents = P.parse_amount_cents(req.amount or req.text or "")
    if cents is None:
        return {"matched": False, "reason": "没解析出金额"}
    hit = P.match_amount(cents)
    if hit is None:
        # 零个或多个候选都不自动动 —— 宁可去管理页人工点，也不能发错人
        logger.warning("到账 %s 分未自动匹配（0 个或多个候选）", cents)
        return {"matched": False, "reason": "无唯一匹配订单，请到 /payadmin 人工确认"}
    logger.info("到账自动发卡: %s %s", hit["order_id"], hit["product"])
    return {"matched": True, "order_id": hit["order_id"]}


@router.get("/pending")
async def pending(request: Request):
    _check_token(request)
    return {"items": P.pending_orders()}


class ConfirmReq(BaseModel):
    order_id: str


@router.post("/confirm")
async def confirm(req: ConfirmReq, request: Request):
    _check_token(request)
    hit = P.confirm_manual(req.order_id.strip().upper())
    if hit is None:
        raise HTTPException(status_code=404, detail="订单不存在或已处理。")
    return {"ok": True, "order_id": hit["order_id"]}
