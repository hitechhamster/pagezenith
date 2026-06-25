"""混合去重验收（规格 §2.2）。用 mock embedding + mock 裁决跑通，无需 key。

验收点：
  1. 同义不同表述 → 判同一条（合簇）。
  2. 数字/方向相反 → 判不同，且确实进了 LLM 裁决（不是被 embedding 直接合掉）。
  3. 同一输入跑两次结果完全一致（缓存生效）。
  4. 中文、英文各一组都正常（多语言）。
  5. info gain 的 novel/shared/missing 随去重合理变化。
"""

import asyncio

from tools.seo_gap.clients.llm import LLMClient
from tools.seo_gap.models import Claim, SinglePageProfile
from tools.seo_gap.scoring import semantic
from tools.seo_gap.scoring.information_gain import build_canonicals, compute_information_gain
from tools.seo_gap.scoring.semantic import GuardStats, SemanticDeduper, _JUDGE_CACHE


def _deduper(guard: bool) -> SemanticDeduper:
    d = SemanticDeduper(LLMClient())
    d.guard = guard
    d.stats = GuardStats(guard_enabled=guard)
    return d


def _prof(url, claims):
    return SinglePageProfile(
        url=url, claims=[Claim(text=t, type=ty) for t, ty in claims]
    )


# 说明：mock embedding 是词法近似，无法识别“regulated≈supervised”这类纯语义同义
# （那是真实 multilingual embedding 的职责，见 test_real_embeddings 旁注）。
# mock 下用高重叠改述来验证“同一事实不同表述 → 合一条”的链路是否打通。
def test_paraphrase_merge_english():
    async def run():
        semantic._reset_caches()
        comps = [
            _prof("https://a.com", [("The broker is regulated by the FCA", "fact")]),
            _prof("https://b.com", [("The broker is regulated by the UK FCA", "fact")]),
        ]
        cans = await build_canonicals(comps, SemanticDeduper(LLMClient()))
        # 两条同义 → 合成一条 canonical，覆盖 2 个竞品
        assert len(cans) == 1, [c.text for c in cans]
        assert cans[0].covered_by == 2
    asyncio.run(run())


def test_number_and_direction_go_to_llm_and_split():
    async def run():
        semantic._reset_caches()
        comps = [
            _prof("https://a.com", [("Withdrawal takes 2 days", "data_point")]),
            _prof("https://b.com", [("Withdrawal takes 5 days", "data_point")]),
            _prof("https://c.com", [("The price rose 10%", "data_point")]),
            _prof("https://d.com", [("The price fell 10%", "data_point")]),
        ]
        before = dict(_JUDGE_CACHE)
        cans = await build_canonicals(comps, SemanticDeduper(LLMClient()))
        # 数字/方向相反 → 不合并，4 条各自成簇
        assert len(cans) == 4, [c.text for c in cans]
        # 这些模糊对确实进了 LLM 裁决缓存（而非被 embedding 直接合掉）
        assert len(_JUDGE_CACHE) > len(before), "number/direction 对未进入 LLM 裁决"
    asyncio.run(run())


def test_chinese_paraphrase_merge():
    async def run():
        semantic._reset_caches()
        comps = [
            _prof("https://a.cn", [("该券商受英国 FCA 监管", "fact")]),
            _prof("https://b.cn", [("该券商受英国 FCA 严格监管", "fact")]),
        ]
        cans = await build_canonicals(comps, SemanticDeduper(LLMClient()))
        assert len(cans) == 1, [c.text for c in cans]
    asyncio.run(run())


def test_chinese_direction_opposite_reaches_llm_and_splits():
    async def run():
        semantic._reset_caches()
        from tools.seo_gap.scoring.semantic import _JUDGE_CACHE
        comps = [
            _prof("https://a.cn", [("黄金价格上涨 10%", "data_point")]),
            _prof("https://b.cn", [("黄金价格下跌 10%", "data_point")]),
        ]
        before = dict(_JUDGE_CACHE)
        cans = await build_canonicals(comps, SemanticDeduper(LLMClient()))
        assert len(cans) == 2, [c.text for c in cans]          # 方向相反 → 不合并
        assert len(_JUDGE_CACHE) > len(before), "方向相反对未进入 LLM 裁决"
    asyncio.run(run())


