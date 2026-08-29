"""验证「年级交给润色」这条决策成不成立。

    /srv/pagezenith/.venv/bin/python scripts/polish_check.py [题材序号]

## 为什么必须验

方案 A 的全部依据是一句假设：「Martin 正文 16 级偏难没关系，润色一步能压到 9–12」。
假设不验就等于没依据。这里对三个写手各润色一遍，同时看两件事：

  1. 年级降下来了吗（目标 9–12）
  2. **语域还在吗** —— 润色是整篇重写，最大的风险是把三个声音抹成同一个。
     所以要看 you/we 用量、表格、数字有没有被润掉。

结构破坏（H2 掉数）也一并检查：那是会触发退点的硬故障。
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
IN = ROOT / "work" / "bakeoff-voices"


def stats(md: str) -> dict:
    body = re.sub(r"^#+.*$", "", md, flags=re.M)
    sents = [s for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) >= 3]
    lens = [len(s.split()) for s in sents]
    try:
        import textstat
        g = round(textstat.flesch_kincaid_grade(md), 1)
    except Exception:  # noqa: BLE001
        g = None
    return {
        "grade": g,
        "sent": round(statistics.mean(lens), 1) if lens else 0,
        "h2": len(re.findall(r"^## ", md, re.M)),
        "tbl": md.count("\n|"),
        "digits": len(re.findall(r"\d", body)),
        "we": len(re.findall(r"\bwe\b", md, re.I)),
        "you": len(re.findall(r"\byou\b", md, re.I)),
        "words": len(re.findall(r"[A-Za-z][A-Za-z'\-]*", body)),
    }


async def main() -> None:
    from tools.seo_gap.config import get_settings
    from tools.seo_writer.providers import LLM, resolve_llm
    from tools.seo_writer.workflow import SEOWriter

    idx = sys.argv[1] if len(sys.argv) > 1 else "2"
    s = get_settings()
    wf = SEOWriter(s, LLM(resolve_llm(s, "pro"), s), "serper")

    # 跳过自己上一轮的产物，否则会把已润色的再润一遍，测出来是噪声
    for f in sorted(x for x in IN.glob(f"{idx}-*.md")
                    if not x.stem.endswith("-polished")):
        vid = f.stem.split("-", 1)[1]
        art = f.read_text(encoding="utf-8")
        before = stats(art)
        ctx = {"language": "English", "voice": vid, "main_keyword": "", "secondary_keyword": ""}
        out = "".join([c async for c in wf.stream_polish(ctx, art)])
        after = stats(out)
        (IN / f"{idx}-{vid}-polished.md").write_text(out, encoding="utf-8")

        band = "✓ 在 9–12" if after["grade"] and 9 <= after["grade"] <= 12 else "✗ 不在区间"
        keep = "✓ 保住" if after["h2"] == before["h2"] else f"✗ H2 {before['h2']}→{after['h2']}"
        print(f"\n【{vid}】")
        print(f"  年级   {before['grade']} → {after['grade']}   {band}")
        print(f"  结构   H2 {before['h2']} → {after['h2']}  表格行 {before['tbl']}→{after['tbl']}   {keep}")
        print(f"  语域   we {before['we']}→{after['we']} · you {before['you']}→{after['you']}")
        print(f"  内容   数字 {before['digits']}→{after['digits']} · 词数 {before['words']}→{after['words']}"
              f" · 句长 {before['sent']}→{after['sent']}")


if __name__ == "__main__":
    asyncio.run(main())
