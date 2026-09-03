"""SEO 工具站后端入口。

  uvicorn main:app  （在 api/ 目录下）

挂载各工具的 router（前缀 /api/<tool>），并服务 web/ 静态前端。
加新工具：在 tools/<新工具>/router.py 写一个 APIRouter，然后在下面 include_router。
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# Windows 上 Playwright 需要 Proactor 事件循环（见 router 内说明）。Linux 无影响。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import hashlib

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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

# ---- HTML 防缓存 + 静态资源版本号自动化 ----
# 2026-09-03 踩过：改完 md.js 和 seo-writer.html 部署后，浏览器仍显示旧页面，
# 一度以为是部署没生效。根因是 HTML 没有 Cache-Control（浏览器按启发式缓存），
# 而 /shared/md.js 又没有 ?v= 版本号（app.css/keys.js 有，但版本号是手写的，改了迟早忘）。
# 现在：HTML 一律 no-cache（配 ETag，内容没变时返回 304，开销可以忽略）；
# 返回 HTML 时按文件内容哈希自动给 /shared/*.js|css 加版本号，不用再手动维护。
_ASSET_REF = re.compile(r'(?P<attr>src|href)="(?P<path>/shared/[^"?]+\.(?:js|css))(?:\?v=[^"]*)?"')
_PAGE_CACHE: dict[str, tuple[float, str]] = {}


def _asset_hash(rel: str) -> str:
    f = WEB / rel.lstrip("/")
    try:
        return hashlib.sha1(f.read_bytes()).hexdigest()[:8]
    except OSError:
        return "0"


def page(path: Path) -> HTMLResponse:
    """返回一个 HTML 页面：静态资源自动带上内容哈希，且本身不被浏览器缓存。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        path = WEB / "index.html"
        mtime = path.stat().st_mtime
    cached = _PAGE_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        html = cached[1]
    else:
        html = _ASSET_REF.sub(
            lambda m: f'{m["attr"]}="{m["path"]}?v={_asset_hash(m["path"])}"',
            path.read_text(encoding="utf-8"))
        _PAGE_CACHE[str(path)] = (mtime, html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, must-revalidate"})

app = FastAPI(title="页面科技 — AI 跨境营销工具")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---- 计费（卡密）----
billing_store.conn()          # 启动即建表，别等第一个请求
# 支付与账户的表/迁移也在启动时跑完 —— 别让第一个真实用户当迁移的小白鼠。
# 2026-08-29 就是因为迁移只在首次调用时跑，线上第一次点购买直接 500。
from billing import accounts as _accounts, payorders as _payorders  # noqa: E402
_payorders._init()
_accounts._init()
app.include_router(billing_router)

# 免签支付（静态收款码 + 金额尾数匹配；商户号下来后整块可删）
from billing.pay_router import router as pay_router  # noqa: E402
app.include_router(pay_router)

# 账户体系：登录后点数记在账户上，卡密降级成充值券
from billing.auth_router import router as auth_router  # noqa: E402
app.include_router(auth_router)

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
    return page(WEB / "index.html")


@app.get("/news")
async def news():
    return page(WEB / "news.html")


@app.get("/history")
async def history_page():
    """我的记录（卡密即身份：余额 / 流水 / 生成结果）。"""
    return page(WEB / "history.html")


@app.get("/login")
async def login_page():
    """登录 / 注册。"""
    return page(WEB / "login.html")


@app.get("/forgot")
async def forgot_page():
    """找回密码（带 token 进来时同一页切成"设新密码"）。"""
    return page(WEB / "forgot.html")


@app.get("/reset")
async def reset_page():
    """邮件里的重置链接指向这里，复用同一个页面。"""
    return page(WEB / "forgot.html")


@app.get("/buy")
async def buy_page():
    """购买页：下单 → 扫静态码按精确金额付款 → 轮询自动出卡密。"""
    return page(WEB / "buy.html")


@app.get("/payadmin")
async def payadmin_page():
    """店主兜底页：到账通知漏了时人工确认订单。有 token 才能操作。"""
    return page(WEB / "payadmin.html")


@app.get("/tools/{name}")
async def tool_page(name: str):
    f = WEB / "tools" / f"{name}.html"
    return page(f if f.exists() else WEB / "index.html")


# 静态资源（/shared/app.css、/shared/keys.js 等）
app.mount("/", StaticFiles(directory=WEB), name="web")
