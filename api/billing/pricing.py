"""点数价目表 + 模型档位。

原则：
1. **点数罩住全部 AI 功能**，不是只有文章生成 —— 各工具成本差一个数量级
   （文章 = 6~8 次 LLM + 2 次搜索；差距分析 = SERP + 抓取 + embeddings + LLM；
   质检 = 1 次 LLM），统一按点数定价才收得住。
2. **模型选择权在服务端**。用户只选 basic / pro 档，具体模型由这里映射 ——
   否则用户人均 Opus，你人均破产。
3. 1 点 ≈ ¥1 售价。价目上线前要按真实 usage 回填复核（毛利守 60%+）。
"""

from __future__ import annotations

# ── 模型档位 ────────────────────────────────────────────────────────
# 全部走 OpenRouter（服务端 key）。用户只能看到 basic / pro。
# 单一档位（2026-08-28 用户拍板：不做基础/进阶两档，太麻烦；就一档，按最好的配）。
# **按任务选模型** —— 这是用户实战总结出来的分工：
#   大纲 = Gemini Pro（创意更好）
#   润色 = Claude（更听指令，润色的本质是保住 H 结构/占位符/链接这些约束）
#   正文 = Claude（长文质量）；杂活（判字数/分类/SEO 元数据）用便宜模型
# 选型依据：3 题材 × 3 模型实测（work/bakeoff）。
#   gemini-3.1-pro-preview 胜出：字数偏差极差仅 6%（两个 flash 分别破 30% 红线 / 在 ±25% 乱跳）、
#   写出来就 11.5 阅读年级（flash 约 16）、真的执行"用 We"（51 次 vs flash 合计 23 次）。
#   润色用 deepseek-v4-flash：13.4→12.6 只要 17 秒、结构零破坏，且**跨模型家族改写**
#   顺带打散单一模型的文风指纹（deepseek-chat 实测几乎不干活，别用）。
TIERS: dict[str, dict[str, str]] = {
    "pro": {
        "label": "标准",
        "outline": "gemini-3.1-pro-preview",
        "article": "gemini-3.1-pro-preview",
        "polish": "deepseek-v4-flash",
        "utility": "gemini-3.1-flash-lite",
        "desc": "全网搜索 + Reddit 真实讨论 + EEAT 大纲 + 可读性润色",
    },
}
DEFAULT_TIER = "pro"


def model_for(task: str, tier: str = DEFAULT_TIER) -> str:
    """按任务取模型。task ∈ outline/article/polish/utility。"""
    t = TIERS.get(tier if tier in TIERS else DEFAULT_TIER)
    return t.get(task) or t["utility"]


def tier_of(name: str | None) -> str:
    return name if name in TIERS else DEFAULT_TIER


def writer_model(tier: str) -> str:
    """兼容旧调用：默认给正文模型。按任务取请用 model_for(task, tier)。"""
    return model_for("article", tier)


def utility_model(tier: str) -> str:
    return model_for("utility", tier)


# ── 价目（点）────────────────────────────────────────────────────────
# key = (tool, action)。tier 不影响的功能两档同价。
PRICES: dict[tuple[str, str], dict[str, int]] = {
    ("seo-writer", "outline"):  {"pro": 2},     # 大纲（含判字数/全网搜索/Reddit/分类）
    ("seo-writer", "revise"):   {"pro": 0},     # 前 N 次免费，超出按 REVISE_EXTRA
    ("seo-writer", "article"):  {"pro": 5},     # 正文 + SEO 元数据  → 一篇 = 2+5 = 7 点
    ("seo-writer", "polish"):   {"pro": 3},     # 又一次整篇长文调用，独立收
    ("seo-writer", "image"):    {"pro": 3},     # 每张配图（高质量档，见下方成本）
    ("seo-gap", "analyze"):     {"pro": 5},
    ("article-quality", "check"): {"pro": 1},
    ("reddit-research", "run"): {"pro": 2},
    ("outreach", "run"):        {"pro": 2},
    ("site-recon", "run"):      {"pro": 2},
}

# 改大纲：每篇免费次数，超出每次扣的点
REVISE_FREE = 3
REVISE_EXTRA = 1


def price(tool: str, action: str, tier: str = DEFAULT_TIER) -> int:
    row = PRICES.get((tool, action))
    if row is None:
        return 0
    return int(row.get(tier_of(tier), row.get(DEFAULT_TIER, 0)))


def price_table() -> list[dict]:
    """给前端展示的价目（首页/工具页标价用）。"""
    out = []
    for (tool, action), row in PRICES.items():
        out.append({"tool": tool, "action": action,
                    "basic": row.get("basic", 0), "pro": row.get("pro", 0)})
    return out


# ── 成本估算（¥/百万 token，用于熔断与对账，非精确账单）──────────────
# 只需数量级正确：熔断阈值本身留了余量。汇率按 1 USD ≈ 7.2 CNY。
MODEL_COST_CNY_PER_MTOK: dict[str, tuple[float, float]] = {
    # model: (输入, 输出)。⚠️ 估值，上线后按 usage 表真实回填校准。
    "gemini-3.1-flash-lite": (0.5, 2.0),
    "gemini-3.1-flash": (2.2, 8.6),
    "gemini-3.7-flash": (2.2, 8.6),
    "gemini-3.1-pro-preview": (18.0, 72.0),
    "deepseek-v4-flash": (2.0, 8.0),
    # 旧的 OpenRouter 命名保留，历史流水还查得到
    "google/gemini-3.1-flash-lite": (0.5, 2.0),
    "google/gemini-3.1-pro": (18.0, 72.0),
    "anthropic/claude-sonnet-5": (21.6, 108.0),
}
_FALLBACK_COST = (5.0, 20.0)

# ── 配图成本（¥/张）────────────────────────────────────────────────
# 图片不按 token 计价，套上面那张表会算错（会 fallback 到文本单价）。
# 实测 2026-08-28：gemini-3-pro-image 出 1376x768，图像输出 1120 token，
# 官方图像输出 $60/M token → $0.067/张；加上思考与文本 token 约 $0.070，
# 按 1 USD≈7.2 CNY 算 ≈ ¥0.50/张。售 3 点（¥2.1-3.0）→ 毛利 76-83%。
MODEL_COST_CNY_PER_IMAGE: dict[str, float] = {
    "gemini-3-pro-image": 0.50,
    "gemini-3.1-flash-image": 0.50,
    "gemini-2.5-flash-image": 0.30,
}
_FALLBACK_IMAGE_COST = 0.60


def est_image_cost_cny(model: str, n: int = 1) -> float:
    return MODEL_COST_CNY_PER_IMAGE.get(model, _FALLBACK_IMAGE_COST) * max(0, int(n))


def est_cost_cny(model: str, tokens_in: int, tokens_out: int) -> float:
    cin, cout = MODEL_COST_CNY_PER_MTOK.get(model, _FALLBACK_COST)
    return (tokens_in / 1_000_000) * cin + (tokens_out / 1_000_000) * cout
