"""个人静态收款码的免签订单（V免签模式）。

## 为什么存在

现阶段没有商户号（站在香港，ICP/公安备案走不通），又不想把钱和数据交给
第三方发卡网。静态收款码收款唯一的难点是"不知道这笔钱对应哪个订单"——
解法是**用金额尾数当订单号**：

  1. 用户下单 → 在标价上随机立减几分钱（¥49 → ¥48.83），
     这个金额在所有未支付订单里唯一
  2. 用户扫静态码、按页面显示的精确金额付款
  3. 店主手机上的到账通知（"支付宝到账48.83元"）被转发工具 POST 到
     /api/pay/notify → 按金额匹配订单 → 自动造卡、自动交付
  4. 买家页面轮询 /api/pay/order/{id}，卡密自己出现

  兜底：通知漏了，店主在 /payadmin 手动点一下确认（10 秒）。

## 已知限制（测试期可接受，换商户号后整个模块可删）

- 依赖一台登着支付宝的安卓手机常开 + 通知转发工具
- 个人码高频收款会触发支付宝风控 —— 日几单到十几单没问题，起量就该换正路
- 卡密在 pay_orders 表里存**明文**（买家要凭订单号取回），billing.db 已 gitignore
"""

from __future__ import annotations

import random
import re
import string
import time
from typing import Any, Optional

from .store import _LOCK, conn, mint

# ── 商品 ────────────────────────────────────────────────────────────
# (名称, 标价分, 点数)。价与点改这里，前端从 /api/pay/products 拿，不写死两份。
PRODUCTS: dict[str, tuple[str, int, int]] = {
    "trial":    ("体验卡", 1990, 20),
    "standard": ("标准卡", 4900, 60),
    "max":      ("大卡",   9900, 140),
}

ORDER_TTL = 30 * 60          # 订单 30 分钟不付自动过期，释放金额尾数
MAX_OFFSET = 60              # 立减 1~60 分 → 每档最多 60 单同时挂着
MAX_PENDING_PER_IP = 5       # 防止有人狂建订单把尾数池占光

_ID_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


def _init() -> None:
    conn().executescript("""
        CREATE TABLE IF NOT EXISTS pay_orders (
            id           TEXT PRIMARY KEY,
            product      TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',  -- pending | paid | expired
            created_at   INTEGER NOT NULL,
            paid_at      INTEGER,
            card_key     TEXT NOT NULL DEFAULT '',          -- 明文：未登录买家凭订单号取回
            account_id   INTEGER,                            -- 登录着买 → 点数直接进账户，不发卡
            via          TEXT NOT NULL DEFAULT '',           -- auto | manual
            ip           TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_pay_status ON pay_orders(status, amount_cents);
    """)
    _migrate()
    conn().commit()


# ⚠️ CREATE TABLE IF NOT EXISTS 不会给**已存在**的表补新列。
# 2026-08-29 就栽在这：account_id 加进了建表语句，但线上表早就建好了，
# 线上一点「购买」直接 500（table pay_orders has no column named account_id）。
# 以后给这张表加列，只往下面的清单里加一行，别指望改建表语句能生效。
_COLUMNS = [
    ("account_id", "INTEGER"),
]


def _migrate() -> None:
    have = {r[1] for r in conn().execute("PRAGMA table_info(pay_orders)")}
    for name, decl in _COLUMNS:
        if name not in have:
            conn().execute(f"ALTER TABLE pay_orders ADD COLUMN {name} {decl}")


def _expire_stale(now: int) -> None:
    conn().execute(
        "UPDATE pay_orders SET status='expired' WHERE status='pending' AND created_at<?",
        (now - ORDER_TTL,))


def _gen_id() -> str:
    return "P" + "".join(random.choice(_ID_ALPHABET) for _ in range(6))


def create_order(product: str, ip: str = "",
                 account_id: Optional[int] = None) -> dict[str, Any]:
    """建订单：挑一个未占用的金额尾数。抛 ValueError 时把 .args[0] 直接给用户看。"""
    if product not in PRODUCTS:
        raise ValueError("没有这个商品。")
    name, base, credits = PRODUCTS[product]
    now = int(time.time())
    with _LOCK:
        _init()
        _expire_stale(now)
        c = conn()
        if ip and c.execute(
                "SELECT COUNT(*) FROM pay_orders WHERE status='pending' AND ip=?",
                (ip,)).fetchone()[0] >= MAX_PENDING_PER_IP:
            raise ValueError("你有太多未支付的订单，请先完成或等它们过期。")

        taken = {r[0] for r in c.execute(
            "SELECT amount_cents FROM pay_orders WHERE status='pending'")}
        offsets = list(range(1, MAX_OFFSET + 1))
        random.shuffle(offsets)
        amount = next((base - k for k in offsets if (base - k) not in taken), None)
        if amount is None:
            raise ValueError("当前下单的人太多，请几分钟后再试。")

        oid = _gen_id()
        c.execute("INSERT INTO pay_orders(id,product,amount_cents,created_at,ip,account_id) "
                  "VALUES(?,?,?,?,?,?)", (oid, product, amount, now, ip, account_id))
        c.commit()
    return {"order_id": oid, "product": product, "name": name, "credits": credits,
            "to_account": account_id is not None,
            "list_cents": base, "amount_cents": amount,
            "amount": f"{amount // 100}.{amount % 100:02d}",
            "expires_in": ORDER_TTL}


