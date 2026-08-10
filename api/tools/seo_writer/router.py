"""SEO 文章生成 API（前缀 /api/seo-writer）。key 按请求传，用完即弃。

三步向导，全部走 SSE —— 写一篇 2000 词的文章要好几分钟，一次性返回的话
浏览器和 Render 都会先超时，用户也只能干等。

  POST /outline         参数 → 搜索 → 判字数 → 分类 → 流式大纲 → 返回 session_id
  POST /outline/revise  session_id + 修改意见 → 流式新大纲（可反复调）
  POST /article         session_id → 流式正文 → SEO 元数据 → 配图 → Word
  POST /polish          已生成的正文 → 整篇改写到"美国 12 年级学生能读懂" → 新 Word
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..seo_gap.config import get_settings
from .docx_export import build_docx, sanitize_filename
from .models import ArticleRequest, LANGUAGES, OutlineRequest, PolishRequest, ReviseRequest
from .providers import LLM, LLM_MODELS, ProviderError, resolve_llm
from .session import get_store
from .workflow import (SEOWriter, count_words, extract_h1, grade_verdict,
                       reading_grade, wordcount_status)

# 配图字节存进会话是为了润色后能重新拼一份带图的 Word。
# 单个会话超过这个体积就不存了（宁可润色后的 Word 没图，也不让内存失控）。
MAX_SESSION_IMAGE_BYTES = 6 * 1024 * 1024

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/seo-writer", tags=["seo-writer"])
_sema = asyncio.Semaphore(get_settings().writer_max_concurrent)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _build(req) -> tuple[Any, SEOWriter]:
    """按请求组装 Settings 副本 + 工作流实例；key 缺失在这里就报掉，别等流开了才发现。"""
    s = get_settings().with_keys(
        req.openrouter_key, None, req.tavily_key, req.deepseek_key, req.exa_key)
    try:
        target = resolve_llm(s, req.llm_provider, req.llm_model)
    except ProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sp = req.search_provider
    if not s.use_mocks:
        if sp == "tavily" and not s.tavily_key:
            raise HTTPException(status_code=400, detail="选了 Tavily 但没填 Tavily API Key。")
        if sp == "exa" and not s.exa_key:
            raise HTTPException(status_code=400, detail="选了 Exa 但没填 Exa API Key。")
    return s, SEOWriter(s, LLM(target, s), sp)


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "languages": LANGUAGES,
            "models": LLM_MODELS,
            "defaults": {"openrouter": s.writer_llm_model, "deepseek": s.writer_deepseek_model}}


# --------------------------------------------------------------------------- #
# 第一步：大纲
# --------------------------------------------------------------------------- #
@router.post("/outline")
async def outline(req: OutlineRequest):
    if not (req.main_keyword.strip() and req.secondary_keyword.strip() and req.topic.strip()):
        raise HTTPException(status_code=400, detail="主关键词 / 次关键词 / 主题 都不能为空。")
    s, wf = _build(req)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙（同时生成的文章已达上限），请稍后重试。")

    async def gen() -> AsyncIterator[str]:
        async with _sema:
            try:
                ctx: dict[str, Any] = {
                    "main_keyword": req.main_keyword.strip(),
                    "secondary_keyword": req.secondary_keyword.strip(),
                    "topic": req.topic.strip(), "specific": req.specific or "",
                    "language": req.language, "enable_images": req.enable_images,
                    "images_per_article": req.images_per_article,
                }

                if req.wordcounts and req.wordcounts > 0:
                    ctx["wordcounts"] = req.wordcounts
                    yield _sse({"type": "step", "key": "wordcount",
                                "message": f"字数：用户指定 {req.wordcounts}"})
                else:
                    yield _sse({"type": "step", "key": "wordcount", "message": "AI 判断合适字数…"})
                    ctx["wordcounts"] = await wf.infer_wordcount(
                        ctx["main_keyword"], ctx["secondary_keyword"],
                        ctx["topic"], ctx["specific"], ctx["language"])
                    yield _sse({"type": "step", "key": "wordcount",
                                "message": f"字数：AI 判断 {ctx['wordcounts']}",
                                "value": ctx["wordcounts"]})

                yield _sse({"type": "step", "key": "search", "message": "搜索主/次关键词的现有内容…"})
                ctx["main_search"], ctx["sec_search"] = await wf.search_context(
                    ctx["main_keyword"], ctx["secondary_keyword"])

                yield _sse({"type": "step", "key": "classify", "message": "判断主题类型…"})
                ctx["topic_type"] = await wf.classify_topic_type(ctx["main_keyword"], ctx["topic"])
                yield _sse({"type": "step", "key": "classify",
                            "message": f"主题类型：{ctx['topic_type']}", "value": ctx["topic_type"]})

                yield _sse({"type": "step", "key": "outline", "message": "生成大纲…"})
                buf: list[str] = []
                async for piece in wf.stream_outline(ctx):
                    buf.append(piece)
                    yield _sse({"type": "chunk", "text": piece})

                ctx["outline"] = "".join(buf)
                sid = get_store().create(ctx)
                yield _sse({"type": "done", "session_id": sid, "outline": ctx["outline"],
                            "wordcounts": ctx["wordcounts"], "topic_type": ctx["topic_type"]})
            except Exception as exc:
                logger.exception("outline failed")
                yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# 第二步：改大纲（可反复）
# --------------------------------------------------------------------------- #
@router.post("/outline/revise")
async def outline_revise(req: ReviseRequest):
    if not req.feedback.strip():
        raise HTTPException(status_code=400, detail="请填写修改意见。")
    s, wf = _build(req)
    ctx = get_store().get(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=410, detail="会话已过期，请回到第一步重新生成大纲。")
    if req.outline:
        ctx["outline"] = req.outline      # 用户在页面上手改过大纲，以页面为准

    async def gen() -> AsyncIterator[str]:
        async with _sema:
            try:
                buf: list[str] = []
                async for piece in wf.stream_revise(ctx, req.feedback.strip()):
                    buf.append(piece)
                    yield _sse({"type": "chunk", "text": piece})
                new_outline = "".join(buf)
                get_store().update(req.session_id, outline=new_outline)
                yield _sse({"type": "done", "session_id": req.session_id, "outline": new_outline})
            except Exception as exc:
                logger.exception("revise failed")
                yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# 第三步：出文
# --------------------------------------------------------------------------- #
@router.post("/article")
async def article(req: ArticleRequest):
    s, wf = _build(req)
    ctx = get_store().get(req.session_id)
    if ctx is None:
        # 会话过期/进程重启：用前端回传的参数降级继续，别让用户白填一遍
        if not (req.outline and req.main_keyword and req.secondary_keyword and req.topic):
            raise HTTPException(status_code=410, detail="会话已过期，请回到第一步重新生成大纲。")
        ctx = {
            "main_keyword": req.main_keyword, "secondary_keyword": req.secondary_keyword,
            "topic": req.topic, "specific": req.specific or "",
            "wordcounts": req.wordcounts or 1800, "language": req.language or "English",
            "topic_type": "conceptual", "main_search": "", "sec_search": "",
            "enable_images": bool(req.enable_images),
            "images_per_article": req.images_per_article or 2,
        }
    if req.outline:
        ctx["outline"] = req.outline
    if not ctx.get("outline"):
        raise HTTPException(status_code=400, detail="还没有大纲，请先完成第一步。")

    want_images = bool(ctx.get("enable_images"))
    if want_images and not s.openrouter_api_key and not s.use_mocks:
        want_images = False   # 只有 OpenRouter 有文生图，没 key 就静默跳过（前端也会提示）

    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙（同时生成的文章已达上限），请稍后重试。")

    async def gen() -> AsyncIterator[str]:
        async with _sema:
            try:
                yield _sse({"type": "step", "key": "article", "message": "撰写文章…"})
                buf: list[str] = []
                async for piece in wf.stream_article(ctx):
                    buf.append(piece)
                    yield _sse({"type": "chunk", "text": piece})
                text = "".join(buf)

                actual = count_words(text)
                level, wc_msg = wordcount_status(actual, ctx.get("wordcounts", 0))
                yield _sse({"type": "wordcount", "actual": actual,
                            "target": ctx.get("wordcounts", 0), "level": level, "message": wc_msg})

                grade = reading_grade(text, ctx["language"])
                g_level, g_msg = grade_verdict(grade)
                yield _sse({"type": "grade", "grade": grade, "level": g_level, "message": g_msg})

                yield _sse({"type": "step", "key": "seo", "message": "生成 SEO 标题与描述…"})
                seo = await wf.generate_seo(text, ctx["main_keyword"], ctx["language"])
                yield _sse({"type": "seo", **seo, "h1": extract_h1(text)})

                image_map: dict[str, bytes] = {}
                if want_images:
                    n = ctx.get("images_per_article", 2)
                    yield _sse({"type": "step", "key": "images", "message": f"生成 {n} 张配图…"})
                    image_map = await wf.generate_images(text, ctx.get("topic_type", "conceptual"), n)
                    for ph, png in image_map.items():
                        yield _sse({"type": "image", "placeholder": ph,
                                    "png_b64": base64.b64encode(png).decode("ascii")})
                    if not image_map:
                        yield _sse({"type": "step", "key": "images", "message": "配图生成失败，已跳过"})

                # 存进会话，润色后重新拼 Word 时还能带上这些图
                if image_map and sum(len(v) for v in image_map.values()) <= MAX_SESSION_IMAGE_BYTES:
                    get_store().update(req.session_id, image_map=image_map)

                docx_bytes = build_docx(text, image_map)
                yield _sse({
                    "type": "done",
                    "article": text,
                    "filename": sanitize_filename(ctx["main_keyword"]) + ".docx",
                    "docx_b64": base64.b64encode(docx_bytes).decode("ascii"),
                    "seo_title": seo.get("seo_title", ""),
                    "seo_description": seo.get("seo_description", ""),
                    "word_count": actual, "wordcount_level": level, "wordcount_message": wc_msg,
                    "grade": grade, "grade_level": g_level, "grade_message": g_msg,
                    "images": len(image_map),
                })
            except Exception as exc:
                logger.exception("article failed")
                yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# 独立环节：润色到「美国 12 年级学生能读懂」
# --------------------------------------------------------------------------- #
@router.post("/polish")
async def polish(req: PolishRequest):
    """整篇改写，只改表达不动结构。单独跑，用户点了才花这笔钱。"""
    if not (req.article or "").strip():
        raise HTTPException(status_code=400, detail="没有可润色的正文。")
    s, wf = _build(req)
    ctx = get_store().get(req.session_id or "") or {}
    language = req.language or ctx.get("language") or "English"
    ctx = {**ctx, "language": language}
    main_keyword = req.main_keyword or ctx.get("main_keyword") or "article"
    before = req.article

    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙（同时生成的文章已达上限），请稍后重试。")

    async def gen() -> AsyncIterator[str]:
        async with _sema:
            try:
                g0 = reading_grade(before, language)
                yield _sse({"type": "step", "key": "polish",
                            "message": f"润色到 12 年级可读水平…（原文 {g0 if g0 is not None else '—'} 年级）"})

                buf: list[str] = []
                async for piece in wf.stream_polish(ctx, before):
                    buf.append(piece)
                    yield _sse({"type": "chunk", "text": piece})
                text = "".join(buf)

                actual = count_words(text)
                level, wc_msg = wordcount_status(actual, ctx.get("wordcounts", 0))
                g1 = reading_grade(text, language)
                g_level, g_msg = grade_verdict(g1)
                yield _sse({"type": "grade", "grade": g1, "before": g0,
                            "level": g_level, "message": g_msg})

                # 标题被改动过才重出 SEO 元数据，没动就别多花一次调用
                seo = {}
                if extract_h1(text) and extract_h1(text) != extract_h1(before):
                    yield _sse({"type": "step", "key": "seo", "message": "标题有改动，重出 SEO 元数据…"})
                    seo = await wf.generate_seo(text, main_keyword, language)
                    yield _sse({"type": "seo", **seo, "h1": extract_h1(text)})

                image_map = (get_store().get(req.session_id or "") or {}).get("image_map") or {}
                docx_bytes = build_docx(text, image_map)
                yield _sse({
                    "type": "done",
                    "article": text,
                    "filename": sanitize_filename(main_keyword) + "-polished.docx",
                    "docx_b64": base64.b64encode(docx_bytes).decode("ascii"),
                    "word_count": actual, "wordcount_level": level, "wordcount_message": wc_msg,
                    "grade": g1, "grade_before": g0, "grade_level": g_level, "grade_message": g_msg,
                    "seo_title": seo.get("seo_title", ""),
                    "seo_description": seo.get("seo_description", ""),
                    "images": len(image_map),
                })
            except Exception as exc:
                logger.exception("polish failed")
                yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(gen(), media_type="text/event-stream")
