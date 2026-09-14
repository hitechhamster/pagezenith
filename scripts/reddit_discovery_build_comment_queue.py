"""Prioritize comment fetching using post text and Serper snippet evidence."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from reddit_discovery_prepare_clusters import NEED_RULES, contains_phrase


POST_RE = re.compile(r"reddit\.com/r/[^/]+/comments/([a-z0-9]+)", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def load_cards(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def load_snippets(path: Path) -> dict[str, list[str]]:
    snippets: dict[str, set[str]] = defaultdict(set)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            match = POST_RE.search(row.get("canonical_url", ""))
            snippet = re.sub(r"\s+", " ", row.get("snippet", "")).strip()
            if match and snippet:
                snippets[match.group(1).lower()].add(snippet)
    return {post_id: sorted(values) for post_id, values in snippets.items()}


def snippet_need_labels(snippets: list[str]) -> list[str]:
    text = " ".join(snippets).lower()
    labels = []
    for label, rule in NEED_RULES.items():
        if any(contains_phrase(text, phrase) for phrase in rule["phrases"]):
            labels.append(label)
    return labels


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    cards = load_cards(run_dir / "demand_cards_preliminary.jsonl")
    snippets = load_snippets(run_dir / "urls_normalized.csv")
    queue = []
    for card in cards:
        if card.get("excluded"):
            continue
        post_id = str(card.get("post_id", "")).lower()
        snippet_labels = snippet_need_labels(snippets.get(post_id, []))
        direct = bool(card.get("direct_need_language"))
        classified = card.get("sector") != "待归类"
        if direct and classified:
            tier, reason = "A", "标题或正文明确表达需求，且已有产品板块"
        elif snippet_labels and classified:
            tier, reason = "B", "Google 摘要含明确需求措辞，且已有产品板块"
        elif direct or snippet_labels:
            tier, reason = "C", "有明确需求措辞，但产品板块待判断"
        else:
            tier, reason = "D", "目前只有搜索 query 来源信号"
        queue.append(
            {
                "tier": tier,
                "post_id": post_id,
                "sector": card.get("sector", ""),
                "subreddit": card.get("subreddit", ""),
                "title": card.get("title", ""),
                "canonical_url": card.get("canonical_url", ""),
                "num_comments": int(card.get("num_comments") or 0),
                "score": int(card.get("score") or 0),
                "time_windows": "|".join(card.get("time_windows") or []),
                "post_need_labels": "|".join(card.get("need_labels") or []),
                "snippet_need_labels": "|".join(snippet_labels),
                "reason": reason,
                "serper_snippets": " || ".join(snippets.get(post_id, []))[:1800],
            }
        )
    tier_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    window_order = {"current": 0, "active": 1, "history": 2}
    queue.sort(
        key=lambda row: (
            tier_order[row["tier"]],
            min((window_order.get(x, 9) for x in row["time_windows"].split("|") if x), default=9),
            -row["num_comments"],
            -row["score"],
        )
    )
    fields = list(queue[0].keys())
    with (run_dir / "comment_fetch_queue.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(queue)
    counts = Counter(row["tier"] for row in queue)
    by_sector = Counter(row["sector"] for row in queue if row["tier"] in {"A", "B", "C"})
    metrics = {
        "posts": len(queue),
        "tiers": dict(counts),
        "recommended_comment_fetch_posts": counts["A"] + counts["B"] + counts["C"],
        "evidence_candidates_by_sector": dict(by_sector.most_common()),
        "tier_meanings": {
            "A": "post title/body direct + classified sector",
            "B": "Serper snippet direct + classified sector",
            "C": "direct evidence + unclassified sector",
            "D": "query provenance only",
        },
    }
    (run_dir / "comment_fetch_queue_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
