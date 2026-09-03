"""免签支付 API（前缀 /api/pay）。机制说明见 payorders.py 顶部。

买家侧三个端点全公开（订单号本身就是取卡凭证）；
店主侧（notify / pending / confirm）用 PAY_NOTIFY_TOKEN 守着 ——
这个 token 同时发给手机上的通知转发工具和 /payadmin 管理页。
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from tools.seo_gap.config import get_settings

from . import accounts, dodo, payorders as P
from .deps import SESSION_COOKIE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pay", tags=["pay"])


def _dodo_products() -> dict[str, str]:
    """DODO_PRODUCTS="trial=pdt_x,standard=pdt_y" → {"trial": "pdt_x", ...}"""
    out: dict[str, str] = {}
    for part in (get_settings().dodo_products or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def dodo_enabled() -> bool:
    """三件套齐了才算启用；缺任一就退回旧的扫码流程，不硬失败。"""
    s = get_settings()
    return bool(s.dodo_api_key and s.dodo_webhook_secret and _dodo_products())


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
    """下单必须登录 —— 点数记在账户上，不再发不记名卡密。

    2026-08-29 砍掉匿名购买：一单未卖，没有老用户要兼容，
    两套身份只会让文案、客服和用户心智都乱掉。
    兑换接口（/api/auth/redeem）保留，留给礼品卡 / 代销 / 补偿点数。
    """
    acct = accounts.account_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if acct is None:
        raise HTTPException(status_code=401, detail="请先登录再购买。")
    ip = (request.client.host if request.client else "") or ""
    try:
        order = P.create_order(req.product, ip, acct["id"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Dodo 开着就建 checkout，把付款链接一起返回；前端有 payment_link 就跳转，
    # 没有就仍显示收款码 —— 两条路并存，切换不需要改前端逻辑。
    if dodo_enabled():
        pid = _dodo_products().get(req.product)
        if not pid:
            logger.warning("Dodo 未配置商品 %s，回退扫码", req.product)
            return order
        s = get_settings()
        try:
            pay = await dodo.create_payment(
                s, order_id=order["order_id"], product_id=pid,
                amount_cents=order["amount_cents"],
                email=acct.get("email") or "", name=acct.get("email") or "",
                return_url=f"{s.site_url.rstrip('/')}/buy?order={order['order_id']}")
            order["payment_link"] = pay["payment_link"]
            order["gateway"] = "dodo"
        except dodo.DodoError as exc:
            # 建单已经落库，付款链接拿不到不该让用户白等 —— 回退扫码路径
            logger.warning("Dodo 建单失败，回退扫码: %s", exc)
    return order


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


@router.post("/dodo/webhook")
async def dodo_webhook(request: Request):
    """Dodo 回调。验签 → 取 order_id → 交付。

    始终回 200（除了验签失败回 401）：Dodo 收到非 2xx 会重投，
    而"订单已处理""不是我们关心的事件"都不是错误，重投也没用。
    """
    raw = await request.body()
    ok, why = dodo.verify_webhook(get_settings().dodo_webhook_secret, request.headers, raw)
    if not ok:
        logger.warning("Dodo webhook 验签失败: %s", why)
        raise HTTPException(status_code=401, detail="签名校验失败")
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {"ok": True, "skipped": "载荷不是 JSON"}

    etype = (body.get("type") or body.get("event_type") or "").lower()
    if etype != "payment.succeeded":
        return {"ok": True, "skipped": etype or "无事件类型"}

    oid = dodo.extract_order_id(body)
    if not oid:
        # 拿不到订单号说明 metadata 没带上 —— 报警但别重投，人工去 /payadmin 处理
        logger.error("Dodo webhook 缺 order_id，需人工确认: %s", str(body)[:300])
        return {"ok": True, "skipped": "载荷里没有 order_id"}

    hit = P.deliver_by_id(oid, via="dodo")
    if hit is None:
        # 幂等：重投或已人工确认过，都会走到这
        logger.info("Dodo webhook 订单 %s 已处理过，跳过", oid)
        return {"ok": True, "already": True, "order_id": oid}
    logger.info("Dodo 付款成功自动到账: %s %s", oid, hit["product"])
    return {"ok": True, "order_id": oid}


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
