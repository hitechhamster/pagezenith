"""Dodo Payments 接入：建 checkout、验 webhook 签名、按订单号交付。

## 为什么换掉支付宝静态码

原来那套（payorders.py 的"金额尾数当订单号"）能跑，但依赖一台登着支付宝的安卓手机
常开 + 通知转发工具，个人码高频收款还会踩风控，起量必挂。Dodo 是 merchant of record，
自己处理收单和合规，站在香港也能收，正好补上没有商户号这个缺口。

## 接法（照 docs.dodopayments.com，2026-09-03 查证）

  下单：POST {base}/payments  →  拿 payment_link，前端跳过去
  回调：Dodo POST 到 /api/pay/dodo/webhook，事件 payment.succeeded
  验签：Standard Webhooks 规范 —— HMAC-SHA256(webhook-id.webhook-timestamp.raw_body)，
        base64 后与 webhook-signature 头里的某个 v1,xxx 比对

**订单号靠 metadata 传**，不再靠金额尾数 —— 这是换渠道最大的好处：
金额可以是整数、并发下单不再抢尾数池、MAX_OFFSET/MAX_PENDING_PER_IP 那套限制作废。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# 允许的时间偏移：防重放。Standard Webhooks 推荐 5 分钟。
WEBHOOK_TOLERANCE = 5 * 60


class DodoError(RuntimeError):
    """建单失败。上层转成给用户看的提示。"""


def _base_url(settings) -> str:
    """test_mode 走沙箱，live 走生产。别把两个环境的 key 混着用。"""
    return (settings.dodo_base_url or "").rstrip("/")


async def create_payment(settings, *, order_id: str, product_id: str,
                         amount_cents: int, email: str, name: str,
                         return_url: str) -> dict[str, Any]:
    """建一笔一次性付款，返回 {payment_id, payment_link}。

    order_id 放进 metadata，webhook 回来时凭它定位订单 —— 不依赖金额匹配。
    """
    if not settings.dodo_api_key:
        raise DodoError("服务端未配置 DODO_API_KEY，请联系站长。")
    payload = {
        "payment_link": True,
        "billing": {"country": "HK", "state": "", "city": "", "street": "", "zipcode": ""},
        "customer": {"email": email, "name": name or email.split("@")[0]},
        "product_cart": [{"product_id": product_id, "quantity": 1}],
        "metadata": {"order_id": order_id},
        "return_url": return_url,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout, trust_env=False,
                                     proxy=settings.proxy_for("dodopayments")) as c:
            r = await c.post(f"{_base_url(settings)}/payments",
                             headers={"Authorization": f"Bearer {settings.dodo_api_key}",
                                      "Content-Type": "application/json"},
                             json=payload)
    except Exception as exc:  # noqa: BLE001
        raise DodoError(f"连不上 Dodo：{exc}") from exc
    if r.status_code >= 400:
        logger.warning("Dodo 建单失败 %s: %s", r.status_code, r.text[:300])
        raise DodoError(f"Dodo 建单失败（{r.status_code}）。请稍后重试或联系站长。")
    d = r.json()
    link = d.get("payment_link") or d.get("checkout_url")
    if not link:
        raise DodoError("Dodo 没返回付款链接。")
    return {"payment_id": d.get("payment_id") or d.get("id") or "", "payment_link": link}


def verify_webhook(secret: str, headers, raw_body: bytes) -> tuple[bool, str]:
    """校验 Standard Webhooks 签名。返回 (通过?, 不通过的原因)。

    签名串 = f"{webhook-id}.{webhook-timestamp}.{raw_body}"，HMAC-SHA256 后 base64。
    webhook-signature 头可能带多个版本（空格分隔的 "v1,xxx"），命中任一即可。

    ⚠️ 必须用**原始 body 字节**算，不能先 json.loads 再 dumps —— 那样空格和键序都会变。
    """
    if not secret:
        return False, "服务端未配置 DODO_WEBHOOK_SECRET"
    wid = headers.get("webhook-id") or ""
    wts = headers.get("webhook-timestamp") or ""
    wsig = headers.get("webhook-signature") or ""
    if not (wid and wts and wsig):
        return False, "缺少 webhook 头"
    try:
        if abs(time.time() - int(wts)) > WEBHOOK_TOLERANCE:
            return False, "时间戳超出容忍范围（疑似重放）"
    except ValueError:
        return False, "时间戳格式不对"

    # Dodo 的 secret 常以 whsec_ 前缀 + base64 给出，两种写法都兼容
    key = secret[6:] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:  # noqa: BLE001
        key_bytes = key.encode()

    signed = b"%s.%s." % (wid.encode(), wts.encode()) + raw_body
    want = base64.b64encode(hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()
    for part in wsig.split():
        got = part.split(",", 1)[1] if "," in part else part
        if hmac.compare_digest(want, got):
            return True, ""
    return False, "签名不匹配"


def extract_order_id(body: dict) -> Optional[str]:
    """从 webhook 载荷里取回我们塞进去的 order_id。

    Dodo 的载荷形状是 {type, data:{...}}；metadata 在 data.metadata。
    多留几个位置是因为不同事件版本的嵌套层级不完全一致，取不到就返回 None
    交给上层报警，绝不猜。
    """
    for path in (("data", "metadata", "order_id"), ("metadata", "order_id"),
                 ("data", "payload", "metadata", "order_id")):
        cur: Any = body
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    return None
