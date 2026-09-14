"""Run resumable, validated Codex extraction batches in parallel."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


PRINT_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch_dir", type=Path)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_batch(input_path: Path, output_path: Path) -> list[str]:
    issues: list[str] = []
    try:
        sources = load_json(input_path).get("records", [])
    except Exception as exc:  # noqa: BLE001
        return [f"input_json:{exc}"]
    try:
        cards = load_json(output_path).get("items", [])
    except Exception as exc:  # noqa: BLE001
        return [f"output_json:{exc}"]

    source_by_id = {row.get("evidence_id"): row for row in sources}
    card_by_id = {row.get("evidence_id"): row for row in cards}
    if len(cards) != len(sources):
        issues.append(f"count:{len(cards)}!={len(sources)}")
    if len(card_by_id) != len(cards):
        issues.append("duplicate_output_id")
    missing = sorted(set(source_by_id) - set(card_by_id))
    extra = sorted(set(card_by_id) - set(source_by_id))
    if missing:
        issues.append(f"missing_ids:{','.join(missing)}")
    if extra:
        issues.append(f"extra_ids:{','.join(extra)}")

    for evidence_id, card in card_by_id.items():
        source = source_by_id.get(evidence_id)
        if not source:
            continue
        quote = card.get("evidence_quote", "")
        if not quote or quote not in source.get("evidence_text", ""):
            issues.append(f"quote_not_verbatim:{evidence_id}")
        if len(quote) > 300:
            issues.append(f"quote_too_long:{evidence_id}")
        is_demand = bool(card.get("is_physical_product_demand"))
        if is_demand and card.get("demand_status") == "no_demand":
            issues.append(f"demand_status_conflict:{evidence_id}")
        if not is_demand and card.get("demand_status") != "no_demand":
            issues.append(f"non_demand_status_conflict:{evidence_id}")
        if not is_demand and card.get("demand_key"):
            issues.append(f"non_demand_key:{evidence_id}")
        if card.get("demand_status") == "purchase_evaluation" and card.get("purchase_intent") == "none":
            issues.append(f"purchase_without_intent:{evidence_id}")
        if is_demand and not card.get("product_object"):
            if card.get("specificity") != "insufficient" or card.get("sector_family") != "unclear":
                issues.append(f"ungrounded_empty_product:{evidence_id}")
    return issues


def prompt_for(input_path: Path, prompt_path: Path) -> str:
    return (
        f"Read {prompt_path.as_posix()} completely, then read {input_path.as_posix()}. "
        "Extract exactly one demand-card item per input record, preserve every evidence_id, "
        "and return only the JSON object required by the supplied schema. Validate every "
        "evidence_quote as a literal substring before answering. Do not modify files or perform web research."
    )


def run_one(
    input_path: Path,
    args: argparse.Namespace,
    root: Path,
) -> dict:
    output_path = input_path.with_name(input_path.name.replace("_input.json", "_output.json"))
    if output_path.exists():
        issues = validate_batch(input_path, output_path)
        if not issues:
            return {"batch": input_path.stem, "status": "skipped_valid", "attempts": 0, "issues": []}

    env = os.environ.copy()
    env["CODEX_HOME"] = str(args.codex_home.resolve())
    logs_dir = input_path.parent / "logs"
    logs_dir.mkdir(exist_ok=True)
    last_issues: list[str] = []
    for attempt in range(1, args.retries + 2):
        attempt_output = output_path.with_suffix(f".attempt{attempt}.json")
        log_path = logs_dir / f"{input_path.stem}.attempt{attempt}.log"
        command = [
            str(args.codex.resolve()),
            "exec",
            "--ephemeral",
            "-s",
            "read-only",
            "-m",
            args.model,
            "-c",
            f'model_reasoning_effort="{args.effort}"',
            "--output-schema",
            str(args.schema.resolve()),
            "-o",
            str(attempt_output.resolve()),
            prompt_for(input_path.resolve(), args.prompt.resolve()),
        ]
        started = time.time()
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            completed = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if completed.returncode == 0 and attempt_output.exists():
            last_issues = validate_batch(input_path, attempt_output)
            if not last_issues:
                os.replace(attempt_output, output_path)
                return {
                    "batch": input_path.stem,
                    "status": "completed",
                    "attempts": attempt,
                    "seconds": round(time.time() - started, 1),
                    "issues": [],
                }
        else:
            last_issues = [f"exit_code:{completed.returncode}"]
        with PRINT_LOCK:
            print(
                json.dumps(
                    {
                        "batch": input_path.stem,
                        "attempt": attempt,
                        "retry_issues": last_issues,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
    return {
        "batch": input_path.stem,
        "status": "failed",
        "attempts": args.retries + 1,
        "issues": last_issues,
    }


def write_state(batch_dir: Path, results: list[dict], total: int) -> None:
    completed = sum(row["status"] in {"completed", "skipped_valid"} for row in results)
    failed = sum(row["status"] == "failed" for row in results)
    state = {
        "total_batches": total,
        "reported_batches": len(results),
        "completed_batches": completed,
        "failed_batches": failed,
        "results": sorted(results, key=lambda row: row["batch"]),
    }
    (batch_dir / "full_run_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    batch_dir = args.batch_dir.resolve()
    root = Path.cwd().resolve()
    inputs = sorted(batch_dir.glob("batch_*_input.json"))
    results: list[dict] = []
    write_state(batch_dir, results, len(inputs))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, path, args, root): path for path in inputs}
        for future in as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {
                    "batch": path.stem,
                    "status": "failed",
                    "attempts": 0,
                    "issues": [f"runner_exception:{exc}"],
                }
            results.append(result)
            write_state(batch_dir, results, len(inputs))
            with PRINT_LOCK:
                print(json.dumps(result, ensure_ascii=False), flush=True)

    accepted_outputs = []
    for input_path in inputs:
        output_path = input_path.with_name(input_path.name.replace("_input.json", "_output.json"))
        if output_path.exists() and not validate_batch(input_path, output_path):
            accepted_outputs.extend(load_json(output_path)["items"])
    with (batch_dir / "semantic_full_raw.jsonl").open("w", encoding="utf-8", newline="") as handle:
        for card in accepted_outputs:
            handle.write(json.dumps(card, ensure_ascii=False) + "\n")
    failures = [row for row in results if row["status"] == "failed"]
    print(
        json.dumps(
            {
                "finished": True,
                "accepted_cards": len(accepted_outputs),
                "failed_batches": len(failures),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
