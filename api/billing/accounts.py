"""账户体系：邮箱 + 密码，点数记在账户上。

## 设计要点：账户内部挂一张"钱包卡"

整个计费层（charge / spend / usage / results）都是按 card_hash 工作的。
与其重写它，不如让每个账户在注册时静默造一张卡，把哈希存在账户行上：

  账户 ──(wallet_hash)──> cards 表的一行 ──> 全部既有计费逻辑原样可用

好处是付费链路核心**零改动**，改的只有鉴权那一层：
从"读请求头里的卡密"变成"读会话 → 查账户 → 拿钱包卡哈希"。

卡密没有废弃，降级成**充值券**：一张不记名的、值 N 点的凭证，兑进账户即失效。
这样已售出的卡继续有效，将来找人代销或做礼品卡也有现成的东西。

## 密码与会话

- 密码用 hashlib.scrypt（标准库，不引依赖），每个账户独立 salt
- 会话 token 只存哈希；cookie 走 HttpOnly + SameSite=Lax
- 没有邮件发送 → 暂无自助找回，忘记密码找站长。接了 SMTP 再补
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from typing import Any, Optional

from .store import _LOCK, conn, hash_card, mint

SESSION_TTL = 60 * 60 * 24 * 30        # 30 天
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _init() -> None:
    conn().executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL UNIQUE,
            pass_salt    TEXT NOT NULL,
            pass_hash    TEXT NOT NULL,
            wallet_hash  TEXT NOT NULL,      -- cards 表里那张"钱包卡"的哈希
            created_at   INTEGER NOT NULL,
            last_login_at INTEGER,
            status       TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash  TEXT PRIMARY KEY,
            account_id  INTEGER NOT NULL,
            created_at  INTEGER NOT NULL,
            expires_at  INTEGER NOT NULL,
            ip          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_sess_acct ON sessions(account_id);
        CREATE INDEX IF NOT EXISTS idx_sess_exp  ON sessions(expires_at);
    """)
    conn().commit()


# ── 密码 ────────────────────────────────────────────────────────────
def _hash_pw(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT).hex()


def _verify_pw(password: str, salt: str, expect: str) -> bool:
    return hmac.compare_digest(_hash_pw(password, salt), expect)


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def check_signup(email: str, password: str) -> None:
    """不合规就抛 ValueError，消息直接给用户看。"""
    if not EMAIL_RE.match(_norm_email(email)):
        raise ValueError("邮箱格式不对。")
    if len(password or "") < 8:
        raise ValueError("密码至少 8 位。")


# ── 注册 / 登录 ─────────────────────────────────────────────────────
def register(email: str, password: str, ip: str = "") -> dict[str, Any]:
    check_signup(email, password)
    em = _norm_email(email)
    with _LOCK:
        _init()
        c = conn()
        if c.execute("SELECT 1 FROM accounts WHERE email=?", (em,)).fetchone():
            raise ValueError("这个邮箱已经注册过了，直接登录吧。")
        # 钱包卡：0 点起步，用户看不到它的卡号（明文当场丢弃）
        wallet_key = mint(1, 0, batch="wallet", label="账户钱包")[0]
        salt = secrets.token_hex(16)
        now = int(time.time())
        cur = c.execute(
            "INSERT INTO accounts(email,pass_salt,pass_hash,wallet_hash,created_at,last_login_at)"
            " VALUES(?,?,?,?,?,?)",
            (em, salt, _hash_pw(password, salt), hash_card(wallet_key), now, now))
        c.commit()
        aid = cur.lastrowid
    return {"id": aid, "email": em, "token": _new_session(aid, ip)}


def login(email: str, password: str, ip: str = "") -> dict[str, Any]:
    em = _norm_email(email)
    with _LOCK:
        _init()
        row = conn().execute("SELECT * FROM accounts WHERE email=?", (em,)).fetchone()
    # 邮箱不存在和密码错给同一句话 —— 别帮人枚举哪些邮箱注册过
    if row is None or not _verify_pw(password or "", row["pass_salt"], row["pass_hash"]):
        raise ValueError("邮箱或密码不对。")
    if row["status"] != "active":
        raise ValueError("这个账户已被停用，请联系站长。")
    with _LOCK:
        conn().execute("UPDATE accounts SET last_login_at=? WHERE id=?",
                       (int(time.time()), row["id"]))
        conn().commit()
    return {"id": row["id"], "email": row["email"], "token": _new_session(row["id"], ip)}


