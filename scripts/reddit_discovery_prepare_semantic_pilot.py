"""Prepare a deterministic 100-record semantic extraction pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path


LABEL_CODES = {
    "零件买不到或断供": "replacement_unavailable",
    "只能更换总成或整机": "forced_whole_assembly",
    "需要临时修复或定制替代件": "temporary_or_custom_fix",
    "产品不可维修或厂商不支持": "aftersales_or_repairability_gap",
    "同一部件反复损坏": "repeat_or_premature_failure",
    "难清洁、难维护或日常使用麻烦": "cleaning_or_use_friction",
    "跨境运费、税费或到手价过高": "landed_cost_too_high",
    "需要商用级耐用性或稳定供货": "durability_or_supply_requirement",
    "耗材或易损件需要持续补充": "repeat_consumable_need",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--size", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--output-name", default="semantic-pilot-v2")
    parser.add_argument("--all", action="store_true", help="Prepare every validated demand card.")
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def stable_key(row: dict) -> str:
    return hashlib.sha256(str(row.get("post_id", "")).encode("ascii", "ignore")).hexdigest()


def round_robin(rows: list[dict], count: int) -> list[dict]:
    grouped: dict[str, deque] = defaultdict(deque)
    for row in sorted(rows, key=stable_key):
        key = f"{row.get('sector', '')}|{row.get('evidence_source', '')}|{'/'.join(row.get('time_windows', []))}"
        grouped[key].append(row)
    queues = sorted(grouped.values(), key=len, reverse=True)
    selected = []
    while queues and len(selected) < count:
        remaining = []
        for queue in queues:
            if queue and len(selected) < count:
                selected.append(queue.popleft())
            if queue:
                remaining.append(queue)
        queues = remaining
    return selected


def to_input(row: dict) -> dict:
    source_code = "post" if row.get("evidence_source") == "正文" else "comment"
    evidence_id = f"{row['post_id']}:{source_code}"
    return {
        "evidence_id": evidence_id,
        "post_id": row["post_id"],
        "source_url": row.get("canonical_url", ""),
        "subreddit": row.get("subreddit", ""),
        "thread_title": row.get("title", ""),
        "evidence_source": source_code,
        "rule_need_labels": [
            LABEL_CODES.get(label, "other") for label in (row.get("need_labels") or [])
        ],
        "evidence_text": normalize_text(str(row.get("evidence_excerpt") or ""))[:1800],
    }


def normalize_text(value: str) -> str:
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": " - ",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    return " ".join(value.split())


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    output_dir = run_dir / args.output_name
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = load_jsonl(run_dir / "validated_demand_cards.jsonl")
    manual = [row for row in rows if row.get("sector") == "待人工归类"]
    classified = [row for row in rows if row.get("sector") != "待人工归类"]
    if args.all:
        selected = rows
    else:
        manual_target = args.size // 2
        selected = round_robin(manual, manual_target) + round_robin(
            classified, args.size - manual_target
        )
    selected = sorted(selected, key=stable_key)
    inputs = [to_input(row) for row in selected]

    with (output_dir / "pilot_input.jsonl").open("w", encoding="utf-8", newline="") as handle:
        for row in inputs:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    for index in range(0, len(inputs), args.batch_size):
        batch = inputs[index : index + args.batch_size]
        batch_no = index // args.batch_size + 1
        (output_dir / f"batch_{batch_no:02d}_input.json").write_text(
            json.dumps({"records": batch}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    manifest = {
        "pilot_size": len(inputs),
        "manual_sector_records": sum(row.get("sector") == "待人工归类" for row in selected),
        "classified_sector_records": sum(row.get("sector") != "待人工归类" for row in selected),
        "post_evidence_records": sum(row.get("evidence_source") == "正文" for row in selected),
        "comment_evidence_records": sum(row.get("evidence_source") == "评论" for row in selected),
        "batches": (len(inputs) + args.batch_size - 1) // args.batch_size,
        "selection": "all" if args.all else "balanced_pilot",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
