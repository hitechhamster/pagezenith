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


# 尚未交付、仍可交付的状态。过期只是展示概念不是拒付理由 —— 详见 deliver_by_id。
# _deliver 的 UPDATE 守卫必须用同一个集合，否则会重复加点。
_DELIVERABLE = "('pending','expired')"


def _expire_stale(now: int) -> None:
    conn().execute(
        "UPDATE pay_orders SET status='expired' WHERE status='pending' AND created_at<?",
        (now - ORDER_TTL,))


def _gen_id() -> str:
    return "P" + "".join(random.choice(_ID_ALPHABET) for _ in range(6))


def create_order(product: str, ip: str = "",
                 account_id: Optional[int] = None, exact: bool = False) -> dict[str, Any]:
    """建订单。抛 ValueError 时把 .args[0] 直接给用户看。

    exact=False：静态码路径，挑一个未占用的金额尾数当订单号（¥19.90 → ¥19.53）。
    exact=True ：Dodo 路径，按原价建单。Dodo 凭 metadata.order_id 认单，不需要尾数；
                 而且 Dodo 实际按商品价扣款，库里再记 19.53 就和真实流水对不上，/payadmin 会错。
    """
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

        if exact:
            amount = base
        else:
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


def mark_refunded(oid: str) -> bool:
    """把订单标成 refunded（收到 Dodo 的退款/拒付事件时）。

    只改订单状态，**不动账户点数** —— 点可能已经花掉了，自动扣会把余额搞成负数，
    而且误伤成本比漏追高。这里的作用是留痕：让 /payadmin 和对账时能看出这单已退，
    真正要不要追讨由人来定。返回是否命中一行。
    """
    with _LOCK:
        _init()
        cur = conn().execute(
            "UPDATE pay_orders SET status='refunded' WHERE id=? AND status='paid'", (oid,))
        conn().commit()
    return cur.rowcount > 0


def order_expectation(oid: str) -> Optional[dict[str, Any]]:
    """这个订单「应该」被付多少钱、买的哪档。给 webhook 核对实付用。

    与 get_order 的区别：那个是给买家看的展示结构（金额是 "19.90" 字符串、商品是
    中文名）。核对要的是原始值：产品 key 和分为单位的金额。
    """
    with _LOCK:
        _init()
        row = conn().execute(
            "SELECT product, amount_cents, status FROM pay_orders WHERE id=?",
            (oid,)).fetchone()
    if row is None:
        return None
    return {"product": row["product"], "amount_cents": row["amount_cents"],
            "status": row["status"]}


def _deliver(row, via: str) -> dict[str, Any]:
    """交付。必须在 _LOCK 里调用。

    正常路径：给账户加点，不产生卡密。

    else 分支（造卡）现在走不到 —— 下单已强制登录。留着是因为它是
    "不记名充值券"的现成实现，将来做礼品卡或找人代销时直接复用。
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
    # ⚠️ 这条 WHERE 就是整个幂等的支点：加点的 UPDATE 是无条件的，靠它把订单钉成
    # paid 来保证只发一次。所以它的状态集合**必须**和 deliver_by_id / confirm_manual
    # 的 SELECT 完全一致 —— 只放宽一边的话，expired 订单会「加了点但状态没变」，
    # webhook 一重投就再加一次。改任意一处务必同步另外两处。
    cur = conn().execute(
        "UPDATE pay_orders SET status='paid', paid_at=?, card_key=?, via=? "
        "WHERE id=? AND status IN " + _DELIVERABLE,
        (int(time.time()), key, via, row["id"]))
    if cur.rowcount != 1:
        # 状态在我们读它和写它之间被人改了（并发重投）。回滚，别把点数留下。
        conn().rollback()
        raise RuntimeError(f"订单 {row['id']} 状态已变，放弃本次交付")
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


def deliver_by_id(oid: str, via: str = "dodo") -> Optional[dict[str, Any]]:
    """按订单号交付 —— Dodo webhook 用。

    与 match_amount 的区别：那个靠"金额尾数唯一"反查订单（静态码时代的无奈），
    这里订单号是支付渠道原样带回来的，不存在匹配歧义。

    幂等：只交付尚未 paid 的行。Dodo 会重投 webhook，第二次拿不到行，返回 None
    —— 上层据此回 200 但不重复加点。

    ⚠️ 2026-09-04：这里原本只认 'pending'，于是出现「钱收了、点数不发」的黑洞——
    订单 30 分钟自动转 expired（而且买家自己那个 3 秒一次的轮询就是执行过期的那只
    手），微信支付稍慢一点、或者用户建单后去吃个饭回来再付，webhook 到达时订单已经
    expired → 查不到行 → 返回 None → 上层回 200 → Dodo 认为投递成功不再重投 →
    钱进账、点数永不到账、没有任何告警。**过期只是前端展示概念，不是拒付理由**，
    所以这里必须连 expired 一起认。真正防重复的是 _deliver 里那条 WHERE。
    """
    with _LOCK:
        _init()
        row = conn().execute(
            "SELECT * FROM pay_orders WHERE id=? AND status IN " + _DELIVERABLE,
            (oid,)).fetchone()
        if row is None:
            return None
        return _deliver(row, via)


def confirm_manual(oid: str) -> Optional[dict[str, Any]]:
    """管理页人工确认（到账通知漏了时的兜底）。

    同样要认 expired —— 需要人工兜底的场景，十有八九订单已经躺过 30 分钟了。
    """
    with _LOCK:
        _init()
        row = conn().execute(
            "SELECT * FROM pay_orders WHERE id=? AND status IN " + _DELIVERABLE,
            (oid,)).fetchone()
        if row is None:
            return None
        return _deliver(row, "manual")


def pending_orders() -> list[dict[str, Any]]:
    with _LOCK:
        _init()
        _expire_stale(int(time.time()))
        # 过期单也列出来：客户说"我明明付了"的时候，要能在这页找到它并人工确认。
        # 只列最近 7 天的，免得越积越长。
        rows = conn().execute(
            "SELECT * FROM pay_orders WHERE status IN " + _DELIVERABLE
            + " AND created_at > ? ORDER BY created_at DESC",
            (int(time.time()) - 7 * 86400,)).fetchall()
    return [{"order_id": r["id"], "product": PRODUCTS.get(r["product"], (r["product"],))[0],
             "amount": f"{r['amount_cents'] // 100}.{r['amount_cents'] % 100:02d}",
             "status": r["status"],
             "age_secs": int(time.time()) - r["created_at"]} for r in rows]


AMOUNT_RE = re.compile(r"(\d+)\.(\d{2})")


def parse_amount_cents(text: str) -> Optional[int]:
    """从到账通知原文里抠金额："支付宝到账48.83元" → 4883。
    立减机制保证金额永远带非零分位，所以只认 x.xx 形态，不猜整数。"""
    m = AMOUNT_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1)) * 100 + int(m.group(2))
