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


_BOOST_SECTION = """Rewrite this one section of an article so that it carries real, checkable
information instead of positioning statements.

Hard rules:
- Statistics are locked: do NOT introduce any percentage, price, bounce rate, sample size,
  or "studies show / according to" claim that is not already in the reference material below.
- Concrete operating values ARE wanted: exact upload dimensions, size thresholds, counts
  per page, timeout seconds, target metric values, menu paths, file and setting names.
  Use your own expertise for these and state them as recommendations, not as findings
  ("upload the hero image at no more than 1600 px wide", not "studies show 1600 px is optimal").
- Keep the same heading, the same language, and roughly the same length ({words} words ±20%).
- Prefer concrete specifics: exact setting names, file names, menu paths, parameter values,
  units, thresholds, failure symptoms. Those are what the reader came for.
- Do NOT invent first-hand experience ("we recently audited a store that…"). You have none.
- Output the rewritten section only — no preamble, no explanation.

## Reference material (the only source of facts you may use)
{material}

## Section to rewrite
{section}"""


async def boost_thin_sections(text: str, report: dict, facts: str, material: str,
                              complete: Optional[CompleteFn],
                              max_sections: int = 3) -> tuple[str, list[str]]:
    """把检测器判为「空转」的小节定向重写一遍，改完再测，没变好就原样退回。

    和塞词修复同一个套路：**验证闭环代替信任**。密度这件事没法纯代码修
    （硬信息不能凭空造），所以只能调模型；但调完必须验证：
      ① 证据密度真的涨了；② 没有引入事实清单和资料之外的新数字；③ 篇幅没崩。
    三条有一条不过就丢弃这次重写 —— 宁可保持原样，也不能为了指标好看而编数字。

    最多修三节，成本压在 ¥0.015 量级以内。
    """
    thin = (report.get("density") or {}).get("thin") or []
    if not thin or complete is None or not text:
        return text, []

    allowed = _facts_numbers(facts) | _facts_numbers(material)
    changes: list[str] = []
    for head, body in density_audit.sections(text):
        if head not in thin or len(changes) >= max_sections:
            continue
        # sections() 返回的 body 自带前导换行，别再补一个 —— 补了就跟原文对不上，
        # replace 静默失败，却照样往 changes 里记一笔「已补写」。
        # (intro) 是虚构的节名（H1 到第一个 H2 之间那段），它在原文里没有对应的标题行，
        # 拼上 "## (intro)" 必然定位失败 —— 实测日志里就是这么报的。
        is_intro = head == "(intro)"
        if is_intro:
            # (intro) 那一块是从文首到第一个 H2 之间的全部内容，**H1 标题也在里面**。
            # 连 H1 一起交给模型重写，它不会原样还回来，结构守卫就会把整次补写毙掉
            # （实测日志：「补写「(intro)」改变了标题结构，已丢弃」）。把 H1 摘出去。
            m = re.match(r"\A\s*#\s+.*(?:\n|$)\s*", body)
            block = body[m.end():] if m else body
        else:
            block = f"## {head}{body}"
        if density_audit.word_count(block) < 60:
            continue
        words = density_audit.word_count(block)
        try:
            raw = await complete(
                _BOOST_SECTION.format(words=words, material=material[:12000], section=block),
                task="polish", temperature=0.3)
        except Exception:  # noqa: BLE001  补写失败不该拖垮整篇交付
            continue
        new = (raw or "").strip()
        if is_intro:
            new = re.sub(r"^#{1,6}\s+.*$", "", new, count=1, flags=re.M).lstrip()
        elif not new.startswith("#"):
            new = f"## {head}\n{new}"
        # 把原块尾部的空行原样接回去。不接的话下一个 "## " 会紧贴在句号后面
        # （实测产出过 "...reducing asset sizes.## How to make a Shopify store..."），
        # markdown 里那就不是标题了，整篇结构从这里开始塌。
        new += block[len(block.rstrip()):] or "\n\n"

        before = density_audit.density(block)["evidence_per100"]
        after = density_audit.density(new)["evidence_per100"]
        n_new = density_audit.word_count(new)
        invented = invented_assertions(new, allowed | {_norm(y) for y in _NUM.findall(block)})
        if after <= before or invented or not (0.75 * words <= n_new <= 1.25 * words):
            reason = ("编了新数字 " + "、".join(invented[:3]) if invented
                      else "密度没提升" if after <= before else "篇幅跑偏")
            logger.info("补写「%s」已丢弃：%s", head, reason)
            continue
        # 只有真的替换成功才记账 —— 报告"改了什么"必须是实际发生的事
        replaced = text.replace(block, new, 1)
        if replaced == text:
            logger.warning("补写「%s」未能定位原文，已跳过", head)
            continue
        # 结构守卫：标题数一个都不许变。实测踩过 —— 补写块尾部少了个换行，
        # 下一个 "## " 紧贴到句号后面（"...asset sizes.## How to..."），
        # 那一行就不再是标题，整篇的 H2 结构从这里塌一半。
        if [len(re.findall(p, replaced, re.M)) for p in (r"^#{1,6} ",)] != \
           [len(re.findall(p, text, re.M)) for p in (r"^#{1,6} ",)]:
            logger.warning("补写「%s」改变了标题结构，已丢弃", head)
            continue
        text = replaced
        changes.append(f"补写空转小节「{head[:32]}」：证据密度 {before}→{after}/100词")
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


async def postfix(text: str, keywords: list[str], facts: str,
                  complete: Optional[CompleteFn],
                  report: Optional[dict] = None,
                  material: str = "", language: str = "English") -> tuple[str, list[str]]:
    """交付前跑一遍：假经验句（纯代码）→ 塞词（小调用 + 闭环）→ 空转小节补写（同上）。"""
    t1, c1 = strip_fake_experience(text, facts)
    t2, c2 = await fix_keyword_stuffing(t1, keywords, complete)
    if not report:
        return t2, c1 + c2
    t3, c3 = await boost_thin_sections(t2, report, facts, material, complete)
    t4, c4 = await ensure_paa_coverage(t3, report, material, language, complete)
    t5, c5 = dedupe_repeated_stats(t4)
    return t5, c1 + c2 + c3 + c4 + c5
