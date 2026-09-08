"""四道永久护栏 —— 把 2026-09-08 那批"单测全绿、真跑才炸"的 bug 各自封一类。

    cd api && python ../tests/test_guards.py

  ① 不许绕过 Settings 读 os.environ（systemd 不加载 .env，那样读线上永远是空）
  ② 日志脱敏：key / token / Bearer 出日志前必须变成 ***
  ③ 会产出正文的 prompt 必须带 {language}
  ④ .env 里"只写在文件里、不设环境变量"的键，Settings 必须读得到
一次性审计只能抓现在的，抓不住下周新写的；这四条跑在测试里，新增就红。
"""
from __future__ import annotations

import ast
import io
import logging
import os
import pathlib
import re
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["USE_MOCKS"] = "true"
os.environ["BILLING_DB"] = str(pathlib.Path(tempfile.gettempdir()) / "pz_test_guards.db")
ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "api"
sys.path.insert(0, str(API))

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


# ── ① 只有 config.py 可以碰 os.environ ────────────────────────────────
def test_no_environ_outside_settings():
    print("\n[① os.environ 只准出现在 config.py]")
    hits = []
    for f in API.rglob("*.py"):
        if f.name == "config.py" and f.parent.name == "seo_gap":
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            hits.append(f"{f.relative_to(ROOT)}: 语法错误 {exc}")
            continue
        for node in ast.walk(tree):
            # os.environ / os.environ.get / os.getenv
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "os" and node.attr in ("environ", "getenv"):
                hits.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    ok("api/ 里没有绕过 Settings 的 os.environ / os.getenv", not hits, hits)

    # 秘密进 URL 的写法也顺手拦一下（出图那次就是 params={"key": …}）
    # 白名单：密码重置邮件链接天然带 token（那是它的本职，且只发邮件不打日志；
    # 就算被打进日志，②的脱敏也会把 token= 抹掉，上面有测）
    _URL_TOKEN_OK = {"auth_router.py"}
    bad = []
    for f in API.rglob("*.py"):
        if f.name in _URL_TOKEN_OK:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"params\s*=\s*\{[^}]*[\"'](?:key|api_key|token|secret)[\"']", code) \
                    or re.search(r"[?&](?:key|api_key|token)=\{", code):
                bad.append(f"{f.relative_to(ROOT)}:{i}")
    ok("没有把 key/token 放进 URL 查询串的写法", not bad, bad)


# ── ② 日志脱敏 ────────────────────────────────────────────────────────
def test_log_redaction():
    print()
    print("[② 日志脱敏]")
    import main  # noqa: E402  挂 filter 的动作在 import 时发生

    # 样本一律运行时拼接，源码里不能出现完整的 key 形态：GitHub 推送保护按形态扫描，
    # 整段的 AIza… / sk-or-v1-… 哪怕内容全是 FAKE 也会当真 key 拦下（2026-09-08 撞过两次）。
    k_aq = "AQ." + "FAKE" * 12
    k_or = "sk-or-v1-" + "deadbeef" * 8
    k_aiza = "AIza" + "FAKE" * 9
    k_tok = "FAKETOKEN" * 3
    samples = {
        "Gemini ?key=":   "POST https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key=" + k_aq,
        "Bearer":         "headers Authorization: Bearer " + k_or,
        "x-goog-api-key": "x-goog-api-key: " + k_aiza,
        "token=":         "https://pagezenith.com/reset?token=" + k_tok + "&x=1",
    }
    for name, raw in samples.items():
        out = main.redact(raw)
        leaked = ("FAKEFAKE" in out) or ("deadbeef" in out) or ("FAKETOKEN" in out)
        ok(f"redact() 抹掉 {name}", not leaked, out[:110])

    # 真走一遍 logging：httpx 这种子 logger 传播上来也要被抹
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(main._RedactFilter())
    root = logging.getLogger()
    root.addHandler(h)
    try:
        logging.getLogger("httpx").info("HTTP Request: POST https://x/y?key=" + k_aq + " '200'")
        logging.getLogger("tools.seo_writer.providers").warning(
            "配图生成失败: Server error for url 'https://x/y?key=%s'", k_aq)
    finally:
        root.removeHandler(h)
    text = buf.getvalue()
    ok("子 logger（httpx）传播上来的记录被抹", "FAKEFAKE" not in text and "***" in text, text.strip()[:120])
    ok("带 %s 参数的记录也被抹", text.count("***") >= 2)
    ok("根 handler 上确实挂了脱敏 filter",
       any(isinstance(f, main._RedactFilter) for hh in logging.getLogger().handlers for f in hh.filters))


