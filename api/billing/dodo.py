"""Dodo Payments 接入：建 Checkout Session、验 webhook 签名、按订单号交付。

## 为什么换掉支付宝静态码

原来那套（payorders.py 的"金额尾数当订单号"）依赖一台登着支付宝的安卓手机常开 +
通知转发工具，个人码高频收款还会踩风控，起量必挂。Dodo 是 merchant of record，
自己处理收单和合规，站在香港也能收，**而且能开微信支付** —— 这才是换渠道的目的。

## 接法（照观象台 server/payments/dodo.mjs 实战过的写法，2026-08 起线上跑着）

  下单：POST {base}/checkouts  →  checkout_url，前端用官方 SDK 在站内开浮层
  回调：Dodo POST 到 /api/pay/dodo/webhook，事件 payment.succeeded
  验签：Standard Webhooks 规范 —— HMAC-SHA256(webhook-id.webhook-timestamp.raw_body)

## 三个直接决定转化率的字段（观象台 2026-08-15 实测，一个都不能少）

  allowed_payment_method_types  → 没有它微信支付不一定出现；顺序按大陆优先排
  billing_currency = 'CNY'      → 不传默认「Pay in USD」，¥49 显示成 $6.8，国内用户一愣
  billing_address 全部预填       → 国内用户买数字商品**没有填地址的习惯**，空表单是第二大弃单点
                                   （第一是英文页发怵）。预填一个脱敏地址：上海 + 品牌名当街道 +
                                   上海邮编 200000，三者必须自洽。

**订单号靠 metadata 传**，不再靠金额尾数 —— 金额可以是整数、并发下单不再抢尾数池。
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

# 大陆线支付方式白名单。ali_pay 是前瞻性的：账号没开通时 Dodo 自动不显示，
# 带着它建单不报错（观象台 2026-08-08 实测）；哪天工单开通了这里一个字不用改。
# 卡类留着是给港澳台/东南亚华人用户的，对大陆用户只是多一个不会点的按钮。
PAYMENT_METHODS_CN = ["we_chat_pay", "ali_pay", "credit", "debit", "apple_pay", "google_pay"]

# 预填地址：脱敏（不暴露经营者真实所在地），但省/市/邮编必须自洽。
# 街道填品牌名 —— 用户看到的是"商家预填、不用改"，反而不会去动它。
BILLING_ADDRESS_CN = {"country": "CN", "state": "上海市", "city": "上海市",
                      "street": "页面科技", "zipcode": "200000"}


class DodoError(RuntimeError):
    """建单失败。上层转成给用户看的提示。"""


def _base_url(settings) -> str:
    return (settings.dodo_base_url or "").rstrip("/")


async def create_checkout(settings, *, order_id: str, product_id: str,
                          email: str, name: str, return_url: str) -> dict[str, Any]:
    """建一个 Checkout Session，返回 {checkout_url, session_id}。

    用 /checkouts 而不是 /payments：后者强制要 customer 完整对象 + billing 全套，
    前者把没给的字段留给 Dodo 托管页自己收，容错高。我们有登录邮箱，就顺带预填。

    ⚠️ customer 是联合类型，**name 和 email 必须一起给**，只给一个会 422
    （观象台 2026-08-07 踩过）。没邮箱就整个不传。
    """
    if not settings.dodo_api_key:
        raise DodoError("服务端未配置 DODO_API_KEY，请联系站长。")
    body: dict[str, Any] = {
        "product_cart": [{"product_id": product_id, "quantity": 1}],
        "return_url": return_url,
        "metadata": {"order_id": order_id},
        "billing_currency": "CNY",
        "billing_address": dict(BILLING_ADDRESS_CN),
        "allowed_payment_method_types": list(PAYMENT_METHODS_CN),
    }
    if email:
        body["customer"] = {"email": email, "name": (name or email.split("@")[0])[:60]}
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout, trust_env=False,
                                     proxy=settings.proxy_for("dodopayments")) as c:
            r = await c.post(f"{_base_url(settings)}/checkouts",
                             headers={"Authorization": f"Bearer {settings.dodo_api_key}",
                                      "Content-Type": "application/json"},
                             json=body)
    except Exception as exc:  # noqa: BLE001
        raise DodoError(f"连不上 Dodo：{exc}") from exc
    if r.status_code >= 400:
        logger.warning("Dodo 建单失败 %s: %s", r.status_code, r.text[:300])
        raise DodoError(f"Dodo 建单失败（{r.status_code}）。请稍后重试或联系站长。")
    d = r.json()
    # 响应字段名按观象台线上代码的兜底链读，哪个有用哪个
    url = d.get("checkout_url") or d.get("payment_link") or d.get("url")
    if not url:
        raise DodoError("Dodo 没返回结账地址。")
    return {"checkout_url": url,
            "session_id": d.get("session_id") or d.get("payment_id") or ""}


# 兼容旧调用名（pay_router 早期版本用的）；新代码一律用 create_checkout。
async def create_payment(settings, *, order_id: str, product_id: str, amount_cents: int,
                         email: str, name: str, return_url: str) -> dict[str, Any]:
    out = await create_checkout(settings, order_id=order_id, product_id=product_id,
                                email=email, name=name, return_url=return_url)
    return {"payment_id": out["session_id"], "payment_link": out["checkout_url"]}


def verify_webhook(secret: str, headers, raw_body: bytes) -> tuple[bool, str]:
    """校验 Standard Webhooks 签名。返回 (通过?, 不通过的原因)。

    签名串 = f"{webhook-id}.{webhook-timestamp}.{raw_body}"，HMAC-SHA256 后 base64。
    webhook-signature 头可能带多个版本（空格分隔的 "v1,xxx"），命中任一即可。

    ⚠️ 必须用**原始 body 字节**算，不能先 json.loads 再 dumps —— 那样空格和键序都会变。
    ⚠️ whsec_ 后面那段要 **base64 解码成字节**当密钥，不是拿字符串直接用。
    三个地方错一个就永远验不过。
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

    载荷形状 {type, data:{...}}，metadata 在 data.metadata。多留几个位置是因为
    不同事件版本嵌套层级不完全一致；取不到就返回 None 交给上层报警，绝不猜。
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
