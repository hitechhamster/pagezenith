"""Fetch a resumable, stratified comment sample for relevance validation."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


BASE_URL = "https://arctic-shift.photon-reddit.com"
USER_AGENT = "web:pagezenith:1.0 (local cross-border product research)"
WRITE_LOCK = threading.Lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--limit", type=int, default=320)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--comments-per-post", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=40)
    parser.add_argument("--retries", type=int, default=4)
    return parser.parse_args()


def read_queue(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_completed(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed = set()
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if row.get("post_id"):
                    completed.add(str(row["post_id"]).lower())
    return completed


def round_robin(rows: list[dict], count: int) -> list[dict]:
    by_sector: dict[str, deque] = defaultdict(deque)
    for row in rows:
        by_sector[row.get("sector", "待归类")].append(row)
    groups = sorted(by_sector.values(), key=len, reverse=True)
    selected = []
    while groups and len(selected) < count:
        remaining = []
        for group in groups:
            if group and len(selected) < count:
                selected.append(group.popleft())
            if group:
                remaining.append(group)
        groups = remaining
    return selected


def select_sample(rows: list[dict], limit: int) -> list[dict]:
    eligible = [row for row in rows if row.get("tier") in {"A", "B", "C"}]
    if limit <= 0 or limit >= len(eligible):
        return eligible
    target_c = min(sum(row["tier"] == "C" for row in eligible), max(1, round(limit * 0.20)))
    target_a = min(sum(row["tier"] == "A" for row in eligible), max(1, round(limit * 0.40)))
    target_b = limit - target_a - target_c
    selected = []
    selected.extend(round_robin([row for row in eligible if row["tier"] == "A"], target_a))
    selected.extend(round_robin([row for row in eligible if row["tier"] == "B"], target_b))
    selected.extend(round_robin([row for row in eligible if row["tier"] == "C"], target_c))
    return selected


def hash_author(author: object, salt: bytes) -> str:
    value = str(author or "").strip().lower()
    if not value or value in {"[deleted]", "automoderator"}:
        return ""
    return hashlib.sha256(salt + b":" + value.encode("utf-8")).hexdigest()[:20]


def request_comments(post_id: str, limit: int, timeout: int, retries: int) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "link_id": post_id,
            "limit": limit,
            "fields": "body,score,author,created_utc,parent_id,id",
        }
    )
    request = urllib.request.Request(
        f"{BASE_URL}/api/comments/search?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = payload.get("data")
            return data if isinstance(data, list) else []
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in {400, 404}:
                return []
            wait = max(2**attempt, int(exc.headers.get("Retry-After", "0") or 0))
            if attempt + 1 < retries:
                time.sleep(wait + random.random())
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + random.random())
    raise RuntimeError(str(last_error))


def fetch_one(row: dict, salt: bytes, args: argparse.Namespace) -> dict:
    post_id = row["post_id"].lower()
    comments = request_comments(post_id, args.comments_per_post, args.timeout, args.retries)
    normalized = []
    for comment in comments:
        body = str(comment.get("body") or "").strip()
        if not body or body in {"[removed]", "[deleted]"}:
            continue
        normalized.append(
            {
                "comment_id": str(comment.get("id") or ""),
                "body": body[:2400],
                "score": int(comment.get("score") or 0),
                "created_utc": comment.get("created_utc"),
                "author_hash": hash_author(comment.get("author"), salt),
            }
        )
    return {
        "post_id": post_id,
        "tier": row.get("tier"),
        "sector": row.get("sector"),
        "subreddit": row.get("subreddit"),
        "title": row.get("title"),
        "canonical_url": row.get("canonical_url"),
        "expected_need_labels": row.get("post_need_labels"),
        "snippet_need_labels": row.get("snippet_need_labels"),
        "comments": normalized,
    }


def append_jsonl(path: Path, row: dict) -> None:
    with WRITE_LOCK:
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    output_path = run_dir / "comment_sample.jsonl"
    error_path = run_dir / "comment_sample_errors.jsonl"
    salt = (run_dir / ".author_hash_salt").read_text(encoding="ascii").strip().encode("ascii")
    sample = select_sample(read_queue(run_dir / "comment_fetch_queue.csv"), args.limit)
    completed = read_completed(output_path)
    pending = [row for row in sample if row["post_id"].lower() not in completed]
    print(
        json.dumps(
            {
                "sample": len(sample),
                "already_completed": len(sample) - len(pending),
                "pending": len(pending),
                "sample_tiers": dict(Counter(row["tier"] for row in sample)),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    success = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(fetch_one, row, salt, args): row for row in pending}
        for future in as_completed(futures):
            row = futures[future]
            try:
                append_jsonl(output_path, future.result())
                success += 1
            except Exception as exc:
                append_jsonl(
                    error_path,
                    {"post_id": row["post_id"], "error": str(exc), "retryable": True},
                )
                failed += 1
            if (success + failed) % 25 == 0 or success + failed == len(pending):
                print(
                    f"progress {success + failed}/{len(pending)}; success={success}; failed={failed}",
                    flush=True,
                )
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
