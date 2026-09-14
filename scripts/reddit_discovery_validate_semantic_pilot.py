"""Validate semantic demand-card batches and build a compact review report."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("pilot_dir", type=Path)
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def issue_flags(source: dict, card: dict) -> list[str]:
    issues: list[str] = []
    evidence = source.get("evidence_text", "")
    quote = card.get("evidence_quote", "")
    product = card.get("product_object", "")
    title = source.get("thread_title", "")

    if not quote or quote not in evidence:
        issues.append("quote_not_verbatim")
    if len(quote) > 300:
        issues.append("quote_too_long")
    if card.get("is_physical_product_demand"):
        if not product:
            issues.append("demand_without_product")
        if card.get("demand_status") == "no_demand":
            issues.append("demand_status_conflict")
        if not card.get("demand_key"):
            issues.append("demand_key_missing")
        if card.get("solution_form") == "none":
            issues.append("demand_solution_missing")
    else:
        if card.get("demand_status") != "no_demand":
            issues.append("non_demand_status_conflict")
        if card.get("demand_key"):
            issues.append("non_demand_has_key")
    if card.get("demand_status") == "purchase_evaluation" and card.get("purchase_intent") == "none":
        issues.append("purchase_status_without_intent")
    if "intimate_product" in card.get("risk_flags", []) and card.get("sector_family") == "eyewear":
        issues.append("eyewear_marked_intimate")
    if "hazardous_shipping" not in card.get("risk_flags", []) and "battery" in (
        product + " " + card.get("component", "")
    ).lower():
        issues.append("battery_shipping_risk_missing")
    if source.get("evidence_source") == "comment" and product:
        in_evidence = product.lower() in evidence.lower()
        in_title = product.lower() in title.lower()
        if in_title and not in_evidence:
            issues.append("product_only_in_thread_title")
    if card.get("is_physical_product_demand") and card.get("solution_form") == "service_or_information":
        issues.append("physical_demand_is_only_service_info")
    return issues


def main() -> int:
    args = parse_args()
    pilot_dir = args.pilot_dir.resolve()
    inputs: list[dict] = []
    outputs: list[dict] = []
    for input_path in sorted(pilot_dir.glob("batch_*_input.json")):
        output_path = input_path.with_name(input_path.name.replace("_input.json", "_output.json"))
        inputs.extend(load_json(input_path).get("records", []))
        if output_path.exists():
            outputs.extend(load_json(output_path).get("items", []))

    input_by_id = {row["evidence_id"]: row for row in inputs}
    output_by_id = {row["evidence_id"]: row for row in outputs}
    missing = sorted(set(input_by_id) - set(output_by_id))
    extra = sorted(set(output_by_id) - set(input_by_id))
    review_rows = []
    issue_counts: Counter[str] = Counter()
    for evidence_id, card in output_by_id.items():
        source = input_by_id.get(evidence_id, {})
        issues = issue_flags(source, card)
        issue_counts.update(issues)
        review_rows.append(
            {
                "evidence_id": evidence_id,
                "evidence_source": source.get("evidence_source", ""),
                "thread_title": source.get("thread_title", ""),
                "is_demand": card.get("is_physical_product_demand", ""),
                "demand_role": card.get("demand_role", ""),
                "demand_status": card.get("demand_status", ""),
                "product_object": card.get("product_object", ""),
                "component": card.get("component", ""),
                "problem_state": card.get("problem_state", ""),
                "purchase_intent": card.get("purchase_intent", ""),
                "sector_family": card.get("sector_family", ""),
                "solution_form": card.get("solution_form", ""),
                "supply_chain_fit": card.get("supply_chain_fit", ""),
                "risk_flags": "|".join(card.get("risk_flags", [])),
                "confidence": card.get("confidence", ""),
                "automatic_issues": "|".join(issues),
                "evidence_text": source.get("evidence_text", ""),
                "evidence_quote": card.get("evidence_quote", ""),
                "source_url": source.get("source_url", ""),
            }
        )

    review_path = pilot_dir / "semantic_review.csv"
    if review_rows:
        with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(review_rows[0]))
            writer.writeheader()
            writer.writerows(review_rows)

    status_counts = Counter(str(row.get("demand_status", "")) for row in outputs)
    sector_counts = Counter(
        str(row.get("sector_family", ""))
        for row in outputs
        if row.get("is_physical_product_demand")
    )
    lines = [
        "# Semantic pilot validation",
        "",
        f"- Planned records: {len(inputs)}",
        f"- Returned records: {len(outputs)}",
        f"- Missing IDs: {len(missing)}",
        f"- Extra IDs: {len(extra)}",
        f"- Records with automatic issues: {sum(bool(row['automatic_issues']) for row in review_rows)}",
        "",
        "## Automatic issues",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in issue_counts.most_common())
    lines.extend(["", "## Demand status", ""])
    lines.extend(f"- {key}: {value}" for key, value in status_counts.most_common())
    lines.extend(["", "## Physical-demand sectors", ""])
    lines.extend(f"- {key}: {value}" for key, value in sector_counts.most_common())
    (pilot_dir / "semantic_validation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 1 if missing or extra else 0


if __name__ == "__main__":
    raise SystemExit(main())
