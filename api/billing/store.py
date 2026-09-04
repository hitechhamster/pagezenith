"""卡密 / 用量 / 生成结果的持久层（SQLite，单文件，无 ORM）。

为什么是 SQLite：本站单进程、单机部署（香港服务器与观象台共存），
并发是"个位数用户同时生成"级别，SQLite 的 WAL 模式绰绰有余，
而且一个文件就能 cron 备份走 —— 不值得为它引入 Postgres。

三张表：
  cards    卡密（只存 sha256，明文卡号只在造卡时输出一次）
  usage    每次扣点的流水（含真实 token 数与估算成本，用于对账和熔断）
  results  生成结果（卡密即身份，用户换电脑插卡即可取回历史）

⚠️ 明文卡号永不入库、不入日志。数据库文件路径由 BILLING_DB 环境变量指定。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import string
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None

# 卡号字母表：去掉容易看错的 0/O/1/I/l，用户要手抄
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CARD_PREFIX = "PZ"


def db_path() -> Path:
    p = os.environ.get("BILLING_DB") or str(Path(__file__).resolve().parents[2] / "data" / "billing.db")
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    return Path(p)


def conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        with _LOCK:
            if _CONN is None:
                c = sqlite3.connect(str(db_path()), check_same_thread=False, timeout=30)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA busy_timeout=10000")
                _CONN = c
                _init(c)
    return _CONN


def _init(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS cards (
            card_hash     TEXT PRIMARY KEY,
            label         TEXT NOT NULL DEFAULT '',
            total_credits INTEGER NOT NULL,
            used_credits  INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'active',   -- active | disabled
            batch         TEXT NOT NULL DEFAULT '',
            created_at    INTEGER NOT NULL,
            first_used_at INTEGER,
            last_used_at  INTEGER,
            first_ip      TEXT NOT NULL DEFAULT '',
            note          TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS usage (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         INTEGER NOT NULL,
            card_hash  TEXT NOT NULL,
            tool       TEXT NOT NULL,
            action     TEXT NOT NULL,
            tier       TEXT NOT NULL DEFAULT '',
            credits    INTEGER NOT NULL,          -- 正数=扣点，负数=退点
            status     TEXT NOT NULL DEFAULT 'ok',-- ok | refunded | failed
            model      TEXT NOT NULL DEFAULT '',
            tokens_in  INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            est_cost   REAL NOT NULL DEFAULT 0,   -- 估算成本（人民币）
            job_id     TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_usage_card ON usage(card_hash, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_usage_ts   ON usage(ts);

        CREATE TABLE IF NOT EXISTS results (
            id        TEXT PRIMARY KEY,
            card_hash TEXT NOT NULL,
            ts        INTEGER NOT NULL,
            tool      TEXT NOT NULL,
            title     TEXT NOT NULL DEFAULT '',
            summary   TEXT NOT NULL DEFAULT '',
            payload   TEXT NOT NULL             -- JSON
        );
        CREATE INDEX IF NOT EXISTS idx_results_card ON results(card_hash, ts DESC);
        """
    )
    c.commit()


# ── 卡号 ────────────────────────────────────────────────────────────
def hash_card(card_key: str) -> str:
    return hashlib.sha256(card_key.strip().upper().encode()).hexdigest()


def gen_card_key() -> str:
    """PZ-XXXX-XXXX-XXXX（32 字母表 × 12 位 ≈ 2^60，撞库不现实）"""
    body = "".join(secrets.choice(ALPHABET) for _ in range(12))
    return f"{CARD_PREFIX}-{body[:4]}-{body[4:8]}-{body[8:]}"


def mint(count: int, credits: int, batch: str = "", label: str = "") -> list[str]:
    """造卡。返回明文卡号列表（**只在这一刻存在**，库里只有哈希）。"""
    c = conn()
    now = int(time.time())
    out: list[str] = []
    for _ in range(count):
        key = gen_card_key()
        c.execute(
            "INSERT OR IGNORE INTO cards(card_hash,label,total_credits,batch,created_at) "
            "VALUES(?,?,?,?,?)",
            (hash_card(key), label, credits, batch, now),
        )
        out.append(key)
    c.commit()
    return out


def get_card(card_hash: str) -> Optional[sqlite3.Row]:
    return conn().execute("SELECT * FROM cards WHERE card_hash=?", (card_hash,)).fetchone()


