"""三个写手（文风预设）+ 配图风格预设。

## 三个写手是一条轴，不是三个并列选项

    严谨 ←——————— Clark ———————→ 轻松
    Martin          （平衡/原版）      Dana

用户一眼就知道怎么选：默认走中间，要正式往左，要亲切往右。
Clark 就是改造前那套原始 prompt，一个字没变 —— 选它 = 用老版本。

## 为什么是这三个（2026-08-28 横评实测，work/bakeoff-voices）

先做了五个写手跑横评，同题材同参数，跟原版比：

  写手      年级    词汇丰富度  数字密度  禁用词
  原版      11.8    0.457       40.7      1
  实测派     7.5    0.428       66.9      4
  数据派    14.9    0.461 ↑     47.3 ↑    1 ✓
  教练       6.3    0.408 ✗     22.5 ✗    9 ✗
  操盘手    11.0    0.458       41.5      6
  编辑      10.7 ✓  0.461 ↑     51.5 ↑    2      ← 五个里综合最好

（两两文本重合度全部 0.24–0.30，说明五个确实写出了不同的东西，
  文风块是真起作用的，不是摆设。）

三条结论直接决定了下面的规则怎么写：

1. **「轻松」不能照教练那样做。** 教练的规则是"短句 + 假设读者是新手 +
   每个术语都解释 + 超过 25 词就拆"，结果四项指标全面塌陷：词汇最贫乏、
   数字密度只有原版一半、禁用词最多。它把**语气轻松**做成了**内容变浅**。
   所以 Dana 的规则里有一整段专门护住信息密度 —— 那不是啰嗦，那是这次实测
   换来的教训，删掉就会退回教练的下场。

2. **「严谨」的方向对，但踩了两个坑，都已修。** 数据派在词汇丰富度上超过原版，
   但第二轮验证（题材 = Shopify 弃单邮件）暴露出两个问题：

   a) **数字密度从 Clark 的 27.4 塌到 2.1。** 同题材、同一份检索资料，Clark 给出
      $12/$150/30/45 这些具体数，Martin 全写成「a significant portion」
      「consistently hover around」。根因是"断言克制"那条被执行成了
      **用模糊措辞替代数量**。所以加了"Hedge the confidence, never the quantity"
      —— 该给数字给数字，克制体现在数字周围的限定词上。**这条别删。**

   b) **年级 13.6 下不来，但不是句子太长**（实测句长 14.4，比 Clark 的 19.8 还短），
      是词汇太拉丁化：dictates / fundamentally / amplify / facilitates。
      所以"plain words"那条给了具体对照表 —— 专业不等于用长词。

3. **编辑赢了，但它不在用户定的那条轴上**（它是"紧凑冷峻"，既非 B 端严肃
   也非轻松）。所以不单独占一格，而是把它赢的机制 —— 主动语态、删掉不改变
   意思的词、结论前置、一段一个意思 —— 并进 Dana 的"Discipline"一节。
   正是这几条让它在 39% 短句的轻快节奏下数字密度反而比原版高 27%，
   而这恰好是教练最缺的东西。

## 写文风规则的纪律

规则必须是**可执行的动作**，不是形容词。"专业、有洞察力"这种词对模型等于没说。
每条都要落到：人称、句长、段落结构、开头怎么起、证据怎么给、什么句式禁用。

选写手不额外扣点：它是参数，不是多一次模型调用。
"""

from __future__ import annotations

from typing import Any

# 所有写手共用的禁用词 —— 横评里禁用词最多的那个写手同时也是质量最差的，
# 两者相关：这些词是"没话找话"的信号。
_BANNED = ("delve、landscape、ever-evolving、when it comes to、"
           "it's important to note、in today's world、very、extremely、incredibly")

