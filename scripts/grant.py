"""开户 / 加点（店主用）。

    python scripts/grant.py <邮箱> <点数> [--password 密码] [--note 备注]

用途：给自己开广告测试账户、给客户补偿点数、给合作方开号。
已存在的邮箱直接加点，不会重复建号。

密码不传就随机生成并打印出来 —— 打印的这一次是唯一一次，库里只存哈希。
"""
from __future__ import annotations

import argparse
import pathlib
import secrets
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("credits", type=int, nargs="?", default=0,
                    help="要加的点数，只改密码时可省略")
    ap.add_argument("--password", default="", help="新建账户时指定密码")
    ap.add_argument("--set-password", default="",
                    help="给已存在的账户重设密码（用户收不到邮件时的人工兜底）")
    ap.add_argument("--note", default="")
    ap.add_argument("--no-mail", action="store_true", help="不发欢迎信")
    a = ap.parse_args()

    from billing import accounts, mailer, store

    em = a.email.strip().lower()
    row = store.conn().execute("SELECT id,email FROM accounts WHERE email=?", (em,)).fetchone()

    if row is None:
        pw = a.password or secrets.token_urlsafe(9)
        import sqlite3 as _sq
        import time as _t
        for i in range(6):                      # 网站在跑时可能撞写锁，退避重试
            try:
                out = accounts.register(em, pw); break
            except _sq.OperationalError as exc:
                if "locked" not in str(exc).lower() or i == 5:
                    raise
                _t.sleep(0.4 * (i + 1))
        aid = out["id"]
        print(f"新建账户 #{aid}  {em}")
        print(f"  密码：{pw}      ← 只打印这一次，库里只有哈希")
    else:
        aid = row["id"]
        print(f"账户已存在 #{aid}  {em}")
        if a.set_password:
            accounts.set_password(aid, a.set_password)
            print(f"  密码已重设为：{a.set_password}   （该账户所有会话已失效）")
        elif a.password:
            print("  （--password 只在新建时生效；给已有账户改密码用 --set-password）")

    accounts.add_credits(aid, a.credits, a.note or "店主手动发放")

    st = store.card_state(
        store.conn().execute("SELECT wallet_hash FROM accounts WHERE id=?",
                             (aid,)).fetchone()["wallet_hash"])
    print(f"  已加 {a.credits} 点 → 当前余额 {st['remaining']}/{st['total']}")

    if not a.no_mail and row is None:
        sent = mailer.send_welcome(em, st["remaining"])
        print(f"  欢迎信：{'已发出' if sent else '没发出（未配 RESEND_API_KEY 或发信失败）'}")


if __name__ == "__main__":
    main()
