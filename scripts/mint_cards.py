"""造卡脚本。明文卡号只在这一刻出现一次 —— 库里只存 sha256。

    python scripts/mint_cards.py --count 20 --credits 60 --batch 2026-09-first --label 标准卡
    python scripts/mint_cards.py --topup PZ-XXXX-XXXX-XXXX --credits 60     # 给老卡续点

输出的 CSV 直接传发卡平台当库存。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
from billing import store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--credits", type=int, required=True)
    ap.add_argument("--batch", default=time.strftime("%Y%m%d"))
    ap.add_argument("--label", default="")
    ap.add_argument("--topup", default="", help="给已有卡加点(传明文卡号)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if a.topup:
        ok = store.top_up(store.hash_card(a.topup), a.credits)
        print(f"{'已加点' if ok else '卡号不存在'}: {a.topup} +{a.credits}")
        return

    keys = store.mint(a.count, a.credits, batch=a.batch, label=a.label)
    lines = ["card_key,credits,batch,label"] + [f"{k},{a.credits},{a.batch},{a.label}" for k in keys]
    out = a.out or f"cards-{a.batch}-{a.credits}pt-{a.count}.csv"
    pathlib.Path(out).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(keys))
    print(f"\n共 {len(keys)} 张 x {a.credits} 点 → {out}")
    print("⚠️ 这些明文卡号只在此刻存在，库里只有哈希。发完请妥善保管/删除该 CSV。")


if __name__ == "__main__":
    main()