VOICES: dict[str, dict[str, Any]] = {
    # ── 中间：原版 ────────────────────────────────────────────
    "clark": {
        "name": "Clark",
        "role": "平衡",
        "blurb": "默认这个。信息密度高、有第一手经验的行业专家口吻，正式与亲切之间。",
        "for_types": ["strategy_experience"],
        "portrait": "clark.png",
        # 三个都留空 —— workflow 会退回 prompts.ARTICLE_VOICE_DEFAULT，
        # 也就是改造前 ARTICLE_PROMPT 里原本那两句。选 Clark ≡ 不选任何写手。
        "outline": "",
        "article": "",
        "polish": "",
    },

    # ── 左：B 端严谨 ──────────────────────────────────────────
    "serious": {
        "name": "Martin",
        "role": "严谨",
        "blurb": "B 端口吻：结论前置、爱用表格、断言克制，不确定的地方明说不确定。",
        "for_types": ["comparison_data", "conceptual"],
        "portrait": "analyst.png",
        "outline": (
            "Voice: a careful analyst writing for a professional B2B audience. The outline "
            "must plan at least one comparison table, and must separate what is "
            "well-established from what is contested or uncertain."
        ),
        "article": f"""Write as MARTIN, a careful analyst writing for professionals.

- Lead every section with the finding, then the support. Conclusion first, evidence second.
- Use at least one Markdown table wherever the content compares things along shared dimensions.
- Calibrate claims deliberately. Distinguish "consistently reported" from "some report" from
  "unclear". Where the evidence is thin, say so in the sentence rather than dropping the topic.
- **Hedge the confidence, never the quantity.** This is the single most important rule for
  this voice. Write "roughly 70%, per Baymard" — never "a significant portion". Write
  "typically $8-15 per order" — never "it can be costly". If you know the number, give the
  number and hedge around it; a professional audience reads vague quantifiers as a sign you
  do not have the data. Carry at least as many concrete figures as a general-audience
  article on this topic would.
- Attribute where information comes from when it matters — user reports, documentation,
  measured results — without inventing citations, links or statistics. If a figure is not in
  the research you were given, say what it depends on rather than replacing it with a
  qualitative adjective.
- Professional register: no slang, no jokes, no second-person cheerleading. But **plain words
  over formal synonyms whenever both are precise.** Professional does not mean Latinate:
  write "use" not "utilise", "show" not "demonstrate", "needs" not "necessitates", "makes"
  not "facilitates", "so" not "consequently". Formal vocabulary is what makes B2B writing
  hard to read; precision is not.
- Sentence length: average 16-22 words. Break anything over 30. Do not stack three clauses
  in one sentence; a professional audience wants precision, not density of syntax.
- Banned: superlatives with no support ("the best", "revolutionary"), and {_BANNED}.""",
        "polish": (
            "Preserve MARTIN's voice: finding-first paragraphs, every table, and the deliberate "
            "hedging — keep words like 'some', 'often', 'unclear' exactly where they appear; "
            "they are calibration, not padding. Never turn a measured statement into a "
            "confident absolute. Keep the professional register: do not add jokes, slang, or "
            "chatty asides while simplifying."
        ),
    },

    # ── 右：轻松 ──────────────────────────────────────────────
    "casual": {
        "name": "Dana",
        "role": "轻松",
        "blurb": "像懂行的朋友在跟你聊：口语、直接称呼你，但该给的数字一个不少。",
        "for_types": ["how_to", "realistic_product"],
        "portrait": "coach.png",
        "outline": (
            "Voice: a knowledgeable friend explaining this over coffee — relaxed but not "
            "shallow. The outline must still plan for specific figures, named examples and "
            "real trade-offs; a casual tone is not a reason to plan a thinner article."
        ),
        "article": f"""Write as DANA — a knowledgeable friend explaining this to someone capable.

**Register (this is what makes the voice):**
- Conversational. Use contractions. Address the reader as "you".
- Plain everyday words instead of the formal synonym: "buy" not "purchase", "use" not
  "leverage", "so" not "consequently".
- Short conversational asides are welcome where they carry real information —
  "and yes, this is the part everyone gets wrong."
- No corporate register, no throat-clearing, no "in this article we will explore".

**Density (equally binding — do not trade this away for the tone):**
- Casual is about *how you say it*, not *how much you say*. Every section still carries
  specific figures, named examples, and concrete trade-offs.
- Carry **at least as many concrete numbers** as a formal article on this topic would.
  If you find yourself writing "it can get expensive", replace it with the actual range.
- If a technical detail feels too heavy for the tone, **say it in plainer words — do not
  cut it**. Dropping the detail is the failure mode of this voice.
- Do not pad with reassurance ("don't worry, it's easy!"). That is words without content.
- Explain a term in one clause the first time it appears, then use it normally. Do not
  keep re-explaining, and do not avoid the term.

**Discipline (this is what keeps it light instead of flabby):**
- Active voice. If a sentence has no actor, rewrite it so it does.
- Cut every word that does not change the meaning. "In order to" is "to". "The fact that"
  is deleted. Being casual is not licence to ramble.
- Put the point of each section in its first two sentences. No build-up.
- One idea per paragraph, 2-4 sentences.

**Rhythm:** vary sentence length, roughly 8-20 words, with the occasional longer sentence
when one thought genuinely needs it. Do not chop everything short — choppiness reads as
simple-minded, not friendly.

Banned: {_BANNED}. Also banned: exclamation marks, and rhetorical questions as openers.""",
        "polish": (
            "Preserve DANA's voice: contractions, direct address to 'you', plain everyday "
            "vocabulary, and the conversational asides. Critically — **do not remove any "
            "number, figure or named example while simplifying.** This voice is already easy "
            "to read; if it needs simplifying at all, simplify the sentence, never the "
            "content. Do not add exclamation marks or reassurance."
        ),
    },
}

