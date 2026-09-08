"""内部 BYOK 模式：拿自己的 OpenRouter + Serper key 跑文章生成流水线。

为什么存在：线上是账户点数制（服务端统一出 key，见 `billing/`），我们自己写文章时
既不想扣自己站的点，也不想烧服务端那把 key 的额度。BYOK 让内部人填两把自己的 key
直接跑，**完全绕开计费**。

⚠️ 正因为绕开计费，它必须自带门禁 —— 否则就是一个「谁摸到就能白嫖」的免费入口。
门禁 = `INTERNAL_PATH` 环境变量，一串随便生成的乱码，**同时充当两个角色**：

  · 页面地址：站点根下的 `/<乱码>`，猜不到就进不去（不对的一律 404，不给任何提示）
  · 请求凭证：页面把地址里那串乱码原样放进请求头，后端拿它跟 env 比对

于是内部人只要收藏一条 URL，不用再记口令。**没配就整个模式关闭**
（不是"默认放行"，是"默认不存在"）。

为什么不能只藏页面：能白嫖的是 `/api/seo-writer/*` 那几个端点，不是那张 HTML。
页面藏起来、API 敞着，等于没锁 —— 所以凭证该带还得带，只是不用人手输。

代价说清楚：秘密进了 URL，就会落进浏览器历史、书签、以及沿途任何中间层的访问日志。
这一页不引任何外部资源（字体都在 web/assets/fonts），所以不会顺着 Referer 漏给第三方。
想换就改 env 重启，旧地址当场失效。

与线上模式的区别，全部集中在这里，工具流水线本身一行不改：

  1. LLM 全部走 OpenRouter（`force_llm_provider`）—— 线上那套模型名是 Gemini 直连
     命名（`gemini-3.1-pro-preview`），而 `provider_of()` 会把 `google/` 前缀也判成
     Gemini 直连，所以光换模型名不够，必须显式钉死供应商。
  2. Serper 用用户自己那把，**不碰服务端的 key 池**（`serper_pool` 读的是
     `serper_keys`，只覆盖 `serper_key` 会漏，两个都要覆盖）。
  3. 服务端的 Gemini / DeepSeek / SerpApi key 一律**清空**。不清的话，模型名或分支
     一旦没命中预期，就会静默回落到公司的 key —— BYOK 的意义当场归零，而且没人看得见。
  4. 不扣点、不落库、不进「我的记录」、不受全局熔断与单卡日限约束。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request

from tools.seo_gap.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 页面地址 + 请求凭证（同一串）。留空 = BYOK 整个不存在（所有请求走正常的账户/点数路径）。
# 两头的斜杠都剥掉，这样 env 里写成 `abc` 还是 `/abc/` 都能用。
INTERNAL_PATH = os.environ.get("INTERNAL_PATH", "").strip().strip("/")

HEADER_TOKEN = "X-Internal-Token"
HEADER_OPENROUTER = "X-Openrouter-Key"
HEADER_SERPER = "X-Serper-Key"
HEADER_MODELS = "X-Byok-Models"          # JSON: {"outline":..,"article":..,...}

# 默认模型 = 线上那套 Gemini 阵容在 OpenRouter 上的对应写法。
# 2026-09-08 对着 openrouter.ai/api/v1/models（公开端点，无需 key）逐个核对过，
# 五个 ID 当时都在，image 那个的 output_modalities 也确实含 image。
# OpenRouter 会下架 / 改名模型，所以页面上五个框**都可编辑** ——
# 哪天某个失效，在页面上改一下就能跑，不用改代码重启。
DEFAULT_MODELS: dict[str, str] = {
    "outline": "google/gemini-3.1-pro-preview",
    "article": "google/gemini-3.1-pro-preview",
    "polish": "google/gemini-3.7-flash",
    "utility": "google/gemini-3.1-flash-lite",
    "image": "google/gemini-3-pro-image",
}
SLOTS = tuple(DEFAULT_MODELS)


@dataclass
class ByokConfig:
    """一次 BYOK 请求带来的全部配置。只活在内存里，不落库、不打日志。"""

    openrouter_key: str
    serper_key: str
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))

    @property
    def identity(self) -> str:
        """任务归属用的合成身份（`jobs.get()` 靠它防止别人拿到你的 job）。

        取 key 的哈希而不是口令的：同一个内部人换台机器用同一把 key，
        断线重连还能取回自己的任务；换了 key 就是另一个身份，互相看不见。
        """
        return "byok:" + hashlib.sha256(self.openrouter_key.encode()).hexdigest()[:16]

    def model_for(self, slot: str) -> str:
        return self.models.get(slot) or DEFAULT_MODELS.get(slot, "")


def enabled() -> bool:
    return bool(INTERNAL_PATH)


def _parse_models(raw: str) -> dict[str, str]:
    """解析前端传来的模型表。只认已知槽位，其余忽略；坏 JSON 直接用默认值。"""
    models = dict(DEFAULT_MODELS)
    if not raw:
        return models
    try:
        got = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("BYOK 模型表不是合法 JSON，已用默认值")
        return models
    if not isinstance(got, dict):
        return models
    for slot in SLOTS:
        v = got.get(slot)
        if isinstance(v, str) and v.strip():
            models[slot] = v.strip()
    return models


def _credential_ok(request: Request) -> bool:
    """请求头里那串乱码对不对。用 compare_digest 比，别给计时攻击留缝。"""
    got = (request.headers.get(HEADER_TOKEN) or "").strip().strip("/")
    return bool(INTERNAL_PATH and got and hmac.compare_digest(got, INTERNAL_PATH))


def require_token(request: Request) -> None:
    """只校验凭证、不要求带 key。给「取默认模型表」这类只读内部接口用。

    不对一律 404 而不是 401 —— 只读接口没有"提示你重试"的必要，
    对外表现成"这个路径不存在"最省事。
    """
    if not _credential_ok(request):
        raise HTTPException(status_code=404)


def parse(request: Request) -> Optional[ByokConfig]:
    """从请求头解出 BYOK 配置。

    返回 None = 这不是 BYOK 请求，调用方继续走正常的账户/点数路径。
    抛错 = 这**是**一个 BYOK 请求但不合法（地址错 / 少 key），不能静默降级成
    正常路径 —— 那样内部人会以为在用自己的 key，实际扣的是站里的点。
    """
    if not (request.headers.get(HEADER_TOKEN) or "").strip():
        return None                       # 没带凭证 = 普通用户，正常放行

    if not _credential_ok(request):
        # 地址不对，或站点根本没开 BYOK。一律 404：别泄露"这里有个内部模式"，
        # 也别把它变成一个可以拿来枚举乱码的提示器。
        logger.warning("BYOK 凭证不正确（来源 %s）",
                       request.client.host if request.client else "-")
        raise HTTPException(status_code=404)

    openrouter = (request.headers.get(HEADER_OPENROUTER) or "").strip()
    serper = (request.headers.get(HEADER_SERPER) or "").strip()
    if not openrouter:
        raise HTTPException(status_code=400, detail="BYOK 模式需要填 OpenRouter Key。")
    if not serper:
        raise HTTPException(status_code=400, detail="BYOK 模式需要填 Serper Key。")

    return ByokConfig(openrouter_key=openrouter, serper_key=serper,
                      models=_parse_models(request.headers.get(HEADER_MODELS) or ""))


def settings_for(cfg: ByokConfig) -> Settings:
    """把用户的 key 灌进一份**新的** Settings（绝不改全局单例）。

    清空服务端 key 的那几行是这个函数的重点，不是顺手写的：
    只要留着，任何一个没走到 OpenRouter 分支的调用都会悄悄花公司的钱。
    """
    s = get_settings()
    return s.model_copy(update={
        # —— 用户自带 ——
        "openrouter_api_key": cfg.openrouter_key,
        "serper_key": cfg.serper_key,
        "serper_keys": cfg.serper_key,       # key 池读的是这个，只写上面那个会被服务端的池盖掉
        "serp_provider": "serper",
        "writer_image_model": cfg.model_for("image"),
        # —— 钉死供应商：模型名一律按 OpenRouter 解析 ——
        "force_llm_provider": "openrouter",
        # —— 服务端 key 全部清空，杜绝静默回落 ——
        "gemini_api_key": "",
        "deepseek_key": "",
        "serpapi_key": "",
        "tavily_key": "",
        "exa_key": "",
        # 写手线不用 embedding（grep 过，零命中），这里只是把回落路径一起堵死
        "embedding_base_url": s.openrouter_base_url,
        "embedding_api_key": cfg.openrouter_key,
        # 服务端万一处在 mock 模式，内部人要的是真产出，不是假文本
        "use_mocks": False,
    })
