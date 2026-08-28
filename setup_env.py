"""把手头已有的 key 汇总进 pagezenith/.env（该文件已被 .gitignore 排除）。

    python setup_env.py            # 写入 / 更新
    python setup_env.py --check    # 只检查，不写

来源：
  GEMINI_API_KEY  ← D:\\GA4数据分析\\.env
  DEEPSEEK_API_KEY← D:\\GA4数据分析\\.env
  SERPER_KEY      ← D:\\WIKIFX项目\\寄生关键词（自动挑余额最高的一个）
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
for _k in list(os.environ):
    if "proxy" in _k.lower():
        del os.environ[_k]

import requests  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
GA4 = pathlib.Path(r"D:\GA4数据分析")
S = requests.Session()
S.trust_env = False
S.proxies = {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"}


def mask(k: str) -> str:
    return f"{k[:8]}…{k[-4:]}" if len(k) > 14 else "…"


def from_env(name: str) -> str:
    p = GA4 / ".env"
    if not p.exists():
        return ""
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.strip().startswith(name) and "=" in line:
            return line.split("=", 1)[1].strip()
    return ""


def best_serper() -> tuple[str, float]:
    base = pathlib.Path(r"D:\WIKIFX项目\寄生关键词")
    keys: list[str] = []
    for f in ("serper_seo_rank.py", "verify_expanded.py", "verify_platforms.py"):
        p = base / f
        if p.exists():
            for m in re.findall(r'["\']([0-9a-f]{40})["\']',
                                p.read_text(encoding="utf-8", errors="ignore")):
                if m not in keys:
                    keys.append(m)
    best, bb = "", -1.0
    for k in keys:
        try:
            r = S.get("https://google.serper.dev/account", headers={"X-API-KEY": k}, timeout=20)
            b = r.json().get("balance", -1) if r.status_code == 200 else -1
            if isinstance(b, (int, float)) and b > bb:
                best, bb = k, float(b)
        except Exception:  # noqa: BLE001
            pass
    return best, bb


def main() -> None:
    check_only = "--check" in sys.argv
    gem = from_env("GEMINI_API_KEY") or from_env("GOOGLE_API_KEY")
    ds = from_env("DEEPSEEK_API_KEY")
    serper, bal = best_serper()

    print("汇总到手的 key：")
    print(f"  GEMINI_API_KEY   {mask(gem) if gem else '❌ 缺'}")
    print(f"  DEEPSEEK_API_KEY {mask(ds) if ds else '❌ 缺'}")
    print(f"  SERPER_KEY       {mask(serper) if serper else '❌ 缺'}"
          + (f"（余额 {bal:.0f}）" if serper else ""))
    if check_only:
        return
    if not (gem and ds and serper):
        print("\n有 key 缺失，未写入。")
        return

    p = HERE / ".env"
    keep = []
    drop = {"GEMINI_API_KEY", "DEEPSEEK_API_KEY", "SERPER_KEY", "USE_MOCKS",
            "SERP_PROVIDER", "FETCH_MODE", "BILLING_DB", "EXA_KEY", "TAVILY_KEY",
            "OPENROUTER_API_KEY"}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and line.split("=", 1)[0].strip() in drop:
                continue
            keep.append(line)
    lines = [l for l in keep if l.strip()] + [
        "# ── 2026-08 卡密化后的服务端 key（本文件已被 .gitignore 排除）──",
        f"GEMINI_API_KEY={gem}",
        f"DEEPSEEK_API_KEY={ds}",
        f"SERPER_KEY={serper}",
        "USE_MOCKS=false",
        "SERP_PROVIDER=serper",
        "FETCH_MODE=httpx",
        f"BILLING_DB={(HERE / 'data' / 'billing.db').as_posix()}",
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ 已写入 {p}（gitignore 已覆盖，不会进仓库）")


if __name__ == "__main__":
    main()
