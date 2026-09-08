"""PAA 意图过滤（2026-09-08 用户报：找代工厂的文章写进了「哪个牌子最好」）。

    cd api && python ../tests/test_paa_intent_filter.py

原来只有 density_audit._on_topic 那道词面过滤，实测两个方向都错：
  放行「What is the best headphone brand in China?」（唯一实词 brand 恰好进了竞品标题）
  丢掉「What is the minimum order quantity for OEM headphones?」（实词没进标题）
因为它判的是"某个词在不在竞品标题里"，跟搜索意图无关。factory（B2B 找代工）
和 brand（B2C 挑品牌）共享全部修饰词，只能问模型。

这里用假的 complete 把 LLM 换掉，重点钉**护栏**而不是模型判得准不准：
过滤器抽风时必须 fail-open，PAA 是本产品少数几个"真实数据不是推测"的输入。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["USE_MOCKS"] = "true"
API = pathlib.Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API))

from tools.seo_writer.workflow import SEOWriter  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


Q_BRAND = "What is the best headphone brand in China?"
Q_MOQ = "What is the minimum order quantity for OEM headphones?"
Q_AUDIT = "How do you audit a headphone factory?"

SEARCH = f"""Title: Best Headphone Factory in China
URL: https://example.com/a
SourceType: blog
Content: some competitor text
---
Q: {Q_AUDIT}
Q: {Q_BRAND}
Q: {Q_MOQ}
Rel: headphone oem china"""


class FakeLLM:
    """只替换 complete()，其余不碰。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def complete(self, prompt, task="", temperature=0.0):
        self.calls += 1
        self.last_prompt = prompt
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def writer(reply):
    w = SEOWriter.__new__(SEOWriter)          # 不跑 __init__，只测这一个方法
    w.llm = FakeLLM(reply)
    return w


async def run(reply):
    w = writer(reply)
    text, dropped = await w.filter_questions(SEARCH, "best headphone factory in china",
                                             "headphone oem", "sourcing guide")
    return text, dropped, w.llm


async def main_() -> int:
    print("\n[PAA 意图过滤]")

    # 正常：模型只留两个 B2B 问题
    text, dropped, llm = await run(f"{Q_AUDIT}\n{Q_MOQ}")
    ok("跑题的品牌问题被剔掉", dropped == [Q_BRAND], dropped)
    ok("B2B 问题都留着", Q_AUDIT in text and Q_MOQ in text)
    ok("Q: 行真的从搜索文本里删了", f"Q: {Q_BRAND}" not in text)
    ok("竞品正文和 Rel 行没被误伤",
       "Content: some competitor text" in text and "Rel: headphone oem china" in text)
    ok("主关键词进了 prompt", "best headphone factory in china" in llm.last_prompt)

    # 模型带编号 / 破折号前缀也认
    _, dropped2, _ = await run(f"- {Q_AUDIT}\n2. {Q_MOQ}")
    ok("容忍编号和短横前缀", dropped2 == [Q_BRAND], dropped2)

    # ── 护栏 ──
    text3, dropped3, _ = await run("NONE")
    ok("模型说一个都不留 → 全部保留（fail-open）", not dropped3 and text3 == SEARCH)

    text4, dropped4, _ = await run("")
    ok("模型返回空 → 全部保留", not dropped4 and text4 == SEARCH)

    text5, dropped5, _ = await run(RuntimeError("boom"))
    ok("调用抛异常 → 全部保留", not dropped5 and text5 == SEARCH)

    # 模型自己编了一个问题：不在原列表里，不算数 → 等于一个都没留 → fail-open
    text6, dropped6, _ = await run("How do I ship headphones to the US?")
    ok("模型编的问题不算数（防改写/发明）", not dropped6 and text6 == SEARCH)

    # 只有一个问题时不值得花一次调用
    one = "Title: t\nURL: u\nContent: c\n---\nQ: only one?"
    w = writer("only one?")
    t7, d7 = await w.filter_questions(one, "k", "s", "t")
    ok("问题少于 2 个时不调用 LLM", w.llm.calls == 0 and t7 == one and not d7)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
