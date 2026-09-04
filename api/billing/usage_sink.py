"""一次请求内的「用量回收槽」。

## 为什么需要它

计费的 `charge()` 上下文里有个 `tx.report_tokens(model, tin, tout)`，SEO 文章生成会
把每次 LLM 调用的真实 token 回填进去，最后写进 usage.est_cost。而
**全局成本熔断（BILLING_GLOBAL_DAILY_CNY）就是靠 SUM(est_cost) 判断的。**

问题：另外五个工具（内容差距 / 质检 / Reddit / 外链 / 站点侦察）从来不报，
它们每一条流水的 est_cost 都是 0 —— 2026-09-04 实测确认，跑三个工具全部记 ¥0。
于是那道熔断只看得见文章生成一个工具，对其余五个是瞎的（而内容差距恰恰是
单次成本最高的：SERP + 抓取 + embeddings + LLM）。

## 为什么用 contextvars 而不是把 tx 传下去

这五个工具共用 `seo_gap/clients/llm.py`，但**一次请求里会 new 出好几个 LLMClient**
（pipeline / report_v2 / extractor / semantic 各一个）。挨个改构造函数要动一大片调用点，
而且以后新加的地方很容易忘了传。

contextvars 是按「异步上下文」隔离的：charge() 进来时把回收函数塞进去，
LLMClient 在任何深度直接取用，出去时还原。`asyncio.create_task` 会复制当前 context，
所以请求内并发的子任务也报得上来。跨请求不会串（各自的 context 不同）。

## 加新 LLM 调用点时

记得调一次 `note_openai_usage(data, 模型名)` —— 少调一次，那部分成本就对熔断隐身。
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

# 值是 tx.report_tokens 那种签名：(model, tokens_in, tokens_out) -> None
_SINK: contextvars.ContextVar[Optional[Callable[[str, int, int], None]]] = \
    contextvars.ContextVar("pz_usage_sink", default=None)


def set_sink(fn: Callable[[str, int, int], None]):
    """由 charge() 调用。返回的 token 要在退出时传给 reset_sink。"""
    return _SINK.set(fn)


def reset_sink(token) -> None:
    try:
        _SINK.reset(token)
    except ValueError:
        # token 属于别的 context（正常情况下不会发生）。宁可漏还原也不能把请求打挂。
        pass


def report(model: str, tokens_in: int, tokens_out: int = 0) -> None:
    """上报一次用量。没有计费上下文（比如脚本里直接调）时静默忽略。"""
    fn = _SINK.get()
    if fn is None:
        return
    try:
        fn(model or "unknown", int(tokens_in or 0), int(tokens_out or 0))
    except Exception:  # noqa: BLE001
        pass          # 记账失败绝不能影响用户拿到结果


def note_openai_usage(data: Any, model: str) -> None:
    """从 OpenAI 兼容响应里取 usage 并上报。

    chat 返回 prompt_tokens / completion_tokens；embeddings 只有 prompt_tokens。
    字段缺失就当 0 —— 少记一点总比抛异常打断用户的请求好。
    """
    if not isinstance(data, dict):
        return
    u = data.get("usage")
    if not isinstance(u, dict):
        return
    report(model,
           u.get("prompt_tokens") or u.get("input_tokens") or 0,
           u.get("completion_tokens") or u.get("output_tokens") or 0)
