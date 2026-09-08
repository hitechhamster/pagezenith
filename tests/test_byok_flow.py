"""内部 BYOK 模式冒烟测试（不发任何真实 API 请求，不花一分钱）。

    cd api && python ../tests/test_byok_flow.py

BYOK 的全部逻辑就是"门禁 + 配置装配"两件事，所以这里测的也是这两件：

  门禁：没凭证 / 错凭证 / 有凭证没 key，各自该是什么结果；
        以及**不能静默降级** —— 带了错凭证绝不能被当成普通用户放进正常扣点路径。
        （凭证 = INTERNAL_PATH 那串乱码，同时也是页面地址。）
  装配：服务端的 Gemini/DeepSeek/SerpApi key 有没有被真的清干净（这是 BYOK
        名存实亡的唯一方式：某个分支没命中，就悄悄花公司的钱），
        Serper 有没有覆盖到 key 池真正读的那个字段，
        以及四个任务槽位是不是都落到 OpenRouter。

不覆盖：真实生成（要真 key）。那部分与线上共用同一条流水线，本来就被
        test_billing_flow.py 覆盖着。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

os.environ["USE_MOCKS"] = "true"
os.environ["BILLING_DB"] = str(pathlib.Path(tempfile.gettempdir()) / "pz_test_byok.db")
os.environ["INTERNAL_PATH"] = "k7Qm2xVb9pLd4Rn8"     # 页面地址 + 请求凭证（同一串）
# 服务端「自己的」key：装配测试要证明它们在 BYOK 里被清空了
os.environ["GEMINI_API_KEY"] = "server-gemini"
os.environ["DEEPSEEK_KEY"] = "server-deepseek"
os.environ["SERPER_KEYS"] = "server-serper-1,server-serper-2"
pathlib.Path(os.environ["BILLING_DB"]).unlink(missing_ok=True)

API = pathlib.Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API))

from httpx import ASGITransport, AsyncClient  # noqa: E402

import byok  # noqa: E402
import main  # noqa: E402
from billing import store  # noqa: E402
from billing.deps import Card, charge  # noqa: E402
from tools.seo_gap.clients import serper_pool  # noqa: E402
from tools.seo_writer.providers import provider_for, resolve_llm  # noqa: E402

TOKEN = os.environ["INTERNAL_PATH"]
GOOD = {"X-Internal-Token": TOKEN, "X-Openrouter-Key": "sk-or-user",
        "X-Serper-Key": "serper-user"}

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


# --------------------------------------------------------------------------- #
# 1. 门禁
# --------------------------------------------------------------------------- #
async def test_gate(client):
    print("\n[门禁]")

    r = await client.get("/api/seo-writer/byok/defaults")
    ok("没带凭证取默认模型表 → 404", r.status_code == 404, r.status_code)

    r = await client.get("/api/seo-writer/byok/defaults",
                         headers={"X-Internal-Token": "wrong"})
    ok("错凭证取默认模型表 → 404", r.status_code == 404, r.status_code)

    r = await client.get("/api/seo-writer/byok/defaults",
                         headers={"X-Internal-Token": TOKEN})
    j = r.json() if r.status_code == 200 else {}
    ok("对凭证取默认模型表 → 200", r.status_code == 200, r.status_code)
    ok("默认模型表覆盖全部槽位",
       set(j.get("models") or {}) == set(byok.SLOTS), sorted(j.get("models") or {}))

    body = {"main_keyword": "a", "secondary_keyword": "b", "topic": "c"}

    r = await client.post("/api/seo-writer/outline", json=body,
                          headers={"X-Internal-Token": "wrong",
                                   "X-Openrouter-Key": "x", "X-Serper-Key": "y"})
    # 两条都要成立：① 必须报错，**不能**被当成普通匿名用户走进正常扣点路径；
    # ② 必须是 404 而不是 401 —— 401 等于告诉扫描器"这个头是对的、只是值不对",
    #    那就成了一个可以拿来枚举乱码的提示器。
    ok("错凭证跑大纲 → 404（不降级、不提示）", r.status_code == 404, r.status_code)

    r = await client.post("/api/seo-writer/outline", json=body,
                          headers={"X-Internal-Token": TOKEN})
    ok("对凭证但没填 key → 400", r.status_code == 400, r.status_code)

    r = await client.post("/api/seo-writer/outline", json=body,
                          headers={"X-Internal-Token": TOKEN, "X-Openrouter-Key": "x"})
    ok("少 Serper key → 400", r.status_code == 400, r.status_code)

    # 回归：没有 BYOK 头的普通请求，行为一如既往（未登录 = 401 请先登录）
    r = await client.post("/api/seo-writer/outline", json=body)
    ok("普通请求仍要求身份 → 401", r.status_code == 401, r.status_code)

    # ── 乱码地址本身 ──
    r = await client.get(f"/{TOKEN}")
    ok("乱码地址能打开写手页", r.status_code == 200, r.status_code)
    ok("返回的确实是写手页", "seo-writer" in r.text or "BYOK" in r.text)

    r = await client.get("/k7Qm2xVb9pLd4Rn9")          # 差一个字符
    ok("猜错一个字符 → 404", r.status_code == 404, r.status_code)

    r = await client.get("/internal/writer")            # 老路径不该还在
    ok("老的 /internal/writer 已经不存在 → 404", r.status_code == 404, r.status_code)

    # 回归：公开的写手页照常
    r = await client.get("/tools/seo-writer")
    ok("公开写手页仍可访问", r.status_code == 200, r.status_code)


# --------------------------------------------------------------------------- #
# 2. 配置装配
# --------------------------------------------------------------------------- #
def test_settings():
    print("\n[配置装配]")
    cfg = byok.ByokConfig(openrouter_key="sk-or-user", serper_key="serper-user")
    s = byok.settings_for(cfg)

    ok("OpenRouter key = 用户的", s.openrouter_api_key == "sk-or-user")
    ok("Gemini key 已清空（否则会静默花公司的钱）", s.gemini_api_key == "")
    ok("DeepSeek key 已清空", s.deepseek_key == "")
    ok("SerpApi key 已清空", s.serpapi_key == "")
    ok("供应商被钉死成 openrouter", s.force_llm_provider == "openrouter")
    ok("use_mocks 被强制关掉（内部人要真产出）", s.use_mocks is False)

    # key 池读的是 serper_keys；只覆盖 serper_key 会被服务端那两把盖掉
    ok("Serper key 池只剩用户那把",
       serper_pool.keys(s) == ["serper-user"], serper_pool.keys(s))

    # 全局单例绝不能被污染
    from tools.seo_gap.config import get_settings
    g = get_settings()
    ok("全局 Settings 未被改动",
       g.gemini_api_key == "server-gemini" and g.force_llm_provider == "",
       f"{g.gemini_api_key!r}/{g.force_llm_provider!r}")


def test_model_routing():
    print("\n[模型路由]")
    cfg = byok.ByokConfig(openrouter_key="sk-or-user", serper_key="serper-user",
                          models={"outline": "google/gemini-3.1-pro-preview",
                                  "article": "anthropic/claude-sonnet-5",
                                  "polish": "google/gemini-3.7-flash",
                                  "utility": "google/gemini-3.1-flash-lite",
                                  "image": "google/gemini-3-pro-image"})
    s = byok.settings_for(cfg)

    # `google/` 前缀在默认规则下会被判成 Gemini 直连 —— BYOK 必须把它按下去
    ok("google/ 前缀被钉回 openrouter",
       provider_for(s, "google/gemini-3.1-pro-preview") == "openrouter")

    t = resolve_llm(s, "pro", models=cfg.models)
    ok("target 落在 openrouter", t.provider == "openrouter", t.provider)
    ok("base_url 是 openrouter", "openrouter" in t.base_url, t.base_url)
    ok("api_key 是用户的", t.api_key == "sk-or-user")

    # 四个任务槽位各自取到用户填的模型（而不是 billing.pricing 的线上档位表）
    got = {task: t.model_for_task(task)
           for task in ("outline", "article", "polish", "seo")}
    ok("大纲用用户填的模型", got["outline"] == "google/gemini-3.1-pro-preview", got["outline"])
    ok("正文用用户填的模型", got["article"] == "anthropic/claude-sonnet-5", got["article"])
    ok("润色用用户填的模型", got["polish"] == "google/gemini-3.7-flash", got["polish"])
    ok("杂活落到 utility 槽", got["seo"] == "google/gemini-3.1-flash-lite", got["seo"])
    ok("配图模型 = 用户填的", s.writer_image_model == "google/gemini-3-pro-image")

    # 线上路径不受影响：没有 models 覆盖时仍读 billing.pricing
    from tools.seo_gap.config import get_settings
    from billing.pricing import model_for
    t2 = resolve_llm(get_settings(), "pro")
    ok("线上仍走档位表", t2.model_for_task("article") == model_for("article", "pro"),
       t2.model_for_task("article"))
    ok("线上仍是 Gemini 直连", t2.provider == "gemini", t2.provider)


# --------------------------------------------------------------------------- #
# 3. 不计费
# --------------------------------------------------------------------------- #
async def test_no_billing():
    print("\n[不计费]")
    cfg = byok.ByokConfig(openrouter_key="sk-or-user", serper_key="serper-user")
    card = Card(key_hash=cfg.identity, ip="1.2.3.4", remaining=10 ** 9,
                label="内部 BYOK", byok=cfg)

    before = store.global_cost_today()
    async with charge(card, "seo-writer", "article", "pro") as tx:
        tx.report_tokens("google/gemini-3.1-pro-preview", 10_000, 5_000)
        tx.set_result(title="t", summary="s", payload={"kind": "article"})

    ok("本次扣点 = 0", tx.credits == 0, tx.credits)
    ok("没有产生 usage 流水行", tx.usage_id is None)
    ok("库里没有为合成身份建卡", store.card_state(cfg.identity) is None)
    ok("全局当日成本未被计入", store.global_cost_today() == before,
       f"{before} → {store.global_cost_today()}")

    # 身份是按 key 算的：同一把 key 断线重连还能认回自己的任务
    same = byok.ByokConfig(openrouter_key="sk-or-user", serper_key="other")
    diff = byok.ByokConfig(openrouter_key="sk-or-OTHER", serper_key="serper-user")
    ok("同 key 同身份", same.identity == cfg.identity)
    ok("换 key 换身份", diff.identity != cfg.identity)


async def main_() -> int:
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await test_gate(client)
    test_settings()
    test_model_routing()
    await test_no_billing()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
