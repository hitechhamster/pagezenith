"""断线不丢的 SSE 任务注册表。

为什么需要它：原来的写法是"在 SSE 生成器里干活"，客户端一断（关标签页、
手机切后台、网络抖动），生成器被取消，**活白干、token 白烧**。
BYOK 时代烧的是用户自己的 key，无所谓；卡密时代这就是退款纠纷。

现在：干活的是独立 asyncio.Task，SSE 只是它的**订阅者**。
断了就断了，任务照跑照落库，用户重连（或去"我的记录"）都能拿回结果。

进程内注册表，重启即失 —— 但结果已落 SQLite，重启只丢"正在跑的那一篇"，
可接受（真跑完的都在库里）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

JOB_TTL = 1800          # 完成后保留半小时供重连取回
MAX_JOBS = 200


@dataclass
class Job:
    id: str
    card_hash: str
    tool: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    _waiters: list[asyncio.Event] = field(default_factory=list)
    task: Optional[asyncio.Task] = None

    def emit(self, evt: dict) -> None:
        self.events.append(evt)
        for ev in self._waiters:
            ev.set()
        self._waiters.clear()

    def finish(self) -> None:
        self.done = True
        self.finished = time.time()
        for ev in self._waiters:
            ev.set()
        self._waiters.clear()

    async def wait_more(self, timeout: float = 30.0) -> None:
        if self.done:
            return
        ev = asyncio.Event()
        self._waiters.append(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


_JOBS: dict[str, Job] = {}


def _gc() -> None:
    now = time.time()
    dead = [k for k, j in _JOBS.items()
            if j.done and j.finished and now - j.finished > JOB_TTL]
    for k in dead:
        _JOBS.pop(k, None)
    if len(_JOBS) > MAX_JOBS:
        for k, _ in sorted(_JOBS.items(), key=lambda kv: kv[1].created)[: len(_JOBS) - MAX_JOBS]:
            j = _JOBS.get(k)
            if j and j.done:
                _JOBS.pop(k, None)


def start(card_hash: str, tool: str,
          work: Callable[[Job], Awaitable[None]]) -> Job:
    """起一个后台任务。work 内部用 job.emit(...) 推事件。"""
    _gc()
    job = Job(id=uuid.uuid4().hex[:16], card_hash=card_hash, tool=tool)
    _JOBS[job.id] = job

    async def runner() -> None:
        try:
            await work(job)
        except asyncio.CancelledError:      # 只有进程关停才会到这里
            job.emit({"type": "error", "message": "任务被中断。"})
            raise
        except Exception as exc:            # noqa: BLE001
            logger.exception("job %s failed", job.id)
            job.emit({"type": "error", "message": str(exc)})
        finally:
            job.finish()

    job.task = asyncio.create_task(runner())
    return job


def get(job_id: str, card_hash: str) -> Optional[Job]:
    """取任务，**必须同卡** —— 别人的 job_id 拿不到内容。"""
    j = _JOBS.get(job_id)
    return j if j and j.card_hash == card_hash else None


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


async def stream(job: Job, from_index: int = 0) -> AsyncIterator[str]:
    """订阅任务事件流。可从任意位置续订（重连时带上已收到的条数）。"""
    yield _sse({"type": "job", "job_id": job.id, "from": from_index})
    i = from_index
    while True:
        while i < len(job.events):
            yield _sse(job.events[i])
            i += 1
        if job.done:
            return
        await job.wait_more()
        if i >= len(job.events) and not job.done:
            yield ": keep-alive\n\n"        # 心跳，防中间层掐连接
