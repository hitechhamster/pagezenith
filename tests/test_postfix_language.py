"""补写块必须跟着文章语言走（2026-09-08 线上实测：英文文章里冒出中文小标题）。

    cd api && python ../tests/test_postfix_language.py

事故经过：一篇 English 文章的正文中间出现了一块
    · 核心技术指标 / 出口建议参数
    · Adaptive hybrid ANC / Up to -45dB
条目是英文、表头是中文。来源是 postfix.boost_thin_sections 的补写块 ——
它的 prompt 当时只写了一句「Same language as the section.」，而**调用方明明知道
language 却没往下传**。参考资料里混着中英文时，模型就自己挑了一个。

同一个文件里 ensure_paa_coverage 早就用的是 `- Write in {language}.`，
补写这条只是漏了。这里把「language 真的传到了 prompt 里」钉住。
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

from tools.seo_writer import postfix  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


SECTION = """# Title

## How do you vet a supplier?

Buying from a factory is mostly a question of who is watching them. You should ask
for documentation early and keep the paper trail. A supplier that cannot produce
records on request is a supplier you cannot audit later on. Ask for the records in
writing so the timeline is clear, and keep every reply in one thread for later use.
This gives you something concrete to point at when a dispute starts months later.

## Closing

Wrap up.
"""

REPORT = {"density": {"thin": ["How do you vet a supplier?"]}, "target": {"per100": 4.0}}


async def run(language):
    """跑一次补写，把模型真正收到的 prompt 抓出来。"""
    seen = {}

    async def fake_complete(prompt, task="", temperature=0.0):
        seen["prompt"] = prompt
        return "- Bluetooth latency / under 40ms is the usual acceptance threshold.\n"

    await postfix.boost_thin_sections(
        SECTION, REPORT, facts="", material="", complete=fake_complete,
        sections=["How do you vet a supplier?"], budget=400, language=language)
    return seen.get("prompt", "")


async def main_() -> int:
    print("\n[补写块的语言]")
    p_en = await run("English")
    ok("prompt 里写明了 English", "Write in English." in p_en,
       [l for l in p_en.split("\n") if "Write in" in l])
    ok("旧的模糊说法已去掉", "Same language as the section." not in p_en)

    p_zh = await run("Chinese (Simplified)")
    ok("换语言时 prompt 跟着换", "Write in Chinese (Simplified)." in p_zh,
       [l for l in p_zh.split("\n") if "Write in" in l])

    # 默认值也不能是空串，否则 format 出来是「Write in .」
    p_def = await run("")
    ok("语言为空时兜底成 English", "Write in English." in p_def)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
