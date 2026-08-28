"""供应商抽象：LLM（OpenRouter / DeepSeek）、搜索（Tavily / Exa）、配图（仅 OpenRouter）。

两家 LLM 都是 OpenAI 兼容的 /chat/completions，所以只有 base_url、key、模型名不同，
一套代码通吃；两家搜索的结果都归一化成 "Title:/URL:/Content:/---" 的文本块，
下游 prompt（prompts.py）完全不用感知用的是谁。

key 由 router 按请求注入到 Settings 副本里，用完即弃，这里不做任何存储或日志。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from ..seo_gap.config import Settings

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """供应商调用失败（网络/额度/返回空），上层负责重试或转成 SSE error。"""


# --------------------------------------------------------------------------- #
# LLM
# --------------------------------------------------------------------------- #
# 各任务的输出 token 上限（沿用线下工作流的档位）
TOKEN_LIMITS = {
    "wordcount": 1500,
    "classify": 2000,
    "seo": 4000,
    "outline": 12000,
    "revise_outline": 12000,
    "article": 24000,
    "polish": 24000,
}
# 这些任务不需要模型长考，OpenRouter 上显式降低 reasoning 开销
LOW_REASONING_TASKS = {"seo", "classify", "wordcount"}

LLM_MODELS = {
    "openrouter": [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-opus-5",
        "google/gemini-3.1-pro",
        "google/gemini-3.1-flash",
        "openai/gpt-5.1",
    ],
    "deepseek": [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ],
}


# 任务 → 模型槽位。用户实战分工:大纲要创意(Gemini Pro)、润色要听指令(Claude)、
# 杂活(判字数/分类/SEO 元数据)不值得用贵模型。
TASK_SLOT = {
    "outline": "outline", "revise_outline": "outline",
    "article": "article", "polish": "polish",
}


@dataclass
class LLMTarget:
    provider: str
    base_url: str
    api_key: str
    model: str          # 兜底模型(未命中槽位时用)
    tier: str = "pro"

    def model_for_task(self, task: str) -> str:
        """按任务取模型;取不到就用兜底。"""
        from billing.pricing import model_for
        return model_for(TASK_SLOT.get(task, "utility"), self.tier) or self.model


def provider_of(model: str) -> str:
    """从模型名推断供应商。三家都是 OpenAI 兼容,只有 base_url/key 不同。"""
    if model.startswith("deepseek"):
        return "deepseek"
    if model.startswith("gemini") or model.startswith("google/"):
        return "gemini"
    return "openrouter"


def endpoint_of(s: Settings, provider: str) -> tuple[str, str]:
    if provider == "deepseek":
        return s.deepseek_base_url, s.deepseek_key
    if provider == "gemini":
        return s.gemini_base_url, s.gemini_api_key
    return s.openrouter_base_url, s.openrouter_api_key


def resolve_llm(s: Settings, tier: str = "pro", model: Optional[str] = None) -> LLMTarget:
    """解析一次调用需要的三件套。

    2026-08 两处结构变化：
    1. 模型选择权在服务端（billing.pricing.TIERS 按**任务**映射），用户只选档位；
    2. 不再绑死 OpenRouter —— 大纲/正文走 Gemini 直连、润色走 DeepSeek 直连，
       少一层加价，key 也都是现成的。供应商由模型名自动推断。
    """
    from billing.pricing import model_for as _model_for  # 延迟导入，避免循环依赖

    m = model or _model_for("article", tier)
    prov = provider_of(m)
    base, key = endpoint_of(s, prov)
    if not key and not s.use_mocks:
        raise ProviderError(f"服务端未配置 {prov} 的 API Key，请联系站长。")
    return LLMTarget(prov, base, key, m, tier=tier)


class LLM:
    """一次请求内复用的 LLM 客户端。complete() 拿完整文本，stream() 逐块吐字。"""

    def __init__(self, target: LLMTarget, settings: Settings, usage_sink=None):
        self.t = target
        self.s = settings
        # usage_sink(model, tokens_in, tokens_out)：把真实用量报给计费层，
        # 熔断和对账都读它（不是估的）。None = 不计费的场景（本地跑脚本）。
        self.usage_sink = usage_sink
        self._last_task_model = ""

    def _report(self, data: dict) -> None:
        if not self.usage_sink:
            return
        u = (data or {}).get("usage") or {}
        try:
            # 用响应里回报的真实 model 名(OpenRouter 会带),拿不到再退回槽位模型
            m = (data or {}).get("model") or self._last_task_model or self.t.model
            self.usage_sink(m, int(u.get("prompt_tokens") or 0),
                            int(u.get("completion_tokens") or 0))
        except Exception:  # noqa: BLE001  计费统计绝不能影响主流程
            logger.warning("usage_sink failed", exc_info=True)

    # ---------------------------------------------------------------- 内部
    def _route(self, task: str) -> tuple[str, str, str]:
        """按任务取 (model, base_url, api_key) —— 一次生成里会跨供应商：
        大纲/正文 Gemini、润色 DeepSeek、杂活 Gemini flash-lite。"""
        model = self.t.model_for_task(task)
        prov = provider_of(model)
        base, key = endpoint_of(self.s, prov)
        return model, (base or self.t.base_url), (key or self.t.api_key)

    def _headers(self, task: str = "general") -> dict[str, str]:
        _, _, key = self._route(task)
        h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self.t.provider == "openrouter":
            # OpenRouter 用这两个头做用量归属统计
            h["HTTP-Referer"] = "https://pagezenith.onrender.com"
            h["X-Title"] = "PageZenith SEO Writer"
        return h

    def _payload(self, prompt: str, task: str, temperature: float, stream: bool) -> dict:
        model, _, _ = self._route(task)
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": TOKEN_LIMITS.get(task, 6000),
            "stream": stream,
        }
        if self.t.provider == "openrouter" and task in LOW_REASONING_TASKS:
            payload["reasoning"] = {"effort": "low", "exclude": True}
        if stream:
            # 让流式也返回 usage（最后一个 chunk），否则长文这一大笔成本统计不到
            payload["stream_options"] = {"include_usage": True}
        self._last_task_model = payload["model"]
        return payload

    # ------------------------------------------------------------- 非流式
    async def complete(self, prompt: str, task: str = "general",
                       temperature: float = 0.7, retry: bool = True) -> str:
        """返回完整文本。失败等 5 秒重试一次（与线下工作流一致）。"""
        if self.s.use_mocks:
            return _mock_text(task)
        try:
            return await self._complete_once(prompt, task, temperature)
        except ProviderError as exc:
            if not retry:
                raise
            logger.warning("LLM %s 第一次失败，5 秒后重试: %s", task, exc)
            await asyncio.sleep(5)
            return await self._complete_once(prompt, task, temperature)

    async def _complete_once(self, prompt: str, task: str, temperature: float) -> str:
        try:
            _, base, _ = self._route(task)
            async with httpx.AsyncClient(timeout=self.s.writer_timeout, trust_env=False,
                                         proxy=self.s.proxy_for(base)) as client:
                resp = await client.post(
                    f"{base}/chat/completions",
                    headers=self._headers(task),
                    json=self._payload(prompt, task, temperature, stream=False),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(_http_error(exc)) from exc
        except Exception as exc:
            raise ProviderError(f"{self.t.provider} 调用异常: {exc}") from exc
        self._report(data)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.t.provider} 返回结构异常: {str(data)[:200]}") from exc
        if not content or not str(content).strip():
            raise ProviderError("模型返回内容为空")
        return content

    # --------------------------------------------------------------- 流式
    async def stream(self, prompt: str, task: str = "general",
                     temperature: float = 0.7) -> AsyncIterator[str]:
        """逐块吐出增量文本。写一篇长文要好几分钟，不流式用户会以为卡死。

        只在「一个字都还没吐出去」的时候才重试；已经吐了一半再重试会把文章接歪。
        """
        if self.s.use_mocks:
            for piece in _mock_text(task).split("\n"):
                await asyncio.sleep(0.01)
                yield piece + "\n"
            return

        emitted = False
        try:
            async for chunk in self._stream_once(prompt, task, temperature):
                emitted = True
                yield chunk
        except ProviderError as exc:
            if emitted:
                raise
            logger.warning("LLM %s 流式第一次失败，5 秒后重试: %s", task, exc)
            await asyncio.sleep(5)
            async for chunk in self._stream_once(prompt, task, temperature):
                yield chunk
            return
        if not emitted:
            raise ProviderError("模型返回内容为空")

    async def _stream_once(self, prompt: str, task: str,
                           temperature: float) -> AsyncIterator[str]:
        payload = self._payload(prompt, task, temperature, stream=True)
        _, base, _ = self._route(task)
        last_usage: dict | None = None
        try:
            # trust_env=False + 显式 proxy：本机有个全局 SOCKS 代理会被 httpx 自动捡走，
            # 而它对 DeepSeek 是坏的 —— 必须按供应商显式决定走不走代理。
            async with httpx.AsyncClient(timeout=self.s.writer_timeout, trust_env=False,
                                         proxy=self.s.proxy_for(base)) as client:
                async with client.stream("POST", f"{base}/chat/completions",
                                         headers=self._headers(task), json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "ignore")
                        raise ProviderError(_status_error(resp.status_code, body))
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        # ⚠️ 不能在这里 _report：Gemini 兼容层每个 chunk 都带**累计** usage，
                        #    逐块上报会把用量放大几十倍（实测 1 万 → 66 万）。只记下最后一次。
                        if obj.get("usage"):
                            last_usage = obj
                        choices = obj.get("choices") or []
                        if not choices:
                            continue
                        piece = (choices[0].get("delta") or {}).get("content")
                        if piece:
                            yield piece
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"{self.t.provider} 流式调用异常: {exc}") from exc
        finally:
            # 整条流结束才上报一次（正常结束、报错、被取消都会走到这里）
            if last_usage:
                self._report(last_usage)


def _http_error(exc: httpx.HTTPStatusError) -> str:
    return _status_error(exc.response.status_code, exc.response.text)


def _status_error(status: int, body: str) -> str:
    hint = {401: "key 无效或已失效", 402: "余额不足", 429: "触发限流，稍后再试"}.get(status, "")
    detail = (body or "")[:300]
    return f"HTTP {status}{('（' + hint + '）') if hint else ''}: {detail}"


# --------------------------------------------------------------------------- #
# 搜索（"红海参考"：给大纲和正文提供竞品语境）
# --------------------------------------------------------------------------- #
async def search(s: Settings, provider: str, query: str) -> str:
    """返回归一化后的搜索文本块；失败不抛异常，返回提示串让流程继续走。

    2026-08：默认 serper —— 它的 /search 找竞品页、/scrape 抓全文（实测一页 1.4 万字符），
    一家就顶掉了原来 Tavily+Exa 两家，而且额度便宜一个数量级。
    """
    if s.use_mocks:
        return _mock_search(query)
    try:
        if provider in ("serper", "auto"):
            return await _search_serper(s, query)
        if provider == "exa":
            return await _search_exa(s, query)
        if provider == "tavily":
            return await _search_tavily(s, query)
    except Exception as exc:
        logger.warning("搜索失败 (%s / %s): %s", provider, query, exc)
        return f"（{provider} 搜索失败：{exc}）"
    return ""


async def _search_serper(s: Settings, query: str, n_scrape: int = 4) -> str:
    """Serper 搜索 + 抓正文。只对前 n_scrape 条抓全文（抓取比搜索贵，且前几名才是"红海"）。"""
    if not s.serper_key:
        raise ProviderError("服务端未配置 SERPER_KEY")
    headers = {"X-API-KEY": s.serper_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=s.request_timeout, trust_env=False,
                                 proxy=s.proxy_for("serper")) as client:
        r = await client.post(f"{s.serper_base_url}/search",
                              headers=headers, json={"q": query, "num": 10})
        r.raise_for_status()
        items = (r.json().get("organic") or [])[: max(n_scrape, 1)]

        async def one(it: dict) -> dict:
            text = it.get("snippet") or ""
            try:
                sr = await client.post(f"{s.serper_base_url}/scrape",
                                       headers=headers, json={"url": it.get("link", "")})
                if sr.status_code == 200:
                    text = (sr.json().get("text") or text)[:4000]
            except Exception:  # noqa: BLE001  单页抓不到不影响其他
                pass
            return {"title": it.get("title", ""), "url": it.get("link", ""), "content": text}

        out = await asyncio.gather(*[one(it) for it in items])
    return _fmt(list(out))


def _fmt(results: list[dict]) -> str:
    return "\n".join(
        f"Title: {r.get('title', '')}\nURL: {r.get('url', '')}\n"
        f"Content: {r.get('content', '')}\n---"
        for r in results
    )


async def _search_tavily(s: Settings, query: str) -> str:
    if not s.tavily_key:
        raise ProviderError("缺少 Tavily API Key")
    async with httpx.AsyncClient(timeout=s.tavily_timeout) as client:
        resp = await client.post(
            f"{s.tavily_base_url}/search",
            headers={"Authorization": f"Bearer {s.tavily_key}",
                     "Content-Type": "application/json"},
            json={"query": query, "search_depth": "advanced", "topic": "general",
                  "max_results": s.writer_search_results, "include_raw_content": True},
        )
        resp.raise_for_status()
        data = resp.json()
    out = []
    for r in data.get("results", []):
        # raw_content 更完整，但很长；截断避免把 prompt 撑爆
        content = (r.get("raw_content") or r.get("content") or "")[:4000]
        out.append({"title": r.get("title", ""), "url": r.get("url", ""), "content": content})
    return _fmt(out)


async def _search_exa(s: Settings, query: str) -> str:
    if not s.exa_key:
        raise ProviderError("缺少 Exa API Key")
    async with httpx.AsyncClient(timeout=s.exa_timeout) as client:
        resp = await client.post(
            f"{s.exa_base_url}/search",
            headers={"x-api-key": s.exa_key, "Content-Type": "application/json"},
            json={"query": query, "numResults": s.writer_search_results, "type": "auto",
                  "contents": {"text": {"maxCharacters": 4000}}},
        )
        resp.raise_for_status()
        data = resp.json()
    out = []
    for r in data.get("results", []):
        text = r.get("text") or r.get("summary") or ""
        out.append({"title": r.get("title", ""), "url": r.get("url", ""), "content": text[:4000]})
    return _fmt(out)


# --------------------------------------------------------------------------- #
# 配图（Gemini 原生文生图）
# --------------------------------------------------------------------------- #
# 2026-08：从 OpenRouter 迁到 Gemini 原生。迁移的原因不是省钱 ——
# 是 OpenRouter 整个被砍掉后，key 没配，这个函数一直静默返回 None，
# 配图功能事实上死了好几周，而价目表还在卖它。
#
# 三个跟老实现不一样、缺一个都不通的地方：
#   1. 必须走代理 —— 香港机器直连 Google 会被拒（老实现完全没设代理）
#   2. 回包结构不同 —— Gemini 是 candidates[].content.parts[].inlineData
#   3. 守卫换成 gemini_api_key
async def generate_image(s: Settings, prompt: str, style_suffix: str) -> Optional[bytes]:
    """返回图片字节；失败返回 None（配图失败不该拖垮整篇文章）。"""
    if s.use_mocks:
        return None
    key = s.gemini_api_key
    if not key:
        return None
    enhanced = (f"{prompt}. {style_suffix} "
                "Wide horizontal aspect ratio (16:9).")
    payload = {
        "contents": [{"parts": [{"text": enhanced}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{s.writer_image_model}:generateContent")
    try:
        async with httpx.AsyncClient(timeout=s.writer_timeout,
                                     proxy=s.proxy_for("gemini"),
                                     trust_env=False) as client:
            resp = await client.post(url, params={"key": key}, json=payload)
            resp.raise_for_status()
            result = resp.json()
        # parts 里 text 和图混着，取第一个带数据的（下划线/驼峰两种键名都见过）
        parts = (result.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
        for part in parts:
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
        return None
    except Exception as exc:
        logger.warning("配图生成失败: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Mock（USE_MOCKS=true 时无 key 冒烟用）
# --------------------------------------------------------------------------- #
_MOCK_OUTLINE = """# 文章标题候选
1. How Do You Choose A Regulated Forex Broker?
2. What Makes A Forex Broker Trustworthy?

