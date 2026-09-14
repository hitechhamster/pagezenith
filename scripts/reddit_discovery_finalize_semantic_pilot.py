"""Apply human review decisions and finalize a semantic pilot dataset."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("pilot_dir", type=Path)
    parser.add_argument("overrides", type=Path)
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    pilot_dir = args.pilot_dir.resolve()
    overrides = load_json(args.overrides.resolve())
    sources: dict[str, dict] = {}
    cards: dict[str, dict] = {}
    for input_path in sorted(pilot_dir.glob("batch_*_input.json")):
        sources.update({row["evidence_id"]: row for row in load_json(input_path)["records"]})
        output_path = input_path.with_name(input_path.name.replace("_input.json", "_output.json"))
        if output_path.exists():
            cards.update({row["evidence_id"]: row for row in load_json(output_path)["items"]})

    if set(sources) != set(cards):
        missing = sorted(set(sources) - set(cards))
        extra = sorted(set(cards) - set(sources))
        raise SystemExit(f"ID mismatch: missing={missing}, extra={extra}")
    unknown_overrides = sorted(set(overrides) - set(cards))
    if unknown_overrides:
        raise SystemExit(f"Overrides reference unknown IDs: {unknown_overrides}")

    finalized: list[dict] = []
    for evidence_id, source in sources.items():
        card = dict(cards[evidence_id])
        review = overrides.get(evidence_id, {})
        card.update(review.get("corrections", {}))
        decision = review.get("review_decision", "accepted")
        product_ready = bool(card.get("product_object")) and card.get("sector_family") not in {
            "unclear",
            "",
        }
        cluster_eligible = (
            bool(card.get("is_physical_product_demand"))
            and product_ready
            and decision != "quarantine_insufficient_context"
        )
        finalized.append(
            {
                **source,
                **card,
                "review_decision": decision,
                "review_note": review.get("review_note", "Reviewed and accepted without correction."),
                "cluster_eligible": cluster_eligible,
            }
        )

    jsonl_path = pilot_dir / "semantic_pilot_reviewed.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="") as handle:
        for row in finalized:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    csv_path = pilot_dir / "semantic_pilot_reviewed.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(finalized[0])
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in finalized:
            flat = dict(row)
            flat["rule_need_labels"] = "|".join(row.get("rule_need_labels", []))
            flat["risk_flags"] = "|".join(row.get("risk_flags", []))
            writer.writerow(flat)

    decisions = Counter(row["review_decision"] for row in finalized)
    demand_rows = [row for row in finalized if row["is_physical_product_demand"]]
    cluster_rows = [row for row in finalized if row["cluster_eligible"]]
    statuses = Counter(row["demand_status"] for row in demand_rows)
    sectors = Counter(row["sector_family"] for row in cluster_rows)
    problems = Counter(row["problem_state"] for row in cluster_rows)
    lines = [
        "# Reddit semantic extraction pilot: 100-record review",
        "",
        "This is a method-validation sample, not a market-size estimate.",
        "",
        "## Review funnel",
        "",
        f"- Reviewed records: {len(finalized)}",
        f"- Accepted without correction: {decisions['accepted']}",
        f"- Corrected after review: {decisions['corrected']}",
        f"- Excluded as false positive: {decisions['excluded']}",
        f"- Quarantined for missing product context: {decisions['quarantine_insufficient_context']}",
        f"- Physical-demand signals after review: {len(demand_rows)}",
        f"- Eligible for product-sector clustering: {len(cluster_rows)}",
        "",
        "## Demand status among retained signals",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in statuses.most_common())
    lines.extend(["", "## Product sectors in the pilot", ""])
    lines.extend(f"- {key}: {value}" for key, value in sectors.most_common())
    lines.extend(["", "## Cross-sector need structures in the pilot", ""])
    lines.extend(f"- {key}: {value}" for key, value in problems.most_common())
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "The sector and need counts above describe only the balanced 100-record review sample. They validate the extraction and clustering fields; they must not be reported as Reddit-wide prevalence or market opportunity rankings.",
        ]
    )
    report = "\n".join(lines) + "\n"
    (pilot_dir / "semantic_pilot_review.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