def change_password(account_id: int, old: str, new: str) -> None:
    if len(new or "") < 8:
        raise ValueError("新密码至少 8 位。")
    with _LOCK:
        _init()
        row = conn().execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None or not _verify_pw(old or "", row["pass_salt"], row["pass_hash"]):
            raise ValueError("当前密码不对。")
        salt = secrets.token_hex(16)
        conn().execute("UPDATE accounts SET pass_salt=?, pass_hash=? WHERE id=?",
                       (salt, _hash_pw(new, salt), account_id))
        # 改密码踢掉其他会话，这是改密码本来就该有的语义
        conn().execute("DELETE FROM sessions WHERE account_id=?", (account_id,))
        conn().commit()


# ── 会话 ────────────────────────────────────────────────────────────
def _new_session(account_id: int, ip: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _LOCK:
        conn().execute(
            "INSERT INTO sessions(token_hash,account_id,created_at,expires_at,ip)"
            " VALUES(?,?,?,?,?)",
            (_tok_hash(token), account_id, now, now + SESSION_TTL, ip))
        conn().execute("DELETE FROM sessions WHERE expires_at<?", (now,))
        conn().commit()
    return token


def _tok_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def account_by_token(token: str) -> Optional[dict[str, Any]]:
    if not token:
        return None
    with _LOCK:
        _init()
        row = conn().execute(
            "SELECT a.* FROM sessions s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND a.status='active'",
            (_tok_hash(token), int(time.time()))).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "email": row["email"], "wallet_hash": row["wallet_hash"]}


def logout(token: str) -> None:
    if not token:
        return
    with _LOCK:
        conn().execute("DELETE FROM sessions WHERE token_hash=?", (_tok_hash(token),))
        conn().commit()


# ── 点数 ────────────────────────────────────────────────────────────
def add_credits(account_id: int, credits: int, note: str = "") -> None:
    """给账户钱包加点（购买成功、后台补偿都走这里）。"""
    if credits <= 0:
        return
    with _LOCK:
        _init()
        row = conn().execute("SELECT wallet_hash FROM accounts WHERE id=?",
                             (account_id,)).fetchone()
        if row is None:
            raise ValueError("账户不存在。")
        conn().execute(
            "UPDATE cards SET total_credits = total_credits + ?, "
            "note = CASE WHEN ?='' THEN note ELSE note || ? || char(10) END "
            "WHERE card_hash=?",
            (credits, note, note, row["wallet_hash"]))
        conn().commit()


def redeem_card(account_id: int, card_key: str) -> int:
    """把一张不记名卡密的剩余点数转进账户钱包，卡随即作废。返回转入点数。"""
    ch = hash_card((card_key or "").strip().upper())
    with _LOCK:
        _init()
        c = conn()
        card = c.execute("SELECT * FROM cards WHERE card_hash=?", (ch,)).fetchone()
        if card is None:
            raise ValueError("卡密不存在。")
        if card["status"] != "active":
            raise ValueError("这张卡已经用过或被停用了。")
        if card["batch"] == "wallet":
            raise ValueError("这不是一张充值卡。")
        left = card["total_credits"] - card["used_credits"]
        if left <= 0:
            raise ValueError("这张卡已经没有余额了。")
        wallet = c.execute("SELECT wallet_hash FROM accounts WHERE id=?",
                           (account_id,)).fetchone()
        if wallet is None:
            raise ValueError("账户不存在。")
        # 先作废原卡、再加点：万一中间挂了，宁可用户少一次也不能凭空多出点数
        c.execute("UPDATE cards SET status='disabled', "
                  "note = note || ? || char(10) WHERE card_hash=? AND status='active'",
                  (f"已兑换至账户 #{account_id}", ch))
        if c.total_changes == 0:
            raise ValueError("这张卡已经用过了。")
        c.execute("UPDATE cards SET total_credits = total_credits + ? WHERE card_hash=?",
                  (left, wallet["wallet_hash"]))
        c.commit()
    return left
