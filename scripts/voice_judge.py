"""对横评产出的文章做盲评打分，决定留哪几个写手。

    /srv/pagezenith/.venv/bin/python scripts/voice_judge.py [题材序号]

## 为什么要有这一步

自动指标（句长、TTR、数字密度）只能测**形**，测不了**质** —— 一篇句子长短交错、
数字很多的文章，完全可能论点空洞。留不留一个写手，要看它写得好不好，不是看它
写得特不特别。

## 盲评

评审拿到的文章是**打乱顺序、去掉写手名字**的，只按 A/B/C… 编号，
避免"原版"这个标签本身带来的偏见。评分用 DeepSeek（跟写作模型不同家族，
减少自己夸自己）。
"""
from __future__ import annotations

import json
import pathlib
import random
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
IN = ROOT / "work" / "bakeoff-voices"

RUBRIC = """You are judging SEO articles written by different writers on the SAME topic.
Score each on four axes, 1-10, and be harsh — a 7 should be uncommon.

1. depth      — Does it say specific, non-obvious things, or does it restate what any
                article on this topic would say? Generic competence scores 3.
2. density    — Information per paragraph. Padding, restatement, and throat-clearing
                push this down hard.
3. readability— Would a busy non-expert get through it without re-reading? Short is not
                automatically better; muddled is worse than long.
4. distinct   — Does this read like a specific person with a point of view wrote it,
                or like generic AI output? Interchangeable prose scores 2.

Then give ONE overall verdict per article: keep / cut, and a one-sentence reason.

Return STRICT JSON only, no prose around it:
{"scores":[{"id":"A","depth":0,"density":0,"readability":0,"distinct":0,
            "verdict":"keep","why":"..."}],
 "ranking":["A","B"],
 "best_reason":"one sentence on why the top one won"}
"""


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in (ROOT / ".env", ROOT / "api" / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    out.setdefault(k.strip(), v.strip())
    return out


def main() -> None:
    idx = sys.argv[1] if len(sys.argv) > 1 else "0"
    files = sorted(IN.glob(f"{idx}-*.md"))
    if not files:
        print(f"没找到 {IN}/{idx}-*.md"); return

    # 盲评：打乱 + 只给字母编号
    items = [(f.stem.split("-", 1)[1], f.read_text(encoding="utf-8")) for f in files]
    random.shuffle(items)
    letters = [chr(65 + i) for i in range(len(items))]
    key = dict(zip(letters, [v for v, _ in items]))

    blob = "\n\n".join(
        f"===== ARTICLE {L} =====\n{re.sub(r'\\[IMAGE:[^]]*\\]', '', art)[:9000]}"
        for L, (_, art) in zip(letters, items))

    e = env()
    r = httpx.post("https://api.deepseek.com/v1/chat/completions",
                   headers={"Authorization": f"Bearer {e['DEEPSEEK_API_KEY']}"},
                   json={"model": "deepseek-v4-flash",
                         "messages": [{"role": "user", "content": RUBRIC + "\n\n" + blob}],
                         "temperature": 0.2, "max_tokens": 3000},
                   timeout=600, trust_env=False)
    r.raise_for_status()
    txt = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        print("评审没返回 JSON：", txt[:400]); return
    data = json.loads(m.group(0))

    print(f"题材 {idx} · 盲评结果（{len(items)} 篇）\n")
    rows = []
    for s in data["scores"]:
        name = key.get(s["id"], "?")
        total = s["depth"] + s["density"] + s["readability"] + s["distinct"]
        rows.append((total, name, s))
    for total, name, s in sorted(rows, reverse=True):
        print(f"  {total:>2}/40  {name:<22} 深度{s['depth']} 密度{s['density']} "
              f"可读{s['readability']} 独特{s['distinct']}  [{s['verdict']}]")
        print(f"         {s['why'][:110]}")
    print(f"\n排名：{[key.get(x, x) for x in data.get('ranking', [])]}")
    print(f"理由：{data.get('best_reason', '')}")

    (IN / f"{idx}-judge.json").write_text(
        json.dumps({"key": key, **data}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
