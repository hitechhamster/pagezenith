"""外链拓客的搜索足迹（footprint）。

用搜索引擎足迹近似"谁可能给外链"——不接付费反链库。每个足迹 = 1 次 SerpApi。
每个足迹自带一个默认机会类型提示（LLM 会再据实纠正）。
"""

from __future__ import annotations

# (查询模板, 默认机会提示)
FOOTPRINTS: list[tuple[str, str]] = [
    ('{kw} "write for us"', "投稿"),
    ('{kw} "guest post"', "投稿"),
    ('{kw} intitle:resources', "资源位加链"),
    ('{kw} "recommended tools"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} "useful links"', "资源位加链"),
    ('{kw} "submit a guest post"', "投稿"),
]


def build_footprints(keyword: str, n: int) -> list[tuple[str, str]]:
    kw = keyword.strip()
    return [(tpl.format(kw=kw), hint) for tpl, hint in FOOTPRINTS[: max(1, n)]]
