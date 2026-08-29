"""账户 + 购买 + 兑换全链路冒烟测试。

    python tests/test_account_flow.py [base_url]

重点验三件事：
  1. 会话能当身份用（工具接口不再需要 X-Card-Key）
  2. 登录着买 → 点数进账户、不产生卡密
  3. 老卡密还能兑进账户，且不能兑两次
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8012").rstrip("/")
DB = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pz_acct_test.db"
TOK = "testtok123"
PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


def opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(op, path, data=None, tok=None, card=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(data).encode() if data is not None else None,
        method="POST" if data is not None else "GET")
    req.add_header("Content-Type", "application/json")
    if tok:
        req.add_header("X-Pay-Token", tok)
    if card:
        req.add_header("X-Card-Key", card)
    try:
        with op.open(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:  # noqa: BLE001
            return e.code, {"raw": body[:200]}


def main() -> None:
    import time
    em = f"t{int(time.time())}@example.com"
    A, B = opener(), opener()          # A = 我们的用户，B = 匿名访客
    print(f"目标 {BASE}   账号 {em}\n")

    # ── 注册 ────────────────────────────────────────────────────
    st, r = call(A, "/api/auth/register", {"email": em, "password": "short"})
    ok("弱密码被拒", st == 400, r.get("detail"))
    st, r = call(A, "/api/auth/register", {"email": "不是邮箱", "password": "goodpass123"})
    ok("烂邮箱被拒", st == 400, r.get("detail"))

    st, r = call(A, "/api/auth/register", {"email": em, "password": "goodpass123"})
    ok("注册成功", st == 200 and r.get("email") == em, r.get("id"))

    st, r = call(A, "/api/auth/register", {"email": em, "password": "goodpass123"})
    ok("同邮箱不能重复注册", st == 400, r.get("detail"))

    st, me = call(A, "/api/auth/me")
    ok("会话有效，能读到自己", me.get("account", {}) and me["account"]["email"] == em)
    ok("新账户 0 点", me["balance"]["remaining"] == 0, me["balance"])

    st, me2 = call(B, "/api/auth/me")
    ok("匿名访客 account 为 null", me2.get("account") is None)

    # ── 会话即身份：不带卡密也能过鉴权 ──────────────────────────
    st, bal = call(A, "/api/billing/balance")
    ok("登录后无需卡密即可读余额", st == 200 and bal["remaining"] == 0, bal)
    st, _ = call(B, "/api/billing/balance")
    ok("匿名且无卡密 → 401", st == 401)

    # ── 登录着买：点数进账户，不发卡 ────────────────────────────
    st, o = call(A, "/api/pay/order", {"product": "standard"})
    ok("下单标记为入账户", o.get("to_account") is True, o.get("order_id"))
    st, n = call(A, "/api/pay/notify", {"amount": o["amount"]}, tok=TOK)
    ok("到账自动交付", n.get("matched"), n)

    st, s = call(A, f"/api/pay/order/{o['order_id']}")
    ok("已付订单不含卡密", s["status"] == "paid" and "card_key" not in s, s)
    ok("订单标记 to_account", s.get("to_account") is True)

    st, me3 = call(A, "/api/auth/me")
    ok("点数已进账户", me3["balance"]["remaining"] == 60, me3["balance"])

    # ── 匿名购买已关闭（2026-08-29 只留登录充值）────────────────
    st, _ = call(B, "/api/pay/order", {"product": "trial"})
    ok("匿名下单被拒", st == 401)

    # 兑换券改由 mint 直接造 —— 它现在的用途是礼品卡 / 补偿点数，不再来自购买
    import subprocess
    voucher = subprocess.run(
        [sys.executable, "-c",
         "import os,sys;os.environ.setdefault('BILLING_DB',r'%s');"
         "sys.path.insert(0,'api');from billing.store import mint;"
         "print(mint(1,20,batch='gift',label='测试券')[0])" % DB],
        capture_output=True, text=True, cwd=".").stdout.strip()
    ok("造出一张兑换券", voucher.startswith("PZ-"), voucher)
    anon_key = voucher

    # ── 兑换：把那张卡并进账户 ──────────────────────────────────
    st, rd = call(A, "/api/auth/redeem", {"card_key": anon_key})
    ok("卡密兑换成功", st == 200 and rd.get("credits") == 20, rd)
    st, me4 = call(A, "/api/auth/me")
    ok("兑换后余额相加", me4["balance"]["remaining"] == 80, me4["balance"])

    st, rd2 = call(A, "/api/auth/redeem", {"card_key": anon_key})
    ok("同一张卡不能兑两次", st == 400, rd2.get("detail"))

    st, rd3 = call(A, "/api/auth/redeem", {"card_key": "PZ-XXXX-XXXX-XXXX"})
    ok("不存在的卡被拒", st == 400, rd3.get("detail"))

    st, rd4 = call(B, "/api/auth/redeem", {"card_key": "PZ-XXXX-XXXX-XXXX"})
    ok("未登录不能兑换", st == 401)

    # ── 登录 / 登出 ─────────────────────────────────────────────
    C = opener()
    st, _ = call(C, "/api/auth/login", {"email": em, "password": "错的密码"})
    ok("错密码登录失败", st == 401)
    st, r = call(C, "/api/auth/login", {"email": em, "password": "goodpass123"})
    ok("登录成功", st == 200 and r.get("email") == em)
    st, meC = call(C, "/api/auth/me")
    ok("新会话看到同一余额", meC["balance"]["remaining"] == 80)

    st, _ = call(C, "/api/auth/logout", {})
    st, meC2 = call(C, "/api/auth/me")
    ok("登出后会话失效", meC2.get("account") is None)

    # ── 改密码踢掉所有会话 ──────────────────────────────────────
    st, r = call(A, "/api/auth/password",
                 {"old_password": "goodpass123", "new_password": "brandnew456"})
    ok("改密码成功", st == 200 and r.get("relogin"))
    st, meA = call(A, "/api/auth/me")
    ok("改密码后旧会话失效", meA.get("account") is None)
    D = opener()
    st, _ = call(D, "/api/auth/login", {"email": em, "password": "brandnew456"})
    ok("新密码能登录", st == 200)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
