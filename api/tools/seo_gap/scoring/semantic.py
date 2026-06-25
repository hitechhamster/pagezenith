"""混合语义去重（规格 §2.2）：embedding 粗判 + LLM 裁边界。

判定带（config 常量）：
    cosine ≥ HIGH        → 直接“同一条”，不调 LLM
    cosine <  LOW        → 直接“不同”，不调 LLM
    [LOW, HIGH)          → 模糊对，批量交 LLM 裁决

两个能力：
  - cluster(items):  竞品 claims 并集去重。两两 cosine + 并查集归簇；模糊对一次性交 LLM。
                     每个簇 = 一条 canonical claim。
  - match_one(...):  目标 claim 对并集“最佳单一匹配”（不做并查集链式合并，避免误传递）。

可复现性：
  - 向量按 claim 文本 hash 缓存（竞品向量命中缓存不重复 embed）。
  - LLM 裁决按“排序后两条文本”的 hash 缓存：同一对永远复用同一答案，
    只有全新的模糊对才真正调 LLM。
缓存为模块级单例，跨请求保留 → 同一输入跑两次结果完全一致。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from ..clients.llm import LLMClient
from ..config import get_settings
from ..lexical import surface_conflict

# 模块级缓存（跨 analysis run 复用，保证可复现） ---------------------------- #
_VEC_CACHE: dict[str, list[float]] = {}      # text-hash -> embedding
_JUDGE_CACHE: dict[str, bool] = {}           # sorted-pair-hash -> same?


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pair_key(a: str, b: str) -> str:
    lo, hi = sorted((a, b))
    return hashlib.sha256(f"{lo}\x00{hi}".encode("utf-8")).hexdigest()


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1e-9
    return float(a @ b / denom)


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def labels(self) -> list[int]:
        return [self.find(i) for i in range(len(self.parent))]


@dataclass
class MatchResult:
    """目标 claim 对并集的最佳单一匹配结果。"""
    best_idx: Optional[int]      # 命中的 canonical 索引；novel 时为 None
    cosine: float
    is_shared: bool              # True=shared，False=novel


@dataclass
class GuardStats:
    """冲突保护统计：评估 embedding 在数字/否定/方向上的盲区严重程度。"""
    guard_enabled: bool = True
    high_cosine_pairs: int = 0          # cosine ≥ HIGH 的对总数
    demoted_pairs: int = 0              # 其中被 surface_conflict 降级到 LLM 的
    demoted_judged_different: int = 0   # 降级对中 LLM 最终判 different 的

    @property
    def demoted_different_ratio(self) -> float:
        return round(self.demoted_judged_different / self.demoted_pairs, 3) if self.demoted_pairs else 0.0

    def as_dict(self) -> dict:
        return {
            "guard_enabled": self.guard_enabled,
            "high_cosine_pairs": self.high_cosine_pairs,
            "demoted_pairs": self.demoted_pairs,
            "demoted_judged_different": self.demoted_judged_different,
            "demoted_different_ratio": self.demoted_different_ratio,
        }


class SemanticDeduper:
    """混合去重器。单次分析内构造一个实例，缓存为模块级，跨实例共享。"""

    def __init__(self, llm: LLMClient | None = None):
        self.llm = llm or LLMClient()
        s = get_settings()
        self.high = s.dedup_cosine_high
        self.low = s.dedup_cosine_low
        self.batch = s.dedup_judge_batch
        self.guard = s.dedup_conflict_guard
        self.stats = GuardStats(guard_enabled=self.guard)

    # ----------------------------------------------------------- embedding
    async def embed(self, texts: list[str]) -> dict[str, np.ndarray]:
        """返回 text -> 向量。命中缓存的不重复 embed。"""
        missing = [t for t in dict.fromkeys(texts) if _h(t) not in _VEC_CACHE]
        if missing:
            vectors = await self.llm.embed(missing)
            for t, v in zip(missing, vectors):
                _VEC_CACHE[_h(t)] = v
        return {t: np.array(_VEC_CACHE[_h(t)], dtype=float) for t in set(texts)}

    # ------------------------------------------------------ LLM 裁边界(批)
    async def _judge(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], bool]:
        """对模糊对批量裁决，结果写入并复用缓存。返回 pair->same。"""
        todo, out = [], {}
        for a, b in pairs:
            key = _pair_key(a, b)
            if key in _JUDGE_CACHE:
                out[(a, b)] = _JUDGE_CACHE[key]
            else:
                todo.append((a, b))
        for i in range(0, len(todo), self.batch):
            chunk = todo[i : i + self.batch]
            decisions = await self.llm.judge_pairs(chunk)
            for (a, b), same in zip(chunk, decisions):
                _JUDGE_CACHE[_pair_key(a, b)] = same
                out[(a, b)] = same
        return out

    def _classify(self, cos: float, text_a: str, text_b: str) -> tuple[str, bool]:
        """按判定带分类，返回 (relation, demoted)。
        relation ∈ same | different | fuzzy；demoted=True 表示该对本可“直接合”，
        被冲突保护降级到 LLM 裁决。

        冲突保护只把对“往更严格方向”降级（same → fuzzy），绝不把对升级成 same
        而跳过本该有的裁决：因此它只增加 LLM 裁决量，不减少。"""
        if cos >= self.high:
            self.stats.high_cosine_pairs += 1
            if self.guard and surface_conflict(text_a, text_b):
                self.stats.demoted_pairs += 1
                return "fuzzy", True
            return "same", False
        if cos < self.low:
            return "different", False
        return "fuzzy", False

    # ----------------------------------------------- 竞品并集去重（聚簇）
    async def cluster(self, texts: list[str]) -> list[int]:
        """两两 cosine + 并查集归簇，模糊对一次性交 LLM。返回每个 index 的簇 label。"""
        n = len(texts)
        if n == 0:
            return []
        vmap = await self.embed(texts)
        vecs = [vmap[t] for t in texts]

        uf = _UnionFind(n)
        fuzzy_pairs: list[tuple[int, int]] = []
        demoted: set[tuple[int, int]] = set()
        for i in range(n):
            for j in range(i + 1, n):
                rel, was_demoted = self._classify(_cosine(vecs[i], vecs[j]), texts[i], texts[j])
                if rel == "same":
                    uf.union(i, j)
                elif rel == "fuzzy":
                    fuzzy_pairs.append((i, j))
                    if was_demoted:
                        demoted.add((i, j))

        if fuzzy_pairs:
            verdicts = await self._judge([(texts[i], texts[j]) for i, j in fuzzy_pairs])
            for i, j in fuzzy_pairs:
                same = verdicts.get((texts[i], texts[j]), False)
                if same:
                    uf.union(i, j)
                elif (i, j) in demoted:
                    self.stats.demoted_judged_different += 1
        return uf.labels()

    # ------------------------------------------- 目标 claims 对并集最佳匹配
    async def match_many(
        self, target_texts: list[str], canonical_texts: list[str]
    ) -> list[MatchResult]:
        """每条目标 claim 在 canonical 里找 cosine 最高的一条（最佳单一匹配，不做链式合并）。
        所有落在模糊带的“目标↔最佳 canonical”对一次性批量交 LLM 裁决。"""
        if not target_texts:
            return []
        if not canonical_texts:
            return [MatchResult(None, 0.0, False) for _ in target_texts]

        vmap = await self.embed([*target_texts, *canonical_texts])
        cvecs = [vmap[c] for c in canonical_texts]
        results: list[Optional[MatchResult]] = [None] * len(target_texts)
        fuzzy: list[tuple[int, int]] = []  # (target_idx, best_canonical_idx)
        demoted: set[int] = set()           # target_idx 被降级的

        for ti, t in enumerate(target_texts):
            cosims = [_cosine(vmap[t], cv) for cv in cvecs]
            bi = int(np.argmax(cosims))
            bc = cosims[bi]
            rel, was_demoted = self._classify(bc, t, canonical_texts[bi])
            if rel == "same":
                results[ti] = MatchResult(bi, bc, True)
            elif rel == "different":
                results[ti] = MatchResult(None, bc, False)
            else:
                fuzzy.append((ti, bi))
                results[ti] = MatchResult(bi, bc, False)  # 占位，待裁决
                if was_demoted:
                    demoted.add(ti)

        if fuzzy:
            verdicts = await self._judge(
                [(target_texts[ti], canonical_texts[bi]) for ti, bi in fuzzy]
            )
            for ti, bi in fuzzy:
                same = verdicts.get((target_texts[ti], canonical_texts[bi]), False)
                results[ti] = MatchResult(bi if same else None, results[ti].cosine, same)
                if not same and ti in demoted:
                    self.stats.demoted_judged_different += 1
        return results  # type: ignore[return-value]


# 测试/调试用：清空缓存（不影响生产路径）
def _reset_caches() -> None:
    _VEC_CACHE.clear()
    _JUDGE_CACHE.clear()
