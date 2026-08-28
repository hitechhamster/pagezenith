"""探测 Gemini 原生文生图：确认 API 形状 + 实测一张图的真实成本。

    /srv/pagezenith/.venv/bin/python scripts/probe_image_cost.py [模型名...]

为什么要跑：配图定价（2→3 还是 4 点）取决于真实成本，不能拍脑袋。
顺便确认回包结构，generate_image() 改写要照着它来。

⚠️ 会真的花钱（每个模型一张图）。
"""
from __future__ import annotations

import base64
import json
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "work" / "imgprobe"
DEFAULT_MODELS = ["gemini-3-pro-image", "gemini-3.1-flash-image"]

PROMPT = ("A clean editorial photograph of a compact home espresso machine on a "
          "kitchen counter, morning light, shallow depth of field. "
          "Professional, clean composition suitable for a blog article. "
          "Wide horizontal aspect ratio (16:9). No text overlays.")


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in (ROOT / ".env", ROOT / "api" / ".env"):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    out.setdefault(k.strip(), v.strip())
    return out


def probe(model: str, key: str, proxy: str | None) -> None:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent")
    payload = {
        "contents": [{"parts": [{"text": PROMPT}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    t0 = time.time()
    try:
        with httpx.Client(proxy=proxy, timeout=300, trust_env=False) as c:
            r = c.post(url, params={"key": key}, json=payload)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 请求失败：{str(exc)[:200]}")
        return
    dt = time.time() - t0
    if r.status_code != 200:
        print(f"  ✗ HTTP {r.status_code}: {r.text[:300]}")
        return

    data = r.json()
    # 回包结构：candidates[0].content.parts[] 里混着 text 和 inlineData
    png = None
    mime = ""
    for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
        blob = part.get("inlineData") or part.get("inline_data")
        if blob and blob.get("data"):
            png = base64.b64decode(blob["data"])
            mime = blob.get("mimeType") or blob.get("mime_type") or ""
            break

    um = data.get("usageMetadata", {})
    print(f"  耗时 {dt:.0f}s | HTTP 200 | mime={mime or '(无)'} | "
          f"图 {len(png)/1024:.0f} KB" if png else f"  耗时 {dt:.0f}s | 回包里没有图")
    print(f"  usageMetadata: {json.dumps(um, ensure_ascii=False)}")

    if png:
        OUT.mkdir(parents=True, exist_ok=True)
        f = OUT / f"{model}.png"
        f.write_bytes(png)
        print(f"  已存 {f}")


def main() -> None:
    e = env()
    key = e.get("GEMINI_API_KEY") or e.get("GOOGLE_API_KEY")
    if not key:
        print("没找到 GEMINI_API_KEY"); return
    proxy = e.get("OUTBOUND_PROXY") or None
    print(f"代理：{proxy or '直连'}\n")
    for m in (sys.argv[1:] or DEFAULT_MODELS):
        print(f"=== {m} ===")
        probe(m, key, proxy)
        print()


if __name__ == "__main__":
    main()
