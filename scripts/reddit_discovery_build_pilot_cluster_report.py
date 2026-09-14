"""Build evidence-linked pilot clusters on product-sector and need-structure axes."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


ACTIVE_STATUSES = {"active_unresolved", "active_workaround", "purchase_evaluation"}
INTENT_STATUSES = {"medium", "strong"}
SUPPLY_FIT = {"likely", "partial"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("reviewed_jsonl", type=Path)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def summarize_group(name: str, rows: list[dict]) -> dict:
    sectors = sorted({row["sector_family"] for row in rows})
    subreddits = sorted({row.get("subreddit", "") for row in rows if row.get("subreddit")})
    return {
        "cluster": name,
        "evidence_cards": len(rows),
        "active_or_evaluating": sum(row["demand_status"] in ACTIVE_STATUSES for row in rows),
        "medium_or_strong_intent": sum(row["purchase_intent"] in INTENT_STATUSES for row in rows),
        "independent_subreddits": len(subreddits),
        "product_sectors": len(sectors),
        "likely_or_partial_supply_fit": sum(row["supply_chain_fit"] in SUPPLY_FIT for row in rows),
        "risk_flagged": sum(bool(row.get("risk_flags")) for row in rows),
        "sector_list": "|".join(sectors),
        "subreddit_list": "|".join(subreddits),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def examples(rows: list[dict], limit: int = 3) -> list[dict]:
    status_rank = {
        "active_unresolved": 0,
        "active_workaround": 1,
        "purchase_evaluation": 2,
        "solved_with_custom_fix": 3,
        "solved_with_replacement": 4,
        "historical_or_advice": 5,
    }
    return sorted(
        rows,
        key=lambda row: (
            status_rank.get(row["demand_status"], 9),
            -float(row.get("confidence", 0)),
            row["evidence_id"],
        ),
    )[:limit]


def main() -> int:
    args = parse_args()
    input_path = args.reviewed_jsonl.resolve()
    output_dir = input_path.parent
    all_rows = load_jsonl(input_path)
    rows = [row for row in all_rows if row.get("cluster_eligible")]

    by_sector: dict[str, list[dict]] = defaultdict(list)
    by_problem: dict[str, list[dict]] = defaultdict(list)
    matrix: Counter[tuple[str, str]] = Counter()
    for row in rows:
        by_sector[row["sector_family"]].append(row)
        by_problem[row["problem_state"]].append(row)
        matrix[(row["sector_family"], row["problem_state"])] += 1

    sector_rows = [summarize_group(name, members) for name, members in by_sector.items()]
    sector_rows.sort(key=lambda row: (-row["evidence_cards"], row["cluster"]))
    problem_rows = [summarize_group(name, members) for name, members in by_problem.items()]
    problem_rows.sort(key=lambda row: (-row["evidence_cards"], row["cluster"]))
    matrix_rows = [
        {"sector_family": sector, "problem_state": problem, "evidence_cards": count}
        for (sector, problem), count in matrix.most_common()
    ]
    write_csv(output_dir / "pilot_product_sector_clusters.csv", sector_rows)
    write_csv(output_dir / "pilot_cross_sector_need_clusters.csv", problem_rows)
    write_csv(output_dir / "pilot_sector_need_matrix.csv", matrix_rows)

    lines = [
        "# Reddit demand pilot: two-axis cluster review",
        "",
        "This report demonstrates the clustering method on 83 reviewed, cluster-eligible cards. It is not a market ranking and does not estimate Reddit-wide prevalence.",
        "",
        "## Product-sector axis",
        "",
        "| Sector | Cards | Active / evaluating | Communities | Main need structures |",
        "|---|---:|---:|---:|---|",
    ]
    for item in sector_rows:
        members = by_sector[item["cluster"]]
        needs = Counter(row["problem_state"] for row in members)
        main_needs = ", ".join(f"{key} ({value})" for key, value in needs.most_common(3))
        lines.append(
            f"| {item['cluster']} | {item['evidence_cards']} | {item['active_or_evaluating']} | "
            f"{item['independent_subreddits']} | {main_needs} |"
        )

    lines.extend(
        [
            "",
            "## Cross-sector need axis",
            "",
            "Only structures appearing in at least two product sectors are shown here.",
            "",
            "| Need structure | Cards | Active / evaluating | Product sectors | Communities | Supply fit likely/partial | Risk-flagged |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    cross_sector = [item for item in problem_rows if item["product_sectors"] >= 2]
    for item in cross_sector:
        lines.append(
            f"| {item['cluster']} | {item['evidence_cards']} | {item['active_or_evaluating']} | "
            f"{item['product_sectors']} | {item['independent_subreddits']} | "
            f"{item['likely_or_partial_supply_fit']} | {item['risk_flagged']} |"
        )

    lines.extend(["", "## Evidence examples for cross-sector structures", ""])
    for item in cross_sector:
        name = item["cluster"]
        lines.extend([f"### {name}", ""])
        for row in examples(by_problem[name]):
            quote = row["evidence_quote"].replace("\n", " ")
            lines.append(
                f"- **{row['sector_family']} · {row['demand_status']}** — “{quote}” "
                f"([source]({row['source_url']}))"
            )
        lines.append("")

    lines.extend(
        [
            "## What this pilot establishes",
            "",
            "1. Product sectors and need structures can be separated without using subreddit as the product label.",
            "2. Current unmet demand can be separated from historical advice and already-solved workarounds.",
            "3. The same structural need can be compared across sectors while retaining evidence and risk flags.",
            "4. The next full-data stage should apply the reviewed extractor to all validated evidence cards, then merge near-duplicate `demand_key` values inside each sector before any commercial scoring.",
            "",
            "## What this pilot does not establish",
            "",
            "The counts cannot yet answer which market is largest, fastest-growing, or most profitable. The 100 records were balanced for method review and the query library intentionally over-samples replacement and repair language.",
        ]
    )
    report = "\n".join(lines) + "\n"
    (output_dir / "pilot_two_axis_cluster_report.md").write_text(report, encoding="utf-8")
    print(f"eligible_cards={len(rows)} sectors={len(by_sector)} need_structures={len(by_problem)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
