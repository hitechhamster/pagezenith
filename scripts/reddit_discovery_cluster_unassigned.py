"""Discover natural topic groups inside posts not covered by the first taxonomy."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


STOP = set(
    "a an and are as at be been being but by can could did do does doing for from get gets getting got "
    "had has have having he her here hers herself him himself his how i if in into is it its itself just "
    "me more most my myself no nor not of off on once only or other our ours ourselves out over own same "
    "she should so some such than that the their theirs them themselves then there these they this those "
    "through to too under up very was we were what when where which while who why will with would you your "
    "yours yourself yourselves reddit anyone any does anyone know help need needed looking want trying question "
    "new old one two first best good bad really still use used using buy bought buying find found available "
    "replacement replacements replace replaced replacing part parts spare stock out broken broke repair repairs "
    "fixed fix fixing issue problem way thing things make made making work works working time years year thanks "
    "advice please like also even much day days today now ever since without due whole full entire cost price "
    "cheap expensive shipping order ordered company manufacturer brand model product products item items".split()
)

TOKEN_RE = re.compile(r"[a-z][a-z0-9]{2,}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--clusters", type=int, default=22)
    parser.add_argument("--vocab", type=int, default=4500)
    return parser.parse_args()


def load_cards(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("excluded") and row.get("sector") == "待归类":
                rows.append(row)
    return rows


def tokens(row: dict) -> list[str]:
    title = str(row.get("title") or "").lower()
    excerpt = str(row.get("evidence_excerpt") or "").lower()
    subreddit = re.sub(r"[^a-z0-9]", "", str(row.get("subreddit") or "").lower())
    words = [token for token in TOKEN_RE.findall(f"{title} {title} {excerpt}") if token not in STOP]
    if subreddit:
        words.extend([f"sub_{subreddit}"] * 3)
    return words


def vectorize(rows: list[dict], max_vocab: int) -> tuple[np.ndarray, list[str]]:
    docs = [tokens(row) for row in rows]
    df: Counter[str] = Counter()
    corpus: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
        corpus.update(doc)
    n_docs = len(docs)
    candidates = [
        term for term, _ in corpus.most_common()
        if 3 <= df[term] <= max(4, int(n_docs * 0.22))
    ][:max_vocab]
    vocab = {term: index for index, term in enumerate(candidates)}
    matrix = np.zeros((n_docs, len(candidates)), dtype=np.float32)
    for row_index, doc in enumerate(docs):
        counts = Counter(term for term in doc if term in vocab)
        if not counts:
            continue
        for term, count in counts.items():
            tf = 1.0 + math.log(count)
            idf = math.log((1.0 + n_docs) / (1.0 + df[term])) + 1.0
            matrix[row_index, vocab[term]] = tf * idf
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= np.maximum(norms, 1e-8)
    return matrix, candidates


def initialize_centers(matrix: np.ndarray, k: int) -> np.ndarray:
    rng = np.random.default_rng(20260914)
    first = int(rng.integers(0, matrix.shape[0]))
    centers = [matrix[first].copy()]
    closest = matrix @ centers[0]
    for _ in range(1, k):
        distances = np.maximum(0.0, 1.0 - closest) ** 2
        total = float(distances.sum())
        if total <= 1e-8:
            index = int(rng.integers(0, matrix.shape[0]))
        else:
            index = int(rng.choice(matrix.shape[0], p=distances / total))
        centers.append(matrix[index].copy())
        closest = np.maximum(closest, matrix @ centers[-1])
    return np.stack(centers)


def spherical_kmeans(matrix: np.ndarray, k: int, iterations: int = 35) -> tuple[np.ndarray, np.ndarray]:
    centers = initialize_centers(matrix, k)
    labels = np.zeros(matrix.shape[0], dtype=np.int32)
    for _ in range(iterations):
        similarities = matrix @ centers.T
        new_labels = similarities.argmax(axis=1).astype(np.int32)
        if np.array_equal(new_labels, labels) and _ > 0:
            break
        labels = new_labels
        new_centers = np.zeros_like(centers)
        for cluster_id in range(k):
            members = matrix[labels == cluster_id]
            if not len(members):
                new_centers[cluster_id] = matrix[int(np.argmin(similarities.max(axis=1)))]
                continue
            center = members.mean(axis=0)
            norm = np.linalg.norm(center)
            new_centers[cluster_id] = center / max(norm, 1e-8)
        centers = new_centers
    return labels, centers


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    rows = load_cards(run_dir / "demand_cards_preliminary.jsonl")
    matrix, terms = vectorize(rows, args.vocab)
    k = min(args.clusters, max(2, len(rows) // 20))
    labels, centers = spherical_kmeans(matrix, k)
    similarity = np.sum(matrix * centers[labels], axis=1)

    grouped: dict[int, list[int]] = defaultdict(list)
    for index, label in enumerate(labels.tolist()):
        grouped[label].append(index)

    cluster_summaries = []
    for cluster_id, indices in grouped.items():
        center = centers[cluster_id]
        top_term_indices = np.argsort(center)[::-1][:14]
        top_terms = [terms[index] for index in top_term_indices if center[index] > 0]
        subreddit_counts = Counter(rows[index].get("subreddit", "") for index in indices)
        need_counts = Counter(label for index in indices for label in rows[index].get("need_labels", []))
        ranked_indices = sorted(
            indices,
            key=lambda index: (similarity[index], rows[index].get("num_comments", 0)),
            reverse=True,
        )
        cluster_summaries.append(
            {
                "cluster_id": cluster_id,
                "posts": len(indices),
                "mean_similarity": round(float(np.mean(similarity[indices])), 4),
                "top_terms": top_terms,
                "top_subreddits": subreddit_counts.most_common(10),
                "top_needs": need_counts.most_common(5),
                "examples": [
                    {
                        "post_id": rows[index].get("post_id"),
                        "title": rows[index].get("title"),
                        "subreddit": rows[index].get("subreddit"),
                        "url": rows[index].get("canonical_url"),
                        "similarity": round(float(similarity[index]), 4),
                    }
                    for index in ranked_indices[:8]
                ],
            }
        )
    cluster_summaries.sort(key=lambda item: (-item["posts"], item["cluster_id"]))

    (run_dir / "unassigned_topic_clusters.json").write_text(
        json.dumps(cluster_summaries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 待归类帖子的自然主题簇",
        "",
        "> 这些簇只用于发现漏掉的板块和噪声类型，簇号没有业务含义。",
        "",
    ]
    for cluster in cluster_summaries:
        terms_text = "、".join(cluster["top_terms"][:10])
        subs_text = "、".join(f"r/{name}({count})" for name, count in cluster["top_subreddits"][:6])
        lines.extend(
            [
                f"## 簇 {cluster['cluster_id']} · {cluster['posts']} 帖",
                "",
                f"- 特征词：{terms_text}",
                f"- 主要社区：{subs_text}",
                "- 代表帖子：",
                "",
            ]
        )
        for example in cluster["examples"][:6]:
            lines.append(
                f"  - [{example['title']}]({example['url']}) — r/{example['subreddit']}"
            )
        lines.append("")
    (run_dir / "unassigned_topic_clusters.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"posts": len(rows), "vocabulary": len(terms), "clusters": k}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
