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

from tools.article_quality.router import router as article_quality_router
from tools.seo_gap.router import router as seo_gap_router
from tools.site_recon.router import router as site_recon_router

WEB = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="PageZenith — AI 跨境营销工具")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---- 工具 API ----
app.include_router(seo_gap_router)
app.include_router(article_quality_router)
app.include_router(site_recon_router)
# app.include_router(other_tool_router)   # 以后加工具在这里


# ---- 前端（无 SEO 需求，纯静态由后端顺手返回）----
@app.get("/")
async def home():
    return FileResponse(WEB / "index.html")


@app.get("/tools/{name}")
async def tool_page(name: str):
    f = WEB / "tools" / f"{name}.html"
    return FileResponse(f) if f.exists() else FileResponse(WEB / "index.html")


# 静态资源（/shared/app.css、/shared/keys.js 等）
app.mount("/", StaticFiles(directory=WEB), name="web")