# ── ③ 写正文的 prompt 必须带 {language} ───────────────────────────────
# 输出是标签 / 原文照抄 / 拼在别的 prompt 后面的，不需要语言指令
_LANG_EXEMPT = {
    "CLASSIFY_PROMPT": "输出的是类型标签",
    "QUESTION_FILTER_PROMPT": "输出的是问题原文照抄，且按原文校验",
    "POLISH_STRICT_RETRY": "拼在 POLISH_PROMPT 后面，那里已有 language",
    "WORDCOUNT_PROMPT": "输出的是数字",
}


def test_prompts_have_language():
    print("\n[③ 会写正文的 prompt 带 {language}]")
    missing = []
    for f in ("prompts.py", "postfix.py"):
        src = (API / "tools" / "seo_writer" / f).read_text(encoding="utf-8")
        for m in re.finditer(r'^([A-Z_][A-Z0-9_]*)\s*=\s*f?"""(.*?)"""', src, re.S | re.M):
            name, body = m.group(1), m.group(2)
            if name.endswith("_BLOCK") or name in _LANG_EXEMPT or len(body) < 200:
                continue
            if not re.search(r"write|rewrite|output|输出|写|改写|补|answer", body, re.I):
                continue
            if "{language}" not in body:
                missing.append(f"{f}:{name}")
    ok("没有缺语言指令的写作模板", not missing, missing)


# ── ④ 只写在 .env、不设环境变量 的键，Settings 读得到 ────────────────
def test_env_file_only():
    print("\n[④ .env-only 的键能被 Settings 读到]")
    from tools.seo_gap.config import Settings
    tmp = pathlib.Path(tempfile.gettempdir()) / "pz_guard_test.env"
    tmp.write_text(
        "INTERNAL_PATH=abc123\nBILLING_CARD_DAILY_LIMIT=7\nBILLING_GLOBAL_DAILY_CNY=12.5\n"
        "BILLING_BAD_KEY_PER_HOUR=3\nBILLING_DB=/tmp/x.db\nPOLISH_MODEL=some-model\n"
        "SIGNUP_CREDITS=0\nLOG_LEVEL=DEBUG\nENABLE_DOCS=1\n", encoding="utf-8")
    # 确保只有 .env 这一条路（pydantic 里环境变量优先于 .env，留着会盖掉文件里的值）。
    # BILLING_DB 是本测试顶部为了 import main 设的，也要临时挪开、测完放回。
    saved_db = os.environ.pop("BILLING_DB", None)
    for k in ("INTERNAL_PATH", "BILLING_CARD_DAILY_LIMIT", "BILLING_GLOBAL_DAILY_CNY",
              "BILLING_BAD_KEY_PER_HOUR", "POLISH_MODEL", "SIGNUP_CREDITS", "LOG_LEVEL", "ENABLE_DOCS"):
        os.environ.pop(k, None)
    try:
        s = Settings(_env_file=str(tmp))
    finally:
        if saved_db is not None:
            os.environ["BILLING_DB"] = saved_db
    ok("INTERNAL_PATH", s.internal_path == "abc123", s.internal_path)
    ok("BILLING_CARD_DAILY_LIMIT", s.billing_card_daily_limit == 7, s.billing_card_daily_limit)
    ok("BILLING_GLOBAL_DAILY_CNY", s.billing_global_daily_cny == 12.5, s.billing_global_daily_cny)
    ok("BILLING_BAD_KEY_PER_HOUR", s.billing_bad_key_per_hour == 3)
    ok("BILLING_DB", s.billing_db == "/tmp/x.db", s.billing_db)
    ok("POLISH_MODEL", s.polish_model == "some-model")
    ok("SIGNUP_CREDITS=0 是 0 不是 None（紧急关注册送点那个开关）", s.signup_credits == 0, s.signup_credits)
    ok("ENABLE_DOCS", s.enable_docs is True)
    tmp.unlink(missing_ok=True)

    # signup_credits() 真的听 Settings 的
    from billing import pricing
    from tools.seo_gap.config import get_settings
    g = get_settings(); old = g.signup_credits
    try:
        g.signup_credits = 0
        ok("signup_credits() 在 0 时返回 0", pricing.signup_credits() == 0, pricing.signup_credits())
        g.signup_credits = None
        ok("signup_credits() 在 None 时回默认", pricing.signup_credits() == pricing.SIGNUP_CREDITS_DEFAULT)
    finally:
        g.signup_credits = old


def main_() -> int:
    test_no_environ_outside_settings()
    test_log_redaction()
    test_prompts_have_language()
    test_env_file_only()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main_())
