"""Serper key 池：多把 key 轮转，**全站共用一个游标**。

2026-09-05 事故：一天测试把额度烧光，线上 Serper 返回 400 "Not enough credits"。
当晚只给写作工具加了轮转，Reddit 选题 / 内容差距 / 外链三个入口还在直接读
`settings.serper_key` 那一把，第二天照样 400（用户 9/6 截图）。

所以轮转放在这里，四个入口共用：谁先撞到额度用尽，谁就把游标推到下一把，
后面的请求直接从好用的那把开始，不用每次都先失败一遍。

配置：`.env` 里 `SERPER_KEYS=k1,k2,k3`（逗号分隔）。没配就退回单把 `SERPER_KEY`，
行为与加轮转之前完全一致。
"""

from __future__ import annotations

import logging

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)

#: 全站共用的游标。进程内状态，重启归零 —— 重启后最多再撞一次用尽的 key 就切走。
_IDX = {"i": 0}


def keys(s: Settings) -> list[str]:
    """可用的 key 列表：优先 SERPER_KEYS（逗号分隔），否则单把 SERPER_KEY。"""
    ks = [k.strip() for k in (getattr(s, "serper_keys", "") or "").split(",") if k.strip()]
    return ks or ([s.serper_key] if s.serper_key else [])


def has_key(s: Settings) -> bool:
    return bool(keys(s))


def first_key(s: Settings) -> str:
    """给护栏检查用的「当前 key」，只用来判断配没配，不要拿去发请求。"""
    ks = keys(s)
    return ks[0] if ks else ""


def _exhausted(r: httpx.Response) -> bool:
    """这把 key 是不是废了：额度用尽（400 + credits）/ 无效 / 被禁。"""
    if r.status_code in (401, 403):
        return True
    if r.status_code == 400:
        try:
            return "credit" in (r.text or "").lower()
        except Exception:  # noqa: BLE001  读不出正文就当不是额度问题
            return False
    return False


async def post(client: httpx.AsyncClient, s: Settings, url: str, payload: dict) -> httpx.Response:
    """发一次 Serper 请求；这把 key 额度用尽就自动换下一把重发。

    返回最后一次的 Response（全部用尽时就是那个 400，交给调用方按原样报错）。
    """
    ks = keys(s)
    if not ks:
        raise RuntimeError("服务端未配置 SERPER_KEY")
    headers_base = {"Content-Type": "application/json"}
    r: httpx.Response | None = None
    for _ in range(len(ks)):
        idx = _IDX["i"] % len(ks)
        r = await client.post(url, headers={"X-API-KEY": ks[idx], **headers_base}, json=payload)
        if not _exhausted(r) or len(ks) == 1:
            return r
        _IDX["i"] = (idx + 1) % len(ks)
        logger.warning("Serper key #%d 额度用尽/无效（%s），切到 #%d",
                       idx + 1, (r.text or "")[:60], _IDX["i"] + 1)
    return r
