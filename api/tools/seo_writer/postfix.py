"""交付前的确定性后处理 —— prompt 管不住的两件事在这里用代码收口。

七轮实测的规律：用代码修的（换模型、数字闸门、结构闸门、事实溯源）一次修好不回头；
用 prompt 修的（塞词 5 轮、假经验句 4 轮）每轮换个说法又回来。这两件事的检测器
（prose_audit.keyword_stuffing / _FAKE_CASE）已经 100% 命中，那就别再求模型自觉，
检测到就机械地改，改完再检测，过不了就删 —— 和数字闸门一个思路：验证闭环代替信任。

两个处理器都返回 (新文本, 改动说明列表)，改动说明直接给用户看。
"""

from __future__ import annotations

import logging
import re
from typing import Awaitable, Callable, Optional

from . import density_audit, prose_audit

logger = logging.getLogger(__name__)

# 带信息量的数字（金额 / 百分比 / 三位以上）。序数和小整数不算。
_NUM = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b\d{3,}\b")
_norm = lambda s: re.sub(r"[\s$%,]", "", s)

# 行首是结构（标题/表格/列表/图片占位）就不当散文处理
_STRUCT_LINE = re.compile(r"^\s*(?:#{1,6}\s|\||[-*+]\s|\d+[.)]\s|\[IMAGE:)")
# 英文分句：句末标点 + 可选引号/括号 + 空白
_SENT = re.compile(r"[^.!?]*[.!?]+[\"”’)]*\s*|[^.!?]+$")

# 已经是限定语开头的句子，不用再加 hedge
_ALREADY_HEDGED = re.compile(
    r"(?i)^(?:many|most|some|several|sellers|users|merchants|store owners|"
    r"usually|often|typically|generally|in most cases)\b")


def _split_sentences(line: str) -> list[str]:
    return [m.group(0) for m in _SENT.finditer(line) if m.group(0).strip()]


def _facts_numbers(facts: str) -> set[str]:
    return {_norm(x) for x in _NUM.findall(facts or "")}


def _cap(s: str) -> str:
    s = s.lstrip()
    return s[:1].upper() + s[1:] if s else s


# --------------------------------------------------------------------------- #
# ① 假经验句：删前缀 → 三分支
# --------------------------------------------------------------------------- #
def strip_fake_experience(text: str, facts: str) -> tuple[str, list[str]]:
    """把「In our work with clients, X」「We have seen that X」「We recommend Y」这类
    声称经验的句子机械处理掉。

    三分支（用户 2026-09-03 定的）：
      ① 剩余断言里的数字**全在**事实清单 → 保留断言（来源由正文规则写成散文）
      ② 剩余断言里有**清单外**数字 → 整句删（那是假权威包着的编造数字）
      ③ 剩余断言没有数字 → 前面加「Many sellers report that」降成限定语
    「We recommend X」单独处理：改成「Consider X」—— 去掉背书主语，保留建议本身。
    「we see across the seller community」这类 hedge 不动（prose_audit 保留表）。
    """
    if prose_audit.is_cjk(text):
        return text, []                       # 中文假经验句形态不同，另做；先不动
    known = _facts_numbers(facts)
    fake_pat, keep_pat = prose_audit._FAKE_CASE, prose_audit._FAKE_CASE_KEEP
    changes: list[str] = []
    out_lines: list[str] = []

    for line in (text or "").split("\n"):
        if _STRUCT_LINE.match(line) or not line.strip():
            out_lines.append(line)
            continue
        new_sents: list[str] = []
        for sent in _split_sentences(line):
            m = fake_pat.search(sent)
            if not m or keep_pat.search(sent):
                new_sents.append(sent)
                continue
            trail = sent[len(sent.rstrip()):]          # 保留句末空白
            body = sent.rstrip()
            head = m.group(0)

            # 「We recommend X」→「Consider X」
            if re.match(r"(?i)we\s+recommend\b", head):
                fixed = re.sub(r"(?i)^\s*we\s+recommend\s+(?:that\s+you\s+)?", "Consider ", body, count=1)
                new_sents.append(fixed + trail)
                changes.append(f"背书改建议：「{body[:50]}…」")
                continue

            # 定位前缀结束点：先找 that / 逗号，都没有就整句是空洞的经验声明 → 删
            after = body[m.end():]
            cut = None
            mt = re.match(r"\s+that\s+", after)
            if mt:
                cut = m.end() + mt.end()
            else:
                mc = re.search(r",\s*", after)
                if mc and m.start() <= 3:           # 前缀在句首才按逗号切
                    cut = m.end() + mc.end()
            if cut is None:
                changes.append(f"删空洞经验句：「{body[:60]}…」")
                continue

            rest = body[cut:].strip()
            if not rest:
                changes.append(f"删空洞经验句：「{body[:60]}…」")
                continue
            nums = {_norm(x) for x in _NUM.findall(rest)}
            if nums and not nums <= known:
                changes.append(f"删（清单外数字 {sorted(nums - known)[:2]}）：「{body[:50]}…」")
                continue
            if nums:
                fixed = _cap(rest)                        # ① 数字在清单 → 保留断言
                changes.append(f"去前缀保留：「{fixed[:50]}…」")
            else:
                if _ALREADY_HEDGED.match(rest):
                    fixed = _cap(rest)
                else:                                     # ③ 无数字 → 降成限定语
                    fixed = "Many sellers report that " + rest[:1].lower() + rest[1:]
                changes.append(f"降限定语：「{fixed[:50]}…」")
            new_sents.append(fixed + trail)
        out_lines.append("".join(new_sents))
    return "\n".join(out_lines), changes


