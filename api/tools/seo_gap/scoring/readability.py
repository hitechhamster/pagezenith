"""可读性评分（规格 §3）。确定性公式（textstat）+ 与前 10 均值对比。

重点：判断"是否匹配该关键词受众"，而非越简单越好。
verdict 相对前 10：目标页比前 10 明显更易读=easier，更难=harder，接近=aligned。
"""

from __future__ import annotations

import re

import textstat

from ..models import PageContent, Readability, ReadabilityMetrics

_SENT = re.compile(r"[.!?。！？]+")
_PASSIVE = re.compile(r"\b(is|are|was|were|be|been|being)\b\s+\w+ed\b", re.I)


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{2,}|\n", text) if p.strip()]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if s.strip()]


def compute_metrics(page: PageContent) -> ReadabilityMetrics:
    text = page.text or ""
    if not text.strip():
        return ReadabilityMetrics()

    sentences = _sentences(text)
    paragraphs = _paragraphs(text)
    words = text.split()
    n_sent = len(sentences) or 1
    n_para = len(paragraphs) or 1

    sent_lens = [len(s.split()) for s in sentences]
    avg_sent = sum(sent_lens) / n_sent
    long_sent_ratio = sum(1 for l in sent_lens if l > 30) / n_sent
    long_para_ratio = sum(1 for p in paragraphs if len(p.split()) > 150) / n_para
    passive_ratio = len(_PASSIVE.findall(text)) / n_sent

    # 标题/列表密度：mock 正文无结构标签时近似为 0；真实抓取可从 raw_html 增强。
    heading_density = 0.0
    list_ratio = 0.0
    if page.raw_html:
        html = page.raw_html.lower()
        n_headings = sum(html.count(f"<h{i}") for i in range(1, 4))
        n_li = html.count("<li")
        heading_density = round(n_headings / max(len(words) / 100, 1), 3)
        list_ratio = round(n_li / n_sent, 3)

    return ReadabilityMetrics(
        flesch_reading_ease=round(textstat.flesch_reading_ease(text), 1),
        fk_grade=round(textstat.flesch_kincaid_grade(text), 1),
        avg_sentence_len=round(avg_sent, 1),
        long_para_ratio=round(long_para_ratio, 3),
        long_sentence_ratio=round(long_sent_ratio, 3),
        passive_ratio=round(passive_ratio, 3),
        heading_density=heading_density,
        list_ratio=list_ratio,
    )


def _average(metrics: list[ReadabilityMetrics]) -> ReadabilityMetrics:
    if not metrics:
        return ReadabilityMetrics()
    n = len(metrics)
    fields = ReadabilityMetrics.model_fields
    agg = {f: round(sum(getattr(m, f) for m in metrics) / n, 3) for f in fields}
    return ReadabilityMetrics(**agg)


def _verdict(target: ReadabilityMetrics, top10: ReadabilityMetrics) -> str:
    # 以 Flesch Reading Ease 为主：越高越易读。差 > 10 分视为明显偏离。
    diff = target.flesch_reading_ease - top10.flesch_reading_ease
    if diff > 10:
        return "easier"
    if diff < -10:
        return "harder"
    return "aligned"


def compute_readability(
    target_page: PageContent,
    competitor_pages: list[PageContent],
    qualitative_notes: list[str] | None = None,
) -> Readability:
    target_metrics = compute_metrics(target_page)
    comp_metrics = [compute_metrics(p) for p in competitor_pages]
    top10 = _average(comp_metrics)
    return Readability(
        metrics=target_metrics,
        top10_avg=top10,
        verdict=_verdict(target_metrics, top10),
        qualitative=qualitative_notes or [],
    )
