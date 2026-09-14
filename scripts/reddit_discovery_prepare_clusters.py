"""Build reviewable post cards and a preliminary sector x need matrix.

The output is deliberately descriptive. It does not score market opportunity or
claim market size. Sector and need labels are first-pass routing labels for the
next human/LLM review stage.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


SECTOR_RULES: dict[str, list[str]] = {
    "3D打印与创客制造": [
        "3dprint", "3d print", "bambulab", "prusa", "filament", "resin printer",
        "ender 3", "stl", "maker", "printed replacement", "printable part",
    ],
    "家电与清洁设备": [
        "appliance", "washing machine", "washer", "dryer", "dishwasher", "refrigerator",
        "fridge", "freezer", "vacuum", "dyson", "roomba", "air purifier", "dehumidifier",
        "humidifier", "microwave", "oven", "cleaning machine", "mop", "keurig",
    ],
    "家具、门窗与家居五金": [
        "ikea", "furniture", "cabinet", "drawer", "hinge", "door handle", "doorknob",
        "window", "blind", "curtain", "shelving", "bookshelf", "desk", "chair", "sofa", "couch",
        "mattress", "bed frame", "closet", "faucet", "toilet", "plumbing", "hvac",
        "garage door", "home improvement", "shower", "bathtub", "bathroom sink", "kitchen sink",
    ],
    "消费电子与电脑配件": [
        "laptop", "computer", "pcmasterrace", "keyboard", "mouse", "monitor", "usb",
        "charger", "charging port", "power supply", "motherboard", "asus", "lenovo",
        "dell", "macbook", "framework", "tablet", "ipad", "phone", "iphone", "android",
        "fairphone", "smartphone", "earbuds", "headphone", "sonyheadphones", "speaker",
        "bluetooth", "electronics", "cable", "connector", "remote control",
    ],
    "游戏硬件与掌机": [
        "steamdeck", "steam deck", "nintendo", "switch", "playstation", "xbox", "controller",
        "joystick", "gameboy", "3ds", "gaming handheld", "console repair", "oculus", "quest 3",
    ],
    "汽车、摩托与通用车件": [
        "mechanicadvice", "askmechanics", "car repair", "automotive", "motorcycle", "truck",
        "vehicle", "engine", "transmission", "alternator", "radiator", "brake", "bumper",
        "windshield", "toyota", "honda", "ford", "bmw", "mercedes", "tesla", "jeep",
        "volkswagen", "subaru", "mazda", "hyundai", "kia", "nissan", "car part",
    ],
    "自行车、电助力与小型出行": [
        "bicycle", "cycling", "bike", "ebike", "e-bike", "radpowerbikes", "scooter",
        "escooter", "e-scooter", "wheelchair", "mobility scooter", "unicycle",
    ],
    "工具、工坊与工业设备": [
        "power tool", "hand tool", "workshop", "machinist", "woodworking", "table saw",
        "drill", "lathe", "welder", "welding", "compressor", "generator", "dewalt",
        "milwaukee", "makita", "ryobi", "snapon", "harborfreight", "commercial equipment",
        "industrial", "cnc", "laser cutter", "forklift", "shop tool",
    ],
    "户外、露营与运动装备": [
        "camping", "backpacking", "hiking", "tent", "trekking pole", "outdoor gear", "kayak",
        "paddleboard", "fishing", "skiing", "snowboard", "golf", "tennis", "pickleball",
        "fitness", "treadmill", "exercise bike", "peloton", "helmet", "sports equipment",
    ],
    "摄影、影音与钟表器材": [
        "camera", "photography", "analogcommunity", "camera lens", "tripod", "gopro", "drone",
        "dji", "projector", "turntable", "audiophile", "vinyl", "amplifier", "seiko",
        "wristwatch", "watches", "watchmaking", "clock", "microphone", "recording studio",
    ],
    "咖啡、厨房与餐饮设备": [
        "espresso", "coffee", "grinder", "kitchen", "cookware", "air fryer", "instant pot",
        "blender", "mixer", "vitamix", "rice cooker", "restaurant", "foodservice", "bbq",
        "grill", "smoker", "knife", "water bottle", "thermos",
    ],
    "鞋服、箱包与纺织用品": [
        "boots", "shoe", "sneaker", "footwear", "clothing", "jacket", "zipper", "button",
        "sewing", "sewhelp", "fabric", "backpack", "luggage", "suitcase", "handbag",
        "leather", "belt", "glove", "helmet strap",
    ],
    "模型、遥控与收藏爱好": [
        "gunpla", "modelmakers", "model kit", "miniature", "warhammer", "lego", "action figure",
        "collectible", "rc car", "rccars", "remote controlled", "airsoft", "nerf", "slot car",
        "train model", "gundam", "boardgame", "tabletop",
    ],
    "母婴、个护与辅助用品": [
        "baby", "stroller", "car seat", "breast pump", "exclusivelypumping", "high chair",
        "diaper", "bottle warmer", "personal care", "shaver", "razor", "hair dryer",
        "electric toothbrush", "prosthetic", "hearing aid", "accessibility",
    ],
    "宠物用品": [
        "dog", "cat", "pet", "aquarium", "fish tank", "reptile", "bird cage", "litter box",
        "pet feeder", "leash", "collar", "cat tree",
    ],
    "庭院、园艺与泳池设备": [
        "gardening", "garden", "lawn", "mower", "trimmer", "chainsaw", "leaf blower", "pool",
        "hot tub", "sprinkler", "irrigation", "greenhouse", "pressure washer", "snowblower",
    ],
    "乐器与演出器材": [
        "guitar", "bass guitar", "piano", "keyboard instrument", "drum", "saxophone", "violin",
        "musical instrument", "guitar pedal", "synthesizer", "stage lighting", "dj equipment",
    ],
}


NEED_RULES: dict[str, dict[str, list[str]]] = {
    "零件买不到或断供": {
        "signals": ["replacement_availability"],
        "phrases": [
            "can't find replacement", "cannot find replacement", "no replacement part",
            "replacement parts unavailable", "discontinued part",
            "hard to find parts", "parts availability", "spare parts", "spare part",
            "replacement parts", "replacement part",
        ],
    },
    "只能更换总成或整机": {
        "signals": ["forced_bundle"],
        "phrases": [
            "whole assembly", "entire assembly", "replace the whole", "replace entire", "whole unit",
            "complete unit", "full assembly", "only sold as", "buy a new one", "replace everything",
        ],
    },
    "需要临时修复或定制替代件": {
        "signals": ["causal_workaround", "custom_demand"],
        "phrases": [
            "temporary fix", "workaround", "diy fix", "3d printed", "3d print a replacement",
            "made my own", "custom replacement", "fabricate", "jury rig", "jerry rig", "hack fix",
        ],
    },
    "产品不可维修或厂商不支持": {
        "signals": ["non_repair_gap", "oem_support"],
        "phrases": [
            "not repairable", "can't be repaired", "cannot be repaired", "no longer supported",
            "manufacturer won't", "manufacturer does not", "right to repair", "service center",
            "repairability", "planned obsolescence", "no support", "warranty replacement",
        ],
    },
    "同一部件反复损坏": {
        "signals": ["repeat_failure"],
        "phrases": [
            "keeps breaking", "keeps failing", "broke again", "failed again", "second time",
            "third time", "common failure", "known issue", "always breaks", "repeatedly breaks",
        ],
    },
    "难清洁、难维护或日常使用麻烦": {
        "signals": ["use_friction"],
        "phrases": [
            "hard to clean", "difficult to clean", "easy to clean", "cleanability", "can't clean",
            "cannot clean", "hard to maintain", "difficult to maintain", "maintenance nightmare",
            "takes forever to clean", "awkward to use", "annoying to use",
        ],
    },
    "跨境运费、税费或到手价过高": {
        "signals": ["landed_cost"],
        "phrases": [
            "shipping costs more", "shipping cost", "shipping is more", "import tax", "customs fee",
            "duty fee", "landed cost", "delivery fee", "postage", "cost to ship",
        ],
    },
    "需要商用级耐用性或稳定供货": {
        "signals": ["professional_use", "b2b_supply"],
        "phrases": [
            "commercial grade", "professional grade", "heavy duty", "daily use", "business use",
            "bulk order", "wholesale", "supplier", "distributor", "reliable supply", "downtime",
        ],
    },
    "耗材或易损件需要持续补充": {
        "signals": ["repeat_purchase"],
        "phrases": [
            "consumable", "refill", "replacement filter", "replacement blade", "replacement pad",
            "buy regularly", "keep buying", "subscription", "wear item",
        ],
    },
}


PHYSICAL_TERMS = [
    "part", "replacement", "repair", "fix", "broken", "broke", "plastic", "metal", "screw",
    "bolt", "hinge", "wheel", "motor", "battery", "cable", "connector", "filter", "handle",
    "assembly", "device", "machine", "equipment", "tool", "gear", "case", "cover", "mount",
]
DIGITAL_TERMS = [
    "software", "app ", "website", "api ", "account", "password", "subscription service",
    "video game bug", "server", "cloud service", "plugin", "browser extension", "codebase",
    "operating system", "windows update", "firmware update", "download", "streaming service",
]
HIGH_RISK_TERMS = [
    "firearm", "gun part", "ghost gun", "silencer", "suppressor", "rifle", "pistol",
    "ammunition", "ammo", "gundeals", "ar15", "controlled substance", "prescription drug",
    "medical diagnosis", "surgical implant", "abrathatfits", "lingerie",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def compact(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def searchable(row: dict) -> tuple[str, str, str]:
    title = compact(row.get("title")).lower()
    body = compact(row.get("selftext")).lower()
    subreddit = compact(row.get("subreddit")).lower()
    return title, body, subreddit


def contains_phrase(text: str, phrase: str) -> bool:
    """Match a complete word/phrase instead of accidental substrings.

    Reddit community names are often concatenated, so exact community aliases
    remain useful through rules such as `sonyheadphones` and `radpowerbikes`.
    """
    normalized = phrase.lower().strip()
    if not normalized:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(normalized) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


def phrase_score(phrase: str, title: str, body: str, subreddit: str) -> float:
    return (
        (3.0 if contains_phrase(title, phrase) else 0.0)
        + (1.0 if contains_phrase(body, phrase) else 0.0)
        + (2.0 if contains_phrase(subreddit, phrase) else 0.0)
    )


def classify_sector(row: dict) -> tuple[str, list[str], float]:
    title, body, subreddit = searchable(row)
    scores: dict[str, float] = {}
    for label, phrases in SECTOR_RULES.items():
        score = sum(phrase_score(phrase, title, body, subreddit) for phrase in phrases)
        if score:
            scores[label] = score
    if not scores:
        return "待归类", [], 0.0
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_label, top_score = ordered[0]
    secondary = [label for label, score in ordered[1:] if score >= max(3.0, top_score * 0.72)][:2]
    confidence = min(1.0, 0.35 + math.log1p(top_score) / 3.2)
    return top_label, secondary, round(confidence, 3)


def classify_needs(row: dict) -> tuple[list[str], dict[str, float]]:
    title, body, _ = searchable(row)
    signal_types = set(row.get("signal_types") or [])
    scores: dict[str, float] = {}
    for label, rule in NEED_RULES.items():
        direct = sum(
            (3.0 if contains_phrase(title, phrase) else 0.0)
            + (1.0 if contains_phrase(body, phrase) else 0.0)
            for phrase in rule["phrases"]
        )
        provenance = 1.0 if signal_types.intersection(rule["signals"]) else 0.0
        score = direct + provenance
        if score:
            scores[label] = score
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    labels = [label for label, score in ordered if score >= 1.0][:3]
    return labels, {label: round(score, 2) for label, score in ordered[:3]}


def exclusion(row: dict) -> tuple[bool, str]:
    title, body, subreddit = searchable(row)
    text = f"{title} {body} {subreddit}"
    if row.get("over_18"):
        return True, "成人内容"
    if any(term in text for term in HIGH_RISK_TERMS):
        return True, "高风险或受监管产品"
    if row.get("removed_by_category") and not body:
        return True, "正文已移除"
    physical_hits = sum(contains_phrase(text, term) for term in PHYSICAL_TERMS)
    digital_hits = sum(contains_phrase(text, term) for term in DIGITAL_TERMS)
    if digital_hits >= 2 and physical_hits == 0:
        return True, "明显为软件或数字服务问题"
    if not title:
        return True, "缺少标题"
    return False, ""


def evidence_excerpt(row: dict) -> str:
    title = compact(row.get("title"))
    body = compact(row.get("selftext"))
    if body in {"[removed]", "[deleted]"}:
        body = ""
    return (title + (" — " + body[:360] if body else ""))[:520]


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    posts = load_jsonl(run_dir / "post_metadata.jsonl")
    cards: list[dict] = []
    for row in posts:
        excluded, reason = exclusion(row)
        sector, secondary, sector_confidence = classify_sector(row)
        needs, need_scores = classify_needs(row)
        title, body, _ = searchable(row)
        direct_need = any(
            contains_phrase(title, phrase) or contains_phrase(body, phrase)
            for rule in NEED_RULES.values()
            for phrase in rule["phrases"]
        )
        if not needs and not excluded:
            needs = ["待判断需求形态"]
        cards.append(
            {
                "post_id": row.get("post_id", ""),
                "canonical_url": row.get("canonical_url", ""),
                "subreddit": row.get("subreddit", ""),
                "created_iso": row.get("created_iso", ""),
                "score": int(row.get("score") or 0),
                "num_comments": int(row.get("num_comments") or 0),
                "subreddit_subscribers": int(row.get("subreddit_subscribers") or 0),
                "author_hash": row.get("author_hash", ""),
                "time_windows": row.get("time_windows") or [],
                "signal_types": row.get("signal_types") or [],
                "sector": sector,
                "secondary_sectors": secondary,
                "sector_confidence": sector_confidence,
                "need_labels": needs,
                "need_scores": need_scores,
                "direct_need_language": direct_need,
                "excluded": excluded,
                "exclusion_reason": reason,
                "title": compact(row.get("title")),
                "evidence_excerpt": evidence_excerpt(row),
            }
        )

    cards_path = run_dir / "demand_cards_preliminary.jsonl"
    with cards_path.open("w", encoding="utf-8", newline="") as handle:
        for card in cards:
            handle.write(json.dumps(card, ensure_ascii=False) + "\n")

    included = [card for card in cards if not card["excluded"]]
    sectors: dict[str, list[dict]] = defaultdict(list)
    for card in included:
        sectors[card["sector"]].append(card)

    sector_rows = []
    for sector, rows in sorted(sectors.items(), key=lambda item: (-len(item[1]), item[0])):
        comments = [row["num_comments"] for row in rows]
        scores = [row["score"] for row in rows]
        need_counts = Counter(label for row in rows for label in row["need_labels"])
        window_counts = Counter(window for row in rows for window in row["time_windows"])
        sector_rows.append(
            {
                "sector": sector,
                "posts": len(rows),
                "direct_need_posts": sum(row["direct_need_language"] for row in rows),
                "unique_subreddits": len({row["subreddit"].lower() for row in rows if row["subreddit"]}),
                "unique_authors": len({row["author_hash"] for row in rows if row["author_hash"]}),
                "current_posts": window_counts["current"],
                "active_posts": window_counts["active"],
                "history_posts": window_counts["history"],
                "median_comments": round(statistics.median(comments), 1) if comments else 0,
                "p75_comments": round(percentile(comments, 0.75), 1),
                "median_score": round(statistics.median(scores), 1) if scores else 0,
                "top_need_labels": " | ".join(f"{label}:{count}" for label, count in need_counts.most_common(4)),
            }
        )
    write_csv(
        run_dir / "sector_summary_preliminary.csv",
        sector_rows,
        list(sector_rows[0].keys()) if sector_rows else ["sector"],
    )

    matrix_rows = []
    all_needs = list(NEED_RULES) + ["待判断需求形态"]
    for sector, rows in sorted(sectors.items(), key=lambda item: (-len(item[1]), item[0])):
        counts = Counter(label for row in rows for label in row["need_labels"])
        matrix_rows.append({"sector": sector, "posts": len(rows), **{need: counts[need] for need in all_needs}})
    write_csv(
        run_dir / "sector_need_matrix_preliminary.csv",
        matrix_rows,
        ["sector", "posts", *all_needs],
    )

    exclusion_counts = Counter(card["exclusion_reason"] for card in cards if card["excluded"])
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_posts": len(cards),
        "included_posts": len(included),
        "excluded_posts": len(cards) - len(included),
        "excluded_reasons": dict(exclusion_counts),
        "sector_counts": {row["sector"]: row["posts"] for row in sector_rows},
        "unclassified_sector_posts": len(sectors.get("待归类", [])),
        "direct_need_language_posts": sum(card["direct_need_language"] for card in included),
        "need_counts": dict(Counter(label for card in included for label in card["need_labels"])),
        "notes": [
            "Counts describe this Serper-discovered sample, not all of Reddit.",
            "Sector labels are preliminary routing labels, not validated markets.",
            "Need labels use post text plus query provenance and require sample review.",
        ],
    }
    (run_dir / "cluster_preparation_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    review_lines = [
        "# Reddit 样本初步分板块（待复核）",
        "",
        "> 这里只描述当前 Serper 发现并由 Reddit 元数据核实的样本，不代表 Reddit 全站，",
        "> 也不把板块或需求频次直接解释为市场机会。",
        "",
        f"- 输入帖子：{len(cards):,}",
        f"- 初筛保留：{len(included):,}",
        f"- 初筛排除：{len(cards) - len(included):,}",
        f"- 暂未分到产品板块：{len(sectors.get('待归类', [])):,}",
        "",
        "## 板块概览",
        "",
        "| 初步板块 | 帖子 | 明确需求措辞 | 社区数 | 当前/活跃/历史 | 主要需求形态 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for summary in sector_rows:
        review_lines.append(
            f"| {summary['sector']} | {summary['posts']} | {summary['direct_need_posts']} | "
            f"{summary['unique_subreddits']} | {summary['current_posts']}/{summary['active_posts']}/"
            f"{summary['history_posts']} | {summary['top_need_labels']} |"
        )

    review_lines.extend(["", "## 每个板块的代表帖子", ""])
    for summary in sector_rows:
        sector = summary["sector"]
        if sector == "待归类":
            continue
        rows = sorted(
            sectors[sector],
            key=lambda row: (row["direct_need_language"], row["num_comments"], row["score"]),
            reverse=True,
        )[:4]
        review_lines.append(f"### {sector}")
        review_lines.append("")
        for row in rows:
            needs = "、".join(row["need_labels"])
            review_lines.append(
                f"- [{row['title']}]({row['canonical_url']}) — r/{row['subreddit']}；"
                f"{row['score']} 分 / {row['num_comments']} 评论；{needs}"
            )
        review_lines.append("")

    (run_dir / "cluster_review_preliminary.md").write_text("\n".join(review_lines), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
