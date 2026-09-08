"""中日韩相关的四个修复（2026-09-08 三篇极端冒烟暴露的）。

    cd api && python ../tests/test_cjk.py

  ① 日文「意图否决 0/6」——_answers 只认汉字，假名整段被忽略 → 按字二元组匹配
  ② 繁中 H2 变成「What is 紫 微 斗 數?」→ fix_cjk_headings 去空格 / 用大纲标题换回
  ③ 中文字数目标按英文词数推 → ×1.6
  ④ CJK 增益/密度是假数字 → audit 明说未测量；且否决降为提示
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["USE_MOCKS"] = "true"
API = pathlib.Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API))

from tools.seo_writer import density_audit as da, postfix  # noqa: E402
from tools.seo_writer.workflow import cjk_wordcount_target, is_cjk_lang  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


JA_ART = """# 越境ECの始め方

## ECサイトを始めるには何が必要ですか？

越境ECを始めるには、まず海外決済に対応したカートと、輸出可能な商品リストが必要です。
サイトの言語設定と配送業者の選定も欠かせません。

## 海外のネットショップで売るには？

海外のネットショップで売るには、現地の決済手段と物流を整えることが第一歩です。
"""


def test_answers():
    print("\n[① 日文问题的答到判定]")
    q1 = "ECサイトを始めるには何が必要ですか？"
    terms = da._question_terms(q1)
    ok("日文问题切出了内容二元组（不再是 ≤1 个）", len(terms) >= 4, terms[:8])
    ok("纯平假名二元组（です/ます 类）被丢掉", not any(da._HIRAGANA_ONLY.match(t) for t in terms))
    ok("答到的问题判成 covered", da._answers(JA_ART, q1))
    ok("另一个答到的问题也 covered", da._answers(JA_ART, "海外のネットショップで売るには？"))
    ok("没答到的问题判成 missing", not da._answers(JA_ART, "ネットショップオーナーの年収はいくらですか？"))
    ok("中文问题同样工作", da._answers("## 紫微斗數怎麼看\n\n紫微斗數要先看命宮主星，再看三方四正。", "紫微斗數怎麼看？"))
    ok("英文路径没被改坏",
       da._answers("## How do you audit a factory\n\nAudit the factory by checking ISO records and test reports.",
                   "How do you audit a factory?"))


def test_headings():
    print("\n[② 中日韩标题收口]")
    outline = "# 大纲\n## 什麼是紫微斗數？【独特价值点】\n## 命盤十二宮位怎麼看？\n"
    art = "# 紫微斗數免費排盤\n\n## What is 紫 微 斗 數?\n\n內容一。\n\n## 命 盤 十二宮位怎麼看？\n\n內容二。\n"
    out, ch = postfix.fix_cjk_headings(art, outline, "Chinese (Traditional)")
    ok("整行英文的 H2 用大纲同位置标题换回（且去掉【】标记）", "## 什麼是紫微斗數？" in out, [l for l in out.split("\n") if l.startswith("##")])
    ok("汉字之间的空格去掉", "## 命盤十二宮位怎麼看？" in out)
    ok("有改动说明", len(ch) == 2, ch)
    same, ch2 = postfix.fix_cjk_headings(art, outline, "English")
    ok("非 CJK 语言一个字不动", same == art and not ch2)
    # 大纲对不上位置时，只去空格、不乱换
    out3, _ = postfix.fix_cjk_headings("## 命 盤 怎麼看\n", "", "Japanese")
    ok("没有大纲时只做去空格", out3.strip() == "## 命盤怎麼看")


def test_wordcount():
    print("\n[③ 中日韩字数目标]")
    ok("英文不变", cjk_wordcount_target(2000, "English") == 2000)
    ok("中文 ×1.6", cjk_wordcount_target(2000, "Chinese (Traditional)") == 3200)
    ok("日文 ×1.6 且封顶 4800", cjk_wordcount_target(3000, "Japanese") == 4800)
    ok("is_cjk_lang 认三种", all(is_cjk_lang(x) for x in ("Chinese (Simplified)", "Japanese", "Korean")) and not is_cjk_lang("Spanish"))


def test_audit_cjk_and_veto():
    print("\n[④ CJK 打分与否决降级]")
    serp = "Title: 越境EC入門\nURL: https://x/1\nContent: 越境EC の 始め方 と 決済 の 話\n---\nQ: ECサイトを始めるには何が必要ですか？\nQ: 海外のネットショップで売るには？\nQ: ネットショップオーナーの年収はいくらですか？\n"
    r = da.audit(JA_ART, search_text=serp, topic_type="how_to", keyword="越境ec 始め方", language="Japanese")
    ok("CJK 文章不崩", isinstance(r, dict))
    ok("增益标成未测量而不是 0.0", r["gain"].get("measurable") is False, r["gain"])
    ok("密度为空（不给假数字）", r["density"] == {})
    ok("有给前端的说明文案", "中日韩" in (r.get("message_override") or ""))
    it = r["intent"]
    ok("意图覆盖照常计算：2/3", len(it["covered"]) == 2 and len(it["questions"]) == 3, (len(it["covered"]), len(it["questions"])))
    ok("否决恒为 False", it["veto"] is False)

    # 英文：覆盖不到一半 → 只是 warning，不再 veto
    # 标题要含问题的实词，否则 _on_topic 词面过滤会把问题全丢掉、列表为空就没有 warning
    en_serp = ("Title: How to audit a factory, check MOQ and lead time\nURL: https://x/a\nContent: c\n---\n"
               "Q: How do you audit a factory?\nQ: What is the MOQ for OEM?\nQ: How long is lead time?\n")
    # 要超过 50 词，否则 audit() 走"文章太短"早退，intent 是空的
    long_en = "# T\n\n## Intro\n\n" + (
        "This paragraph talks around the topic in general terms without giving any concrete "
        "figure, name, threshold or procedure that a reader could act on or verify later. " * 4) + "\n"
    en = da.audit(long_en, search_text=en_serp, keyword="factory", language="English")
    ok("英文覆盖不足只给 warning 不否决", en["intent"]["veto"] is False and en["intent"]["warnings"], en["intent"]["warnings"])


def main_() -> int:
    test_answers(); test_headings(); test_wordcount(); test_audit_cjk_and_veto()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main_())
