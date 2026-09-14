"""Build the preliminary sector x shared-need matrix from validated evidence only."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from reddit_discovery_prepare_clusters import classify_sector
from reddit_discovery_validate_comment_sample import labels_in_text


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


def truth(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    metadata = {row["post_id"]: row for row in load_jsonl(run_dir / "post_metadata.jsonl")}
    cards = {row["post_id"]: row for row in load_jsonl(run_dir / "demand_cards_preliminary.jsonl")}
    validations = load_csv(run_dir / "comment_sample_validation.csv")
    validated_cards = []
    for validation in validations:
        if not truth(validation.get("evidence_validated")):
            continue
        post_id = validation["post_id"]
        meta = metadata[post_id]
        base = cards[post_id]
        post_text = f"{meta.get('title', '')}\n{meta.get('selftext', '')}"
        direct_labels = set(labels_in_text(post_text))
        comment_labels = set(filter(None, validation.get("matched_quote_labels", "").split("|")))
        if direct_labels:
            labels = sorted(direct_labels)
            sector = base.get("sector") or "待归类"
            sector_confidence = float(base.get("sector_confidence") or 0)
            source = "正文"
            evidence_excerpt = base.get("evidence_excerpt", "")
        elif comment_labels:
            labels = sorted(comment_labels)
            comment_excerpt = validation.get("matched_comment_excerpt", "")
            # A comment can discuss a different product from the thread topic.
            # Route the demand by the evidence sentence itself, not by subreddit.
            sector, _, sector_confidence = classify_sector(
                {"title": comment_excerpt, "selftext": "", "subreddit": ""}
            )
            source = "评论"
            evidence_excerpt = comment_excerpt
        else:
            continue
        if sector == "待归类" or sector_confidence < 0.72:
            sector = "待人工归类"
        validated_cards.append(
            {
                "post_id": post_id,
                "canonical_url": meta.get("canonical_url", ""),
                "subreddit": meta.get("subreddit", ""),
                "author_hash": meta.get("author_hash", ""),
                "created_iso": meta.get("created_iso", ""),
                "time_windows": meta.get("time_windows") or [],
                "score": int(meta.get("score") or 0),
                "num_comments": int(meta.get("num_comments") or 0),
                "subreddit_subscribers": int(meta.get("subreddit_subscribers") or 0),
                "sector": sector,
                "sector_confidence": sector_confidence,
                "need_labels": labels,
                "evidence_source": source,
                "title": meta.get("title", ""),
                "evidence_excerpt": evidence_excerpt,
            }
        )

    output_path = run_dir / "validated_demand_cards.jsonl"
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        for row in validated_cards:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    by_sector: dict[str, list[dict]] = defaultdict(list)
    for row in validated_cards:
        by_sector[row["sector"]].append(row)
    sector_rows = []
    for sector, rows in sorted(by_sector.items(), key=lambda item: (-len(item[1]), item[0])):
        windows = Counter(window for row in rows for window in row["time_windows"])
        needs = Counter(label for row in rows for label in row["need_labels"])
        sources = Counter(row["evidence_source"] for row in rows)
        comment_counts = [row["num_comments"] for row in rows]
        score_counts = [row["score"] for row in rows]
        sector_rows.append(
            {
                "sector": sector,
                "validated_posts": len(rows),
                "unique_authors": len({row["author_hash"] for row in rows if row["author_hash"]}),
                "unique_subreddits": len({row["subreddit"].lower() for row in rows if row["subreddit"]}),
                "current_posts": windows["current"],
                "active_posts": windows["active"],
                "history_posts": windows["history"],
                "post_evidence": sources["正文"],
                "comment_only_evidence": sources["评论"],
                "median_comments": round(statistics.median(comment_counts), 1),
                "p75_comments": round(percentile(comment_counts, 0.75), 1),
                "median_score": round(statistics.median(score_counts), 1),
                "top_needs": " | ".join(f"{label}:{count}" for label, count in needs.most_common(5)),
            }
        )
    write_csv(
        run_dir / "validated_sector_summary.csv",
        sector_rows,
        list(sector_rows[0].keys()),
    )

    all_needs = sorted({label for row in validated_cards for label in row["need_labels"]})
    matrix_rows = []
    for sector, rows in sorted(by_sector.items(), key=lambda item: (-len(item[1]), item[0])):
        counts = Counter(label for row in rows for label in row["need_labels"])
        matrix_rows.append(
            {"sector": sector, "validated_posts": len(rows), **{label: counts[label] for label in all_needs}}
        )
    write_csv(
        run_dir / "validated_sector_need_matrix.csv",
        matrix_rows,
        ["sector", "validated_posts", *all_needs],
    )

    need_totals = Counter(label for row in validated_cards for label in row["need_labels"])
    metrics = {
        "validated_cards": len(validated_cards),
        "classified_sector_cards": sum(row["sector"] != "待人工归类" for row in validated_cards),
        "manual_sector_review_cards": sum(row["sector"] == "待人工归类" for row in validated_cards),
        "sector_counts": {row["sector"]: row["validated_posts"] for row in sector_rows},
        "shared_need_counts": dict(need_totals.most_common()),
        "evidence_sources": dict(Counter(row["evidence_source"] for row in validated_cards)),
        "scope_note": "Serper-discovered and evidence-validated sample; not all Reddit and not a market-size estimate.",
    }
    (run_dir / "validated_matrix_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# Reddit 板块 × 共性需求（证据核验版）",
        "",
        "> 只统计在帖子正文或实际评论中找到需求措辞的帖子。这里仍是检索样本的结构化结果，",
        "> 不是市场规模、机会优先级或 Reddit 全站比例。",
        "",
        f"- 证据卡：{len(validated_cards):,}",
        f"- 已落入初步产品板块：{metrics['classified_sector_cards']:,}",
        f"- 仍待人工判断产品板块：{metrics['manual_sector_review_cards']:,}",
        "",
        "## 板块概览",
        "",
        "| 板块 | 核验帖 | 独立作者 | 社区 | 当前/活跃/历史 | 评论证据占比 | 主要共性需求 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in sector_rows:
        comment_ratio = row["comment_only_evidence"] / max(1, row["validated_posts"])
        lines.append(
            f"| {row['sector']} | {row['validated_posts']} | {row['unique_authors']} | "
            f"{row['unique_subreddits']} | {row['current_posts']}/{row['active_posts']}/{row['history_posts']} | "
            f"{comment_ratio:.0%} | {row['top_needs']} |"
        )
    lines.extend(["", "## 跨板块重复出现的需求形态", ""])
    for label, count in need_totals.most_common():
        sector_presence = sum(
            any(label in row["need_labels"] for row in rows) for rows in by_sector.values()
        )
        lines.append(f"- {label}：{count} 帖，出现在 {sector_presence} 个初步板块")
    lines.extend(["", "## 每个板块的核验证据示例", ""])
    for sector, rows in sorted(by_sector.items(), key=lambda item: (-len(item[1]), item[0])):
        if sector == "待人工归类":
            continue
        lines.append(f"### {sector}")
        lines.append("")
        ranked = sorted(rows, key=lambda row: (row["num_comments"], row["score"]), reverse=True)
        for row in ranked[:4]:
            labels = "、".join(row["need_labels"])
            lines.append(
                f"- [{row['title']}]({row['canonical_url']}) — r/{row['subreddit']}；{labels}"
            )
            quote = row["evidence_excerpt"]
            if quote:
                lines.append(f"  - 证据：{quote[:420]}")
        lines.append("")
    (run_dir / "validated_sector_need_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