# --------------------------------------------------------------------------- #
# ② 塞词：单句改写 + 验证闭环，两轮不过就删
# --------------------------------------------------------------------------- #
_REWRITE_SENT = """Rewrite this one sentence so that the phrase "{kw}" appears only as a plain noun phrase
(the subject or object of the sentence). Do NOT put a gerund, infinitive, adjective, or clause opener in front of it
(no "Figuring out…", "Implementing…", "To …", "True …", "When we evaluate…").
Keep the meaning, keep the length within ±20%, keep the same tone. If the phrase cannot fit naturally,
drop it from the sentence entirely and say the same thing in plain words.
Output the rewritten sentence only — no quotes, no explanation.

Sentence: {sent}"""

_REWRITE_HEAD = """Rewrite this article heading so it does NOT contain the exact phrase "{kw}".
Keep the meaning, 4-9 words, plain language, sentence case. Output the heading text only — no # marks, no quotes.

Heading: {head}"""

CompleteFn = Callable[..., Awaitable[str]]


def _clean_llm_line(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^```[a-z]*\s*|\s*```$", "", s).strip()
    s = s.strip("\"'“”‘’ ")
    return s.split("\n")[0].strip()


async def fix_keyword_stuffing(text: str, keywords: list[str], complete: Optional[CompleteFn],
                               max_rounds: int = 2) -> tuple[str, list[str]]:
    """对检测器命中的句子逐句重写；重写后再检测，两轮仍命中就删句 / 从标题里去掉关键词。

    complete(prompt, task=..., temperature=...) -> str 是 LLM 调用；传 None 就只走删除兜底。
    每句一次小调用（3.7-flash，低思考），一篇通常 3-5 句，成本 ¥0.005 量级。
    """
    kws = [k.strip() for k in (keywords or []) if k and len(k.strip()) >= 6]
    if not kws or not text:
        return text, []
    changes: list[str] = []

    async def rewrite_one(unit: str, kw: str, is_head: bool) -> Optional[str]:
        if complete is None:
            return None
        tpl = _REWRITE_HEAD if is_head else _REWRITE_SENT
        try:
            raw = await complete(tpl.format(kw=kw, sent=unit.strip(), head=unit.strip().lstrip("# ").strip()),
                                 task="polish", temperature=0.2)
        except Exception:  # noqa: BLE001  改不动就走兜底
            return None
        new = _clean_llm_line(raw)
        if not new or len(new) < 8:
            return None
        return new

    for _ in range(max_rounds):
        hits = prose_audit.keyword_stuffing(text, kws)
        if not hits:
            break
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            is_head = bool(re.match(r"^#{2,3}\s", line))
            if is_head:
                for kw in kws:
                    if re.search(re.escape(kw), line, re.I):
                        new = await rewrite_one(line, kw, True)
                        if new and not re.search(re.escape(kw), new, re.I):
                            level = re.match(r"^(#{2,3})\s", line).group(1)
                            lines[i] = f"{level} {new}"
                            changes.append(f"标题去关键词：「{line.lstrip('# ')[:40]}」→「{new[:40]}」")
                        break
                continue
            if _STRUCT_LINE.match(line):
                continue
            sents = _split_sentences(line)
            changed = False
            for j, sent in enumerate(sents):
                for kw in kws:
                    if not prose_audit.keyword_stuffing(sent, [kw]):
                        continue
                    trail = sent[len(sent.rstrip()):]
                    new = await rewrite_one(sent, kw, False)
                    if new and not prose_audit.keyword_stuffing(new, [kw]):
                        sents[j] = new + (trail or " ")
                        changes.append(f"改写塞词句：「{sent.strip()[:45]}…」")
                        changed = True
                    break
            if changed:
                lines[i] = "".join(sents)
        text = "\n".join(lines)

    # 兜底：仍命中的，正文句直接删（塞词句按定义删掉不影响意思），标题把关键词短语抠掉
    remaining = prose_audit.keyword_stuffing(text, kws)
    if remaining:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if re.match(r"^#{2,3}\s", line):
                for kw in kws:
                    if re.search(re.escape(kw), line, re.I):
                        stripped = re.sub(re.escape(kw), "", line, flags=re.I)
                        stripped = re.sub(r"\s{2,}", " ", stripped).replace(" :", ":").rstrip(" :-–—")
                        if len(stripped.lstrip("# ").split()) >= 3:
                            lines[i] = stripped
                            changes.append(f"标题抠掉关键词：「{line.lstrip('# ')[:40]}」")
                continue
            if _STRUCT_LINE.match(line) or not line.strip():
                continue
            sents = _split_sentences(line)
            kept = [s for s in sents if not prose_audit.keyword_stuffing(s, kws)]
            if len(kept) != len(sents):
                for s in sents:
                    if s not in kept:
                        changes.append(f"删塞词句：「{s.strip()[:50]}…」")
                lines[i] = "".join(kept)
        text = "\n".join(lines)
    return text, changes


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
#: 断言型数字：声称"世界上发生了什么"。这些编不得。
#: 建议型数字（1600 像素、24 条、2.5 秒）不在此列 —— 那是模型该用自己知识给的操作值，
#: 2026-09-05 放开：一刀切禁掉所有清单外数字，实测把信息增益压到 0.22（四篇最低），
#: 因为正文只敢复述竞品都有的官方文档。
_ASSERTION_NUM = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?\s?percent\b", re.I)
_CLAIM_SENT = re.compile(
    r"(?i)\b(accord\w+ to|study|studies|research|survey|report(?:s|ed)?|data shows?|"
    r"found that|statistics|on average|median|benchmark|analy[sz]\w+)\b")


def invented_assertions(text: str, allowed: set[str]) -> list[str]:
    """新文本里**编出来的断言型数字**。返回空表示这段可以接受。

    两类算断言：① 百分比 / 金额，本身就是在讲"世界上发生了什么"；
    ② 出现在带引用信号的句子里的任何数字（"a study found 4 seconds…"）。
    其余（尺寸、条数、阈值）是操作建议，放行。
    """
    out: list[str] = []
    for m in _ASSERTION_NUM.findall(text or ""):
        if _norm(m) not in allowed:
            out.append(m.strip())
    for sent in _split_sentences(text or ""):
        if not _CLAIM_SENT.search(sent):
            continue
        for m in _NUM.findall(sent):
            if _norm(m) not in allowed:
                out.append(m.strip())
    return list(dict.fromkeys(out))


_BOOST_ADDENDUM = """Write an ADDITION to one section of an article. The section is thin on concrete,
checkable information; your job is to supply what it is missing, not to rewrite it.

Output ONLY a compact markdown block to be appended to the end of the section:
a table (2-4 columns) or a bullet list of concrete items — between {need_new} and {max_new} items,
and no more than {max_words} words in total. Short cells, no explanations inside the table.
Each item must be something a reader can act on or verify: an exact value with units,
a model / product / tool name, a menu path or file name, a threshold, a specific failure symptom.

Hard rules:
- Do NOT repeat anything already in the section (listed below). New items only.
- No headings (no # lines), no introduction sentence, no closing sentence. Block only.
- Statistics are locked: do NOT introduce any percentage, price, bounce rate, sample size,
  or "studies show / according to" claim that is not already in the reference material below.
  Concrete operating values from your own expertise are fine, stated as recommendations.
- Same language as the section.

## Items the article already has (do not repeat any of these)
{have_list}

## Reference material (the only source of statistics you may use)
{material}

## The section
{section}"""


async def boost_thin_sections(text: str, report: dict, facts: str, material: str,
                              complete: Optional[CompleteFn],
                              max_sections: int = 4,
                              sections: Optional[list[str]] = None,
                              budget: Optional[int] = None) -> tuple[str, list[str]]:
    """给检测器判为「空转」的小节**追加**一块具体信息，原文一个字不动。

    2026-09-05 从「整节重写」改成「只加不换」。重写版实测 8 次补写落地 0 次：
    模型一重写就会重排小标题（结构护栏拒 2 次）、把 0.1/0.2/0.3 mm 折成一句"thin to thick"
    （保留护栏拒 4 次）—— 两道护栏都对，是重写这个动作本身和"加东西"的目标打架。
    附加块从构造上就不可能丢原文、不可能动标题，剩下的检查只有三条：
      ① 真的加了 ≥3 条新单元；② 没编统计；③ 篇幅没飞（≤1.6×）。
    仍然是验证闭环：一条不过就整块丢弃。最多修四节，¥0.02/篇量级。
    """
    # sections 显式指定就按它来（整篇低于竞品时，点名最稀的几节局部补，而不是重跑）；
    # 不指定就按检测器判的薄节。
    thin = sections if sections is not None else ((report.get("density") or {}).get("thin") or [])
    if not thin or complete is None or not text:
        return text, []
    per100 = float(((report.get("target") or {}).get("per100")) or 4.0)
    allowed = _facts_numbers(facts) | _facts_numbers(material)
    changes: list[str] = []
    # 全文篇幅预算：补写总共最多加两成。实测 3PL 三块附加表把 2280 词撑到 2956（+30%），
    # 会把用户定的字数目标顶成"偏多"。预算用完就停，剩下的薄节留给用户看提示。
    # budget 由调用方跨轮传入（router 算一次原稿的 20%，每轮扣减）；不传就按本次文本算。
    # 实测三轮各算各的 20%，Dana 那篇 2206 → 2842 词（+29%），Martin +41%。
    if budget is None:
        budget = int(0.2 * density_audit.word_count(text))
    added = 0
    for head, body in density_audit.sections(text):
        if head not in thin or len(changes) >= max_sections or added >= budget:
            continue
        is_intro = head == "(intro)"
        if is_intro:
            m = re.match(r"\A\s*#\s+.*(?:\n|$)\s*", body)
            block = body[m.end():] if m else body
        else:
            block = f"## {head}{body}"
        words = density_audit.word_count(block)
        if words < 60:
            continue
        old_units = density_audit._evidence_units(block, material)
        need_new = max(3, int(per100 * words / 100) - len(old_units))
        # 篇幅上限先算好告诉模型：实测白鞋那篇七个薄节五个被"篇幅跑偏"拒掉 ——
        # 短节配了一张十几行的大表。给它条数区间和词数上限，落地率才上得去。
        max_words = int(max(0.6 * words, 220))
        if added + max_words > budget:            # 这块补完会超预算 → 不开工，别事后才发现
            continue
        # 整篇不达标触发的补写，把全文已有的单元都列给模型 —— 它才知道什么算"新"
        have = sorted(density_audit._evidence_units(text, material) if sections is not None else old_units)
        try:
            raw = await complete(
                _BOOST_ADDENDUM.format(need_new=need_new, max_new=need_new + 6, max_words=max_words,
                                       have_list="\n".join(f"- {u}" for u in have[:80]) or "- (none)",
                                       material=material[:12000], section=block),
                task="polish", temperature=0.3)
        except Exception:  # noqa: BLE001  补写失败不该拖垮整篇交付
            continue
        add = re.sub(r"^```[a-z]*\s*|\s*```$", "", (raw or "").strip()).strip()
        # 附加块里绝不能有标题：有就整块不要，别去"修"它
        add = "\n".join(l for l in add.split("\n") if not re.match(r"^\s*#{1,6}\s", l)).strip()
        if not add:
            continue
        trailing = block[len(block.rstrip()):] or "\n\n"
        new = block.rstrip() + "\n\n" + add + trailing

        gained = density_audit._evidence_units(new, material) - old_units
        if sections is not None:
            # 触发点是"整篇低于竞品"时，只认**全文里没有的**单元。实测 Martin 那篇两轮落地
            # 四块（每块 +15~19 条），整篇证据密度只从 3.81 挪到 4.16 —— 加的东西别的节早写过，
            # 对整篇指标是零，对读者也是重复。
            gained -= density_audit._evidence_units(text, material)
        invented = invented_assertions(add, allowed | {_norm(y) for y in _NUM.findall(block)})
        n_new = density_audit.word_count(new)
        # 篇幅上限：与告诉模型的 max_words 对齐再留一成余量。
        # 短节配一张表本来就会超 1.6 倍，实测白鞋那篇七个薄节五个因此被拒，而那正是最该补的地方。
        too_long = n_new > words + max_words * 1.1
        if len(gained) < 3 or invented or too_long:
            reason = ("编了新数字 " + "、".join(invented[:3]) if invented
                      else "篇幅跑偏" if too_long
                      else f"只加了 {len(gained)} 条")
            logger.info("补写「%s」已丢弃：%s", head, reason)
            continue
        replaced = text.replace(block, new, 1)
        if replaced == text:
            logger.warning("补写「%s」未能定位原文，已跳过", head)
            continue
        before = density_audit.density(block)["evidence_per100"]
        after = density_audit.density(new)["evidence_per100"]
        text = replaced
        added += n_new - words
        changes.append(f"补写空转小节「{head[:32]}」：+{len(gained)} 条具体信息，证据密度 {before}→{after}/100词")
    return text, changes


_FAQ_SECTION = """Write a short FAQ section for an article, answering these questions that real
searchers ask on Google for this topic.

Questions:
{questions}

Rules:
- Output markdown: one `### <question exactly as given>` heading per question, then 45-90 words
  of answer underneath. No preamble, no closing paragraph, no extra questions.
- Each answer must stand alone: someone who reads only that answer should get a complete reply.
  Do not open with "This", "That", "As mentioned" or refer to earlier parts of the article.
- Every answer must contain at least one concrete, checkable specific: a setting name, a menu
  path, a file name, a threshold with units, or a named tool.
- Statistics are locked: no percentage, price, bounce rate or "studies show" claim unless it
  appears in the reference material below. Concrete operating values from your own expertise
  are fine, stated as recommendations.
- Write in {language}.

## Reference material
{material}"""


#: FAQ 小节标题按文章语言走。写死英文的话，一篇西班牙语文章末尾会冒出
#: "Frequently asked questions" —— 交付物里最显眼的位置出一个外语标题。
_FAQ_HEADS = {
    "english": "Frequently asked questions", "chinese": "常见问题",
    "spanish": "Preguntas frecuentes", "french": "Questions fréquentes",
    "german": "Häufige Fragen", "japanese": "よくある質問", "portuguese": "Perguntas frequentes",
    "korean": "자주 묻는 질문", "italian": "Domande frequenti", "indonesian": "Pertanyaan umum",
}


def _faq_heading(language: str) -> str:
    low = (language or "").lower()
    for k, v in _FAQ_HEADS.items():
        if low.startswith(k):
            return v
    return _FAQ_HEADS["english"]


def dedupe_repeated_stats(text: str) -> tuple[str, list[str]]:
    """同一条论据统计写进两个以上小节的，只留第一次，后面的整句删。纯代码，零 LLM。

    实测：Yottaa「63% / 5 亿访问 / 1300 站」被写进三个小节，读者读到第三遍，
    信息量一个单位都没增加。检测器（density.repeated_stats）能抓到，
    这里把它收口。

    删得很保守 —— 一句话只有同时满足三条才删：
      ① 含一个前文已经出现过的统计；② 带引用信号（according to / study / data…），
      说明它就是在复述那条统计，不是顺带提了个数；③ 句里没有任何**前文没出现过的**硬信息。
    三条缺一不删。宁可留一句重复，也不能删掉一句带新东西的话。
    """
    if not text:
        return text, []
    seen_stats: set[str] = set()
    seen_units: set[str] = set()
    changes: list[str] = []
    out_secs: list[str] = []
    for head, body in density_audit.sections(text):
        lines = body.split("\n")
        for i, line in enumerate(lines):
            if _STRUCT_LINE.match(line) or not line.strip():
                continue
            kept: list[str] = []
            for sent in _split_sentences(line):
                u = density_audit.hard_units(sent)
                stats = u["stat"]
                new_units = (u["param"] | u["entity"] | stats) - seen_units
                repeat = stats & seen_stats
                if repeat and _CLAIM_SENT.search(sent) and not (new_units - repeat):
                    changes.append(f"删重复统计句（{'、'.join(sorted(repeat)[:2])}）：「{sent.strip()[:44]}…」")
                    continue
                kept.append(sent)
                seen_units |= u["param"] | u["entity"] | stats
                seen_stats |= stats
            lines[i] = "".join(kept)
        out_secs.append(("" if head == "(intro)" else f"## {head}") + "\n".join(lines))
    return "".join(out_secs), changes


def weakest_sections(report: dict, limit: int = 3) -> list[str]:
    """整篇密度不达标时该补哪几节：证据密度最低的几节（FAQ 不算，它天然是复述）。"""
    rows = (report.get("density") or {}).get("sections") or []
    rows = [r for r in rows if r.get("words", 0) >= 80
            and not re.search(r"(?i)^(frequently asked|faq|常见问题)", r.get("head", ""))]
    rows.sort(key=lambda r: r.get("per100", 0))
    return [r["head"] for r in rows[:limit]]


def density_below(report: dict) -> bool:
    """整篇证据密度是否**还没到**竞品中位数。补写循环的继续条件（用户 2026-09-05：不许不如竞品）。
    打标（density_short，差 20% 以上才标红）和它是两回事：补写往 100% 追，标红只在差得远时亮。"""
    return density_short(report, tolerance=1.0)


_STALE_YEAR = re.compile(r"\b(20[12]\d)\b")


def fix_stale_year(text: str) -> tuple[str, list[str]]:
    """H1 里的往年年份 → 今年。只动 H1：正文里的 2024 可能是真实数据年份，不能碰。
    2026-09-05 实测 ebike 那篇 H1 带 "(2024)"。"""
    import datetime as _dt
    year = _dt.date.today().year
    m = re.search(r"^#\s+(.+)$", text or "", re.M)
    if not m:
        return text, []
    h1 = m.group(1)
    stale = [y for y in _STALE_YEAR.findall(h1) if int(y) < year]
    if not stale:
        return text, []
    new = _STALE_YEAR.sub(lambda mm: str(year) if int(mm.group(1)) < year else mm.group(1), h1)
    return text[:m.start(1)] + new + text[m.end(1):], [f"标题里的 {stale[0]} 改成 {year}"]


def density_short(report: dict, tolerance: float = 0.8) -> bool:
    """整篇证据密度是否低于竞品中位数的 tolerance（默认 80%，即差 20% 以上就打标）。
    没有竞品基线就不打标 —— 拿不到锚点的时候不给结论。"""
    bench = report.get("benchmark") or {}
    if not bench.get("measurable"):
        return False
    mine = (report.get("density") or {}).get("evidence_per100") or 0
    return mine < tolerance * float(bench.get("median") or 0)


async def ensure_paa_coverage(text: str, report: dict, material: str, language: str,
                              complete: Optional[CompleteFn]) -> tuple[str, list[str]]:
    """搜索页的子问题一个都不能漏 —— 正文没答到的，补一个 FAQ 小节答掉。

    为什么用 FAQ 而不是改写正文：改写会动已经写好的结构，风险高；FAQ 是纯追加，
    结构零风险，而且本来就是 PAA / AI 摘要最容易直接引用的形态。

    照例是验证闭环：每个问题必须真的变成"答到了"（用检测器同一个 _answers 判据），
    答案不许编统计、不许靠上文接续。有一条不过就整块丢弃 —— 宁可覆盖率低，
    也不能往交付物里塞一段答非所问的 FAQ。
    """
    missing = ((report.get("intent") or {}).get("missing") or [])[:5]
    if not missing or complete is None or not text:
        return text, []
    try:
        raw = await complete(
            _FAQ_SECTION.format(questions="\n".join(f"- {q}" for q in missing),
                                material=material[:12000], language=language or "English"),
            task="polish", temperature=0.3)
    except Exception:  # noqa: BLE001  补不上不该拖垮交付
        return text, []

    block = re.sub(r"^```[a-z]*\s*|\s*```$", "", (raw or "").strip()).strip()
    if not block:
        return text, []
    allowed = _facts_numbers(material)
    bad = invented_assertions(block, allowed)
    if bad:
        logger.info("FAQ 补写已丢弃：编了统计 %s", bad[:3])
        return text, []

    candidate = text.rstrip() + f"\n\n## {_faq_heading(language)}\n\n" + block + "\n"
    covered = [q for q in missing if density_audit._answers(candidate, q)]
    if len(covered) < len(missing):
        logger.info("FAQ 补写已丢弃：%d/%d 个问题仍未答到", len(covered), len(missing))
        return text, []
    return candidate, [f"补 FAQ 小节答掉搜索页 {len(missing)} 个未覆盖的问题："
                       + "；".join(q[:36] for q in missing[:3])]


# --------------------------------------------------------------------------- #
# ④ 三个句子级的局部修：开头空转 / H2 首句接上文 / 标题体裁
#    规矩（用户 2026-09-05）：每处最多三轮，不行就算了；差不多就算过关。
#    这三处各只改**一次** —— 都是一句话或一段话的事，改一次不成说明模型就是这么想的，
#    再逼两轮只会换个说法重复失败。
# --------------------------------------------------------------------------- #
#: 全流程局部修的总轮数上限（密度补写：薄节一轮 + 整篇不达标最多再两轮）
MAX_FIX_ROUNDS = 3

_LEAD_REWRITE = """Rewrite ONLY this opening paragraph so its first two sentences lead with the most
concrete, actionable point the paragraph ALREADY contains — a value with units, a setting, a named
tool / product / file, or a specific condition. Cut scene-setting, "in today's world", and any
restating of the title.

Hard limits — the rewrite is discarded if any is broken:
- Use ONLY facts, names, numbers and files that are already in this paragraph. Do NOT introduce a
  new tool, product, file name, spreadsheet, template, standard, number or percentage. If the
  paragraph has no concrete item, just get to the point faster; do not invent one.
- Do not add first-person claims ("we control / we audit / in our work").
- Keep every concrete item already in the paragraph. Same language, same or shorter length.
Output the rewritten paragraph only.

Paragraph:
{para}"""

_FILE_TOKEN = re.compile(r"\b[\w-]+\.(?:xlsx|xls|csv|pdf|docx?|json|txt|ya?ml|liquid|js|py|xml|md)\b", re.I)
_NEW_WE = re.compile(r"(?i)\b(?:we|our team)\s+(?:control|audit|inspect|verify|enforce|manage|run|operate|"
                     r"own|dictate|handle|monitor|supervise|source|build|test|check|require)\b")


def introduced_names(new: str, *sources: str) -> list[str]:
    """改写稿里冒出来、而原材料里没有的专名 / 文件 / 代码段 / 第一人称经手声明。

    2026-09-05 ebike 那篇：开头改写的提示词要求「必须给一个具体值或工具/文件名」，
    模型就编了个 `Bill_of_Materials.xlsx` 放在第一句，核验只查数字，放行了。
    专名和文件也得核验：没在原段 / 事实清单里出现过的，一律算编造。
    """
    pool = density_audit.squash(" ".join(s or "" for s in sources))
    out: list[str] = []
    seen: set[str] = set()

    def add(x: str) -> None:
        k = density_audit.squash(x)
        if k and k not in seen and k not in pool:
            seen.add(k)
            out.append(x)

    for m in density_audit._CODE.findall(new or ""):
        add(m.strip("` "))
    for m in _FILE_TOKEN.findall(new or ""):
        add(m)
    for ent in density_audit.hard_units(new or "").get("entity", set()):
        add(ent)
    m = _NEW_WE.search(new or "")
    if m and not _NEW_WE.search(" ".join(s or "" for s in sources)):
        out.append(m.group(0))
    return out

_SELF_CONTAINED = """Rewrite this single sentence so it can stand alone as the first sentence of a
section titled "{head}": name the subject explicitly instead of "this / that / it / they / as
mentioned", keep every fact and number, same language, similar length. Output the sentence only.

Sentence: {sent}"""

_TITLE_SHAPE = """Rewrite this article title so it reads as {hint}, while keeping the phrase
"{kw}" in it. 6-12 words, sentence case, no quotes. Do not add a year. Output the title only.

Title: {h1}"""
_SHAPE_HINT = {
    "how_to": "a how-to guide (starts with 'How to …')",
    "listicle": "a numbered list ('7 ways to …' / '10 best …')",
    "comparison": "a head-to-head comparison ('A vs B: …')",
    "definition": "an explainer ('What is … and …')",
    "tool": "a practical guide ('How to … with …')",
}


async def fix_opening_gap(text: str, material: str, complete: Optional[CompleteFn],
                          max_gap: int = 120) -> tuple[str, list[str]]:
    """开头 120 词之内没有任何可照做的信息 → 只重写第一段，让它两句内给出答案。

    验证：空转词数真的变小、没编统计、没丢原段里的具体信息、没变长。一条不过就丢。
    """
    if complete is None or not text:
        return text, []
    gap = density_audit.density(text)["opening_gap"]
    if gap <= max_gap:
        return text, []
    # 找**第一个散文块**，不是"H1 后面那一块"：实测白鞋那篇 H1 下面直接是 H2，
    # 没有导语，第一段正文在 H2 底下。跳过标题 / 表格 / 列表 / 图片占位，
    # 但 "**粗体开头**" 的段落是正文，不能按首字符一刀切跳掉。
    m2 = None
    for cand in re.compile(r"\S.*?(?=\n\s*\n|\Z)", re.S).finditer(text):
        if _STRUCT_LINE.match(cand.group(0)):
            continue
        if density_audit.word_count(cand.group(0)) >= 25:
            m2 = cand
            break
        if cand.start() > 1500:
            break
    if not m2:
        return text, []
    para = m2.group(0)
    try:
        raw = await complete(_LEAD_REWRITE.format(para=para), task="polish", temperature=0.3)
    except Exception:  # noqa: BLE001
        return text, []
    new = re.sub(r"^```[a-z]*\s*|\s*```$", "", (raw or "").strip()).strip()
    if not new or re.search(r"^\s*#", new, re.M):
        return text, []
    allowed = _facts_numbers(material) | {_norm(y) for y in _NUM.findall(para)}
    if invented_assertions(new, allowed):
        logger.info("开头改写已丢弃：编了统计")
        return text, []
    made_up = introduced_names(new, para, material, text)   # 全文里出现过的名字不算编
    if made_up:
        logger.info("开头改写已丢弃：引入了原段没有的专名/文件（%s）", "、".join(made_up[:3]))
        return text, []
    old_units = density_audit._evidence_units(para)
    if old_units and len(density_audit.lost_units(para, new)) > 0.2 * len(old_units):
        logger.info("开头改写已丢弃：丢了原有具体信息")
        return text, []
    if density_audit.word_count(new) > 1.2 * density_audit.word_count(para):
        logger.info("开头改写已丢弃：变长了")
        return text, []
    candidate = text[:m2.start()] + new + text[m2.end():]
    gap2 = density_audit.density(candidate)["opening_gap"]
    # 要么进到 120 词以内，要么至少砍一半；381→357 这种"改了但没改到"不收（一次不成就算了）
    if not (gap2 <= max_gap or gap2 <= gap * 0.5):
        logger.info("开头改写已丢弃：空转没实质缩短（%d→%d）", gap, gap2)
        return text, []
    return candidate, [f"开头改成先给答案：第一个可照做的信息从第 {gap} 词提前到第 {gap2} 词"]


async def fix_orphan_h2(text: str, complete: Optional[CompleteFn],
                        max_heads: int = 3) -> tuple[str, list[str]]:
    """H2 首句靠 This / 因此 接上文 → 单句改写成自足的，最多改三节。
    验证：改完不再以指代词开头、句里的具体信息一个不少。"""
    if complete is None or not text:
        return text, []
    orphans = density_audit.structural_readability(text)["orphan_h2"][:max_heads]
    if not orphans:
        return text, []
    changes: list[str] = []
    cjk = prose_audit.is_cjk(text)
    for head in orphans:
        body = dict(density_audit.sections(text)).get(head, "")
        sents = prose_audit.split_sentences(body, cjk)
        if not sents:
            continue
        first = sents[0].strip()
        if first not in body:
            continue
        try:
            raw = await complete(_SELF_CONTAINED.format(head=head, sent=first),
                                 task="polish", temperature=0.2)
        except Exception:  # noqa: BLE001
            continue
        new = _clean_llm_line(raw)
        if (not new or density_audit._ANAPHORA.match(new)
                or density_audit.lost_units(first, new)
                or len(new.split()) > 1.5 * max(len(first.split()), 4)):
            logger.info("H2「%s」首句改写已丢弃", head)
            continue
        new_body = body.replace(first, new, 1)
        text = text.replace(body, new_body, 1)
        changes.append(f"「{head[:32]}」首句改成自足的")
    return text, changes


async def fix_title_shape(text: str, report: dict, keyword: str,
                          complete: Optional[CompleteFn]) -> tuple[str, list[str]]:
    """标题体裁和搜索页主流体裁不符（一票否决项）→ 只改 H1，改一次。
    验证：新标题的体裁落在搜索页主流体裁里、关键词还在。正文结构不动 —— 那不是局部修能管的。"""
    intent = report.get("intent") or {}
    if complete is None or intent.get("shape_match", True) or not intent.get("serp_shapes"):
        return text, []
    m = re.search(r"^#\s+(.+)$", text or "", re.M)
    if not m:
        return text, []
    h1 = m.group(1).strip()
    shape = intent["serp_shapes"][0]
    try:
        raw = await complete(_TITLE_SHAPE.format(hint=_SHAPE_HINT.get(shape, "a how-to guide"),
                                                 kw=keyword, h1=h1), task="polish", temperature=0.3)
    except Exception:  # noqa: BLE001
        return text, []
    new = _clean_llm_line(raw).strip("#").strip()
    if not new or not (density_audit._shapes_of(new) & set(intent["serp_shapes"])):
        logger.info("标题体裁改写已丢弃：仍不匹配")
        return text, []
    if keyword and keyword.lower() not in new.lower() and not all(
            w in new.lower() for w in re.findall(r"[a-z]{4,}", keyword.lower())[:2]):
        logger.info("标题体裁改写已丢弃：关键词丢了")
        return text, []
    text = text[:m.start(1)] + new + text[m.end(1):]
    return text, [f"标题改成搜索页的体裁（{shape}）：「{new[:48]}」"]



async def postfix(text: str, keywords: list[str], facts: str,
                  complete: Optional[CompleteFn],
                  report: Optional[dict] = None,
                  material: str = "", language: str = "English") -> tuple[str, list[str]]:
    """交付前跑一遍：假经验句（纯代码）→ 塞词（小调用 + 闭环）→ 空转小节补写（同上）。"""
    t1, c1 = strip_fake_experience(text, facts)
    t2, c2 = await fix_keyword_stuffing(t1, keywords, complete)
    if not report:
        return t2, c1 + c2
    changes = c1 + c2
    t3, c3 = await boost_thin_sections(t2, report, facts, material, complete)
    t4, c4 = await ensure_paa_coverage(t3, report, material, language, complete)
    t5, c5 = dedupe_repeated_stats(t4)
    # 句子级的三个局部修，各只改一次
    t6, c6 = await fix_opening_gap(t5, material, complete)
    t7, c7 = await fix_orphan_h2(t6, complete)
    t8, c8 = await fix_title_shape(t7, report, (keywords or [""])[0], complete)
    t9, c9 = fix_stale_year(t8)
    return t9, changes + c3 + c4 + c5 + c6 + c7 + c8 + c9
