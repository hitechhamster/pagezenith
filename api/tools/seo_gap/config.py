"""集中配置。从环境变量 / .env 读取，所有模块共用同一个 settings 实例。"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ⚠️ .env 必须按**代码位置**定位，不能按当前工作目录。
# 2026-08-28 踩过：uvicorn 从上层目录启动时，相对路径的 ".env" 找到了另一个项目的 .env，
# 结果 USE_MOCKS 保持默认 True —— 服务看起来正常，产出的却全是 mock 文本，非常难发现。
_REPO = Path(__file__).resolve().parents[3]      # …/pagezenith
_ENV_FILES = (_REPO / ".env", _REPO / "api" / ".env", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILES, extra="ignore")

    # SERP 数据源：serper（默认，便宜一个数量级）| serpapi | dataforseo
    serp_provider: str = "serper"

    # Serper.dev（2026-08 起的默认 SERP 源，服务端出 key）
    serper_key: str = Field(
        default="", validation_alias=AliasChoices("SERPER_KEY", "SERPER_API_KEY"))
    serper_base_url: str = "https://google.serper.dev"

    # SerpApi（旧源，保留兼容，不再配置）
    serpapi_key: str = ""
    serpapi_base_url: str = "https://serpapi.com"

    # Tavily（可选）：填了就能用它把竞品页解析成更干净的正文
    tavily_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    tavily_timeout: float = 30.0

    # DataForSEO（SERP + backlinks；backlinks 目前无替代源）
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_base_url: str = "https://api.dataforseo.com"

    # Exa：2026-08 下线（额度耗尽 + Serper 一家就能搜索+抓正文）。保留字段兼容旧配置。
    exa_key: str = ""
    exa_base_url: str = "https://api.exa.ai"
    exa_timeout: float = 40.0

    # Gemini（直连 Google，走 OpenAI 兼容端点 —— 流式与 usage 回报实测都正常）
    # 不经 OpenRouter：少一层加价，key 本来就有。
    gemini_api_key: str = Field(
        default="", validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY"))
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"

    # LLM (OpenRouter, OpenAI 兼容)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "google/gemini-3.1-flash-lite"
    writer_model: str = ""  # 增补段落写作模型；留空=同 llm_model

    # DeepSeek（润色专用；**国内直连，绝不能走代理** —— 走了反而不通）
    # 别名：DEEPSEEK_API_KEY 是各项目里通行的叫法，两种都认，省得部署时对不上。
    deepseek_key: str = Field(
        default="", validation_alias=AliasChoices("DEEPSEEK_KEY", "DEEPSEEK_API_KEY"))
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    # Embedding（语义去重粗判用）。统一走 OpenRouter /embeddings，多语言模型。
    # base_url / api_key 留空则复用上面的 openrouter_*；只在需要换服务时才填。
    embedding_model: str = "openai/text-embedding-3-large"
    embedding_base_url: str = ""
    embedding_api_key: str = ""

    def embedding_endpoint(self) -> tuple[str, str]:
        """返回 (base_url, api_key)：未单独配置时复用 OpenRouter。"""
        return (
            self.embedding_base_url or self.openrouter_base_url,
            self.embedding_api_key or self.openrouter_api_key,
        )

    # 行为开关
    use_mocks: bool = True
    competitor_cache_ttl: int = 604800  # 7 天
    top_n: int = 10
    # 硬上限：实际抓取/抽取/外链的竞品页数（控额度）。生效值 = min(top_n, max_competitors)
    max_competitors: int = 10
    # 四部分报告(v2)分析前几名竞品
    report_competitors: int = 10
    # 批量模式（关键词簇）：上限关键词数、跨词去重后实际抓取的竞品数、参与 Reddit 的关键词数
    batch_max_keywords: int = 10
    batch_max_competitors: int = 12     # 跨词去重后按"命中关键词数"取前 N 个竞品抓取
    batch_reddit_keywords: int = 3      # 只对前 N 个关键词跑 Reddit（控 SerpApi 额度）

    # 外链拓客：足迹找站 + 抓邮箱 + 邮件草稿（全免费，BYO SerpApi）
    outreach_max_prospects: int = 100   # 硬上限（安全阀）；实际由 breadth 档位决定
    outreach_concurrency: int = 6       # 抓取+分析候选站的并发
    # 混合去重阈值（替换原 semantic_dup_threshold）：
    #   cosine ≥ HIGH        直接判“同一条”，不调 LLM
    #   cosine <  LOW        直接判“不同”，不调 LLM
    #   [LOW, HIGH) 的模糊对  交 LLM 裁决
    dedup_cosine_high: float = 0.88
    dedup_cosine_low: float = 0.78
    # 单次 LLM 裁边界最多处理多少对
    dedup_judge_batch: int = 25
    # 冲突保护：true 时 ≥HIGH 的对先过 surface_conflict，可疑的（数字/否定/方向）
    #   降级到 LLM 裁决带；false 时严格按 cosine 带路由（≥HIGH 直接合）。
    #   guard 只会把对“往更严格方向”降级（直接合→交 LLM），绝不跳过本该有的裁决。
    #   用于和 true 做 A/B，量化 embedding 在数字/否定上的盲区严重程度。
    dedup_conflict_guard: bool = True

    # 输出语言：zh=所有抽取文本/报告用简体中文（默认）
    output_lang: str = "zh"

    # Reddit 内容研究：发现走 SerpApi（site:reddit.com），读帖+全评论走 Arctic-Shift
    # （Pushshift 维护中继任归档，免费、无需 key、数据中心 IP 可用、新鲜到当天）。
    reddit_enabled: bool = True
    reddit_user_agent: str = "web:pagezenith:1.0 (cross-border content research)"
    arctic_base_url: str = "https://arctic-shift.photon-reddit.com"
    reddit_max_threads: int = 10        # 每个关键词最多分析几帖
    reddit_max_comments: int = 40       # 每帖按点赞取前 N 条评论
    reddit_comment_min_score: int = 1   # 评论点赞下限（滤掉 0/负分噪声）
    reddit_max_chars_per_thread: int = 6000  # 单帖正文+评论拼接后的字符上限
    reddit_timeout: float = 20.0
    reddit_cache_ttl: int = 86400       # 关键词→帖子结果缓存（秒），削减 Arctic-Shift 调用
    reddit_concurrency: int = 4         # 同时抓评论的帖子数（保护共享限流）

    # 本地 Excel 落盘：每次分析追加一行（多用户服务器上应关闭）
    excel_enabled: bool = False
    excel_path: str = "reports/seo_reports.xlsx"

    # 公开部署安全：同时进行的分析数上限（每个分析开浏览器+抓多页，防资源/账单失控）
    max_concurrent_runs: int = 2
    # SSRF 防护：禁止抓取私有/内网/元数据地址
    block_private_urls: bool = True

    # SEO 文章生成（工具⑥）：写长文的 token 上限与超时都比其他工具高
    writer_llm_model: str = "anthropic/claude-sonnet-5"   # OpenRouter 默认写作模型
    writer_deepseek_model: str = "deepseek-v4-flash"      # DeepSeek 默认写作模型
    # 配图：高质量档。实测输出 1376x768（16:9）、1120 图像 token ≈ $0.067/张 ≈ ¥0.48
    writer_image_model: str = "gemini-3-pro-image"
    writer_timeout: float = 600.0        # 写一篇 3000 词可能 3-5 分钟
    writer_search_results: int = 20      # 每个关键词取几条"红海参考"
    writer_session_ttl: int = 7200       # 三步向导之间的会话存活时间（秒）
    writer_max_sessions: int = 200       # 进程内会话数上限（超出按最旧淘汰）
    # 单独的并发闸：写文只发 HTTP 不开浏览器，比 seo_gap 轻得多，
    # 但单次要跑几分钟，用 max_concurrent_runs(=2) 会把整站堵死
    writer_max_concurrent: int = 4

    def serp_key(self) -> str:
        """当前 SERP 源实际用的 key（护栏检查用）。"""
        return {"serper": self.serper_key, "serpapi": self.serpapi_key}.get(
            self.serp_provider, self.dataforseo_login)

    def with_keys(self, openrouter_key: str | None, serpapi_key: str | None,
                  tavily_key: str | None = None, deepseek_key: str | None = None,
                  exa_key: str | None = None) -> "Settings":
        """按请求覆盖用户 key，返回新实例（绝不改全局单例，绝不落库/日志）。"""
        upd = {}
        if openrouter_key:
            upd["openrouter_api_key"] = openrouter_key
        if serpapi_key:
            upd["serpapi_key"] = serpapi_key
        if tavily_key:
            upd["tavily_key"] = tavily_key
        if deepseek_key:
            upd["deepseek_key"] = deepseek_key
        if exa_key:
            upd["exa_key"] = exa_key
        return self.model_copy(update=upd) if upd else self

    # ── 出站代理 ──────────────────────────────────────────────────
    # 两种场景都靠这一个开关：
    #   开发机（大陆）：Gemini 必须走代理
    #   香港服务器    ：**Google 不给 HK IP 用 Gemini API**（400 FAILED_PRECONDITION
    #                   "User location is not supported"），经自有纽约 VPS 中转
    # ⚠️ 分流规则不能一刀切,谁走谁不走是实测出来的：
    #   Gemini            → 走代理（唯一被地域封的）
    #   DeepSeek          → **必须直连**，走代理反而不通
    #   Serper / Reddit   → 香港直连正常，不进隧道（少一条依赖就少一个故障点）
    # 免签支付：手机到账通知转发 与 /payadmin 管理页 共用的口令（见 billing/payorders.py）
    pay_notify_token: str = ""
    # 发信（Resend）。没配就不发信，只记日志 —— 见 billing/mailer.py
    resend_api_key: str = ""
    mail_from: str = "页面科技 <noreply@pagezenith.com>"
    site_url: str = "https://pagezenith.com"
    outbound_proxy: str = ""
    # 需要走代理的目的地关键词；留空则用默认（只有 gemini/google）
    proxy_targets: str = "gemini,generativelanguage,googleapis"

    def proxy_for(self, host_or_provider: str) -> str | None:
        """返回该目的地该用的代理；不需要代理时返回 None。"""
        if not self.outbound_proxy:
            return None
        s = (host_or_provider or "").lower()
        if "deepseek" in s:
            return None
        keys = [k.strip().lower() for k in self.proxy_targets.split(",") if k.strip()]
        return self.outbound_proxy if any(k in s for k in keys) else None

    # 抓取
    fetch_timeout: float = 20.0
    request_timeout: float = 180.0  # LLM 对大页面抽取/生成可能 >60s，给足余量
    # 抓取方式：httpx（快，但易被反爬 403）| browser（Playwright，**已停用**）
    # 2026-08：香港服务器与观象台共存，只有 1.6G 内存，Chromium 峰值会把邻居 OOM 掉。
    # 竞品正文改走 Exa contents，browser 模式不再使用。
    fetch_mode: str = "httpx"
    browser_headless: bool = True       # browser 模式：True=无窗口；False=弹出真实窗口
    browser_wait_ms: int = 1800         # 导航后等 JS 渲染的毫秒数
    browser_nav_timeout_ms: int = 25000
    # 用系统已装的浏览器，免下载 Playwright 自带 Chromium：chrome | msedge | ""(自带)
    browser_channel: str = "chrome"


@lru_cache
def get_settings() -> Settings:
    return Settings()
