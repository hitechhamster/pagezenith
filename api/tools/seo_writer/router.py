"""SEO 文章生成 API（前缀 /api/seo-writer）。凭 X-Card-Key 卡密鉴权 + 按点计费。

三步向导，全部走 SSE —— 写一篇 2000 词的文章要好几分钟，一次性返回的话
浏览器和 Render 都会先超时，用户也只能干等。

  POST /outline         参数 → 搜索 → 判字数 → 分类 → 流式大纲 → 返回 session_id
  POST /outline/revise  session_id + 修改意见 → 流式新大纲（前 N 次免费）
  POST /article         session_id → 流式正文 → SEO 元数据 → 配图 → Word
  POST /polish          已生成的正文 → 整篇改写到"美国 12 年级学生能读懂" → 新 Word

2026-08 卡密化改造的两个结构性变化：
1. **干活的是后台 Job，SSE 只是订阅者**（billing/jobs.py）。客户端断开不再取消任务 ——
   否则用户关个标签页，token 白烧、文章没了、钱还扣了。断线后可用 job_id 重连，
   或直接去「我的记录」取（结果已落 SQLite）。
2. **模型选择权在服务端**：请求只带 tier（basic/pro），映射见 billing/pricing.py。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from billing import jobs
from billing.deps import Card, InsufficientCredits, charge, require_card
from billing.pricing import REVISE_EXTRA, REVISE_FREE, TIERS, price, tier_of

from ..seo_gap.config import get_settings
from . import postfix, prose_audit
from .docx_export import build_docx, sanitize_filename
from .models import ArticleRequest, LANGUAGES, OutlineRequest, PolishRequest, ReviseRequest
from .providers import LLM, ProviderError, resolve_llm
from .session import get_store
from .voices import VOICES, image_style_list, recommend_voice, voice_list
from .workflow import (SEOWriter, count_words, extract_h1, grade_verdict,
                       reading_grade, wordcount_status)

# 配图字节存进会话是为了润色后能重新拼一份带图的 Word。
# 单个会话超过这个体积就不存了（宁可润色后的 Word 没图，也不让内存失控）。
MAX_SESSION_IMAGE_BYTES = 6 * 1024 * 1024
TOOL = "seo-writer"

class PolishStructureError(RuntimeError):
    """润色两次都破坏了结构 —— 触发 charge 的异常路径自动退点。"""


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/seo-writer", tags=["seo-writer"])
_sema = asyncio.Semaphore(get_settings().writer_max_concurrent)


def _build(tier: str, usage_sink=None) -> tuple[Any, SEOWriter]:
    """按档位组装 Settings + 工作流实例。搜索源固定 Serper（搜索+抓正文一家全包）。"""
    s = get_settings()
    try:
        target = resolve_llm(s, tier)
    except ProviderError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not s.serper_key and not s.use_mocks:
        raise HTTPException(status_code=500, detail="服务端未配置 SERPER_KEY，请联系站长。")
    return s, SEOWriter(s, LLM(target, s, usage_sink=usage_sink), "serper")


def _precheck(card: Card, action: str, tier: str, credits: int | None = None) -> None:
    """在起任务前先挡掉「点数不足」和「排队已满」，这样客户端拿到的是干净的 HTTP 错误。
    真正的原子扣点在 charge() 里，并发抢点由那里的条件更新兜底。"""
    need = price(TOOL, action, tier) if credits is None else credits
    if card.remaining < need:
        raise InsufficientCredits(need, card.remaining)
    if _sema.locked():
        raise HTTPException(status_code=429, detail="服务繁忙（同时生成的文章已达上限），请稍后重试。")


def _stream(job) -> StreamingResponse:
    return StreamingResponse(jobs.stream(job), media_type="text/event-stream",
                             headers={"X-Job-Id": job.id, "Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/health")
async def health() -> dict:
    s = get_settings()
    return {"status": "ok", "use_mocks": s.use_mocks, "languages": LANGUAGES,
            "tiers": [{"key": k, **v} for k, v in TIERS.items()],
            "prices": {a: {t: price(TOOL, a, t) for t in TIERS}
                       for a in ("outline", "article", "polish", "image")},
            "voices": voice_list(), "image_styles": image_style_list(),
            "revise_free": REVISE_FREE}


# --------------------------------------------------------------------------- #
# 第一步：大纲
# --------------------------------------------------------------------------- #
@router.post("/outline")
async def outline(req: OutlineRequest, card: Card = Depends(require_card)):
    if not (req.main_keyword.strip() and req.secondary_keyword.strip() and req.topic.strip()):
        raise HTTPException(status_code=400, detail="主关键词 / 次关键词 / 主题 都不能为空。")
    tier = tier_of(req.tier)
    _precheck(card, "outline", tier)

    async def work(job) -> None:
        async with _sema:
            async with charge(card, TOOL, "outline", tier, job_id=job.id) as tx:
                _, wf = _build(tier, tx.report_tokens)
                ctx: dict[str, Any] = {
                    "main_keyword": req.main_keyword.strip(),
                    "secondary_keyword": req.secondary_keyword.strip(),
                    "topic": req.topic.strip(), "specific": req.specific or "",
                    "language": req.language, "enable_images": req.enable_images,
                    "images_per_article": req.images_per_article,
                    "image_style": req.image_style or "auto",
                    "voice": req.voice or "",
                    "tier": tier, "revise_count": 0,
                }

                if req.wordcounts and req.wordcounts > 0:
                    ctx["wordcounts"] = req.wordcounts
                    job.emit({"type": "step", "key": "wordcount",
                              "message": f"字数：用户指定 {req.wordcounts}"})
                else:
                    job.emit({"type": "step", "key": "wordcount", "message": "AI 判断合适字数…"})
                    ctx["wordcounts"] = await wf.infer_wordcount(
                        ctx["main_keyword"], ctx["secondary_keyword"],
                        ctx["topic"], ctx["specific"], ctx["language"])
                    job.emit({"type": "step", "key": "wordcount",
                              "message": f"字数：AI 判断 {ctx['wordcounts']}",
                              "value": ctx["wordcounts"]})

                job.emit({"type": "step", "key": "search", "message": "全网搜索主/次关键词的现有内容…"})
                ctx["main_search"], ctx["sec_search"] = await wf.search_context(
                    ctx["main_keyword"], ctx["secondary_keyword"])

                # 社媒真实讨论：与全网搜索互补 —— 那边是竞品成品文，这边是真人原话
                job.emit({"type": "step", "key": "reddit", "message": "抓 Reddit 真实讨论（痛点/高频问题）…"})
                ctx["reddit_context"] = await wf.reddit_context(ctx["main_keyword"])
                # 把抓到的帖子结构化发给前端 —— Reddit 真人讨论和事实清单是本产品的差异点，
                # 只在日志里写一句"已纳入"等于白抓了，用户看不见价值。
                threads = re.findall(r"^\[r/([^\]]+)\]\s*(.+?)（(\d+)赞/(\d+)评）\s*$",
                                     ctx["reddit_context"] or "", re.M)
                job.emit({"type": "reddit", "count": len(threads),
                          "threads": [{"sub": a, "title": b, "score": int(c), "comments": int(d)}
                                      for a, b, c, d in threads[:8]],
                          "message": (f"已纳入 {len(threads)} 条 Reddit 真实讨论（真人原话，"
                                      f"用于找竞品没覆盖的痛点）"
                                      if threads else "没抓到相关 Reddit 讨论，跳过")})

                # 推荐产品（可选）：抓产品页正文，供大纲规划推荐位、正文写锚文本
                if (req.product_url or "").strip():
                    job.emit({"type": "step", "key": "product", "message": "读取推荐产品页…"})
                    pi = await wf.product_info(req.product_url)
                    if pi:
                        ctx["product_url"] = pi.get("url") or req.product_url
                        ctx["product_title"] = pi.get("title") or ""
                        ctx["product_content"] = pi.get("content") or ""
                        ctx["product_level"] = req.product_level
                        job.emit({"type": "step", "key": "product",
                                  "message": f"产品：{ctx['product_title'] or req.product_url}"})
                    else:
                        job.emit({"type": "step", "key": "product", "message": "产品页读取失败，已跳过"})

                # 事实清单：锁定数字与专有名称的唯一来源。不锁的话同一系统对同一事实
                # 每次给不同答案（实测「Shopify Email」被写成不存在的「Shopify Messaging」）。
                job.emit({"type": "step", "key": "facts", "message": "从资料里抽取可核实的事实…"})
                ctx["facts"] = await wf.extract_facts(ctx)
                _fact_lines = [l.strip() for l in (ctx["facts"] or "").splitlines()
                               if " — " in l or " - " in l]
                job.emit({"type": "facts", "count": len(_fact_lines),
                          "facts": ctx.get("facts", ""),
                          "message": (f"已锁定 {len(_fact_lines)} 条可核实事实（带出处），"
                                      f"正文只允许使用清单内的数字"
                                      if _fact_lines else "资料里没抽到可核实事实，跳过")})

                job.emit({"type": "step", "key": "classify", "message": "判断主题类型…"})
                ctx["topic_type"] = await wf.classify_topic_type(ctx["main_keyword"], ctx["topic"])
                job.emit({"type": "step", "key": "classify",
                          "message": f"主题类型：{ctx['topic_type']}", "value": ctx["topic_type"]})

                # 顺手推荐写手：topic_type 刚算出来，推荐不额外花钱。
                # 用户没选写手时才提，选了就尊重用户的选择，不啰嗦。
                if not ctx.get("voice"):
                    rec = recommend_voice(ctx["topic_type"])
                    job.emit({"type": "voice_hint", "voice": rec,
                              "message": f"这类主题建议用「{VOICES[rec]['role']}」写手"})

                job.emit({"type": "step", "key": "outline", "message": "生成大纲…"})
                buf: list[str] = []
                async for piece in wf.stream_outline(ctx):
                    buf.append(piece)
                    job.emit({"type": "chunk", "text": piece})

                ctx["outline"] = "".join(buf)
                sid = get_store().create(ctx)
                tx.set_result(
                    title=f"大纲：{ctx['main_keyword']}",
                    summary=(ctx["outline"] or "")[:300],
                    payload={"kind": "outline", "session_id": sid, "outline": ctx["outline"],
                             "main_keyword": ctx["main_keyword"],
                             "secondary_keyword": ctx["secondary_keyword"],
                             "topic": ctx["topic"], "language": ctx["language"],
                             "wordcounts": ctx["wordcounts"], "topic_type": ctx["topic_type"],
                             "tier": tier},
                )
                job.emit({"type": "done", "session_id": sid, "outline": ctx["outline"],
                          "wordcounts": ctx["wordcounts"], "topic_type": ctx["topic_type"],
                          "facts": ctx.get("facts", ""),
                          "result_id": tx.result_id, "charged": tx.credits})

    return _stream(jobs.start(card.key_hash, TOOL, work))


# --------------------------------------------------------------------------- #
# 第二步：改大纲（前 REVISE_FREE 次免费，之后每次 REVISE_EXTRA 点）
# --------------------------------------------------------------------------- #
@router.post("/outline/revise")
async def outline_revise(req: ReviseRequest, card: Card = Depends(require_card)):
    if not req.feedback.strip():
        raise HTTPException(status_code=400, detail="请填写修改意见。")
    ctx = get_store().get(req.session_id)
    if ctx is None:
        raise HTTPException(status_code=410, detail="会话已过期，请回到第一步重新生成大纲。")
    if req.outline:
        ctx["outline"] = req.outline      # 用户在页面上手改过大纲，以页面为准

    tier = tier_of(ctx.get("tier") or req.tier)
    used = int(ctx.get("revise_count") or 0)
    cost = 0 if used < REVISE_FREE else REVISE_EXTRA
    _precheck(card, "revise", tier, credits=cost)

    async def work(job) -> None:
        async with _sema:
            async with charge(card, TOOL, "revise", tier, credits=cost, job_id=job.id) as tx:
                _, wf = _build(tier, tx.report_tokens)
                buf: list[str] = []
                async for piece in wf.stream_revise(ctx, req.feedback.strip()):
                    buf.append(piece)
                    job.emit({"type": "chunk", "text": piece})
                new_outline = "".join(buf)
                # 修订是整篇重生成，好的节会无声消失（实测三版连丢）。
                # 锁定节功能上线前，先把「删了哪些节」摆到用户面前，让他能拒绝。
                diff = wf.outline_section_diff(ctx.get("outline", ""), new_outline)
                if diff["removed"]:
                    job.emit({"type": "section_diff", "level": "warn", **diff,
                              "message": (f"这次修订删掉了 {len(diff['removed'])} 个小节："
                                          f"{'；'.join(diff['removed'][:4])}"
                                          f"{'…' if len(diff['removed']) > 4 else ''}"
                                          f"。如果其中有你想保留的，在修改意见里写明「保留 X 节」再改一次。")})
                get_store().update(req.session_id, outline=new_outline, revise_count=used + 1)
                left = max(0, REVISE_FREE - (used + 1))
                job.emit({"type": "done", "session_id": req.session_id, "outline": new_outline,
                          "charged": cost, "free_revises_left": left, "section_diff": diff,
                          "message": (f"还可免费修改 {left} 次" if left
                                      else f"免费次数已用完，之后每次改大纲 {REVISE_EXTRA} 点")})

    return _stream(jobs.start(card.key_hash, TOOL, work))


# --------------------------------------------------------------------------- #
# 第三步：出文
# --------------------------------------------------------------------------- #
@router.post("/article")
async def article(req: ArticleRequest, card: Card = Depends(require_card)):
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
            "image_style": req.image_style or "auto",
            "voice": req.voice or "",
            "tier": req.tier,
        }
    if req.outline:
        ctx["outline"] = req.outline
    if not ctx.get("outline"):
        raise HTTPException(status_code=400, detail="还没有大纲，请先完成第一步。")

    tier = tier_of(ctx.get("tier") or req.tier)
    s = get_settings()
    want_images = bool(ctx.get("enable_images"))
    if want_images and not s.gemini_api_key and not s.use_mocks:
        want_images = False
    n_images = int(ctx.get("images_per_article", 2)) if want_images else 0

    # 正文 + 配图一次性算清楚：图是按张收费的，别让用户点完才发现点数不够
    total = price(TOOL, "article", tier) + n_images * price(TOOL, "image", tier)
    _precheck(card, "article", tier, credits=total)

    async def work(job) -> None:
        async with _sema:
            async with charge(card, TOOL, "article", tier, credits=total, job_id=job.id) as tx:
                _, wf = _build(tier, tx.report_tokens)
                job.emit({"type": "step", "key": "article", "message": "撰写文章…"})
                buf: list[str] = []
                async for piece in wf.stream_article(ctx):
                    buf.append(piece)
                    job.emit({"type": "chunk", "text": piece})
                text = "".join(buf)

                actual = count_words(text)
                level, wc_msg = wordcount_status(actual, ctx.get("wordcounts", 0))
                job.emit({"type": "wordcount", "actual": actual,
                          "target": ctx.get("wordcounts", 0), "level": level, "message": wc_msg})

                grade = reading_grade(text, ctx["language"])
                g_level, g_msg = grade_verdict(grade)
                job.emit({"type": "grade", "grade": grade, "level": g_level, "message": g_msg})

                # 清单外的数字：提示不拦截 —— 清单不可能穷尽所有合理数字，交给人判断
                unlisted = prose_audit.unlisted_numbers(text, ctx.get("facts", ""))
                if unlisted:
                    job.emit({"type": "facts_warn", "values": unlisted,
                              "level": "warn",
                              "message": (f"有 {len(unlisted)} 个数字不在事实清单里，"
                                          f"发布前请核实：{'、'.join(unlisted[:6])}")})

                job.emit({"type": "step", "key": "seo", "message": "生成 SEO 标题与描述…"})
                seo = await wf.generate_seo(text, ctx["main_keyword"], ctx["language"])
                job.emit({"type": "seo", **seo, "h1": extract_h1(text)})

                image_map: dict[str, bytes] = {}
                if n_images:
                    job.emit({"type": "step", "key": "images", "message": f"生成 {n_images} 张配图…"})
                    image_map = await wf.generate_images(
                        text, ctx.get("topic_type", "conceptual"), n_images,
                        ctx.get("image_style") or "auto")
                    # 成本入账：图片按张算，不走 report_tokens（那按 token 单价，会算错）
                    if image_map:
                        tx.report_image(s.writer_image_model, len(image_map))
                    for ph, png in image_map.items():
                        job.emit({"type": "image", "placeholder": ph,
                                  "png_b64": base64.b64encode(png).decode("ascii")})
                    # 少出几张就退几张的点。以前不管出没出都按 n_images 收 —— 要 3 张只
                    # 出 1 张照收 3 张的钱，全挂了也一点不退，跟首页"生成失败自动退点"
                    # 直接打架。这是最容易被用户抓到的超收。
                    missing = n_images - len(image_map)
                    if missing > 0:
                        back = tx.refund_credits(missing * price(TOOL, "image", tier))
                        job.emit({"type": "step", "key": "images",
                                  "message": (f"配图生成失败，已跳过（退回 {back} 点）" if not image_map
                                              else f"只生成了 {len(image_map)}/{n_images} 张，"
                                                   f"少的已退回 {back} 点")})

                if image_map and sum(len(v) for v in image_map.values()) <= MAX_SESSION_IMAGE_BYTES:
                    get_store().update(req.session_id, image_map=image_map)

                # 交付前后处理：假经验句（纯代码三分支）+ 塞词（单句改写 + 检测闭环，两轮不过就删）。
                # 这两件事 prompt 管了 5 轮没管住；检测器已 100% 命中，改为代码收口。
                # 实测三篇：塞词 5/3/3→0，假经验 1→0，字数 -1%，¥0.004/篇。改了什么原样告诉用户。
                text, fixes = await postfix.postfix(
                    text, [ctx.get("main_keyword", ""), ctx.get("secondary_keyword", "")],
                    ctx.get("facts", ""), wf.llm.complete)
                if fixes:
                    job.emit({"type": "step", "key": "postfix",
                              "message": (f"交付前修正 {len(fixes)} 处："
                                          + "；".join(f[:44] for f in fixes[:3])
                                          + ("…" if len(fixes) > 3 else ""))})

                docx_bytes = build_docx(text, image_map)
                payload = {
                    "kind": "article",
                    "article": text,
                    "filename": sanitize_filename(ctx["main_keyword"]) + ".docx",
                    "seo_title": seo.get("seo_title", ""),
                    "seo_description": seo.get("seo_description", ""),
                    "word_count": actual, "wordcount_level": level, "wordcount_message": wc_msg,
                    "grade": grade, "grade_level": g_level, "grade_message": g_msg,
                    "images": len(image_map), "tier": tier,
                    "main_keyword": ctx["main_keyword"], "language": ctx["language"],
                }
                # 正文落库（Word 可由正文随时重建，不存二进制，省库）
                tx.set_result(title=extract_h1(text) or ctx["main_keyword"],
                              summary=f"{actual} 词 · {tier} · {ctx['language']}",
                              payload=payload)
                job.emit({"type": "done", **payload,
                          "docx_b64": base64.b64encode(docx_bytes).decode("ascii"),
                          "result_id": tx.result_id, "charged": tx.credits})

    return _stream(jobs.start(card.key_hash, TOOL, work))


# --------------------------------------------------------------------------- #
# 独立环节：润色到「美国 12 年级学生能读懂」
# --------------------------------------------------------------------------- #
@router.post("/polish")
async def polish(req: PolishRequest, card: Card = Depends(require_card)):
    """整篇改写，只改表达不动结构。单独跑，用户点了才花这笔钱。"""
    if not (req.article or "").strip():
        raise HTTPException(status_code=400, detail="没有可润色的正文。")
    sess = get_store().get(req.session_id or "") or {}
    tier = tier_of(sess.get("tier") or req.tier)
    _precheck(card, "polish", tier)

    language = req.language or sess.get("language") or "English"
    ctx = {**sess, "language": language}
    if req.voice:                    # 前端回传优先：会话丢了也不能把文风弄丢
        ctx["voice"] = req.voice
    main_keyword = req.main_keyword or sess.get("main_keyword") or "article"
    before = req.article

    async def work(job) -> None:
        async with _sema:
            async with charge(card, TOOL, "polish", tier, job_id=job.id) as tx:
                _, wf = _build(tier, tx.report_tokens)
                g0 = reading_grade(before, language)
                job.emit({"type": "step", "key": "polish",
                          "message": f"润色到 12 年级可读水平…（原文 {g0 if g0 is not None else '—'} 年级）"})

                buf: list[str] = []
                async for piece in wf.stream_polish(ctx, before):
                    buf.append(piece)
                    job.emit({"type": "chunk", "text": piece})
                text = "".join(buf)

                # 数字护栏：润色只准删/拆/重排/换说法，不准新增信息。
                # 新增的数字一律是编的（正文那边才有事实清单），直接退回润色前的稿子 ——
                # 一个编造的数字比"没润色"糟得多。不重试：这是模型的稳定倾向，重试还会犯。
                added = wf.polish_added_numbers(before, text)
                if added:
                    job.emit({"type": "step", "key": "polish",
                              "message": (f"润色新增了原文没有的数字（{'、'.join(added[:4])}），"
                                          f"已保留润色前的版本。")})
                    job.emit({"type": "reset"})
                    text = before

                # 结构护栏：润色模型偶尔会把 H2 全删掉（实测过），而且时灵时不灵。
                # 坏了就重跑一次；再坏就退还原稿 —— 宁可不润，也不能交一堵散文墙。
                broke = wf.polish_broke_structure(before, text)
                if broke:
                    job.emit({"type": "step", "key": "polish",
                              "message": f"润色破坏了结构（{broke}），正在重试…"})
                    job.emit({"type": "reset"})
                    buf = []
                    async for piece in wf.stream_polish(ctx, before, strict=True):
                        buf.append(piece)
                        job.emit({"type": "chunk", "text": piece})
                    text = "".join(buf)
                    broke2 = wf.polish_broke_structure(before, text)
                    if broke2:
                        job.emit({"type": "step", "key": "polish",
                                  "message": f"重试仍破坏结构（{broke2}），已保留润色前的版本，本次不扣点。"})
                        raise PolishStructureError(broke2)

                actual = count_words(text)
                level, wc_msg = wordcount_status(actual, ctx.get("wordcounts", 0))
                g1 = reading_grade(text, language)
                g_level, g_msg = grade_verdict(g1)
                job.emit({"type": "grade", "grade": g1, "before": g0,
                          "level": g_level, "message": g_msg})

                # 标题被改动过才重出 SEO 元数据，没动就别多花一次调用
                seo: dict[str, str] = {}
                if extract_h1(text) and extract_h1(text) != extract_h1(before):
                    job.emit({"type": "step", "key": "seo", "message": "标题有改动，重出 SEO 元数据…"})
                    seo = await wf.generate_seo(text, main_keyword, language)
                    job.emit({"type": "seo", **seo, "h1": extract_h1(text)})

                image_map = (get_store().get(req.session_id or "") or {}).get("image_map") or {}
                # 交付前后处理：假经验句（纯代码三分支）+ 塞词（单句改写 + 检测闭环，两轮不过就删）。
                # 这两件事 prompt 管了 5 轮没管住；检测器已 100% 命中，改为代码收口。
                # 实测三篇：塞词 5/3/3→0，假经验 1→0，字数 -1%，¥0.004/篇。改了什么原样告诉用户。
                text, fixes = await postfix.postfix(
                    text, [ctx.get("main_keyword", ""), ctx.get("secondary_keyword", "")],
                    ctx.get("facts", ""), wf.llm.complete)
                if fixes:
                    job.emit({"type": "step", "key": "postfix",
                              "message": (f"交付前修正 {len(fixes)} 处："
                                          + "；".join(f[:44] for f in fixes[:3])
                                          + ("…" if len(fixes) > 3 else ""))})

                docx_bytes = build_docx(text, image_map)
                payload = {
                    "kind": "polish",
                    "article": text,
                    "filename": sanitize_filename(main_keyword) + "-polished.docx",
                    "word_count": actual, "wordcount_level": level, "wordcount_message": wc_msg,
                    "grade": g1, "grade_before": g0, "grade_level": g_level, "grade_message": g_msg,
                    "seo_title": seo.get("seo_title", ""),
                    "seo_description": seo.get("seo_description", ""),
                    "images": len(image_map), "tier": tier, "main_keyword": main_keyword,
                    "language": language,
                }
                tx.set_result(title=(extract_h1(text) or main_keyword) + "（润色版）",
                              summary=f"{actual} 词 · 阅读年级 {g0 or '—'} → {g1 or '—'}",
                              payload=payload)
                job.emit({"type": "done", **payload,
                          "docx_b64": base64.b64encode(docx_bytes).decode("ascii"),
                          "result_id": tx.result_id, "charged": tx.credits})

    return _stream(jobs.start(card.key_hash, TOOL, work))
