"""报告汇总 + priority_actions 生成（规格 §5、§6）。

priority_actions 排序逻辑（由代码从三维差距生成，按 impact）：
  缺失基本盘 > 权威性差距 > 可读性不匹配 > 锦上添花的独特性
这是用户真正要看的部分，比一堆分数更可执行。
"""

from __future__ import annotations

from .models import (
    AnalysisReport,
    Dimension,
    Eeat,
    InformationGain,
    PriorityAction,
    Readability,
)

LIMITATIONS = [
    "信息增益/EEAT 为启发式信号代理，非 Google 真实评分（Google 实现不公开）。",
    "EEAT 不是 Google 的直接排名因子，这里评估的是 EEAT 信号代理。",
    "用户停留时间/参与度无法取得准确值，本工具不纳入。",
    "LLM 抽取有噪声，已用证据抽取+代码评分+语义去重压低方差；建议对同页跑两次抽取做一致性抽查。",
]


def build_priority_actions(
    gain: InformationGain, readability: Readability, eeat: Eeat
) -> list[PriorityAction]:
    actions: list[PriorityAction] = []

    # 1) 缺失基本盘（被多个竞品共同覆盖却漏掉）—— 最高优先
    #    覆盖强度分级：≥3 家=high（强共识基本盘），==2 家=medium，
    #    ==1 家=low（单一竞品的特有内容，多为品牌/促销噪声，弱信号）。
    #    单竞品项最多带 2 条，避免淹没共识缺口。
    consensus = [m for m in gain.missing_points if m.covered_by_n_competitors >= 2][:5]
    singles = [m for m in gain.missing_points if m.covered_by_n_competitors == 1][:2]
    for mp in consensus + singles:
        n = mp.covered_by_n_competitors
        impact = "high" if n >= 3 else ("medium" if n == 2 else "low")
        actions.append(PriorityAction(
            action=f"补充子话题/事实：{mp.text}（{n} 个竞品已覆盖、你缺）",
            dimension=Dimension.information_gain,
            impact=impact,
        ))

    # 2) 权威性差距（外链数据不可用时跳过，不给误导性建议）
    auth = eeat.authoritativeness
    if auth.available and auth.top10_avg_referring_domains and \
            auth.referring_domains < 0.5 * auth.top10_avg_referring_domains:
        actions.append(PriorityAction(
            action=(f"提升站外权威：引荐域名 {auth.referring_domains} 远低于前10均值 "
                    f"{auth.top10_avg_referring_domains}，需外链建设"),
            dimension=Dimension.eeat,
            impact="high",
        ))

    # 2b) 其它 EEAT 缺失项（Experience/Expertise/Trust）
    for sub, label in ((eeat.trust, "可信"), (eeat.experience, "经验"), (eeat.expertise, "专业")):
        if sub.score < 50 and sub.missing:
            actions.append(PriorityAction(
                action=f"补强 {label}：{sub.missing[0]}",
                dimension=Dimension.eeat,
                impact="medium",
            ))

    # 3) 可读性不匹配（n/a=单页模式无前10可比，跳过）
    if readability.verdict in ("easier", "harder"):
        direction = "偏难，需简化" if readability.verdict == "harder" else "偏浅，可加深匹配受众"
        actions.append(PriorityAction(
            action=f"可读性相对前10{direction}（Flesch {readability.metrics.flesch_reading_ease} vs "
                   f"{readability.top10_avg.flesch_reading_ease}）",
            dimension=Dimension.readability,
            impact="medium" if readability.verdict == "harder" else "low",
        ))

    # 4) 锦上添花：突出独特性
    if gain.novel_points:
        actions.append(PriorityAction(
            action=f"强化已有独特内容（{len(gain.novel_points)} 条竞品没有的 claim），在标题/摘要中前置",
            dimension=Dimension.information_gain,
            impact="low",
        ))

    rank = {"high": 0, "medium": 1, "low": 2}
    return sorted(actions, key=lambda a: rank[a.impact])


def build_report(
    keyword: str,
    target_url: str,
    in_top_10: bool,
    rank: int | None,
    gain: InformationGain,
    readability: Readability,
    eeat: Eeat,
) -> AnalysisReport:
    return AnalysisReport(
        keyword=keyword,
        target_url=target_url,
        in_top_10=in_top_10,
        rank=rank,
        information_gain=gain,
        readability=readability,
        eeat=eeat,
        priority_actions=build_priority_actions(gain, readability, eeat),
        limitations=LIMITATIONS,
    )