def test_chinese_direction_opposite_split():
    async def run():
        semantic._reset_caches()
        comps = [
            _prof("https://a.cn", [("出金需要 2 天", "data_point")]),
            _prof("https://b.cn", [("出金需要 5 天", "data_point")]),
        ]
        cans = await build_canonicals(comps, SemanticDeduper(LLMClient()))
        assert len(cans) == 2, [c.text for c in cans]
    asyncio.run(run())


def test_reproducible_across_two_runs():
    async def run():
        comps = [
            _prof("https://a.com", [("Regulated by the FCA", "fact"),
                                    ("Leverage capped at 30:1", "data_point")]),
            _prof("https://b.com", [("Overseen by the FCA in the UK", "fact"),
                                    ("EU leverage limit is 30:1", "data_point")]),
        ]
        target = _prof("https://me.com", [
            ("We tested 12 brokers ourselves", "fact"),     # novel
            ("Leverage capped at 30:1", "data_point"),       # shared
        ])
        semantic._reset_caches()
        g1 = await compute_information_gain(target, comps, SemanticDeduper(LLMClient()))
        # 不清缓存，再跑一次（应命中向量 + 裁决缓存）
        g2 = await compute_information_gain(target, comps, SemanticDeduper(LLMClient()))
        assert g1.model_dump() == g2.model_dump()
        return g1
    g = asyncio.run(run())
    # novel/shared/missing 合理：1 条 novel，1 条 shared，余下竞品事实 missing
    assert any("tested 12" in p.text for p in g.novel_points)
    assert 0.0 < g.redundancy_ratio <= 1.0
    assert any(p.covered_by_n_competitors >= 1 for p in g.missing_points)


def test_conflict_guard_ab_toggle():
    """A/B：同一“出金 2 天 vs 5 天”输入，guard 开/关行为对比。"""
    async def run():
        comps = [
            _prof("https://a.com", [("Withdrawal takes 2 days", "data_point")]),
            _prof("https://b.com", [("Withdrawal takes 5 days", "data_point")]),
        ]
        # guard ON：高 cosine 被降级 → LLM 判 different → 不合并（2 簇）
        semantic._reset_caches()
        d_on = _deduper(True)
        cans_on = await build_canonicals(comps, d_on)
        judged_on = len(_JUDGE_CACHE)
        # guard OFF：严格按 cosine，≥HIGH 直接合 → 合并（1 簇），不调 LLM
        semantic._reset_caches()
        d_off = _deduper(False)
        cans_off = await build_canonicals(comps, d_off)
        judged_off = len(_JUDGE_CACHE)
        return d_on.stats, d_off.stats, len(cans_on), len(cans_off), judged_on, judged_off

    s_on, s_off, n_on, n_off, j_on, j_off = asyncio.run(run())

    # guard ON 把这对拆开并经过 LLM 裁决
    assert n_on == 2 and s_on.demoted_pairs >= 1 and s_on.demoted_judged_different >= 1
    # guard OFF 直接合并、零降级、不调 LLM
    assert n_off == 1 and s_off.demoted_pairs == 0
    # 关键不变量：guard 只增加 LLM 裁决量，绝不减少
    assert j_on >= j_off


def test_guard_never_reduces_judgements():
    """guard ON 的裁决集合应是 guard OFF 的超集（只增不减）。"""
    async def run():
        comps = [
            _prof("https://a.com", [("Regulated by the FCA", "fact"),
                                    ("Withdrawal takes 2 days", "data_point")]),
            _prof("https://b.com", [("Regulated by the FCA in the UK", "fact"),
                                    ("Withdrawal takes 5 days", "data_point")]),
        ]
        semantic._reset_caches()
        await build_canonicals(comps, _deduper(False))
        off_keys = set(_JUDGE_CACHE)
        semantic._reset_caches()
        await build_canonicals(comps, _deduper(True))
        on_keys = set(_JUDGE_CACHE)
        return off_keys, on_keys
    off_keys, on_keys = asyncio.run(run())
    assert off_keys <= on_keys  # 超集关系


if __name__ == "__main__":
    test_paraphrase_merge_english()
    test_number_and_direction_go_to_llm_and_split()
    test_chinese_paraphrase_merge()
    test_chinese_direction_opposite_reaches_llm_and_splits()
    test_chinese_direction_opposite_split()
    test_reproducible_across_two_runs()
    test_conflict_guard_ab_toggle()
    test_guard_never_reduces_judgements()
    print("ALL PASSED")
