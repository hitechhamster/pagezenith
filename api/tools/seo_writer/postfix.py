"""交付前的确定性后处理 —— prompt 管不住的两件事在这里用代码收口。

七轮实测的规律：用代码修的（换模型、数字闸门、结构闸门、事实溯源）一次修好不回头；
用 prompt 修的（塞词 5 轮、假经验句 4 轮）每轮换个说法又回来。这两件事的检测器
（prose_audit.keyword_stuffing / _FAKE_CASE）已经 100% 命中，那就别再求模型自觉，
检测到就机械地改，改完再检测，过不了就删 —— 和数字闸门一个思路：验证闭环代替信任。

两个处理器都返回 (新文本, 改动说明列表)，改动说明直接给用户看。
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

from . import prose_audit

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
async def postfix(text: str, keywords: list[str], facts: str,
                  complete: Optional[CompleteFn]) -> tuple[str, list[str]]:
    """交付前跑一遍：先假经验句（纯代码），再塞词（小调用 + 闭环）。"""
    t1, c1 = strip_fake_experience(text, facts)
    t2, c2 = await fix_keyword_stuffing(t1, keywords, complete)
    return t2, c1 + c2