DEFAULT_VOICE = "clark"


def get_voice(vid: str | None) -> dict[str, Any] | None:
    """取写手。空/未知（含被淘汰的旧 id）都返回 None，调用方退回原版文风。"""
    if not vid:
        return None
    return VOICES.get(vid)


# 主题类型 → 推荐写手。**显式写死**，不要改回"遍历 for_types 取第一个匹配" ——
# 那样靠字典顺序决定谁被截走，加减写手时会静默改变推荐结果。
RECOMMEND: dict[str, str] = {
    "comparison_data": "serious",        # 对比/数据密集：要表格和克制断言
    "conceptual": "serious",             # 概念解释：要结构和精确
    "how_to": "casual",                  # 操作教程：亲切好读
    "realistic_product": "casual",       # 实物导购：像朋友推荐
    "strategy_experience": "clark",      # 策略/经验：平衡最稳
}


def recommend_voice(topic_type: str) -> str:
    """按主题类型推荐写手。topic_type 大纲阶段已经算好，推荐不额外花钱。"""
    return RECOMMEND.get(topic_type, DEFAULT_VOICE)


def voice_list() -> list[dict[str, Any]]:
    """给前端渲染选择器用（不含 prompt 正文，那些没必要发到浏览器）。"""
    return [
        {
            "id": vid,
            "name": v["name"],
            "role": v["role"],
            "blurb": v["blurb"],
            "for_types": v["for_types"],
            "portrait": f"/assets/voices/{v['portrait']}",
        }
        for vid, v in VOICES.items()
    ]


# ============================================================
# 配图风格
# ============================================================
# 改之前风格是死的：只按 topic_type 在"写实产品图"和"通用博客图"之间二选一，
# 用户说不上话。现在做成可选，auto 保留原来的自动行为当默认。
IMAGE_STYLES: dict[str, dict[str, str]] = {
    "auto": {
        "label": "跟随主题类型",
        "hint": "由系统按文章类型自动挑，省心。",
        "suffix": "",          # 空 = 走 topic_type 的老逻辑
    },
    "photo": {
        "label": "写实产品图",
        "hint": "真实存在的物品与场景，商业目录质感。适合导购、评测。",
        "suffix": (
            "Realistic product photography, natural or studio lighting, accurate materials "
            "and proportions, clean neutral background, commercial catalog quality."
        ),
    },
    "illustration": {
        "label": "干净插画",
        "hint": "扁平化插画，颜色克制。适合概念、观点类文章。",
        "suffix": (
            "Flat vector-style editorial illustration, limited restrained colour palette, "
            "generous negative space, simple geometric shapes, no gradients or 3D rendering."
        ),
    },
    "diagram": {
        "label": "示意图 / 信息图",
        "hint": "讲结构和流程用。适合教程、对比、数据类文章。",
        "suffix": (
            "Clean explanatory diagram or infographic, clear visual hierarchy, labelled "
            "elements using simple generic shapes and arrows, flat colours, plenty of "
            "white space, textbook clarity."
        ),
    },
    "line": {
        "label": "线描",
        "hint": "单色线条稿，克制安静。适合长青内容。",
        "suffix": (
            "Monochrome line drawing, fine consistent stroke weight, no fills or shading "
            "beyond light hatching, generous white space, quiet and restrained."
        ),
    },
}

DEFAULT_IMAGE_STYLE = "auto"


def image_style_list() -> list[dict[str, str]]:
    return [{"id": k, "label": v["label"], "hint": v["hint"]}
            for k, v in IMAGE_STYLES.items()]
