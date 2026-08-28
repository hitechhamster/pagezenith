"""五个写手 + 原版基线的横评：同一组关键词各跑一遍，量化比对。

    /srv/pagezenith/.venv/bin/python scripts/voice_bakeoff.py [关键词组序号]

绕过计费直接调 workflow —— 这是内部评测，不该花卡里的点。真 API、真成本约 ¥0.56/篇。

## 判据

这次要回答的问题只有两个：
  1. **五个写手真的写得不一样吗？** 区分不出来就是文风规则写得不够具体，
     该重写规则，而不是接受"看起来有五个选项"。
  2. **哪三个值得留？** 留下的标准不是"文风鲜明"，是**质量、信息密度、
     可读性三项都不输原版**。文风再独特，写得比原版差就不该留。

自动指标只能测形，测不了质。所以最后还有一轮 LLM 评审打分。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
OUT = ROOT / "work" / "bakeoff-voices"

# 三组题材，覆盖不同 topic_type，避免只用一个题材得出偏颇结论
TOPICS = [
    dict(main_keyword="best espresso machine under 500",
         secondary_keyword="home espresso machine buying guide",
         topic="How to choose a home espresso machine under $500 without regretting it",
         wordcounts=1400),
    dict(main_keyword="shopify abandoned cart email",
         secondary_keyword="how to reduce cart abandonment shopify",
         topic="Setting up abandoned-cart emails that actually recover revenue",
         wordcounts=1400),
    dict(main_keyword="what is programmatic seo",
         secondary_keyword="programmatic seo examples",
         topic="What programmatic SEO is and when it is worth doing",
         wordcounts=1400),
]

BANNED = ["delve", "landscape", "ever-evolving", "when it comes to", "it's important to note",
          "in today's world", "very ", "extremely ", "incredibly ", "revolutionary"]


# ---------------------------------------------------------------- 指标
def metrics(md: str) -> dict:
    body = re.sub(r"^#+.*$", "", md, flags=re.M)
    body = re.sub(r"\[IMAGE:[^\]]*\]", "", body)
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", body)
    sents = [s for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) >= 3]
    lens = [len(s.split()) for s in sents]
    low = md.lower()
    try:
        import textstat
        grade = round(textstat.flesch_kincaid_grade(md), 1)
    except Exception:  # noqa: BLE001
        grade = None
    return {
        "words": len(words),
        "grade": grade,
        "sent_mean": round(statistics.mean(lens), 1) if lens else 0,
        "sent_sd": round(statistics.pstdev(lens), 1) if len(lens) > 1 else 0,
        "short_pct": round(100 * sum(1 for x in lens if x <= 10) / len(lens)) if lens else 0,
        "long_pct": round(100 * sum(1 for x in lens if x >= 25) / len(lens)) if lens else 0,
        # 信息密度的两个代理指标：实词丰富度 + 数字密度（具体性）
        "ttr": round(len(set(w.lower() for w in words)) / max(len(words), 1), 3),
        "digits_per_1k": round(1000 * len(re.findall(r"\d", body)) / max(len(words), 1), 1),
        "h2": len(re.findall(r"^## ", md, re.M)),
        "h3": len(re.findall(r"^### ", md, re.M)),
        "table_rows": md.count("\n|"),
        "bullets": len(re.findall(r"^\s*[-*] ", md, re.M)),
        "we": len(re.findall(r"\bwe\b", md, re.I)),
        "you": len(re.findall(r"\byou\b", md, re.I)),
        "i": len(re.findall(r"\bI\b", md)),
        "banned": sum(low.count(b) for b in BANNED),
    }


def similarity(a: str, b: str) -> float:
    """两篇正文的实词重合度。用来看五个写手是不是在写同一篇东西。"""
    def bag(t):
        return set(w.lower() for w in re.findall(r"[A-Za-z]{4,}", re.sub(r"^#+.*$", "", t, flags=re.M)))
    x, y = bag(a), bag(b)
    return round(len(x & y) / max(len(x | y), 1), 3)


# ---------------------------------------------------------------- 跑
async def fetch_research(topic: dict, wf_factory) -> dict:
    """检索一次，三个写手共用。

    ⚠️ 这是本脚本最关键的一处，别"优化"回每个写手各跑一次。
    2026-08-28 实测：同一个 clark、同一题材连跑两次，数字密度 27.4 vs 3.6、
    阅读年级 12.4 vs 15.5 —— 因为每次拿到的搜索/Reddit 资料不同，
    而资料里有没有数字直接决定文章里有没有数字。
    资料不固定，测出来的就是资料的差异，不是文风的差异。
    """
    wf = wf_factory()
    main, sec = await wf.search_context(topic["main_keyword"], topic["secondary_keyword"])
    return {
        "main_search": main, "sec_search": sec,
        "reddit_context": await wf.reddit_context(topic["main_keyword"]),
        "topic_type": await wf.classify_topic_type(topic["main_keyword"], topic["topic"]),
    }


async def run_one(vid: str | None, topic: dict, wf_factory, research: dict) -> dict:
    from tools.seo_writer.workflow import SEOWriter  # noqa: F401
    wf = wf_factory()
    ctx = dict(topic, language="English", specific="", enable_images=False,
               images_per_article=0, voice=vid or "", **research)
    t0 = time.time()
    outline = "".join([c async for c in wf.stream_outline(ctx)])
    ctx["outline"] = outline
    article = "".join([c async for c in wf.stream_article(ctx)])
    return {"voice": vid or "_baseline", "topic": ctx["main_keyword"],
            "topic_type": ctx["topic_type"], "secs": round(time.time() - t0),
            "outline": outline, "article": article, **metrics(article)}


async def main() -> None:
    import os
    from tools.seo_gap.config import get_settings as gs
    from tools.seo_writer.providers import LLM, resolve_llm
    from tools.seo_writer.workflow import SEOWriter
    from tools.seo_writer.voices import VOICES

    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    topic = TOPICS[idx % len(TOPICS)]
    s = gs()
    if s.use_mocks:
        print("USE_MOCKS=true，测不出东西，退出"); return

    def factory():
        return SEOWriter(s, LLM(resolve_llm(s, "pro"), s), "serper")

    OUT.mkdir(parents=True, exist_ok=True)
    runs = []
    # clark 本身就是原版基线（规则全空 → 退回 ARTICLE_VOICE_DEFAULT），
    # 不用再单跑一轮 None，那是同一份 prompt。
    order = list(VOICES)
    print(f"题材：{topic['main_keyword']}  ·  {len(order)} 轮\n")

    # 检索一次，三个写手共用 —— 见 fetch_research 的说明，这是受控对比的前提
    print("检索一次，三个写手共用同一份资料…", flush=True)
    research = await fetch_research(topic, factory)
    print(f"  主题类型 {research['topic_type']} · 搜索 {len(research['main_search'])} 字"
          f" · Reddit {len(research['reddit_context'])} 字", flush=True)

    for vid in order:
        label = vid or "_baseline(Clark原版)"
        print(f"→ {label} …", flush=True)
        try:
            r = await run_one(vid, topic, factory, research)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {str(exc)[:200]}"); continue
        runs.append(r)
        (OUT / f"{idx}-{r['voice']}.md").write_text(r["article"], encoding="utf-8")
        print(f"  {r['secs']}s | {r['words']}词 | 年级{r['grade']} | "
              f"句长{r['sent_mean']}±{r['sent_sd']} | 短句{r['short_pct']}% | "
              f"TTR{r['ttr']} | 数字{r['digits_per_1k']} | H2 {r['h2']} | "
              f"we{r['we']}/you{r['you']} | 禁用词{r['banned']}")

    (OUT / f"{idx}-runs.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k not in ("outline", "article")}
                    for r in runs], ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n=== 两两文本重合度（越低越说明真的写得不一样）===")
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            sim = similarity(runs[i]["article"], runs[j]["article"])
            flag = "  ⚠️ 太像" if sim > 0.55 else ""
            print(f"  {runs[i]['voice']:<14} vs {runs[j]['voice']:<14} {sim}{flag}")


if __name__ == "__main__":
    asyncio.run(main())
