"""集中配置。从环境变量 / .env 读取，所有模块共用同一个 settings 实例。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 本地开发从 .env 读（兼容从 api/ 或仓库根目录启动）；生产用 Render 环境变量。
    model_config = SettingsConfigDict(env_file=(".env", "../.env", "../../.env"), extra="ignore")

    # SERP 数据源：serpapi | dataforseo
    serp_provider: str = "dataforseo"

    # SerpApi（SERP 专用，免费额度 250/月，无 $50 门槛）
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

    # LLM (OpenRouter, OpenAI 兼容)
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "google/gemini-3.1-flash-lite"
    writer_model: str = ""  # 增补段落写作模型；留空=同 llm_model

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

    def with_keys(self, openrouter_key: str | None, serpapi_key: str | None,
                  tavily_key: str | None = None) -> "Settings":
        """按请求覆盖用户 key，返回新实例（绝不改全局单例，绝不落库/日志）。"""
        upd = {}
        if openrouter_key:
            upd["openrouter_api_key"] = openrouter_key
        if serpapi_key:
            upd["serpapi_key"] = serpapi_key
        if tavily_key:
            upd["tavily_key"] = tavily_key
        return self.model_copy(update=upd) if upd else self

    # 抓取
    fetch_timeout: float = 20.0
    request_timeout: float = 180.0  # LLM 对大页面抽取/生成可能 >60s，给足余量
    # 抓取方式：httpx（快，但易被反爬 403）| browser（Playwright 本地 Chromium，慢但能过反爬）
    fetch_mode: str = "httpx"
    browser_headless: bool = True       # browser 模式：True=无窗口；False=弹出真实窗口
    browser_wait_ms: int = 1800         # 导航后等 JS 渲染的毫秒数
    browser_nav_timeout_ms: int = 25000
    # 用系统已装的浏览器，免下载 Playwright 自带 Chromium：chrome | msedge | ""(自带)
    browser_channel: str = "chrome"


@lru_cache
def get_settings() -> Settings:
    return Settings()
