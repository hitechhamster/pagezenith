"""真 API 冒烟：跑一篇文章，断言今天这批 bug 都没复发。要花真钱（约 ¥1-2/篇）。

    python scripts/smoke_article.py --base https://pagezenith.com --card PZ-XXXX-XXXX-XXXX \
        --keyword "best headphone factory in china" --secondary "oem headphone supplier" \
        --topic "..." --language English --images 2 --out work/smoke

走的是线上 HTTP 接口（X-Card-Key），所以连计费 / 后台 Job / SSE 一起测到。
2026-09-08 那五个 bug 里四个只在真输出里现形（中文混入 / 无图 / 关键词大写 / PAA 跑题），
单测全绿照样上线炸 —— 这个脚本就是补那块：每次改 prompt 或流水线后跑一遍。

断言分 FAIL（必须修）和 WARN（人眼看一下）。文章和全部 SSE 事件落盘到 --out，
方便复盘。退出码 1 = 有 FAIL。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CJK = re.compile(r"[一-鿿぀-ヿ가-힯]")
CJK_LANGS = ("chinese", "japanese", "korean")
SENT_END = ".!?:;\"“([*_-"
FAILS: list[str] = []
WARNS: list[str] = []


def fail(msg: str) -> None:
    FAILS.append(msg)
    print(f"  FAIL  {msg}")


def warn(msg: str) -> None:
    WARNS.append(msg)
    print(f"  WARN  {msg}")


def good(msg: str) -> None:
    print(f"  ok    {msg}")


def sse(client: httpx.Client, url: str, payload: dict, headers: dict, label: str) -> list[dict]:
    """读 SSE 到结束，顺手把 step / error 打出来当进度。"""
    evts: list[dict] = []
    t0 = time.time()
    with client.stream("POST", url, json=payload, headers=headers) as r:
        if r.status_code != 200:
            body = r.read().decode("utf-8", "replace")[:300]
            fail(f"{label}: HTTP {r.status_code} {body}")
            return evts
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                for line in raw.split("\n"):
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    evts.append(ev)
                    t = ev.get("type")
                    if t == "step":
                        print(f"    [{time.time() - t0:5.0f}s] {(ev.get('message') or '')[:110]}")
                    elif t == "error":
                        print(f"    [{time.time() - t0:5.0f}s] ERROR {(ev.get('message') or '')[:200]}")
    return evts


def body_lines(article: str) -> list[str]:
    """正文行：去掉标题 / 表格 / 图片占位 / 代码块。"""
    out: list[str] = []
    fence = False
    for line in article.split("\n"):
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if fence or not line.strip() or re.match(r"^\s*(#{1,6}\s|\||\[IMAGE:)", line):
            continue
        out.append(line)
    return out


def keyword_mid_sentence_caps(lines: list[str], kw: str) -> list[str]:
    """关键词出现在句中、且首字母大写的片段。句首 / 标题位的大写是对的，不算。"""
    parts = kw.split()
    if not parts:
        return []
    pat = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in parts) + r"\b", re.I)
    hits: list[str] = []
    for line in lines:
        for m in pat.finditer(line):
            before = re.sub(r"[*_~\"'(\[]+$", "", line[:m.start()].rstrip()).rstrip()
            if not before or before[-1] in SENT_END:
                continue
            if m.group(0)[0].isupper():
                hits.append(line[max(0, m.start() - 25):m.end()])
    return hits


def check(article: str, done: dict, args: argparse.Namespace, events: list[dict]) -> None:
    lang = (args.language or "English").lower()
    is_cjk = any(lang.startswith(x) for x in CJK_LANGS)
    lines = body_lines(article)
    h2s = [l.lstrip("# ").strip() for l in article.split("\n") if l.startswith("## ")]

    # 1. 语言纯度 —— 今天最常见的一类：英文文章里冒中文
    if not is_cjk:
        bad = [l for l in lines if CJK.search(l)]
        if bad:
            fail(f"非中日韩文章里混进 CJK 字符 {len(bad)} 行，例：{bad[0][:80]}")
        else:
            good("正文没有 CJK 字符")
        badh = [h for h in h2s if CJK.search(h)]
        if badh:
            fail(f"H2 里有 CJK：{badh[:2]}")
    else:
        letters = re.findall(r"[A-Za-z]", article)
        cjk = CJK.findall(article)
        ratio = len(cjk) / max(len(cjk) + len(letters), 1)
        if ratio >= 0.5:
            good(f"CJK 占比 {ratio:.0%}")
        else:
            fail(f"CJK 占比只有 {ratio:.0%}，英文太多，疑似语言跑偏")
        runs = [l for l in lines if re.search(r"(?:\b[A-Za-z]+\b[ ,]+){14,}", l)]
        if runs:
            warn(f"有 {len(runs)} 行连续 14+ 个英文词（可能整段英文），例：{runs[0][:80]}")

    # 2. 关键词句中大写
    if not is_cjk:
        for kw in (args.keyword, args.secondary):
            kw = (kw or "").strip()
            if not kw:
                continue
            hits = keyword_mid_sentence_caps(lines, kw)
            if hits:
                fail(f"关键词「{kw}」在句中首字母大写 {len(hits)} 处，例：…{hits[0]}")
            else:
                good(f"关键词「{kw}」句中无大写")

    # 3. 配图
    n_img = int(done.get("images") or 0)
    placeholders = len(re.findall(r"\[IMAGE:", article))
    if args.images:
        if n_img == args.images:
            good(f"配图 {n_img}/{args.images}")
        else:
            refund = [e.get("message") for e in events
                      if e.get("type") == "step" and "退回" in (e.get("message") or "")]
            fail(f"配图只出了 {n_img}/{args.images}（{refund[0] if refund else '无退点提示'}）")
    else:
        good(f"未要求配图（正文里占位符 {placeholders} 个）")

    # 4. PAA 过滤有没有跑 + H2 列表给人眼看
    dropped = [e.get("message") for e in events
               if e.get("type") == "step" and "意图不符" in (e.get("message") or "")]
    good(f"PAA 意图过滤：{dropped[0][:90] if dropped else '没有剔掉任何问题'}")
    print("  H2 列表（人眼判断有没有跑题）：")
    for h in h2s:
        print(f"      - {h}")

    # 5. 质量 / 意图否决 / 字数 / 可读性
    q = done.get("quality") or {}
    if q.get("veto"):
        fail(f"意图否决：{q.get('veto_reasons')}")
    else:
        good(f"意图覆盖 {q.get('intent_covered')}/{q.get('intent_total')} · 增益 {q.get('gain')} · 密度 {q.get('density')}")
    if done.get("wordcount_level") == "bad":
        fail(f"字数：{done.get('wordcount_message')}")
    else:
        good(f"字数：{done.get('wordcount_message')}")
    if lang.startswith("english"):
        if done.get("grade") is not None:
            good(f"FK 阅读年级：{done.get('grade')}")
        else:
            fail("英文文章没有算出 FK 阅读年级")

    # 6. 大纲元信息漏进正文
    meta = [l for l in article.split("\n") if re.search(r"预估字数|\[E-?E-?A-?T|黄金答案句|写作要求", l)]
    if meta:
        fail(f"大纲元信息漏进正文：{meta[0][:80]}")
    else:
        good("没有大纲元信息漏进正文")

    # 7. SEO 元数据
    if done.get("seo_title") and done.get("seo_description"):
        good(f"SEO title {len(done['seo_title'])} 字符 / description {len(done['seo_description'])} 字符")
    else:
        fail("SEO title / description 缺失")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--card", required=True)
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--secondary", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--language", default="English")
    ap.add_argument("--images", type=int, default=0)
    ap.add_argument("--voice", default="")
    ap.add_argument("--wordcounts", type=int, default=0)
    ap.add_argument("--out", default="work/smoke")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"[^a-z0-9]+", "-", args.keyword.lower()).strip("-")[:40] + time.strftime("-%m%d-%H%M")
    headers = {"X-Card-Key": args.card}
    base = args.base.rstrip("/")
    print(f"\n=== {args.keyword} · {args.language} · 配图 {args.images} ===")

    with httpx.Client(timeout=httpx.Timeout(900, connect=30), trust_env=False) as c:
        print("[1/2] 大纲")
        ev1 = sse(c, f"{base}/api/seo-writer/outline",
                  {"main_keyword": args.keyword, "secondary_keyword": args.secondary,
                   "topic": args.topic, "language": args.language,
                   "enable_images": args.images > 0, "images_per_article": max(args.images, 1),
                   "voice": args.voice, "wordcounts": args.wordcounts, "tier": "pro"},
                  headers, "outline")
        d1 = next((e for e in ev1 if e.get("type") == "done"), None)
        if not d1:
            fail("大纲没有 done 事件")
            (out / f"{tag}.events.json").write_text(json.dumps(ev1, ensure_ascii=False, indent=1), encoding="utf-8")
            return 1
        print("[2/2] 正文")
        ev2 = sse(c, f"{base}/api/seo-writer/article",
                  {"session_id": d1["session_id"], "outline": d1["outline"], "tier": "pro"},
                  headers, "article")
        d2 = next((e for e in ev2 if e.get("type") == "done"), None)

    (out / f"{tag}.events.json").write_text(json.dumps(ev1 + ev2, ensure_ascii=False, indent=1), encoding="utf-8")
    if not d2:
        fail("正文没有 done 事件（看 events.json 里的 error）")
        return 1
    article = d2.get("article") or ""
    (out / f"{tag}.md").write_text(article, encoding="utf-8")
    print(f"\n文章已存：{out / (tag + '.md')}\n断言：")
    check(article, d2, args, ev1 + ev2)
    charged = sum(int(e.get("charged") or 0) for e in ev1 + ev2 if e.get("type") == "done")
    print(f"\n{len(FAILS)} FAIL · {len(WARNS)} WARN · 扣点 {charged}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
