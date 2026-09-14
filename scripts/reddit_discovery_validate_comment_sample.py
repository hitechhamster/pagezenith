"""Validate demand-language evidence in the stratified comment sample."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from reddit_discovery_prepare_clusters import NEED_RULES, classify_sector, contains_phrase


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def labels_in_text(text: str) -> list[str]:
    lowered = text.lower()
    labels = []
    for label, rule in NEED_RULES.items():
        if any(contains_phrase(lowered, phrase) for phrase in rule["phrases"]):
            labels.append(label)
    return labels


def matched_quote(comments: list[dict], expected: set[str]) -> tuple[str, str]:
    for comment in sorted(comments, key=lambda row: int(row.get("score") or 0), reverse=True):
        body = re.sub(r"\s+", " ", str(comment.get("body") or "")).strip()
        labels = set(labels_in_text(body))
        overlap = labels.intersection(expected) if expected else labels
        if overlap:
            return body[:500], "|".join(sorted(overlap))
    return "", ""


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    cards = {row["post_id"]: row for row in load_jsonl(run_dir / "demand_cards_preliminary.jsonl")}
    queue_rows = load_csv(run_dir / "comment_fetch_queue.csv")
    queue = {
        row["post_id"]: row for row in queue_rows if row.get("tier") in {"A", "B", "C"}
    }
    samples = [
        row for row in load_jsonl(run_dir / "comment_sample.jsonl") if row.get("post_id") in queue
    ]
    results = []
    for sample in samples:
        card = cards[sample["post_id"]]
        current_queue = queue[sample["post_id"]]
        comments = sample.get("comments") or []
        comments_text = "\n".join(str(comment.get("body") or "") for comment in comments)
        comment_labels = set(labels_in_text(comments_text))
        snippet_labels = set(
            filter(None, str(current_queue.get("snippet_need_labels") or "").split("|"))
        )
        post_labels = set(card.get("need_labels") or [])
        expected = snippet_labels or post_labels
        quote, quote_labels = matched_quote(comments, expected)
        direct_post = bool(card.get("direct_need_language"))
        comment_confirmation = bool(comment_labels.intersection(expected))
        evidence_validated = direct_post or comment_confirmation

        enriched_sector = card.get("sector", "待归类")
        sector_confidence = float(card.get("sector_confidence") or 0)
        if enriched_sector == "待归类" and comments:
            pseudo = {
                "title": card.get("title", ""),
                "selftext": comments_text[:12000],
                "subreddit": card.get("subreddit", ""),
            }
            candidate_sector, _, candidate_confidence = classify_sector(pseudo)
            if candidate_sector != "待归类":
                enriched_sector = candidate_sector
                sector_confidence = candidate_confidence

        results.append(
            {
                "post_id": sample["post_id"],
                "tier": current_queue.get("tier", ""),
                "sector_before": card.get("sector", ""),
                "sector_after_comments": enriched_sector,
                "sector_confidence": sector_confidence,
                "subreddit": card.get("subreddit", ""),
                "title": card.get("title", ""),
                "canonical_url": card.get("canonical_url", ""),
                "comments_fetched": len(comments),
                "direct_post_evidence": direct_post,
                "comment_confirmation": comment_confirmation,
                "evidence_validated": evidence_validated,
                "expected_need_labels": "|".join(sorted(expected)),
                "comment_need_labels": "|".join(sorted(comment_labels)),
                "matched_quote_labels": quote_labels,
                "matched_comment_excerpt": quote,
            }
        )

    fields = list(results[0].keys())
    with (run_dir / "comment_sample_validation.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    tier_summary = {}
    for tier in ["A", "B", "C"]:
        rows = [row for row in results if row["tier"] == tier]
        tier_summary[tier] = {
            "posts": len(rows),
            "with_comments": sum(row["comments_fetched"] > 0 for row in rows),
            "comment_confirmed": sum(row["comment_confirmation"] for row in rows),
            "evidence_validated": sum(row["evidence_validated"] for row in rows),
            "comment_confirmation_rate": round(
                sum(row["comment_confirmation"] for row in rows) / max(1, len(rows)), 4
            ),
            "validated_rate": round(sum(row["evidence_validated"] for row in rows) / max(1, len(rows)), 4),
        }
    reassigned = Counter(
        row["sector_after_comments"]
        for row in results
        if row["sector_before"] == "待归类" and row["sector_after_comments"] != "待归类"
    )
    metrics = {
        "sample_posts": len(results),
        "tiers": tier_summary,
        "posts_with_comments": sum(row["comments_fetched"] > 0 for row in results),
        "total_comments_fetched": sum(row["comments_fetched"] for row in results),
        "validated_posts": sum(row["evidence_validated"] for row in results),
        "unclassified_reassigned_from_comments": dict(reassigned.most_common()),
        "still_unclassified_in_tier_c": sum(
            row["tier"] == "C" and row["sector_after_comments"] == "待归类" for row in results
        ),
    }
    (run_dir / "comment_sample_validation_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# Reddit 评论样本核验",
        "",
        "> 这是检索与归类方法的核验，不是市场机会报告。命中需求措辞只说明帖子相关，",
        "> 不等于需求可盈利、可由中国供应链解决，也不代表 Reddit 全站发生率。",
        "",
        f"- 样本帖子：{len(results)}",
        f"- 抓到评论的帖子：{metrics['posts_with_comments']}",
        f"- 评论总数：{metrics['total_comments_fetched']:,}",
        f"- 至少有正文或评论证据：{metrics['validated_posts']}",
        "",
        "## 分档核验",
        "",
        "| 档位 | 样本 | 有评论 | 评论复现预期需求 | 正文或评论证据成立 |",
        "|---|---:|---:|---:|---:|",
    ]
    for tier, summary in tier_summary.items():
        lines.append(
            f"| {tier} | {summary['posts']} | {summary['with_comments']} | "
            f"{summary['comment_confirmed']} ({summary['comment_confirmation_rate']:.1%}) | "
            f"{summary['evidence_validated']} ({summary['validated_rate']:.1%}) |"
        )
    lines.extend(["", "## 已核验的代表样本", ""])
    validated_by_sector: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        if row["evidence_validated"] and row["sector_after_comments"] != "待归类":
            validated_by_sector[row["sector_after_comments"]].append(row)
    for sector, rows in sorted(validated_by_sector.items(), key=lambda item: (-len(item[1]), item[0])):
        lines.append(f"### {sector} · 样本中 {len(rows)} 帖")
        lines.append("")
        for row in rows[:4]:
            lines.append(f"- [{row['title']}]({row['canonical_url']}) — r/{row['subreddit']}")
            if row["matched_comment_excerpt"]:
                lines.append(f"  - 评论证据：{row['matched_comment_excerpt']}")
        lines.append("")
    (run_dir / "comment_sample_validation.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
