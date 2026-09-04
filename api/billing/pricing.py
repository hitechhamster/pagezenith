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

import os

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
#   润色 2026-09-02 从 deepseek-v4-flash 换成 gemini-3.7-flash。换的理由不是省钱
#   （润色只占单篇成本的 11%），是 **DeepSeek 根本不可用**：3 篇对拍里只有 1 篇成功，
#   一篇返回空、一篇把 2228 词截成 280 词（在句子中间断掉，靠结构护栏才拦下）。
#   根因见 providers.LOW_REASONING_TASKS 的注释——它把 max_tokens 全烧在思考上。
#   对拍结果（同一份 prompt，3 篇，work/pz-polish-bakeoff）：
#     deepseek-v4-flash       1/3 成功  127s  out 21841  FK 9.8
#     gemini-3.1-pro-preview  3/3        60s  out  2475  FK 7.7  ← 爱剁短句、擅自加形容词
#     gemini-3.7-flash        3/3        15s  out  2324  FK 8.6  ← 采用
#     gemini-3.1-flash-lite   3/3        14s  out  2586  FK 9.9  ← 与原文 90-98% 雷同，等于没干活
#   “跨模型家族改写打散文风指纹”那条旧理由随 DeepSeek 一起作废：正文用 pro、润色用
#   flash，仍是跨型号，且实测文风指纹本来就不是靠这个压住的。
TIERS: dict[str, dict[str, str]] = {
    "pro": {
        "label": "标准",
        "outline": "gemini-3.1-pro-preview",
        "article": "gemini-3.1-pro-preview",
        "polish": "gemini-3.7-flash",
        "utility": "gemini-3.1-flash-lite",
        "desc": "全网搜索 + Reddit 真实讨论 + EEAT 大纲 + 可读性润色",
    },
}
DEFAULT_TIER = "pro"

# 润色模型可用环境变量覆盖，零代码切换/回滚（气流报告线同款做法）。
# 背景：DeepSeek 偶尔把思考链当正文吐出来，且它的 reasoning token 占润色成本的九成以上。
# 换 Gemini 前先跑 work/pz-polish-bakeoff 对拍，别凭感觉切。
_POLISH_OVERRIDE = os.getenv("POLISH_MODEL", "").strip()
if _POLISH_OVERRIDE:
    TIERS["pro"]["polish"] = _POLISH_OVERRIDE


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
    ("seo-writer", "outline"):  {"pro": 2},     # 大纲（含判字数/全网搜索/Reddit/事实清单/分类）
    ("seo-writer", "revise"):   {"pro": 0},     # 前 N 次免费，超出按 REVISE_EXTRA
    ("seo-writer", "article"):  {"pro": 5},     # 正文 + SEO 元数据 + 交付前后处理
    # 润色 2026-09-03 从 3 点降到 2 点：**一篇（不含配图）总价定死 9 点**（2+5+2）。
    # 3 点是 DeepSeek 时代定的，那时假设"润色成本和写一篇差不多"；换 gemini-3.7-flash 后
    # 实测润色成本 ¥0.086，只有正文（¥0.349）的 25%，3 点明显超收。降到 2 点该动作仍有 95.7% 毛利。
    # 整篇成本 ¥0.80 → 9 点毛利 91.1%。改价前先跑一遍上面那段成本核算，别拍脑袋。
    ("seo-writer", "polish"):   {"pro": 2},     # 正文写完自动跑（不再是可选按钮）
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


def full_article_credits() -> int:
    """写完整一篇（大纲 + 正文 + 自动润色，不含配图）要多少点。

    这个数散落在首页、结账页、欢迎邮件里，以前各写各的（出现过 9 / 10 / 按 /10 估三个
    口径同时在线）。要引用就调这个函数，别再手抄。
    """
    return (price("seo-writer", "outline")
            + price("seo-writer", "article")
            + price("seo-writer", "polish"))


