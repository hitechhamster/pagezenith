"""每日 SEO/GEO 简报生成器。

抓取一组权威 SEO/GEO 源的 RSS → LLM 汇总成中文简报（保留原文来源）→
写 web/data/briefings/YYYY-MM-DD.json + 更新 index.json。

本地跑：OPENROUTER_API_KEY=... python scripts/build_briefing.py
GitHub Action 每天自动跑（key 走仓库 secret）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx

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

SYSTEM = """\
你是中文 SEO/GEO 行业简报编辑。从给定的英文资讯条目里，挑出对“做跨境/外贸独立站 SEO 与 GEO
（生成式引擎优化）”最重要的 6-12 条，逐条用简体中文写出标题与 2-3 句摘要，标注分类
（SEO/GEO/算法更新/研究/工具/AI），并**原样保留来源名与原文链接**（绝不编造链接）。
再写一句话当日总览。只输出一个 JSON 对象，不要前言/markdown。"""


def fetch_items():
    items = []
    for name, url in FEEDS:
        try:
            d = feedparser.parse(url)
            for e in d.entries[:8]:
                summary = (getattr(e, "summary", "") or "")[:400]
                items.append({
                    "source": name, "title": getattr(e, "title", ""),
                    "url": getattr(e, "link", ""), "excerpt": summary,
                    "published": getattr(e, "published", "")[:25],
                })
        except Exception as exc:  # 单个源失败不影响整体
            print(f"feed failed {name}: {exc}", file=sys.stderr)
    # 去重
    seen, uniq = set(), []
    for it in items:
        k = it["title"].strip().lower()
        if it["title"] and it["url"] and k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq[:40]


def summarize(items):
    user = ("从下列资讯里编当日简报。每条必须带 source 和 url（用我给的原文链接）。\n\n"
            + json.dumps(items, ensure_ascii=False)
            + "\n\n输出 JSON：{\"overview\":\"一句话总览\",\"items\":["
              "{\"title\":\"中文标题\",\"summary\":\"中文摘要\",\"category\":\"SEO|GEO|算法更新|研究|工具|AI\","
              "\"source\":\"来源名\",\"url\":\"原文链接\"}]}")
    payload = {"model": MODEL, "temperature": 0.2,
               "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}
    r = httpx.post(BASE + "/chat/completions",
                   headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                   json=payload, timeout=120)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1].lstrip("json").strip()
    s, e = content.find("{"), content.rfind("}")
    return json.loads(content[s:e + 1])


def main():
    if not KEY:
        print("ERROR: 缺少 OPENROUTER_API_KEY", file=sys.stderr)
        sys.exit(1)
    items = fetch_items()
    if not items:
        print("没抓到任何资讯，放弃。", file=sys.stderr)
        sys.exit(1)
    data = summarize(items)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    brief = {"date": today, "overview": data.get("overview", ""),
             "items": [i for i in data.get("items", []) if i.get("title") and i.get("url")]}

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
    print(f"OK {today}: {len(brief['items'])} 条")


if __name__ == "__main__":
    main()
