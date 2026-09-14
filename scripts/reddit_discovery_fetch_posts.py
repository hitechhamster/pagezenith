"""Fetch real Reddit post metadata for a completed discovery run.

This is a local research utility. It reads the deduplicated URLs produced by
``reddit_discovery_full.py`` and enriches them through Arctic Shift. Progress is
append-only so an interrupted run can resume without repeating finished work.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


POST_ID_RE = re.compile(r"reddit\.com/r/[^/]+/comments/([a-z0-9]+)", re.I)
DEFAULT_BASE_URL = "https://arctic-shift.photon-reddit.com"
USER_AGENT = "web:pagezenith:1.0 (local cross-border product research)"
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path, help="Full discovery run directory")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=45)
    return parser.parse_args()


def jsonl_rows(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_url_provenance(path: Path) -> tuple[dict[str, dict], list[dict]]:
    posts: dict[str, dict] = {}
    malformed: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            canonical_url = row.get("canonical_url", "")
            match = POST_ID_RE.search(canonical_url)
            if not match:
                malformed.append(row)
                continue
            post_id = match.group(1).lower()
            entry = posts.setdefault(
                post_id,
                {
                    "post_id": post_id,
                    "canonical_url": canonical_url,
                    "query_ids": set(),
                    "signal_types": set(),
                    "time_windows": set(),
                    "serper_subreddits": set(),
                },
            )
            entry["query_ids"].add(row.get("query_id", ""))
            entry["signal_types"].add(row.get("signal_type", ""))
            entry["time_windows"].add(row.get("time_window", ""))
            entry["serper_subreddits"].add(row.get("subreddit_normalized", ""))
    return posts, malformed


def load_or_create_salt(path: Path) -> bytes:
    if path.exists():
        return path.read_text(encoding="ascii").strip().encode("ascii")
    salt = os.urandom(24).hex()
    path.write_text(salt, encoding="ascii")
    return salt.encode("ascii")


def author_hash(author: object, salt: bytes) -> str:
    value = str(author or "").strip().lower()
    if not value or value in {"[deleted]", "automoderator"}:
        return ""
    return hashlib.sha256(salt + b":" + value.encode("utf-8")).hexdigest()[:20]


def request_batch(
    base_url: str,
    ids: list[str],
    timeout: int,
    retries: int,
) -> list[dict]:
    # The /api/posts/ids endpoint rejects the otherwise-supported `fields`
    # parameter. Keep batches small and normalize the full response locally.
    params = urllib.parse.urlencode({"ids": ",".join(ids)})
    url = f"{base_url.rstrip('/')}/api/posts/ids?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = payload.get("data")
            if not isinstance(data, list):
                raise ValueError("Arctic Shift response did not contain a data list")
            return [row for row in data if isinstance(row, dict)]
        except urllib.error.HTTPError as exc:
            # One unavailable/malformed ID can make the whole endpoint return
            # 400. Split only that batch until the exact ID is isolated.
            if exc.code == 400 and len(ids) > 1:
                middle = len(ids) // 2
                return request_batch(base_url, ids[:middle], timeout, retries) + request_batch(
                    base_url, ids[middle:], timeout, retries
                )
            if exc.code == 400:
                return []
            last_error = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + random.random())
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + random.random())
    raise RuntimeError(f"batch failed after {retries} attempts: {last_error}")


def normalize_post(raw: dict, provenance: dict, salt: bytes) -> dict:
    created = raw.get("created_utc")
    created_iso = ""
    if isinstance(created, (int, float)):
        created_iso = datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
    permalink = str(raw.get("permalink") or "")
    canonical_url = provenance["canonical_url"]
    if permalink.startswith("/"):
        canonical_url = "https://www.reddit.com" + permalink
    return {
        "post_id": provenance["post_id"],
        "canonical_url": canonical_url,
        "title": str(raw.get("title") or "").strip(),
        "selftext": str(raw.get("selftext") or "").strip(),
        "subreddit": str(raw.get("subreddit") or "").strip(),
        "created_utc": created,
        "created_iso": created_iso,
        "score": int(raw.get("score") or 0),
        "num_comments": int(raw.get("num_comments") or 0),
        "subreddit_subscribers": int(raw.get("subreddit_subscribers") or 0),
        "over_18": bool(raw.get("over_18")),
        "removed_by_category": str(raw.get("removed_by_category") or ""),
        "author_hash": author_hash(raw.get("author"), salt),
        "query_ids": sorted(x for x in provenance["query_ids"] if x),
        "signal_types": sorted(x for x in provenance["signal_types"] if x),
        "time_windows": sorted(x for x in provenance["time_windows"] if x),
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    input_path = run_dir / "urls_normalized.csv"
    if not input_path.exists():
        raise SystemExit(f"missing input: {input_path}")

    output_path = run_dir / "post_metadata.jsonl"
    errors_path = run_dir / "post_metadata_errors.jsonl"
    salt_path = run_dir / ".author_hash_salt"
    provenance, malformed = load_url_provenance(input_path)
    salt = load_or_create_salt(salt_path)

    completed = {
        str(row.get("post_id", "")).lower()
        for row in (jsonl_rows(output_path) or [])
        if row.get("post_id")
    }
    pending = [post_id for post_id in sorted(provenance) if post_id not in completed]
    print(
        json.dumps(
            {
                "discovered_ids": len(provenance),
                "already_completed": len(completed),
                "pending": len(pending),
                "malformed_url_rows": len(malformed),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    found_now = 0
    missing_now = 0
    failed_batches = 0
    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset : offset + args.batch_size]
        try:
            raw_posts = request_batch(DEFAULT_BASE_URL, batch, args.timeout, args.retries)
            by_id = {str(row.get("id") or "").lower(): row for row in raw_posts}
            for post_id in batch:
                raw = by_id.get(post_id)
                if raw is None:
                    append_jsonl(
                        errors_path,
                        {"post_id": post_id, "kind": "not_returned", "retryable": True},
                    )
                    missing_now += 1
                    continue
                append_jsonl(output_path, normalize_post(raw, provenance[post_id], salt))
                found_now += 1
        except Exception as exc:  # preserve batch for a later resume
            append_jsonl(
                errors_path,
                {"post_ids": batch, "kind": "batch_error", "error": str(exc), "retryable": True},
            )
            failed_batches += 1
        done = min(offset + len(batch), len(pending))
        print(
            f"progress {done}/{len(pending)}; found={found_now}; "
            f"missing={missing_now}; failed_batches={failed_batches}",
            flush=True,
        )

    final_rows = list(jsonl_rows(output_path) or [])
    final_ids = {str(row.get("post_id", "")).lower() for row in final_rows}
    unresolved = sorted(set(provenance) - final_ids)
    subreddit_counts = Counter(row.get("subreddit", "") for row in final_rows if row.get("subreddit"))
    signal_counts: Counter[str] = Counter()
    for row in final_rows:
        signal_counts.update(row.get("signal_types") or [])

    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_unique_post_ids": len(provenance),
        "fetched_posts": len(final_rows),
        "unresolved_post_ids": len(unresolved),
        "malformed_url_rows": len(malformed),
        "unique_subreddits": len(subreddit_counts),
        "over_18_posts": sum(bool(row.get("over_18")) for row in final_rows),
        "removed_posts": sum(bool(row.get("removed_by_category")) for row in final_rows),
        "empty_body_posts": sum(not str(row.get("selftext") or "").strip() for row in final_rows),
        "signal_type_counts": dict(signal_counts.most_common()),
        "top_subreddits": subreddit_counts.most_common(30),
    }
    (run_dir / "post_metadata_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(
        run_dir / "post_metadata_unresolved.csv",
        [{"post_id": post_id, "canonical_url": provenance[post_id]["canonical_url"]} for post_id in unresolved],
        ["post_id", "canonical_url"],
    )
    write_csv(
        run_dir / "post_metadata_malformed_urls.csv",
        malformed,
        list(malformed[0].keys()) if malformed else ["canonical_url"],
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2), flush=True)
    return 0 if not failed_batches else 2


if __name__ == "__main__":
    raise SystemExit(main())
