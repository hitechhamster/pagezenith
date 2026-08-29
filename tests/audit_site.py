"""全站自查：把关键路径按真实用户的走法过一遍。

    python tests/audit_site.py [base_url]

不是单元测试，是"上线前扫一眼有没有明显不对"的体检。
分三段：匿名访客 / 新注册用户 / 有余额的用户，每段查它该看到和不该看到的东西。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://pagezenith.com").rstrip("/")
TOK = sys.argv[2] if len(sys.argv) > 2 else ""
OK, BAD = [], []


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print(f"  {'✓' if cond else '✗'}  {name}{('   ' + str(extra)) if extra else ''}")


def mk():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def get(op, path, data=None, tok=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Pay-Token", tok)
    try:
        with op.open(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except json.JSONDecodeError:
                return r.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def main() -> None:
    print(f"体检目标 {BASE}\n")
    anon = mk()

    # ── 一、页面都在，且没有残留旧概念 ──────────────────────────
    print("【页面可达 + 文案一致性】")
    pages = {"/": "首页", "/login": "登录", "/buy": "充值", "/history": "我的记录",
             "/tools/seo-writer": "文章生成", "/payadmin": "收款兜底"}
    html = {}
    for path, label in pages.items():
        st, body = get(anon, path)
        html[path] = body if isinstance(body, str) else ""
        chk(f"{label} {path}", st == 200, f"HTTP {st}")

    for path, label in pages.items():
        if path == "/payadmin":
            continue          # 店主页提到"充值券"是合理的
        bad = [w for w in ("卡密", "输入卡密", "① ") if w in html.get(path, "")]
        chk(f"{label} 无旧「卡密」话术", not bad, bad or "")

    chk("全站无 Google Fonts（国内打不开会阻塞渲染）",
        "fonts.googleapis.com" not in html.get("/", "").replace("fonts.googleapis.com ——", ""),
        "")
    chk("首页购买入口指向 /buy", 'href="/buy"' in html.get("/", ""))
    chk("静态资源带版本号（防旧缓存）",
        "app.css?v=" in html.get("/", "") and "keys.js?v=" in html.get("/", ""))

    # ── 二、匿名访客的边界 ──────────────────────────────────────
    print("\n【匿名访客】")
    st, me = get(anon, "/api/auth/me")
    chk("身份接口返回未登录而非报错", st == 200 and me.get("account") is None, me)
    st, _ = get(anon, "/api/billing/balance")
    chk("查余额被拒", st == 401)
    st, _ = get(anon, "/api/pay/order", {"product": "standard"})
    chk("下单被拒（服务端强制，不只前端拦）", st == 401)
    st, _ = get(anon, "/api/pay/pending")
    chk("店主接口被拒", st == 401)
    st, prods = get(anon, "/api/pay/products")
    chk("商品列表公开可读", st == 200 and len(prods) == 3,
        [p["price"] for p in prods] if isinstance(prods, list) else prods)
    st, _ = get(anon, "/api/auth/redeem", {"card_key": "PZ-AAAA-AAAA-AAAA"})
    chk("未登录不能兑换", st == 401)

    # ── 三、新用户从零走一遍 ────────────────────────────────────
    print("\n【新注册用户】")
    u = mk()
    em = f"audit{int(time.time())}@example.com"
    st, r = get(u, "/api/auth/register", {"email": em, "password": "auditpass123"})
    chk("注册成功", st == 200, em)
    st, me = get(u, "/api/auth/me")
    chk("会话立即生效", me.get("account", {}).get("email") == em)
    chk("新账户 0 点", me.get("balance", {}).get("remaining") == 0)
    st, bal = get(u, "/api/billing/balance")
    chk("登录后无需卡密即可过鉴权", st == 200, bal)

    st, o = get(u, "/api/pay/order", {"product": "trial"})
    chk("能下单", st == 200 and o.get("order_id"), o.get("order_id"))
    chk("订单入账户（不发卡密）", o.get("to_account") is True)
    chk("金额带非零分位（靠它认单）", o.get("amount_cents", 0) % 100 != 0, "¥" + str(o.get("amount")))
    st, so = get(anon, f"/api/pay/order/{o['order_id']}")
    chk("订单状态可公开查询（买家轮询用）", st == 200 and so.get("status") == "pending")
    chk("未付款时不泄露任何凭证", "card_key" not in so)

    # ── 四、有余额之后 ──────────────────────────────────────────
    if TOK:
        print("\n【模拟到账 → 有余额】")
        st, n = get(u, "/api/pay/notify", {"amount": o["amount"]}, tok=TOK)
        chk("到账通知自动匹配", n.get("matched") is True, n)
        st, me2 = get(u, "/api/auth/me")
        chk("点数进账户", me2.get("balance", {}).get("remaining") == 20, me2.get("balance"))
        st, n2 = get(u, "/api/pay/notify", {"amount": o["amount"]}, tok=TOK)
        chk("同笔重复通知不重复发放", n2.get("matched") is not True, n2.get("reason"))
    else:
        print("\n【模拟到账】跳过 —— 没给 PAY_NOTIFY_TOKEN")

    # ── 五、防回归：这些坑今天都踩过 ────────────────────────────
    print("\n【已踩过的坑，防回归】")
    keys = get(anon, "/shared/keys.js")[1]
    chk("身份刷新是无条件的（不是 if(read())）",
        "ready = refresh();" in keys and "if (read()) refresh();" not in keys)
    chk("不再覆盖页面自带的 onclick",
        'if (!el.getAttribute("onclick"))' in keys)
    chk("卡密弹窗已移除", "km-card" not in keys and "输入卡密" not in keys)
    chk("401 自动跳转排除了 auth / pay 接口",
        '/api/auth/' in keys and '/api/pay/' in keys and "selfHandled" in keys)
    buy = html.get("/buy", "")
    chk("/buy 未登录会送去登录页", "/login?next=" in buy)
    chk("/buy 文案是充值不是买卡", "充值" in buy and "卡密立刻显示" not in buy)

    print(f"\n{len(OK)} 项通过，{len(BAD)} 项有问题")
    if BAD:
        print("需要处理：")
        for b in BAD:
            print("  -", b)
        sys.exit(1)


if __name__ == "__main__":
    main()
