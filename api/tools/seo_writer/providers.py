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
    "links": 3000,
    "seo": 4000,
    "outline": 12000,
    "revise_outline": 12000,
    "article": 24000,
}
# 这些任务不需要模型长考，OpenRouter 上显式降低 reasoning 开销
LOW_REASONING_TASKS = {"seo", "classify", "wordcount", "links"}

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


@dataclass
class LLMTarget:
    provider: str
    base_url: str
    api_key: str
    model: str


def resolve_llm(s: Settings, provider: str, model: Optional[str]) -> LLMTarget:
    """把「供应商 + 模型」解析成一次具体调用需要的三件套，并校验 key 是否齐。"""
    if provider == "deepseek":
        if not s.deepseek_key and not s.use_mocks:
            raise ProviderError("缺少 DeepSeek API Key，请在右上角「API Key 设置」里填写。")
        return LLMTarget("deepseek", s.deepseek_base_url, s.deepseek_key,
                         model or s.writer_deepseek_model)
    if not s.openrouter_api_key and not s.use_mocks:
        raise ProviderError("缺少 OpenRouter API Key，请在右上角「API Key 设置」里填写。")
    return LLMTarget("openrouter", s.openrouter_base_url, s.openrouter_api_key,
                     model or s.writer_llm_model)


class LLM:
    """一次请求内复用的 LLM 客户端。complete() 拿完整文本，stream() 逐块吐字。"""

    def __init__(self, target: LLMTarget, settings: Settings):
        self.t = target
        self.s = settings

    # ---------------------------------------------------------------- 内部
    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.t.api_key}", "Content-Type": "application/json"}
        if self.t.provider == "openrouter":
            # OpenRouter 用这两个头做用量归属统计
            h["HTTP-Referer"] = "https://pagezenith.onrender.com"
            h["X-Title"] = "PageZenith SEO Writer"
        return h

    def _payload(self, prompt: str, task: str, temperature: float, stream: bool) -> dict:
        payload: dict = {
            "model": self.t.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": TOKEN_LIMITS.get(task, 6000),
            "stream": stream,
        }
        if self.t.provider == "openrouter" and task in LOW_REASONING_TASKS:
            payload["reasoning"] = {"effort": "low", "exclude": True}
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
            async with httpx.AsyncClient(timeout=self.s.writer_timeout) as client:
                resp = await client.post(
                    f"{self.t.base_url}/chat/completions",
                    headers=self._headers(),
                    json=self._payload(prompt, task, temperature, stream=False),
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(_http_error(exc)) from exc
        except Exception as exc:
            raise ProviderError(f"{self.t.provider} 调用异常: {exc}") from exc
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
        try:
            async with httpx.AsyncClient(timeout=self.s.writer_timeout) as client:
                async with client.stream("POST", f"{self.t.base_url}/chat/completions",
                                         headers=self._headers(), json=payload) as resp:
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
    """返回归一化后的搜索文本块；失败不抛异常，返回提示串让流程继续走。"""
    if s.use_mocks:
        return _mock_search(query)
    try:
        if provider == "exa":
            return await _search_exa(s, query)
        if provider == "tavily":
            return await _search_tavily(s, query)
    except Exception as exc:
        logger.warning("搜索失败 (%s / %s): %s", provider, query, exc)
        return f"（{provider} 搜索失败：{exc}）"
    return ""


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
# 配图（只有 OpenRouter 有文生图；DeepSeek/Exa 都不支持）
# --------------------------------------------------------------------------- #
async def generate_image(s: Settings, prompt: str, style_suffix: str) -> Optional[bytes]:
    """返回 PNG 字节；失败返回 None（配图失败不该拖垮整篇文章）。"""
    if s.use_mocks:
        return None
    if not s.openrouter_api_key:
        return None
    enhanced = (f"{prompt}. {style_suffix} "
                "Wide horizontal aspect ratio (16:9). No text overlays unless requested.")
    payload = {
        "model": s.writer_image_model,
        "messages": [{"role": "user", "content": enhanced}],
        "modalities": ["image", "text"],
    }
    try:
        async with httpx.AsyncClient(timeout=s.writer_timeout) as client:
            resp = await client.post(
                f"{s.openrouter_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {s.openrouter_api_key}",
                         "Content-Type": "application/json",
                         "HTTP-Referer": "https://pagezenith.onrender.com",
                         "X-Title": "PageZenith SEO Writer"},
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()
        images = (result.get("choices", [{}])[0].get("message", {}) or {}).get("images") or []
        if not images:
            return None
        url = (images[0].get("image_url") or {}).get("url", "")
        if not url.startswith("data:"):
            return None
        return base64.b64decode(url.split(",", 1)[1])
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
        "links": '{"links": [{"url": "https://example.com/a", "title": "Broker Guide", '
                 '"anchor_text": "how brokers are supervised", "suggested_section": "What Does Regulation Actually Mean?"}]}',
        "seo": "Title: How To Choose A Regulated Forex Broker\n"
               "Description: A practical checklist for choosing a regulated forex broker, "
               "from verifying licences to spotting withdrawal red flags.",
    }.get(task, "mock")


def _mock_search(query: str) -> str:
    return (f"Title: Mock result for {query}\nURL: https://example.com/mock\n"
            f"Content: 这是 USE_MOCKS 模式下的假搜索结果，用于无 key 冒烟测试。\n---")
