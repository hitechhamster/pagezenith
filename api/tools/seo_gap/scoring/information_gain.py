"""信息增益评分（规格 §2）。分数由代码算，可调权重。

流程（混合去重见 semantic.py）：
  1. 竞品全部 claims 聚簇去重 → 每簇一条 canonical claim；
     covered_by_n_competitors = 簇内不同竞品 URL 数。
  2. 目标 claims 对 canonical 做最佳单一匹配：
     shared = 命中 canonical；novel = 没命中任何 canonical；
     missing = 没有被任何目标 claim 命中的 canonical。
  3. 评分：
     - 正分 ∝ novel 中 fact/data_point 条数（事实型权重高，opinion 低）。
     - 扣分 ∝ missing 中被多个竞品共同覆盖的条数（越多人覆盖越该补）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import InformationGain, MissingPoint, NovelPoint, SinglePageProfile
from .semantic import SemanticDeduper

# 可调权重 ----------------------------------------------------------------- #
TYPE_WEIGHT = {
    "data_point": 1.0,
    "fact": 0.9,
    "definition": 0.5,
    "how_to_step": 0.5,
    "opinion": 0.2,
}
NOVEL_FULL_SCORE_AT = 6.0    # 加权 novel 量达到该值即拿满正分段
MISSING_FULL_PENALTY_AT = 6.0
NOVEL_WEIGHT = 60            # 正分上限
MISSING_WEIGHT = 40         # 扣分上限


@dataclass
class _Canonical:
    text: str
    type: str
    covered_by: int          # 簇内不同竞品 URL 数


@dataclass
class _Breakdown:
    novel: list[NovelPoint] = field(default_factory=list)
    missing: list[MissingPoint] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)


async def build_canonicals(
    competitor_profiles: list[SinglePageProfile], deduper: SemanticDeduper
) -> list[_Canonical]:
    """竞品 claims 聚簇 → canonical 列表。代表取簇内最长文本；类型取簇内出现最多的类型。"""
    items: list[tuple[str, str, str]] = []  # (text, type, url)
    for prof in competitor_profiles:
        for claim in prof.claims:
            items.append((claim.text, claim.type, prof.url))
    if not items:
        return []

    labels = await deduper.cluster([t for t, _, _ in items])

    clusters: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        clusters.setdefault(label, []).append(idx)

    canonicals: list[_Canonical] = []
    for member_idxs in clusters.values():
        members = [items[i] for i in member_idxs]
        rep_text, rep_type, _ = max(members, key=lambda m: len(m[0]))  # 代表取最长文本
        covered = len({m[2] for m in members})                        # 不同竞品 URL 数
        canonicals.append(_Canonical(text=rep_text, type=rep_type, covered_by=covered))
    return canonicals


async def classify(
    target: SinglePageProfile, canonicals: list[_Canonical], deduper: SemanticDeduper
) -> _Breakdown:
    b = _Breakdown()
    target_texts = [c.text for c in target.claims]
    canonical_texts = [c.text for c in canonicals]

    matches = await deduper.match_many(target_texts, canonical_texts)
    matched_canonical: set[int] = set()
    for claim, m in zip(target.claims, matches):
        if m.is_shared and m.best_idx is not None:
            b.shared.append(claim.text)
            matched_canonical.add(m.best_idx)
        else:
            b.novel.append(NovelPoint(text=claim.text, type=claim.type))

    for i, c in enumerate(canonicals):
        if i not in matched_canonical:
            b.missing.append(MissingPoint(text=c.text, covered_by_n_competitors=c.covered_by))
    return b


def _score(b: _Breakdown) -> int:
    novel_weighted = sum(TYPE_WEIGHT.get(p.type, 0.3) for p in b.novel)
    missing_weighted = sum(min(p.covered_by_n_competitors, 3) / 3.0 for p in b.missing)
    pos = NOVEL_WEIGHT * min(novel_weighted / NOVEL_FULL_SCORE_AT, 1.0)
    neg = MISSING_WEIGHT * min(missing_weighted / MISSING_FULL_PENALTY_AT, 1.0)
    raw = 40 + pos - neg
    return max(0, min(100, round(raw)))


async def compute_information_gain(
    target: SinglePageProfile,
    competitor_profiles: list[SinglePageProfile],
    deduper: SemanticDeduper | None = None,
) -> InformationGain:
    deduper = deduper or SemanticDeduper()
    canonicals = await build_canonicals(competitor_profiles, deduper)
    b = await classify(target, canonicals, deduper)

    total_target = len(target.claims) or 1
    redundancy = len(b.shared) / total_target

    return InformationGain(
        novel_points=b.novel,
        missing_points=sorted(b.missing, key=lambda m: -m.covered_by_n_competitors),
        redundancy_ratio=round(redundancy, 3),
        gain_score=_score(b),
    )
