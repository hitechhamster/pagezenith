"""轻量词法工具：多语言分词 + embedding 盲区探测（数字/否定/反义）。

用途：
  - surface_conflict(a, b): 探测 embedding 的两个已知盲区。即便 cosine 很高，
    只要存在数字不一致 / 否定不一致 / 反义方向，就把这对 claim **降级**到 LLM 裁决带，
    避免被 embedding 直接合掉（裁决权仍归 LLM，这里只负责路由）。
  - heuristic_same(a, b): mock 模式下的裁决启发式（无 LLM key 时用）。
  - multi_tokens(text):   latin 词 + CJK 单字/双字，供 mock embedding 与冲突探测共用。

注意：这些是 mock/路由用的廉价启发式，不替代真实 LLM 裁决。
"""

from __future__ import annotations

import re

_LATIN = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[一-鿿]")
NUMERIC = re.compile(r"\d+(?:[.,]\d+)?%?")

# 否定标记（多语言）
_NEG_TOKENS = ("not", "no", "never", "without", "un", "非", "不", "未", "无")
# 反义/方向相反对（embedding 易混）
_ANTONYMS = [
    ("regulated", "unregulated"), ("rise", "fall"), ("up", "down"),
    ("increase", "decrease"), ("gain", "loss"), ("rises", "falls"),
    ("rose", "fell"), ("gained", "lost"),
    ("涨", "跌"), ("升", "降"), ("受监管", "不受监管"), ("上涨", "下跌"),
]

# 反义词 → 簇内规范形（两端映射到同一代表），供 mock embedding 模拟“方向盲区”。
_ANTONYM_CANON: dict[str, str] = {}
for _x, _y in _ANTONYMS:
    _ANTONYM_CANON[_x] = _x
    _ANTONYM_CANON[_y] = _x


def detect_lang(text: str) -> str:
    """按主语言判定（zh/en/other）。用 CJK 占比，避免英文页里夹带少量中文
    （语言切换、页脚等）就被误判成中文。CJK 占比 ≥ 20% 才算中文页。"""
    cjk = len(_CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    total = cjk + latin
    if total == 0:
        return "other"
    return "zh" if cjk / total >= 0.20 else "en"


def multi_tokens(text: str) -> list[str]:
    t = text.lower()
    tokens = _LATIN.findall(t)
    cjk = _CJK.findall(text)
    tokens += cjk                                                  # 单字
    tokens += [cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)]  # 双字
    return tokens


def _has_neg(text: str, toks: set[str]) -> bool:
    return any(n in toks for n in _NEG_TOKENS)


def _antonym_flip(a: str, b: str, ta: set[str], tb: set[str]) -> bool:
    for x, y in _ANTONYMS:
        x_in_a = x in ta or x in a
        y_in_b = y in tb or y in b
        y_in_a = y in ta or y in a
        x_in_b = x in tb or x in b
        if (x_in_a and y_in_b and not (y_in_a or x_in_b)) or (
            y_in_a and x_in_b and not (x_in_a or y_in_b)
        ):
            return True
    return False


def surface_conflict(a: str, b: str) -> bool:
    """True = 存在 embedding 盲区差异，应交 LLM 裁决而非直接合并。"""
    if set(NUMERIC.findall(a)) != set(NUMERIC.findall(b)):
        return True
    ta, tb = set(multi_tokens(a)), set(multi_tokens(b))
    if _has_neg(a, ta) != _has_neg(b, tb):
        return True
    if _antonym_flip(a, b, ta, tb):
        return True
    return False


def heuristic_same(a: str, b: str) -> bool:
    """mock 裁决：有盲区冲突判不同，否则判同一事实的不同表述。"""
    return not surface_conflict(a, b)


def embed_tokens(text: str) -> list[str]:
    """mock embedding 的 token 流：刻意复刻真实 embedding 的盲区——
    对数字/否定/方向不敏感，使“只差一个数字/方向”的句子 cosine 仍很高，
    从而被冲突保护降级到 LLM 裁决（而非 embedding 直接合/分）。
      - 数字 → 统一 <num>
      - 否定词 → 丢弃
      - 反义词 → 映射到规范形
    """
    out: list[str] = []
    for tok in multi_tokens(text):
        if tok in _NEG_TOKENS:
            continue
        if NUMERIC.fullmatch(tok):
            out.append("<num>")
            continue
        out.append(_ANTONYM_CANON.get(tok, tok))
    return out