def card_state(card_hash: str) -> Optional[dict[str, Any]]:
    row = get_card(card_hash)
    if row is None:
        return None
    return {
        "label": row["label"],
        "total": row["total_credits"],
        "used": row["used_credits"],
        "remaining": row["total_credits"] - row["used_credits"],
        "status": row["status"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
    }


def top_up(card_hash: str, credits: int) -> bool:
    """给已有卡加点（续费用）。"""
    c = conn()
    cur = c.execute("UPDATE cards SET total_credits = total_credits + ? WHERE card_hash=?",
                    (credits, card_hash))
    c.commit()
    return cur.rowcount > 0


# ── 扣点 / 退点 ─────────────────────────────────────────────────────
def spend(card_hash: str, credits: int, *, tool: str, action: str, tier: str = "",
          job_id: str = "", ip: str = "") -> Optional[int]:
    """原子扣点。余额不足返回 None，成功返回 usage.id。

    用 `WHERE total-used >= n` 的条件更新保证并发下不会超支 ——
    两个请求同时来，只有一个能把余额扣成功。
    """
    c = conn()
    now = int(time.time())
    with _LOCK:
        cur = c.execute(
            "UPDATE cards SET used_credits = used_credits + ?, "
            "  last_used_at = ?, "
            "  first_used_at = COALESCE(first_used_at, ?), "
            "  first_ip = CASE WHEN first_ip='' THEN ? ELSE first_ip END "
            "WHERE card_hash=? AND status='active' AND total_credits - used_credits >= ?",
            (credits, now, now, ip, card_hash, credits),
        )
        if cur.rowcount == 0:
            c.commit()
            return None
        uid = c.execute(
            "INSERT INTO usage(ts,card_hash,tool,action,tier,credits,job_id) VALUES(?,?,?,?,?,?,?)",
            (now, card_hash, tool, action, tier, credits, job_id),
        ).lastrowid
        c.commit()
    return uid


def refund(usage_id: int) -> None:
    """生成失败/中途报错时退点，并把流水标成 refunded。用户不为失败付费。"""
    c = conn()
    with _LOCK:
        row = c.execute("SELECT card_hash, credits, status FROM usage WHERE id=?", (usage_id,)).fetchone()
        if row is None or row["status"] != "ok" or row["credits"] <= 0:
            return
        c.execute("UPDATE cards SET used_credits = MAX(0, used_credits - ?) WHERE card_hash=?",
                  (row["credits"], row["card_hash"]))
        c.execute("UPDATE usage SET status='refunded' WHERE id=?", (usage_id,))
        c.commit()


def refund_partial(usage_id: int, credits: int) -> int:
    """部分退点：只退回其中 N 点，流水仍算 ok。返回实际退了多少。

    用在「一次预扣、部分没交付」的场景 —— 典型是配图：正文 + N 张图一次性扣，
    图挂了但正文好好的，整单退不对（用户确实拿到了正文），一点不退更不对
    （首页白纸黑字写着"生成失败自动退点"）。
    """
    if credits <= 0:
        return 0
    c = conn()
    with _LOCK:
        row = c.execute("SELECT card_hash, credits, status FROM usage WHERE id=?",
                        (usage_id,)).fetchone()
        if row is None or row["status"] != "ok" or row["credits"] <= 0:
            return 0
        give = min(int(credits), int(row["credits"]))
        c.execute("UPDATE cards SET used_credits = MAX(0, used_credits - ?) WHERE card_hash=?",
                  (give, row["card_hash"]))
        c.execute("UPDATE usage SET credits = credits - ? WHERE id=?", (give, usage_id))
        c.commit()
    return give


def finalize_usage(usage_id: int, *, model: str = "", tokens_in: int = 0,
                   tokens_out: int = 0, est_cost: float = 0.0) -> None:
    """把真实 token 用量与成本回填到流水（熔断和对账都读这里）。"""
    c = conn()
    with _LOCK:
        c.execute(
            "UPDATE usage SET model=?, tokens_in=?, tokens_out=?, est_cost=? WHERE id=?",
            (model, tokens_in, tokens_out, est_cost, usage_id),
        )
        c.commit()


# ── 限额 / 熔断读数 ─────────────────────────────────────────────────
def _day_start(now: Optional[int] = None) -> int:
    """按 UTC+8 分日（用户和服务器都在这个时区语境里）。"""
    now = now or int(time.time())
    return (now + 8 * 3600) // 86400 * 86400 - 8 * 3600


def card_spent_today(card_hash: str) -> int:
    r = conn().execute(
        "SELECT COALESCE(SUM(credits),0) AS s FROM usage "
        "WHERE card_hash=? AND ts>=? AND status='ok'",
        (card_hash, _day_start()),
    ).fetchone()
    return int(r["s"] or 0)


def global_cost_today() -> float:
    r = conn().execute(
        "SELECT COALESCE(SUM(est_cost),0) AS s FROM usage WHERE ts>=? AND status='ok'",
        (_day_start(),),
    ).fetchone()
    return float(r["s"] or 0.0)


# ── 结果持久化 ──────────────────────────────────────────────────────
def save_result(rid: str, card_hash: str, tool: str, title: str, summary: str,
                payload: dict[str, Any]) -> None:
    c = conn()
    with _LOCK:
        c.execute(
            "INSERT OR REPLACE INTO results(id,card_hash,ts,tool,title,summary,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (rid, card_hash, int(time.time()), tool, title[:200], summary[:500],
             json.dumps(payload, ensure_ascii=False)),
        )
        c.commit()
    _prune_results(card_hash)


def _prune_results(card_hash: str, keep: int = 50, max_age_days: int = 90) -> None:
    """每卡只留最近 keep 条 / max_age_days 天，防止库无限膨胀。"""
    c = conn()
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c.execute("DELETE FROM results WHERE card_hash=? AND ts<?", (card_hash, cutoff))
        c.execute(
            "DELETE FROM results WHERE card_hash=? AND id NOT IN "
            "(SELECT id FROM results WHERE card_hash=? ORDER BY ts DESC LIMIT ?)",
            (card_hash, card_hash, keep),
        )
        c.commit()


def list_results(card_hash: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn().execute(
        "SELECT id,ts,tool,title,summary FROM results WHERE card_hash=? ORDER BY ts DESC LIMIT ?",
        (card_hash, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_result(rid: str, card_hash: str) -> Optional[dict[str, Any]]:
    """按 id 取回，**必须同时匹配卡密** —— 防止拿别人的 id 撞库。"""
    r = conn().execute(
        "SELECT id,ts,tool,title,summary,payload FROM results WHERE id=? AND card_hash=?",
        (rid, card_hash),
    ).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return d


def list_usage(card_hash: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn().execute(
        "SELECT ts,tool,action,tier,credits,status FROM usage "
        "WHERE card_hash=? ORDER BY ts DESC LIMIT ?",
        (card_hash, limit),
    ).fetchall()
    return [dict(r) for r in rows]
