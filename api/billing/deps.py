"""卡密鉴权 + 扣点上下文 + 三道护栏。

护栏（钱是自己的了，这些不是可选项）：
  1. 单卡日限   —— 防单个买家脚本狂刷
  2. 全局日成本熔断 —— 防任何未预料的情况把账单烧穿（读 usage 表当日真实成本）
  3. 无效卡限流 —— 防爆破卡号

用法（在 SSE 生成器里）：

    async with charge(card, "seo-writer", "article", tier) as tx:
        ... 干活 ...
        tx.report_tokens(model, tin, tout)      # 回填真实用量
        tx.set_result(title=..., payload=...)   # 成功才落库
    # 正常结束 = 已扣点 + 结果入库；抛异常 = 自动退点
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import Header, HTTPException, Request

from . import store
from . import accounts
from .pricing import est_cost_cny, est_image_cost_cny, price, tier_of

# 会话 cookie 名。前端不读它（HttpOnly），只有服务端认。
SESSION_COOKIE = "pz_session"

logger = logging.getLogger(__name__)

# 护栏参数（可用环境变量覆盖）
CARD_DAILY_LIMIT = int(os.environ.get("BILLING_CARD_DAILY_LIMIT", "200"))      # 点/天/卡
GLOBAL_DAILY_COST_CNY = float(os.environ.get("BILLING_GLOBAL_DAILY_CNY", "300"))
BAD_KEY_PER_HOUR = int(os.environ.get("BILLING_BAD_KEY_PER_HOUR", "20"))

_bad_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")) or "-"


def _rate_limit_bad_key(ip: str) -> None:
    now = time.time()
    hits = [t for t in _bad_attempts.get(ip, []) if now - t < 3600]
    hits.append(now)
    _bad_attempts[ip] = hits
    if len(_bad_attempts) > 5000:            # 简易防内存膨胀
        for k in [k for k, v in _bad_attempts.items() if not v or now - v[-1] > 3600]:
            _bad_attempts.pop(k, None)
    if len(hits) > BAD_KEY_PER_HOUR:
        raise HTTPException(status_code=429, detail="卡密尝试过于频繁，请一小时后再试。")


@dataclass
class Card:
    key_hash: str
    ip: str
    remaining: int
    label: str = ""


async def require_card(request: Request,
                       x_card_key: str = Header(default="", alias="X-Card-Key")) -> Card:
    """FastAPI 依赖：解析身份 + 熔断检查。挂在每个花钱的端点上。

    两种身份，解析出来都是一个 card_hash，下游计费逻辑一律不区分：
      1. **登录会话**（主路径）—— cookie 里的 token → 账户 → 它的钱包卡
      2. **裸卡密**（兼容路径）—— X-Card-Key 请求头，给老用户和未登录直接用卡的场景

    会话优先。名字还叫 require_card 是为了不动六个工具路由的签名，
    它现在的语义是"要求一个能扣点的身份"。
    """
    ip = _client_ip(request)

    acct = accounts.account_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if acct is not None:
        h = acct["wallet_hash"]
        st = store.card_state(h)
        if st is None:                      # 钱包卡被人删了，属于数据损坏
            logger.error("账户 #%s 的钱包卡不存在", acct["id"])
            raise HTTPException(status_code=500, detail="账户数据异常，请联系站长。")
    else:
        key = (x_card_key or "").strip()
        if not key:
            _rate_limit_bad_key(ip)
            raise HTTPException(status_code=401, detail="请先登录，或输入卡密。")
        h = store.hash_card(key)
        st = store.card_state(h)
        if st is None:
            _rate_limit_bad_key(ip)
            raise HTTPException(status_code=401, detail="卡密无效。")

    if st["status"] != "active":
        raise HTTPException(status_code=403, detail="该卡密已停用。")

    if store.global_cost_today() >= GLOBAL_DAILY_COST_CNY:
        logger.error("全局日成本熔断触发：今日已 ¥%.2f", store.global_cost_today())
        raise HTTPException(status_code=503, detail="今日服务量已达上限，请明天再试（我们会尽快扩容）。")

    if store.card_spent_today(h) >= CARD_DAILY_LIMIT:
        raise HTTPException(status_code=429, detail=f"该卡今日用量已达上限（{CARD_DAILY_LIMIT} 点），明天恢复。")

    return Card(key_hash=h, ip=ip, remaining=st["remaining"], label=st["label"])


class InsufficientCredits(HTTPException):
    def __init__(self, need: int, have: int):
        super().__init__(status_code=402,
                         detail=f"点数不足：本次需要 {need} 点，卡内还剩 {have} 点。")


@dataclass
class Charge:
    """一次扣点的生命周期。异常 = 退点，正常 = 落用量与结果。"""

    card: Card
    tool: str
    action: str
    tier: str
    credits: int
    usage_id: Optional[int] = None
    result_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    # 按模型分桶：一次请求会跨模型（正文=pro，SEO 元数据=flash-lite，润色=deepseek），
    # 用单个字段记会被后来者覆盖，成本按错的单价算。
    _usage: dict[str, list[int]] = field(default_factory=dict)
    # 配图单独记：图片不按 token 计价，混进 _usage 会被按文本单价算错
    _images: dict[str, int] = field(default_factory=dict)
    _result: Optional[dict[str, Any]] = None

    def report_tokens(self, model: str, tokens_in: int, tokens_out: int) -> None:
        """累加真实用量。同一次请求里每个模型各记一桶。"""
        m = model or "unknown"
        b = self._usage.setdefault(m, [0, 0])
        b[0] += max(0, int(tokens_in or 0))
        b[1] += max(0, int(tokens_out or 0))

    def report_image(self, model: str, n: int = 1) -> None:
        """记配图张数。不走 report_tokens —— 图片按张计价，不按 token。"""
        if n > 0:
            self._images[model or "unknown"] = self._images.get(model or "unknown", 0) + int(n)

    @property
    def tokens_in(self) -> int:
        return sum(v[0] for v in self._usage.values())

    @property
    def tokens_out(self) -> int:
        return sum(v[1] for v in self._usage.values())

    @property
    def cost_cny(self) -> float:
        """逐模型按各自单价算，再相加 —— 不能用某一个模型的单价套全部 token。"""
        text = sum(est_cost_cny(m, v[0], v[1]) for m, v in self._usage.items())
        images = sum(est_image_cost_cny(m, n) for m, n in self._images.items())
        return text + images

    @property
    def model_label(self) -> str:
        """流水里存哪些模型参与了这次请求（按输出 token 降序）。"""
        models = [m for m, _ in sorted(self._usage.items(), key=lambda kv: -kv[1][1])]
        models += [f"{m}x{n}" for m, n in self._images.items()]
        return "+".join(models)

    def set_result(self, *, title: str, summary: str = "", payload: dict[str, Any]) -> None:
        self._result = {"title": title, "summary": summary, "payload": payload}


@contextlib.asynccontextmanager
async def charge(card: Card, tool: str, action: str, tier: str = "basic",
                 credits: Optional[int] = None, job_id: str = ""):
    """先扣后干活；失败自动退点。"""
    tier = tier_of(tier)
    need = price(tool, action, tier) if credits is None else int(credits)
    tx = Charge(card=card, tool=tool, action=action, tier=tier, credits=need)

    if need > 0:
        uid = store.spend(card.key_hash, need, tool=tool, action=action,
                          tier=tier, job_id=job_id, ip=card.ip)
        if uid is None:
            st = store.card_state(card.key_hash) or {"remaining": 0}
            raise InsufficientCredits(need, st["remaining"])
        tx.usage_id = uid

    try:
        yield tx
    except Exception:
        if tx.usage_id is not None:
            store.refund(tx.usage_id)
        raise
    else:
        if tx.usage_id is not None:
            store.finalize_usage(tx.usage_id, model=tx.model_label,
                                 tokens_in=tx.tokens_in, tokens_out=tx.tokens_out,
                                 est_cost=tx.cost_cny)
        if tx._result is not None:
            store.save_result(tx.result_id, card.key_hash, tool,
                              tx._result["title"], tx._result["summary"],
                              tx._result["payload"])
