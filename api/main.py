"""SEO 工具站后端入口。

  uvicorn main:app  （在 api/ 目录下）

挂载各工具的 router（前缀 /api/<tool>），并服务 web/ 静态前端。
加新工具：在 tools/<新工具>/router.py 写一个 APIRouter，然后在下面 include_router。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Windows 上 Playwright 需要 Proactor 事件循环（见 router 内说明）。Linux 无影响。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from billing import store as billing_store
from billing.router import router as billing_router

from tools.article_quality.router import router as article_quality_router
from tools.outreach.router import router as outreach_router
from tools.reddit_research.router import router as reddit_research_router
from tools.seo_gap.router import router as seo_gap_router
from tools.seo_writer.router import router as seo_writer_router
from tools.site_recon.router import router as site_recon_router

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="PageZenith — AI 跨境营销工具")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---- 计费（卡密）----
billing_store.conn()          # 启动即建表，别等第一个请求
app.include_router(billing_router)

# 免签支付（静态收款码 + 金额尾数匹配；商户号下来后整块可删）
from billing.pay_router import router as pay_router  # noqa: E402
app.include_router(pay_router)

# ---- 工具 API ----
app.include_router(seo_writer_router)
app.include_router(seo_gap_router)
app.include_router(article_quality_router)
app.include_router(site_recon_router)
app.include_router(reddit_research_router)
app.include_router(outreach_router)
# app.include_router(other_tool_router)   # 以后加工具在这里


# ---- 前端（无 SEO 需求，纯静态由后端顺手返回）----
@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")


@app.get("/news")
async def news():
    return FileResponse(WEB / "news.html")


@app.get("/history")
async def history_page():
    """我的记录（卡密即身份：余额 / 流水 / 生成结果）。"""
    return FileResponse(WEB / "history.html")


@app.get("/buy")
async def buy_page():
    """购买页：下单 → 扫静态码按精确金额付款 → 轮询自动出卡密。"""
    return FileResponse(WEB / "buy.html")


@app.get("/payadmin")
async def payadmin_page():
    """店主兜底页：到账通知漏了时人工确认订单。有 token 才能操作。"""
    return FileResponse(WEB / "payadmin.html")


@app.get("/tools/{name}")
async def tool_page(name: str):
    f = WEB / "tools" / f"{name}.html"
    return FileResponse(f) if f.exists() else FileResponse(WEB / "index.html")


# 静态资源（/shared/app.css、/shared/keys.js 等）
app.mount("/", StaticFiles(directory=WEB), name="web")
