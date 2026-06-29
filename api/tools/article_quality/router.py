"""文章质量检测 API（前缀 /api/article-quality）。key 按请求传，用完即弃。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..seo_gap.config import get_settings
from .analyzer import ArticleAnalyzer, fetch_article
from .batch import build_xlsx, default_filename, parse_notion_zip
from .models import ArticleCheck, CheckRequest, FetchRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/article-quality", tags=["article-quality"])
_sema = asyncio.Semaphore(get_settings().max_concurrent_runs)


@router.post("/fetch")
async def fetch(req: FetchRequest) -> dict:
    """抓取网址正文供编辑器载入（无需 key）。"""
    s = get_settings()
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")
    async with _sema:
        try:
            title, text = await fetch_article(req.url, req.fetch_mode, s)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"title": title, "text": (title + "\n\n" + text) if title else text}


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "model": s.llm_model}


@router.post("/check", response_model=ArticleCheck)
async def check(req: CheckRequest) -> ArticleCheck:
    s = get_settings().with_keys(req.openrouter_key, None)
    if not s.use_mocks and not s.openrouter_api_key:
        raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
    try:
        return await ArticleAnalyzer(s).check(req)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("article check failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@router.post("/batch_stream")
async def batch_stream(
    file: UploadFile = File(...),
    openrouter_key: str = Form(""),
):
    """Notion 导出 ZIP（Markdown & CSV）→ 逐篇评审 → SSE 进度，done 事件带 base64 的 xlsx。"""
    s = get_settings().with_keys(openrouter_key or None, None)
    if not s.use_mocks and not s.openrouter_api_key:
        raise HTTPException(status_code=400, detail="缺少 OpenRouter API Key，请在设置里填写。")
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙，请稍后重试。")

    data = await file.read()
    try:
        articles = parse_notion_zip(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not articles:
        raise HTTPException(status_code=400, detail="ZIP 里没找到文章（.md）。请确认用「Markdown & CSV」导出并勾选包含子页面。")

    async def gen():
        analyzer = ArticleAnalyzer(s)
        # 限制单篇并发，避免账单/限流失控（每篇 3 次 LLM 调用）
        inner = asyncio.Semaphore(min(4, s.max_concurrent_runs * 2))
        results: list[tuple[str, ArticleCheck]] = []

        async def one(art):
            async with inner:
                try:
                    c = await analyzer.check(CheckRequest(text=art["text"], openrouter_key=openrouter_key or None))
                    return art, c, None
                except Exception as exc:  # 单篇失败不拖垮整批
                    logger.warning("批量评审单篇失败 %s: %s", art["filename"], exc)
                    return art, None, str(exc)

        async with _sema:
            yield _sse({"type": "start", "total": len(articles)})
            done_n = 0
            tasks = [asyncio.create_task(one(a)) for a in articles]
            for fut in asyncio.as_completed(tasks):
                art, c, err = await fut
                done_n += 1
                if c is not None:
                    results.append((art["filename"], c))
                    yield _sse({"type": "item", "done": done_n, "total": len(articles),
                                "title": c.detected_title or art["title"],
                                "score": c.overall_score, "grade": c.grade})
                else:
                    yield _sse({"type": "item", "done": done_n, "total": len(articles),
                                "title": art["title"], "score": None, "grade": "失败", "error": err})

            if not results:
                yield _sse({"type": "error", "message": "全部文章评审失败，请检查 API Key 或额度。"})
                return
            xlsx = build_xlsx(results)
            yield _sse({"type": "done", "ok": len(results), "total": len(articles),
                        "filename": default_filename(),
                        "xlsx_b64": base64.b64encode(xlsx).decode("ascii")})

    return StreamingResponse(gen(), media_type="text/event-stream")
