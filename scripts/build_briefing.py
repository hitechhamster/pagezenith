"""每日 SEO/GEO 简报生成器（详细版）。

流程：
  1. 抓权威源 RSS → 候选条目。
  2. 一次 LLM 调用：筛选 8-10 条最重要的 + 中文标题 + 分类 + 一句话摘要（保留原文来源/链接）。
  3. 逐条：抓原文正文 → 单独 LLM 调用写 200-300 字详细中文解读。
  4. 写 web/data/briefings/YYYY-MM-DD.json + 更新 index.json。

本地：OPENROUTER_API_KEY=... python scripts/build_briefing.py
GitHub Action 每天自动跑。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
from selectolax.parser import HTMLParser

FEEDS = [
    ("Google Search Central", "https://developers.google.com/search/blog/feed.xml"),
    ("Search Engine Land", "https://searchengineland.com/feed"),
    ("Search Engine Roundtable", "https://www.seroundtable.com/feed"),
    ("Search Engine Journal", "https://www.searchenginejournal.com/feed/"),
    ("Moz Blog", "https://moz.com/blog/feed"),
    ("Ahrefs Blog", "https://ahrefs.com/blog/feed/"),
]

OUT_DIR = Path(__file__).resolve().parent.parent / "web" / "data" / "briefings"
MODEL = os.environ.get("LLM_MODEL", "google/gemini-3.1-flash-lite")
KEY = os.environ.get("OPENROUTER_API_KEY", "")
BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_DROP = ("script", "style", "noscript", "nav", "footer", "header", "aside", "form")

SELECT_SYSTEM = """\
你是中文 SEO/GEO 行业简报编辑。从给定英文资讯里，挑出对“做跨境/外贸独立站 SEO 与 GEO
（生成式引擎优化）”最重要的 8-10 条，逐条给出中文标题、分类、一句话中文摘要，
并**原样保留来源名与原文链接**（绝不编造链接）。再写一句话当日总览。
只输出 JSON，不要前言/markdown。"""

DETAIL_SYSTEM = """\
你是中文 SEO/GEO 资深编辑。根据给定的英文原文，写一段【详细中文解读】，250-350 字，分 2-3 段：
说清楚这篇到底讲了什么（背景/核心要点/具体数据或做法），以及对“做跨境独立站 SEO/GEO”的实际意义。
要具体、有信息量，不要空话套话。只输出正文，不要标题、不要 markdown。"""


def chat(system, user, as_json=True):
    r = httpx.post(BASE + "/chat/completions",
                   headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                   json={"model": MODEL, "temperature": 0.25,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]},
                   timeout=120)
    r.raise_for_status()
    c = r.json()["choices"][0]["message"]["content"].strip()
    if not as_json:
        return c
    if c.startswith("```"):
        c = c.split("```", 2)[1].lstrip("json").strip()
    s, e = c.find("{"), c.rfind("}")
    return json.loads(c[s:e + 1])


def fetch_items():
    items = []
    for name, url in FEEDS:
        try:
            d = feedparser.parse(url)
            for e in d.entries[:8]:
                items.append({"source": name, "title": getattr(e, "title", ""),
                              "url": getattr(e, "link", ""),
                              "excerpt": (getattr(e, "summary", "") or "")[:400]})
        except Exception as exc:
            print(f"feed failed {name}: {exc}", file=sys.stderr)
    seen, uniq = set(), []
    for it in items:
        k = it["title"].strip().lower()
        if it["title"] and it["url"] and k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq[:40]


def article_text(url: str) -> str:
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        if r.status_code >= 400 or len(r.text) < 400:
            return ""
        tree = HTMLParser(r.text)
        for sel in _DROP:
            for n in tree.css(sel):
                n.decompose()
        body = tree.body or tree.root
        return body.text(separator="\n", strip=True)[:6000] if body else ""
    except Exception:
        return ""


def main():
    if not KEY:
        print("ERROR: 缺少 OPENROUTER_API_KEY", file=sys.stderr)
        sys.exit(1)
    cands = fetch_items()
    if not cands:
        print("没抓到任何资讯，放弃。", file=sys.stderr)
        sys.exit(1)

    # 1) 选题 + 分类 + 标题 + 短摘要
    sel = chat(SELECT_SYSTEM,
               "从下列资讯选 8-10 条编当日简报，每条带 source 和 url（用我给的原文链接）。\n\n"
               + json.dumps(cands, ensure_ascii=False)
               + "\n\n输出 JSON：{\"overview\":\"一句话总览\",\"items\":[{\"title\":\"中文标题\","
                 "\"category\":\"SEO|GEO|算法更新|研究|工具|AI\",\"summary\":\"一句话中文摘要\","
                 "\"source\":\"来源名\",\"url\":\"原文链接\"}]}")
    picked = [i for i in sel.get("items", []) if i.get("title") and i.get("url")][:10]

    # 2) 逐条抓原文 → 详细解读
    by_url = {c["url"]: c for c in cands}
    for it in picked:
        text = article_text(it["url"])
        if len(text) < 300:  # 抓不到正文就用 RSS 摘要兜底
            text = by_url.get(it["url"], {}).get("excerpt", "") or it.get("summary", "")
        try:
            it["detail"] = chat(DETAIL_SYSTEM,
                                 f"标题：{it['title']}\n来源：{it['source']}\n原文：\n{text}",
                                 as_json=False)
        except Exception as exc:
            print(f"detail failed {it['url']}: {exc}", file=sys.stderr)
            it["detail"] = ""

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    brief = {"date": today, "overview": sel.get("overview", ""), "items": picked}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{today}.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    idx_path = OUT_DIR / "index.json"
    dates = []
    if idx_path.exists():
        try:
            dates = json.loads(idx_path.read_text(encoding="utf-8")).get("dates", [])
        except Exception:
            dates = []
    if today not in dates:
        dates.insert(0, today)
    dates = sorted(set(dates), reverse=True)[:365]
    idx_path.write_text(json.dumps({"dates": dates}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK {today}: {len(picked)} 条（含详细解读）")


if __name__ == "__main__":
    main()
