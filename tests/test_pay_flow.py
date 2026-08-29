"""免签支付全链路冒烟测试（不花钱，不碰生产库）。

    python tests/test_pay_flow.py [base_url]

覆盖：下单拿到唯一金额 / 同档并发下单金额不撞 / 未付时查不到卡密 /
      到账通知自动发卡 / 卡密真能用（余额对得上）/ 金额撞车时拒绝自动发 /
      人工兜底确认 / 无口令打不了店主接口 / 订单号即凭证。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8012").rstrip("/")
TOK = "testtok123"
PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


def call(path, data=None, tok=None, method=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("POST" if data is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Pay-Token", tok)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body[:200]}


def main() -> None:
    print(f"目标 {BASE}\n")

    st, prods = call("/api/pay/products")
    ok("商品列表可取", st == 200 and len(prods) == 3, [p["id"] for p in prods])

    # ── 下单 ────────────────────────────────────────────────────
    st, o1 = call("/api/pay/order", {"product": "standard"})
    ok("下单成功", st == 200 and o1.get("order_id"), o1.get("order_id"))
    ok("金额带非零分位（靠它认单）", o1["amount_cents"] % 100 != 0, "¥" + o1["amount"])
    ok("金额低于标价（立减机制）", o1["amount_cents"] < o1["list_cents"],
       f"{o1['list_cents']} → {o1['amount_cents']}")

    st, o2 = call("/api/pay/order", {"product": "standard"})
    ok("同档第二单金额不撞", o2["amount_cents"] != o1["amount_cents"],
       f"{o1['amount']} vs {o2['amount']}")

    st, bad = call("/api/pay/order", {"product": "不存在的档"})
    ok("非法商品被拒", st == 400)

    # ── 未付时的状态 ────────────────────────────────────────────
    st, s1 = call(f"/api/pay/order/{o1['order_id']}")
    ok("未付时状态 pending", s1["status"] == "pending")
    ok("未付时不泄露卡密", "card_key" not in s1)

    st, _ = call("/api/pay/order/PZZZZZZ")
    ok("不存在的订单 404", st == 404)

    # ── 店主接口鉴权 ────────────────────────────────────────────
    st, _ = call("/api/pay/pending")
    ok("无口令查待付 → 401", st == 401)
    st, _ = call("/api/pay/notify", {"amount": o1["amount"]}, tok="wrong-token")
    ok("错口令发通知 → 401", st == 401)

    # ── 到账通知自动发卡 ────────────────────────────────────────
    st, n = call("/api/pay/notify", {"text": f"支付宝到账{o1['amount']}元"}, tok=TOK)
    ok("到账通知自动匹配", st == 200 and n.get("matched") and n["order_id"] == o1["order_id"], n)

    st, s1b = call(f"/api/pay/order/{o1['order_id']}")
    ok("买家轮询拿到卡密", s1b["status"] == "paid" and s1b.get("card_key", "").startswith("PZ-"),
       s1b.get("card_key"))

    # ── 发出去的卡真能用 ────────────────────────────────────────
    req = urllib.request.Request(BASE + "/api/billing/balance")
    req.add_header("X-Card-Key", s1b["card_key"])
    with urllib.request.urlopen(req, timeout=30) as r:
        bal = json.loads(r.read().decode())
    ok("卡密可用且点数正确", bal["total"] == 60 and bal["remaining"] == 60, bal)

    # ── 重复通知不该再发一张 ────────────────────────────────────
    st, n2 = call("/api/pay/notify", {"amount": o1["amount"]}, tok=TOK)
    ok("同一笔重复通知不重发", not n2.get("matched"), n2.get("reason"))

    # ── 金额撞车时拒绝自动发（宁可人工，不能发错人）──────────────
    st, a = call("/api/pay/order", {"product": "trial"})
    st, dup = call("/api/pay/notify", {"amount": "0.01"}, tok=TOK)
    ok("匹配不上时不乱发", not dup.get("matched"), dup.get("reason"))

    # ── 人工兜底 ────────────────────────────────────────────────
    st, pend = call("/api/pay/pending", tok=TOK)
    ids = [x["order_id"] for x in pend["items"]]
    ok("待付列表含未支付订单", o2["order_id"] in ids and a["order_id"] in ids, len(ids))

    st, c = call("/api/pay/confirm", {"order_id": o2["order_id"]}, tok=TOK)
    ok("人工确认成功", st == 200 and c.get("ok"))
    st, s2 = call(f"/api/pay/order/{o2['order_id']}")
    ok("人工确认后出卡", s2["status"] == "paid" and s2.get("card_key", "").startswith("PZ-"))

    st, c2 = call("/api/pay/confirm", {"order_id": o2["order_id"]}, tok=TOK)
    ok("已处理订单不能重复确认", st == 404)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