def get_order(oid: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        _init()
        _expire_stale(int(time.time()))
        row = conn().execute("SELECT * FROM pay_orders WHERE id=?", (oid,)).fetchone()
    if row is None:
        return None
    name, _, credits = PRODUCTS.get(row["product"], (row["product"], 0, 0))
    out = {"order_id": row["id"], "status": row["status"], "name": name,
           "credits": credits,
           "amount": f"{row['amount_cents'] // 100}.{row['amount_cents'] % 100:02d}",
           "expires_in": max(0, row["created_at"] + ORDER_TTL - int(time.time()))}
    if row["status"] == "paid":
        if row["account_id"]:
            out["to_account"] = True           # 点数已进账户，没有卡密要保存
        else:
            out["card_key"] = row["card_key"]  # 订单号就是取卡凭证
    return out


def _deliver(row, via: str) -> dict[str, Any]:
    """交付。必须在 _LOCK 里调用。

    两条路：登录着下的单直接给账户加点（不产生卡密，用户也不用保存什么）；
    未登录的单照旧造一张卡，买家凭订单号取回。
    """
    name, _, credits = PRODUCTS[row["product"]]
    key = ""
    if row["account_id"]:
        # 这里不能调 accounts.add_credits —— 它自己要拿 _LOCK，会死锁。
        # 直接按 wallet_hash 加点，逻辑跟那边一致。
        w = conn().execute("SELECT wallet_hash FROM accounts WHERE id=?",
                           (row["account_id"],)).fetchone()
        if w is None:
            raise RuntimeError(f"订单 {row['id']} 指向的账户不存在")
        conn().execute("UPDATE cards SET total_credits = total_credits + ? WHERE card_hash=?",
                       (credits, w["wallet_hash"]))
    else:
        key = mint(1, credits, batch=f"pay-{via}", label=name)[0]
    conn().execute(
        "UPDATE pay_orders SET status='paid', paid_at=?, card_key=?, via=? "
        "WHERE id=? AND status='pending'",
        (int(time.time()), key, via, row["id"]))
    conn().commit()
    return {"order_id": row["id"], "amount_cents": row["amount_cents"],
            "product": row["product"], "card_key": key,
            "account_id": row["account_id"], "credits": credits}


def match_amount(amount_cents: int) -> Optional[dict[str, Any]]:
    """到账通知按金额匹配。恰好一个待支付订单 → 自动交付；零个或多个 → 不动，人工兜底。"""
    with _LOCK:
        _init()
        _expire_stale(int(time.time()))
        rows = conn().execute(
            "SELECT * FROM pay_orders WHERE status='pending' AND amount_cents=?",
            (amount_cents,)).fetchall()
        if len(rows) != 1:
            return None
        return _deliver(rows[0], "auto")


def confirm_manual(oid: str) -> Optional[dict[str, Any]]:
    """管理页人工确认（到账通知漏了时的兜底）。"""
    with _LOCK:
        _init()
        row = conn().execute(
            "SELECT * FROM pay_orders WHERE id=? AND status='pending'", (oid,)).fetchone()
        if row is None:
            return None
        return _deliver(row, "manual")


def pending_orders() -> list[dict[str, Any]]:
    with _LOCK:
        _init()
        _expire_stale(int(time.time()))
        rows = conn().execute(
            "SELECT * FROM pay_orders WHERE status='pending' ORDER BY created_at DESC"
        ).fetchall()
    return [{"order_id": r["id"], "product": PRODUCTS.get(r["product"], (r["product"],))[0],
             "amount": f"{r['amount_cents'] // 100}.{r['amount_cents'] % 100:02d}",
             "age_secs": int(time.time()) - r["created_at"]} for r in rows]


AMOUNT_RE = re.compile(r"(\d+)\.(\d{2})")


def parse_amount_cents(text: str) -> Optional[int]:
    """从到账通知原文里抠金额："支付宝到账48.83元" → 4883。
    立减机制保证金额永远带非零分位，所以只认 x.xx 形态，不猜整数。"""
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 100 + int(m.group(2))
