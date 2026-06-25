"""EEAT 评分（规格 §4）。四个子维度各自数据来源不同：

  Experience       — LLM：firsthand_markers 数量与质量
  Expertise        — LLM：author_credentials + 内容技术深度（由 claims 近似）
  Authoritativeness— DataForSEO backlinks：引荐域名/外链/域名 rank，与前 10 对比（不让 LLM 猜）
  Trustworthiness  — 抽取+LLM：引用来源数、更新新鲜度、about/contact、red_flags

每个子维度输出 分数 + 证据/缺失项。Authoritativeness 用真实站外信号对比。
"""

from __future__ import annotations

from datetime import date, datetime

from ..models import (
    Authoritativeness,
    BacklinkSummary,
    Eeat,
    SinglePageProfile,
    SubScore,
)


def _clamp(x: float) -> int:
    return max(0, min(100, round(x)))


# ------------------------------------------------------------- Experience
def score_experience(target: SinglePageProfile) -> SubScore:
    markers = target.eeat_signals.firsthand_markers
    firsthand_claims = [c for c in target.claims if c.is_firsthand]
    score = _clamp(min(len(markers), 4) * 18 + min(len(firsthand_claims), 3) * 9)
    evidence = list(markers) + [f"第一手 claim: {c.text}" for c in firsthand_claims]
    missing = [] if markers or firsthand_claims else ["无任何第一手经验证据（实测/原创数据/原创图）"]
    return SubScore(score=score, evidence=evidence[:8], missing=missing)


# -------------------------------------------------------------- Expertise
def score_expertise(target: SinglePageProfile) -> SubScore:
    creds = target.eeat_signals.author_credentials
    depth = sum(1 for c in target.claims if c.type in ("fact", "data_point", "definition"))
    score = _clamp((40 if creds else 0) + min(depth, 6) * 10)
    evidence, missing = [], []
    if creds:
        evidence.append(f"作者资质: {creds}")
    else:
        missing.append("未标注作者资质")
    evidence.append(f"技术性 claim 数: {depth}")
    if depth < 3:
        missing.append("内容技术深度不足（事实/数据/定义类 claim 偏少）")
    return SubScore(score=score, evidence=evidence, missing=missing)


# ------------------------------------------------------- Authoritativeness
def score_authoritativeness(
    target_bl: BacklinkSummary, competitor_bls: list[BacklinkSummary]
) -> Authoritativeness:
    avail_comp = [b for b in competitor_bls if b.available]
    if not target_bl.available or not avail_comp:
        # 外链数据不可用（如 DataForSEO 账号未验证）：不给误导性分数
        return Authoritativeness(
            score=0, available=False,
            referring_domains=target_bl.referring_domains,
            evidence=["外链数据不可用（DataForSEO 未授权/失败）；权威性无法评估"],
        )
    avg_rd = sum(b.referring_domains for b in avail_comp) / len(avail_comp)
    # 相对前 10 的引荐域名比值映射到 0-100，封顶 1.0。
    ratio = target_bl.referring_domains / avg_rd if avg_rd else (1.0 if target_bl.referring_domains else 0.0)
    score = _clamp(min(ratio, 1.0) * 100)
    evidence = [
        f"引荐域名 {target_bl.referring_domains} vs 前10均值 {round(avg_rd,1)}",
        f"外链 {target_bl.backlinks}",
    ]
    if target_bl.domain_rank is not None:
        evidence.append(f"域名 rank {target_bl.domain_rank}")
    return Authoritativeness(
        score=score,
        referring_domains=target_bl.referring_domains,
        top10_avg_referring_domains=round(avg_rd, 1),
        backlinks=target_bl.backlinks,
        domain_rank=target_bl.domain_rank,
        evidence=evidence,
    )


# ------------------------------------------------------------------- Trust
def _freshness_points(signals) -> tuple[int, str]:
    raw = signals.updated_date or signals.published_date
    if not raw:
        return 0, "无发布/更新日期"
    try:
        d = datetime.fromisoformat(raw).date()
    except ValueError:
        return 0, f"日期无法解析: {raw}"
    age_days = (date.today() - d).days
    if age_days <= 365:
        return 25, f"较新（{age_days} 天内）"
    if age_days <= 730:
        return 12, f"略旧（{age_days} 天）"
    return 0, f"陈旧（{age_days} 天）"


def score_trust(target: SinglePageProfile) -> SubScore:
    sig = target.eeat_signals
    cited = sum(1 for c in target.claims if c.has_source)
    citation_pts = min(sig.outbound_citations, 6) * 5 + min(cited, 4) * 5
    fresh_pts, fresh_note = _freshness_points(sig)
    about_pts = 15 if sig.has_about_or_contact else 0
    penalty = len(sig.red_flags) * 15

    score = _clamp(citation_pts + fresh_pts + about_pts - penalty)
    evidence = [f"外部引用 {sig.outbound_citations}", f"有来源的 claim {cited}", fresh_note]
    if sig.has_about_or_contact:
        evidence.append("有 about/contact")
    missing = []
    if not sig.has_about_or_contact:
        missing.append("缺 about/contact 页")
    if sig.outbound_citations == 0:
        missing.append("无外部引用来源")
    if sig.red_flags:
        missing.append(f"red flags: {', '.join(sig.red_flags)}")
    return SubScore(score=score, evidence=evidence, missing=missing)


def compute_eeat(
    target: SinglePageProfile,
    target_bl: BacklinkSummary,
    competitor_bls: list[BacklinkSummary],
) -> Eeat:
    return Eeat(
        experience=score_experience(target),
        expertise=score_expertise(target),
        authoritativeness=score_authoritativeness(target_bl, competitor_bls),
        trust=score_trust(target),
    )
