"""卡密全链路冒烟测试（USE_MOCKS=true，不花一分钱）。

    cd api && python ../tests/test_billing_flow.py

覆盖：无卡401 / 错卡401 / 扣点 / 余额 / 点数不足402 / 失败退点 /
      结果落库+取回 / 断线不丢（客户端断开后任务照跑照落库）/ 单卡日限。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import tempfile

os.environ["USE_MOCKS"] = "true"
os.environ["BILLING_DB"] = str(pathlib.Path(tempfile.gettempdir()) / "pz_test_billing.db")
os.environ["BILLING_CARD_DAILY_LIMIT"] = "50"
pathlib.Path(os.environ["BILLING_DB"]).unlink(missing_ok=True)

API = pathlib.Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API))

from httpx import ASGITransport, AsyncClient  # noqa: E402

import main  # noqa: E402
from billing import store  # noqa: E402
from billing.pricing import price  # noqa: E402

P_OUTLINE = price("seo-writer", "outline")
P_ARTICLE = price("seo-writer", "article")

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


async def sse_events(client, url, payload, headers, stop_at_done=False):
    """读 SSE，返回事件列表。stop_at_done=True 模拟用户中途关页。"""
    evts = []
    async with client.stream("POST", url, json=payload, headers=headers) as r:
        if r.status_code != 200:
            await r.aread()
            return r.status_code, evts
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                e = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            evts.append(e)
            if stop_at_done and e.get("type") == "chunk":
                break          # 模拟断线：只收到第一块正文就走人
    return 200, evts


async def main_test() -> None:
    key, = store.mint(1, 20, batch="test", label="测试卡")
    small, = store.mint(1, 1, batch="test", label="小卡")
    H = {"X-Card-Key": key}

    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://t", timeout=60) as c:
        # 1. 无卡 / 错卡
        r = await c.post("/api/seo-writer/outline", json={"main_keyword": "a", "secondary_keyword": "b", "topic": "c"})
        ok("无卡密 → 401", r.status_code == 401, r.status_code)
        r = await c.post("/api/article-quality/check", json={"text": "hello world"},
                         headers={"X-Card-Key": "PZ-XXXX-XXXX-XXXX"})
        ok("错卡密 → 401", r.status_code == 401, r.status_code)

        # 2. 余额
        r = await c.get("/api/billing/balance", headers=H)
        ok("查余额 = 20", r.status_code == 200 and r.json()["remaining"] == 20, r.text[:80])

        # 3. 大纲（价目从 pricing 读，调价不用改测试）
        code, evts = await sse_events(c, "/api/seo-writer/outline",
                                      {"main_keyword": "forex broker", "secondary_keyword": "regulation",
                                       "topic": "how to choose", "tier": "basic"}, H)
        done = [e for e in evts if e.get("type") == "done"]
        ok("大纲生成成功", code == 200 and done, f"{code} evts={len(evts)}")
        sid = done[0]["session_id"] if done else ""
        r = await c.get("/api/billing/balance", headers=H)
        ok(f"大纲扣 {P_OUTLINE} 点", r.json()["remaining"] == 20 - P_OUTLINE, r.json())

        # 4. 改大纲：前 3 次免费
        for i in range(3):
            await sse_events(c, "/api/seo-writer/outline/revise",
                             {"session_id": sid, "feedback": f"改第{i+1}次"}, H)
        r = await c.get("/api/billing/balance", headers=H)
        ok("改大纲 3 次免费（余额不变）", r.json()["remaining"] == 20 - P_OUTLINE, r.json())
        _, evts = await sse_events(c, "/api/seo-writer/outline/revise",
                                   {"session_id": sid, "feedback": "第4次"}, H)
        r = await c.get("/api/billing/balance", headers=H)
        ok("第 4 次改大纲扣 1 点", r.json()["remaining"] == 20 - P_OUTLINE - 1, r.json())

        # 5. 出正文
        code, evts = await sse_events(c, "/api/seo-writer/article", {"session_id": sid, "tier": "pro"}, H)
        done = [e for e in evts if e.get("type") == "done"]
        ok("正文生成成功", code == 200 and done and done[0].get("article"), f"{code} evts={len(evts)}")
        r = await c.get("/api/billing/balance", headers=H)
        ok(f"正文扣 {P_ARTICLE} 点", r.json()["remaining"] == 20 - P_OUTLINE - 1 - P_ARTICLE, r.json())

        # 6. 结果落库 + 取回
        r = await c.get("/api/billing/history", headers=H)
        items = r.json()["items"]
        ok("历史记录有条目", len(items) >= 2, f"{len(items)} 条")
        if items:
            rid = items[0]["id"]
            r2 = await c.get(f"/api/billing/result/{rid}", headers=H)
            ok("按 id 取回结果", r2.status_code == 200 and r2.json()["payload"].get("article"), r2.status_code)
            r3 = await c.get(f"/api/billing/result/{rid}", headers={"X-Card-Key": small})
            ok("别人的卡取不到我的结果", r3.status_code == 404, r3.status_code)

        # 7. 断线不丢：只收一块就断，任务应继续跑完并落库
        before = len(store.list_results(store.hash_card(key)))
        code, evts = await sse_events(c, "/api/seo-writer/article",
                                      {"session_id": sid, "tier": "pro"}, H, stop_at_done=True)
        ok("断线时只收到部分事件", any(e.get("type") == "chunk" for e in evts)
           and not any(e.get("type") == "done" for e in evts), f"evts={len(evts)}")
        for _ in range(60):                      # 等后台任务自己跑完
            await asyncio.sleep(0.1)
            if len(store.list_results(store.hash_card(key))) > before:
                break
        ok("断线后任务仍完成并落库", len(store.list_results(store.hash_card(key))) > before,
           f"{before} → {len(store.list_results(store.hash_card(key)))}")

        # 8. 点数不足
        code, evts = await sse_events(c, "/api/seo-writer/outline",
                                      {"main_keyword": "a", "secondary_keyword": "b", "topic": "c",
                                       "tier": "pro"}, {"X-Card-Key": small})
        ok("点数不足 → 402", code == 402, code)

        # 9. 失败自动退点（打断 analyzer 让它抛异常）
        import tools.article_quality.analyzer as aq

        orig = aq.ArticleAnalyzer.check

        async def boom(self, req):
            raise RuntimeError("模拟供应商炸了")

        aq.ArticleAnalyzer.check = boom
        bal0 = (await c.get("/api/billing/balance", headers=H)).json()["remaining"]
        r = await c.post("/api/article-quality/check", json={"text": "x" * 200}, headers=H)
        bal1 = (await c.get("/api/billing/balance", headers=H)).json()["remaining"]
        aq.ArticleAnalyzer.check = orig
        ok("生成失败 → 已退点（余额不变）", bal0 == bal1, f"{bal0} → {bal1}")

        # 10. 单卡日限（本测试设为 50）：用一张大卡刷到 50 点
        big, = store.mint(1, 500, batch="test", label="大卡")
        hb = store.hash_card(big)
        for _ in range(5):
            store.spend(hb, 10, tool="t", action="a")
        spent = store.card_spent_today(hb)
        r = await c.get("/api/billing/balance", headers={"X-Card-Key": big})
        ok("超日限 → 429", r.status_code == 429, f"今日已花 {spent} 点，返回 {r.status_code}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main_test())
