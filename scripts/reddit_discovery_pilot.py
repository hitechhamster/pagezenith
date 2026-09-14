"""Run and persist the first Reddit × Serper physical-product discovery pilot.

This is deliberately a discovery-only runner.  It does not scrape Reddit pages,
profile authors, or decide whether a result is a viable product.  It persists the
search evidence required for the next verification stage:

    24 high-signal phrases × 2 time windows = 48 Google/Serper queries.

Usage on the production host (where SERPER_KEYS is configured):

    cd /srv/pagezenith
    .venv/bin/python scripts/reddit_discovery_pilot.py

Output defaults to data/reddit-discovery/<UTC run id>/, which is intentionally
under the already-ignored data/ directory.  API keys are never written to output.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


REPO = Path(__file__).resolve().parents[1]
API = REPO / "api"
if str(API) not in sys.path:
    sys.path.insert(0, str(API))

from tools.seo_gap.clients import serper_pool  # noqa: E402
from tools.seo_gap.config import get_settings  # noqa: E402


SIGNALS: tuple[tuple[str, str], ...] = (
    ("buy_intent", "looking for a replacement"),
    ("buy_intent", "where can I buy a"),
    ("buy_intent", "recommend an alternative"),
    ("buy_intent", "need a better"),
    ("gap", "wish someone made"),
    ("gap", "can't find a"),
    ("gap", "there should be a"),
    ("gap", "why doesn't anyone make"),
    ("fit_failure", "doesn't fit properly"),
    ("fit_failure", "won't fit"),
    ("fit_failure", "not compatible with"),
    ("fit_failure", "hard to install"),
    ("quality_failure", "keeps breaking"),
    ("quality_failure", "falls apart"),
    ("quality_failure", "poorly designed"),
    ("quality_failure", "stopped working"),
    ("workaround", "made my own"),
    ("workaround", "DIY solution"),
    ("workaround", "used zip ties"),
    ("workaround", "3D printed"),
    ("price_availability", "too expensive for"),
    ("price_availability", "overpriced for what"),
    ("price_availability", "out of stock everywhere"),
    ("price_availability", "custom made"),
)

SUBREDDIT_RE = re.compile(r"^https?://(?:www\.)?reddit\.com/r/([^/?#]+)", re.I)


def canonical_url(value: str) -> str:
    """Drop query/fragment and the trailing slash, retaining original evidence elsewhere."""
    try:
        parts = urlsplit(value)
        path = parts.path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
    except ValueError:
        return value.strip()


def subreddit_from_url(value: str) -> str:
    match = SUBREDDIT_RE.match(value or "")
    return match.group(1).lower() if match else ""


def windows(today: date) -> tuple[dict[str, str], dict[str, str]]:
    """The dates match the stated D-30 and D-31..D-90 pilot windows."""
    current_after = today - timedelta(days=30)
    active_after = today - timedelta(days=90)
    return (
        {"time_window": "current", "after_date": current_after.isoformat(), "before_date": ""},
        {"time_window": "active", "after_date": active_after.isoformat(),
         "before_date": current_after.isoformat()},
    )


def build_plan(today: date) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for signal_type, phrase in SIGNALS:
        for window in windows(today):
            query = f'site:reddit.com/r/ "{phrase}" after:{window["after_date"]}'
            if window["before_date"]:
                query += f' before:{window["before_date"]}'
            out.append({
                "query_id": f"q{len(out) + 1:03d}",
                "query_text": query,
                "signal_type": signal_type,
                "signal_phrase": phrase,
                "time_window": window["time_window"],
                "after_date": window["after_date"],
                "before_date": window["before_date"],
                "phase": "pilot_discovery",
                "status": "planned",
            })
    return out


async def run_query(client: httpx.AsyncClient, query: dict[str, str], semaphore: asyncio.Semaphore) -> dict[str, Any]:
    settings = get_settings()
    async with semaphore:
        try:
            response = await serper_pool.post(client, settings, f"{settings.serper_base_url}/search", {
                "q": query["query_text"], "gl": "us", "hl": "en", "num": 10,
            })
            raw: dict[str, Any]
            try:
                raw = response.json()
            except ValueError:
                raw = {"parse_error": True, "body_preview": (response.text or "")[:500]}
            organic = raw.get("organic") if isinstance(raw, dict) else []
            if not isinstance(organic, list):
                organic = []
            return {**query, "status": "completed" if response.is_success else "http_error",
                    "http_status": response.status_code, "organic_count": len(organic), "raw": raw}
        except Exception as exc:  # noqa: BLE001 - individual failures must not lose the run
            return {**query, "status": "error", "http_status": 0, "organic_count": 0,
                    "raw": {"error": str(exc)[:500]}}


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def persist(output: Path, planned: list[dict[str, str]], completed: list[dict[str, Any]], started: datetime) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    ended = datetime.now(timezone.utc)
    raw_path = output / "serper_raw_results.jsonl"
    with raw_path.open("w", encoding="utf-8") as handle:
        for row in completed:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    plan_by_id = {row["query_id"]: row for row in completed}
    plan_rows = [{**row, "status": plan_by_id.get(row["query_id"], {}).get("status", "planned")}
                 for row in planned]
    write_csv(output / "query_plan.csv", [
        "query_id", "query_text", "signal_type", "signal_phrase", "time_window",
        "after_date", "before_date", "phase", "status",
    ], plan_rows)

    url_rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    subreddit_queries: dict[str, set[str]] = {}
    subreddit_urls: dict[str, set[str]] = {}
    subreddit_signals: dict[str, Counter[str]] = {}
    for row in completed:
        organic = row.get("raw", {}).get("organic", []) if isinstance(row.get("raw"), dict) else []
        for rank, item in enumerate(organic, start=1):
            if not isinstance(item, dict):
                continue
            original = str(item.get("link") or "").strip()
            canon = canonical_url(original)
            subreddit = subreddit_from_url(canon)
            if not canon:
                continue
            url_rows.append({
                "query_id": row["query_id"], "canonical_url": canon, "original_url": original,
                "subreddit_normalized": subreddit, "rank": rank,
                "title": item.get("title") or "", "snippet": item.get("snippet") or "",
                "serper_date": item.get("date") or "", "signal_type": row["signal_type"],
                "time_window": row["time_window"],
            })
            if subreddit:
                subreddit_queries.setdefault(subreddit, set()).add(row["query_id"])
                subreddit_urls.setdefault(subreddit, set()).add(canon)
                subreddit_signals.setdefault(subreddit, Counter())[row["signal_type"]] += 1
            seen_urls.add(canon)

    write_csv(output / "urls_normalized.csv", [
        "query_id", "canonical_url", "original_url", "subreddit_normalized", "rank", "title", "snippet",
        "serper_date", "signal_type", "time_window",
    ], url_rows)
    write_csv(output / "signal_posts.csv", [
        "query_id", "canonical_url", "subreddit_normalized", "title", "snippet", "serper_date",
        "reddit_created_utc", "signal_type", "is_physical_problem", "exclusion_reason", "confidence",
        "first_seen_at",
    ], [{
        "query_id": r["query_id"], "canonical_url": r["canonical_url"],
        "subreddit_normalized": r["subreddit_normalized"], "title": r["title"], "snippet": r["snippet"],
        "serper_date": r["serper_date"], "reddit_created_utc": "", "signal_type": r["signal_type"],
        "is_physical_problem": "unknown", "exclusion_reason": "", "confidence": "",
        "first_seen_at": started.isoformat(),
    } for r in url_rows])

    community_rows = []
    for name in sorted(subreddit_urls, key=lambda item: (-len(subreddit_urls[item]), item)):
        signal_counts = subreddit_signals[name]
        strong = sum(signal_counts[k] for k in ("gap", "workaround", "fit_failure", "quality_failure"))
        total = sum(signal_counts.values())
        community_rows.append({
            "subreddit_normalized": name,
            "first_seen_at": started.isoformat(), "last_signal_at": ended.isoformat(),
            "signal_posts_30d": "", "signal_posts_90d": "", "active_months_12m": "",
            "strong_signal_ratio": round(strong / total, 3) if total else 0,
            "unique_url_count": len(subreddit_urls[name]),
            "query_hit_count": len(subreddit_queries[name]),
            "discovery_score": "", "status": "discovered_unverified",
            "notes": "Serper discovery only; requires Reddit API time and activity validation.",
        })
    write_csv(output / "subreddit_candidates.csv", [
        "subreddit_normalized", "first_seen_at", "last_signal_at", "signal_posts_30d", "signal_posts_90d",
        "active_months_12m", "strong_signal_ratio", "unique_url_count", "query_hit_count",
        "discovery_score", "status", "notes",
    ], community_rows)

    metrics = {
        "queries_planned": len(planned), "queries_completed": sum(r["status"] == "completed" for r in completed),
        "query_errors": sum(r["status"] != "completed" for r in completed),
        "raw_urls": len(url_rows), "unique_canonical_urls": len(seen_urls),
        "unique_subreddits": len(subreddit_urls),
        "mean_organic_results_per_query": round(sum(int(r["organic_count"]) for r in completed) / len(completed), 2) if completed else 0,
        "run_started_at": started.isoformat(), "run_finished_at": ended.isoformat(),
        "scope": "Discovery only; no Reddit page/API validation and no physical-product classification yet.",
    }
    (output / "run_metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "run_manifest.json").write_text(json.dumps({
        "run": output.name, "gl": "us", "hl": "en", "results_per_query": 10,
        "signals": len(SIGNALS), "windows": ["current", "active"], "metrics_file": "run_metrics.json",
        "key_material_written": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return metrics


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--today", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    settings = get_settings()
    if not serper_pool.has_key(settings):
        raise SystemExit("SERPER_KEYS / SERPER_KEY is not configured.")

    started = datetime.now(timezone.utc)
    planned = build_plan(args.today)
    output = args.output or (REPO / "data" / "reddit-discovery" / started.strftime("%Y%m%dT%H%M%SZ"))
    if output.exists():
        raise SystemExit(f"Output directory already exists: {output}")
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=settings.request_timeout, trust_env=False,
                                 proxy=settings.proxy_for("serper")) as client:
        completed = await asyncio.gather(*(run_query(client, row, semaphore) for row in planned))
    metrics = persist(output, planned, completed, started)
    print(json.dumps({"output": str(output), **metrics}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
