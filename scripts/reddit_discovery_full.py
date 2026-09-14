"""Exhaust selected Reddit discovery queries within bounded Serper pagination.

"Full" here means all retrievable result pages for the selected query catalog and
three explicit date windows. It never means all Reddit content. Each query/window
series stops on an empty page or a page that yields no unseen canonical URLs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reddit_discovery_pilot import (
    REPO,
    canonical_url,
    configured_keys,
    load_catalog,
    persist,
    run_query,
)


def full_windows(today: date) -> tuple[dict[str, str], ...]:
    d30 = today - timedelta(days=30)
    d90 = today - timedelta(days=90)
    d365 = today - timedelta(days=365)
    return (
        {"time_window": "current", "after_date": d30.isoformat(), "before_date": ""},
        {"time_window": "active", "after_date": d90.isoformat(), "before_date": d30.isoformat()},
        {"time_window": "history", "after_date": d365.isoformat(), "before_date": d90.isoformat()},
    )


def page_query(row: dict[str, str], window: dict[str, str], series_id: int, page: int) -> dict[str, str]:
    text = f'site:reddit.com/r/ {row["query_text"]} after:{window["after_date"]}'
    if window["before_date"]:
        text += f' before:{window["before_date"]}'
    return {
        "query_id": f"s{series_id:03d}p{page:02d}",
        "candidate_id": row["candidate_id"],
        "query_text": text,
        "signal_type": row["query_family"],
        "signal_phrase": row["query_text"],
        "time_window": window["time_window"],
        "serper_page": str(page),
        "after_date": window["after_date"],
        "before_date": window["before_date"],
        "phase": "selected_query_full_discovery",
        "hypothesis": row["hypothesis"],
        "status": "planned",
    }


async def run_series(
    *,
    base_url: str,
    timeout: float,
    semaphore: asyncio.Semaphore,
    catalog_row: dict[str, str],
    window: dict[str, str],
    series_id: int,
    max_pages: int,
    checkpoint,
    start_page: int = 1,
    initial_seen: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set(initial_seen or set())
    stop_reason = "max_pages"
    for page in range(start_page, max_pages + 1):
        query = page_query(catalog_row, window, series_id, page)
        result = await run_query(base_url, timeout, query, semaphore)
        organic = result.get("raw", {}).get("organic", []) if isinstance(result.get("raw"), dict) else []
        if not isinstance(organic, list):
            organic = []
        page_urls = {
            canonical_url(str(item.get("link") or ""))
            for item in organic
            if isinstance(item, dict) and str(item.get("link") or "").strip()
        }
        new_urls = page_urls - seen
        result["new_unique_urls_in_series"] = len(new_urls)
        rows.append(result)
        await checkpoint(result)
        print(json.dumps({
            "series": f"s{series_id:03d}", "candidate": catalog_row["candidate_id"],
            "window": window["time_window"], "page": page,
            "results": len(organic), "new_urls": len(new_urls), "status": result["status"],
        }, ensure_ascii=False), flush=True)
        if result["status"] != "completed":
            stop_reason = result["status"]
            break
        if not organic:
            stop_reason = "empty_page"
            break
        if not new_urls:
            stop_reason = "no_new_urls"
            break
        seen.update(new_urls)
    return rows, {
        "series_id": f"s{series_id:03d}",
        "candidate_id": catalog_row["candidate_id"],
        "time_window": window["time_window"],
        "pages_requested": len(rows),
        "unique_urls_in_series": len(seen),
        "stop_reason": stop_reason,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--resume-output", type=Path, default=None,
                        help="Continue an interrupted or page-capped full run in place.")
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=None,
                        help="Debug-only cap on selected catalog candidates.")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--base-url", default="https://google.serper.dev")
    args = parser.parse_args()
    if args.max_pages < 1 or args.concurrency < 1:
        parser.error("--max-pages and --concurrency must be positive")
    if not configured_keys():
        raise SystemExit("SERPER_KEYS / SERPER_KEY is not configured.")

    if args.output and args.resume_output:
        parser.error("--output and --resume-output cannot be used together")
    catalog = load_catalog(args.catalog)
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit must be positive")
        catalog = catalog[:args.limit]
    now = datetime.now(timezone.utc)
    output = args.resume_output or args.output or (
        REPO / "data" / "reddit-discovery" / now.strftime("%Y%m%dT%H%M%SZ-full")
    )
    prior_results: list[dict[str, Any]] = []
    if args.resume_output:
        state_path = output / "run_state.json"
        checkpoint_path = output / "checkpoint_results.jsonl"
        if not state_path.exists() or not checkpoint_path.exists():
            raise SystemExit("Resume output needs run_state.json and checkpoint_results.jsonl")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        started = datetime.fromisoformat(str(state["started_at"]))
        with checkpoint_path.open(encoding="utf-8") as handle:
            prior_results = [json.loads(line) for line in handle if line.strip()]
    else:
        started = now
        output.mkdir(parents=True, exist_ok=False)
    checkpoint_path = output / "checkpoint_results.jsonl"
    checkpoint_lock = asyncio.Lock()

    async def checkpoint(result: dict[str, Any]) -> None:
        async with checkpoint_lock:
            with checkpoint_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")

    (output / "run_state.json").write_text(json.dumps({
        "status": "resuming" if args.resume_output else "running",
        "started_at": started.isoformat(), "selected_candidates": len(catalog),
        "windows": [item["time_window"] for item in full_windows(args.today)],
        "max_pages_per_series": args.max_pages, "key_material_written": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    semaphore = asyncio.Semaphore(args.concurrency)
    jobs = []
    series_id = 0
    prior_by_series: dict[int, list[dict[str, Any]]] = {}
    for result in prior_results:
        query_id = str(result.get("query_id") or "")
        if query_id.startswith("s") and "p" in query_id:
            prior_by_series.setdefault(int(query_id[1:4]), []).append(result)
    for row in catalog:
        for window in full_windows(args.today):
            series_id += 1
            previous = sorted(
                prior_by_series.get(series_id, []), key=lambda item: int(item.get("serper_page", 0))
            )
            start_page = 1
            seen: set[str] = set()
            if previous:
                for result in previous:
                    organic = result.get("raw", {}).get("organic", []) if isinstance(result.get("raw"), dict) else []
                    if isinstance(organic, list):
                        seen.update(
                            canonical_url(str(item.get("link") or ""))
                            for item in organic if isinstance(item, dict) and item.get("link")
                        )
                last = previous[-1]
                last_organic = last.get("raw", {}).get("organic", []) if isinstance(last.get("raw"), dict) else []
                if (
                    last.get("status") != "completed"
                    or not isinstance(last_organic, list)
                    or not last_organic
                    or int(last.get("new_unique_urls_in_series", 0)) == 0
                ):
                    continue
                start_page = int(last.get("serper_page", 0)) + 1
                if start_page > args.max_pages:
                    continue
            jobs.append(run_series(
                base_url=args.base_url, timeout=args.timeout, semaphore=semaphore,
                catalog_row=row, window=window, series_id=series_id, max_pages=args.max_pages,
                checkpoint=checkpoint, start_page=start_page, initial_seen=seen,
            ))
    series_results = await asyncio.gather(*jobs)
    new_results = [result for rows, _summary in series_results for result in rows]
    completed = sorted(
        prior_results + new_results,
        key=lambda item: (int(str(item["query_id"])[1:4]), int(item.get("serper_page", 0))),
    )
    summaries = []
    all_by_series: dict[int, list[dict[str, Any]]] = {}
    for result in completed:
        all_by_series.setdefault(int(str(result["query_id"])[1:4]), []).append(result)
    for current_series_id, rows in sorted(all_by_series.items()):
        rows.sort(key=lambda item: int(item.get("serper_page", 0)))
        last = rows[-1]
        organic = last.get("raw", {}).get("organic", []) if isinstance(last.get("raw"), dict) else []
        if last.get("status") != "completed":
            stop_reason = str(last.get("status"))
        elif not isinstance(organic, list) or not organic:
            stop_reason = "empty_page"
        elif int(last.get("new_unique_urls_in_series", 0)) == 0:
            stop_reason = "no_new_urls"
        else:
            stop_reason = "max_pages"
        urls = {
            canonical_url(str(item.get("link") or ""))
            for result in rows
            for item in (result.get("raw", {}).get("organic", []) if isinstance(result.get("raw"), dict) else [])
            if isinstance(item, dict) and item.get("link")
        }
        summaries.append({
            "series_id": f"s{current_series_id:03d}", "candidate_id": last["candidate_id"],
            "time_window": last["time_window"], "pages_requested": len(rows),
            "unique_urls_in_series": len(urls), "stop_reason": stop_reason,
        })
    metrics = persist(output, completed, completed, started, allow_existing_output=True)
    (output / "series_summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "run_state.json").write_text(json.dumps({
        "status": "completed", "started_at": started.isoformat(),
        "finished_at": metrics["run_finished_at"], "selected_candidates": len(catalog),
        "query_window_series": len(summaries), "pages_requested": len(completed),
        "new_pages_this_invocation": len(new_results),
        "key_material_written": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output), "selected_candidates": len(catalog),
        "query_window_series": len(summaries), "pages_requested": len(completed),
        "new_pages_this_invocation": len(new_results), **metrics,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
