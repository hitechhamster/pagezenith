"""一次性生成三个写手的头像，输出到 web/assets/voices/。

    /srv/pagezenith/.venv/bin/python scripts/gen_voice_portraits.py [写手id...]

## 为什么是丁丁风（ligne claire），不是写实头像

1. 用户 2026-08-28 指定：动漫感、丁丁历险记那种。
2. **写实假头像会被当成真人拿去署名。** 用户是拿这工具给自己的商用站写文章的，
   一张像真人的头像配上名字，很容易被贴到 About 页上当真作者 —— 那正是 Google
   垃圾政策盯的 fake E-E-A-T。卡通插画不会有这个误会。
3. 统一的平涂线条画能保证三张卡片风格一致；写实头像做不到。

注：模型即使被明确要求 no border 也照画外框，生成后要裁边（见 crop 步骤）。

界面上这三个人是**文风**，不是作者。文案里不要出现"作者""署名""by"。
"""
from __future__ import annotations

import base64
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import httpx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
OUT = ROOT / "web" / "assets" / "voices"
MODEL = "gemini-3-pro-image"

# 所有肖像共用的风格底子 —— 五张必须像出自同一套版画，所以这段一字不改地复用。
HOUSE_STYLE = (
    "Style: ligne claire — the flat European comic style of Hergé's Tintin albums. "
    "Uniform-weight clean black ink outlines on every shape, no line weight variation. "
    "Flat solid colour fills with NO shading, NO gradients, NO hatching, NO cross-hatching, "
    "NO cast shadows, NO texture. Simplified cartoon face: minimal features, small dot or "
    "simple line eyes, clean simple nose and mouth, no rendered skin detail. "
    "Restrained warm palette of four or five flat colours only — muted ochre, dusty red, "
    "slate blue, cream — on a plain flat cream background. "
    "Head and shoulders, three-quarter view, calm friendly expression. "
    "Plain flat background, no border, no frame, no panel, no speech bubble, no ornament. "
    "Centred composition with even margins, square aspect ratio. "
    "CRITICAL: no text, no letters, no numbers, no signature, no watermark anywhere. "
    "Not a photograph, not a realistic rendering. Not a real or recognisable person. "
    "No brand or logo."
)

# 每个写手的长相描述。三个人要一眼分得开：年龄、性别、气质都错开。
# 键必须跟 voices.VOICES 的 id 一致，否则 generate() 会 KeyError。
SUBJECTS: dict[str, str] = {
    "clark": (
        "A man in his early thirties of East Asian appearance, short neat black hair, "
        "friendly and alert, wearing a plain dark crew-neck. Approachable, quietly confident. "
        "Turned in a three-quarter view like the others — not facing the camera straight on."
    ),
    "serious": (
        "An older man in his early sixties, thinning grey hair swept back, deep-set eyes, "
        "wearing thin-rimmed glasses and a plain collared shirt under a dark cardigan. "
        "Senior, measured, unhurried — the look of someone who has read every report twice."
    ),
    "casual": (
        "A woman in her forties with short wavy hair and warm laugh lines around the eyes, "
        "wearing a simple crew-neck sweater. Open, relaxed, easy to talk to."
    ),
}


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


def _meta(vid: str) -> tuple[str, str]:
    """(输出文件名, 显示名)。文件名以 voices.VOICES 为准，别在这里另写一份。"""
    from tools.seo_writer.voices import VOICES
    v = VOICES[vid]
    return v["portrait"], v["role"]


def generate(vid: str, key: str, proxy: str | None) -> bool:
    fname, label = _meta(vid)
    prompt = f"{SUBJECTS[vid]} {HOUSE_STYLE}"
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{MODEL}:generateContent")
    try:
        with httpx.Client(proxy=proxy, timeout=300, trust_env=False) as c:
            r = c.post(url, params={"key": key}, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseModalities": ["IMAGE"]},
            })
        if r.status_code != 200:
            print(f"  ✗ {vid}: HTTP {r.status_code} {r.text[:160]}")
            return False
        for part in (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                OUT.mkdir(parents=True, exist_ok=True)
                f = OUT / fname
                f.write_bytes(base64.b64decode(blob["data"]))
                print(f"  ✓ {vid} ({label}) → {f.name}  {f.stat().st_size // 1024} KB")
                return True
        print(f"  ✗ {vid}: 回包里没有图")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {vid}: {str(exc)[:160]}")
        return False


def main() -> None:
    from tools.seo_writer.voices import VOICES
    e = env()
    key = e.get("GEMINI_API_KEY") or e.get("GOOGLE_API_KEY")
    if not key:
        print("没找到 GEMINI_API_KEY"); return
    proxy = e.get("OUTBOUND_PROXY") or None
    todo = [v for v in (sys.argv[1:] or list(VOICES)) if v in SUBJECTS]
    print(f"代理 {proxy or '直连'} · 模型 {MODEL} · 生成 {len(todo)} 张\n")
    ok = 0
    for vid in todo:
        ok += generate(vid, key, proxy)
        time.sleep(1)
    print(f"\n完成 {ok}/{len(todo)} → {OUT}")


if __name__ == "__main__":
    main()