## 目标受众与语言风格
面向刚接触外汇交易的散户，语言直白、结论先行。

# How Do You Choose A Regulated Forex Broker?

## What Does Regulation Actually Mean?
[黄金答案句: 监管意味着券商受某个金融监管机构约束，客户资金需隔离存放。]
[预估字数: 400 字]

## How Do You Verify A License?
[黄金答案句: 到监管机构官网的公开注册库里，用牌照号反查券商名称。]
[预估字数: 450 字]

## Which Red Flags Should You Watch?
[黄金答案句: 出金拖延、盈利后封号、无法核实的牌照号是三个最强烈的危险信号。]
[预估字数: 400 字]
"""

_MOCK_ARTICLE = """# How Do You Choose A Regulated Forex Broker?

**A regulated broker is one supervised by a recognised financial authority that forces client funds to be held separately from company money.**

Choosing a broker is mostly a question of who is watching them.

## What Does Regulation Actually Mean?

**Regulation means a licensed authority can audit the broker and compensate clients when it fails.**

Regulators set capital requirements and require segregated client accounts.

## How Do You Verify A License?

**You check the licence number against the regulator's own public register, not the broker's website.**

Every major regulator publishes a searchable register.

## Which Red Flags Should You Watch?

**Delayed withdrawals, accounts frozen after a profitable run, and unverifiable licence numbers are the three strongest warning signs.**

Treat any of them as a reason to move your money out.
"""


def _mock_text(task: str) -> str:
    return {
        "wordcount": "1600",
        "classify": "conceptual",
        "outline": _MOCK_OUTLINE,
        "revise_outline": _MOCK_OUTLINE + "\n\n（已按修改意见调整）",
        "article": _MOCK_ARTICLE,
        "polish": _MOCK_ARTICLE.replace("supervised by a recognised financial authority",
                                        "watched by a real financial regulator"),
        "seo": "Title: How To Choose A Regulated Forex Broker\n"
               "Description: A practical checklist for choosing a regulated forex broker, "
               "from verifying licences to spotting withdrawal red flags.",
    }.get(task, "mock")


def _mock_search(query: str) -> str:
    return (f"Title: Mock result for {query}\nURL: https://example.com/mock\n"
            f"Content: 这是 USE_MOCKS 模式下的假搜索结果，用于无 key 冒烟测试。\n---")