# 注册赠送的默认点数。9 = 一篇不带图（大纲2+正文5+润色2）；11 是用户 2026-09-04 定的，
# 留了 2 点余量。⚠️ 注意：**11 点不够写一篇带配图的**（1 张图要 +3 = 12 点，
# 界面默认的 2 张要 15 点）。想让赠送额度覆盖带图的一篇，把这个数改成 12 或 15。
SIGNUP_CREDITS_DEFAULT = 11


def signup_credits() -> int:
    """新账户注册即送的点数，默认 = 正好够完整写一篇。

    2026-09-04 加：在此之前新账户是 0 点，用户注册完立刻撞"点数不足"——两个真实
    陌生人（8/29、8/30）就是这样注册完一次没跑过。送一篇让"先试后付"名副其实。

    ⚠️ 成本敞口：注册无邮箱验证，等于任何人可以无限刷邮箱白嫖，一份约 ¥0.8 真实
    API 成本。兜底有两层：全局 ¥300/天熔断，以及这里 ——
    **被薅时把 .env 里 SIGNUP_CREDITS 设成 0，重启即关，不用改代码。**
    """
    raw = os.getenv("SIGNUP_CREDITS", "").strip()
    if raw.isdigit():
        return int(raw)
    return SIGNUP_CREDITS_DEFAULT


def price(tool: str, action: str, tier: str = DEFAULT_TIER) -> int:
    row = PRICES.get((tool, action))
    if row is None:
        return 0
    return int(row.get(tier_of(tier), row.get(DEFAULT_TIER, 0)))


def price_table() -> list[dict]:
    """给前端展示的价目（首页/工具页标价用）。

    ⚠️ 2026-09-04 修：这里以前同时吐 basic 和 pro 两列。但 TIERS 早就只剩 "pro" 一档，
    `row.get("basic", 0)` 于是对每一行都返回 0 —— 首页那张表的"基础档"整列显示 0 点，
    等于白纸黑字告诉用户基础档所有功能免费。现在只吐实际存在的那一档。
    加档位时这里要跟着改，别再让展示层去 get 一个不存在的键然后拿默认值当真。
    """
    return [{"tool": tool, "action": action, "credits": row.get(DEFAULT_TIER, 0)}
            for (tool, action), row in PRICES.items()]


# ── 成本估算（¥/百万 token，用于熔断与对账，非精确账单）──────────────
# 汇率按 1 USD ≈ 7.2 CNY。
#
# 2026-09-02 按 ai.google.dev/gemini-api/docs/pricing 官方价全表回填。
# 回填前这里是拍脑袋的估值，**低估了 2.9-4.9 倍**（flash 系列错得最狠），
# 熔断阈值因此一直是虚的。以后加模型请查官方价，别照着相邻型号猜。
#
# ⚠️ gemini-3.7-flash 现在是促销价，**2027-01-01 起翻倍**到 (10.8, 54.0)。
#    到期没改这里的话，实际支出会是账面的两倍。
MODEL_COST_CNY_PER_MTOK: dict[str, tuple[float, float]] = {
    # model: (输入, 输出)。括号内是官方 USD/M 原值。
    "gemini-3.1-flash-lite": (1.8, 10.8),    # $0.25 / $1.50
    "gemini-3.7-flash": (5.4, 27.0),         # $0.75 / $3.75（促销，2027-01-01 起 $1.50/$7.50）
    "gemini-3.1-pro-preview": (14.4, 86.4),  # $2.00 / $12.00（prompt ≤200k；超过则 $4/$18）
    "deepseek-v4-flash": (2.0, 8.0),         # ⚠️ 未核实，仅历史流水用（已停用，见 TIERS 注释）
    # 旧命名保留，历史流水还查得到
    "gemini-3.1-flash": (5.4, 27.0),
    "google/gemini-3.1-flash-lite": (1.8, 10.8),
    "google/gemini-3.1-pro": (14.4, 86.4),
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
