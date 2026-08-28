"""SEO 文章生成的全部 prompt 与主题档案。

原样移植自线下 Colab 工作流（V11），只把 f-string 拆成 format 模板。
改 prompt 只动这个文件，workflow.py 不用碰。
"""

from __future__ import annotations

# ============================================================
# 主题类型档案（决定配图风格）
# ============================================================
TYPE_PROFILES: dict[str, dict[str, str]] = {
    "comparison_data": {
        "name": "对比/数据密集型",
        "image_style_hint": "infographic / comparison chart / data visualization 风格",
    },
    "conceptual": {
        "name": "概念解释/长青型",
        "image_style_hint": "conceptual illustration / metaphor visualization / clean diagram 风格",
    },
    "how_to": {
        "name": "操作教程型",
        "image_style_hint": "step-by-step diagram / process flow / annotated screenshot mockup 风格",
    },
    "strategy_experience": {
        "name": "策略/经验/警示型",
        "image_style_hint": "narrative scene / decision-tree diagram / cinematic mood image 风格",
    },
    "realistic_product": {
        "name": "实物产品/B2B 写实型",
        "image_style_hint": (
            "realistic product photography 风格: 真实存在的产品/设备/场景,"
            "自然光或棚拍光,材质、比例、细节真实准确,中性背景,"
            "商业产品目录质感。绝不要科幻、未来感、发光特效、概念渲染。"
        ),
    },
}


def get_type_profile(type_key: str) -> dict[str, str]:
    return TYPE_PROFILES.get(type_key, TYPE_PROFILES["conceptual"])


NEGATIVE_STYLE_REALISTIC = (
    "Strictly photorealistic. NO futuristic elements, NO sci-fi, NO concept-art rendering, "
    "NO glowing or neon effects, NO holograms, NO floating UI, NO fantasy. "
    "The product must look like a real, currently-existing physical object with accurate "
    "materials, proportions and wear. Plausible real-world setting only."
)
NEGATIVE_STYLE_BASIC = (
    "Avoid unnecessary glowing/neon effects, avoid sci-fi or futuristic styling unless the "
    "topic genuinely calls for it."
)


def image_style_suffix(topic_type: str) -> str:
    """配图 prompt 的风格后缀。"""
    if topic_type == "realistic_product":
        return (
            f"{NEGATIVE_STYLE_REALISTIC} "
            "Realistic product photography, high resolution, natural lighting, "
            "accurate materials and proportions, clean neutral background, "
            "commercial catalog quality."
        )
    return (
        "Professional, clean composition suitable for a blog article. "
        f"{NEGATIVE_STYLE_BASIC} High quality."
    )


# ============================================================
# 字数判断
# ============================================================
WORDCOUNT_PROMPT = """你是 SEO 内容策略师。请基于下面的信息,判断一个最合适的文章字数(严格在 800-3000 之间)。

## 信息
- 主关键词: {main_keyword}
- 次关键词: {secondary_keyword}
- 主题: {topic}
- 特殊要求: {specific}
- 语言: {language}

## 判断参考
- 简单定义 / 单一概念解释: 800-1300
- 一般概念深度展开 / 简短教程: 1300-1800
- 操作教程 / 步骤指南 / 中等深度对比: 1800-2300
- 深度分析 / 全面对比 / 策略指南 / 复杂主题: 2300-3000

## 输出格式(严格)
直接输出一个 800-3000 之间的整数,不要任何其他内容。
"""


# ============================================================
# 主题类型分类
# ============================================================
CLASSIFY_PROMPT = """判断这个 SEO 文章主题属于哪种内容类型(只用于决定配图风格)。

主题: {topic}
核心关键词: {main_keyword}

类型选项(只输出 key):
1. comparison_data → 对比、参数、数据类
2. conceptual → 抽象概念解释(不是实物产品)
3. how_to → 操作流程、教程
4. strategy_experience → 策略经验、误区警示
5. realistic_product → 真实实体产品/设备/硬件/工业品/材料

如果主题指向实体产品,优先选 realistic_product。
直接输出一个 key 名,不要解释。
"""


# ============================================================
# 大纲生成
# ============================================================
SPECIFIC_BLOCK = """
## ‼️🔴 特殊要求(最高优先级 — 必须严格执行,不可忽略,不可弱化,不可与其他指令冲突)‼️

以下是用户明确提出的特殊要求,这些要求的优先级高于本 prompt 中的所有其他指令。
如果特殊要求与其他规则产生冲突,以特殊要求为准。

{specific}

---
"""

SPECIFIC_REMINDER_OUTLINE = """
---
## ⚠️ 再次提醒: 特殊要求必须落实
请回顾上方的「特殊要求」部分,确认大纲中已经充分体现了以下需求:
{specific}
如果特殊要求中提到某品牌/产品/经纪商引流需求,设置至少 3 处自然钩子引导点击。
"""

OUTLINE_PROMPT = """# 指令：创建以用户为中心的内容大纲

## 角色：
你是一位经验丰富的内容策略师和主题专家，专注于创作符合Google Helpful Content和E-E-A-T原则的高质量、高价值 {language} 内容。

## 核心任务：
为核心主题{main_keyword} 和 {secondary_keyword} 推断出的用户核心查询/主题]" 创建一份详细、实用且以用户为中心的 {language} 文章大纲。最终目标是创作出能真正帮助目标受众、解答他们疑问、提供独特价值，并让他们读后感到满意的内容。
这个文章的主题必须是：{topic}
这个文章的预估总字数应该是{wordcounts}

## 输入信息：
1.  **核心关键词：** `{main_keyword}`
2.  **次要关键词/相关概念：** `{secondary_keyword}` (用于拓宽和深化内容角度)
3.  **大致篇幅指导：** 文章旨在提供全面且深入的信息，预估篇幅约为 `{wordcounts}` 字。

你还需要遵循这个特殊要求：{specific}(如果特殊要求为空或者没有的情况下，忽略这一要求,如果这个需求不为空，以这个要求为最高需求，和其他指令冲突时以这个指令为准。

{product_context}
{image_context}
{reddit_context}

## 大纲核心要求 (深度融合 Helpful Content & E-E-A-T):

1.  **以用户为中心 & 满足核心意图 (People-First & Intent Fulfillment):**
    * **快速解答核心问题：** 大纲的开篇部分（如引言或第一主要章节）必须直接、清晰地回应用户最可能关心的核心问题（基于上述意图分析）。避免不必要的冗长引入。
    * **逻辑清晰的用户旅程：** 设计一个自然的、符合用户思考路径的内容流。结构应引导读者从基础认知到深入理解，最终有效满足其搜索意图，让用户读完感觉"问题解决了"。
    * **全面性与实质性内容 (Completeness & Substance):** 确保大纲覆盖了用户围绕该主题可能关心的所有关键方面，提供足够深入、实质性的信息，避免内容过于肤浅或遗漏关键点。

2.  **原创性 & 显著附加价值 (Originality & Significant Added Value):**
    * **超越参考，创造独特：** 大纲中必须明确规划出 **至少1-2个核心部分**，其目标是提供现有高排名内容（见下文参考）**所缺乏的独特价值**。这可以是：**原创的深入分析、独特的视角/观点、第一手经验的详细分享（非简单提及）、具体的案例研究、实用的、非泛泛而谈的操作步骤、或者对现有信息的批判性整合与提炼**。请在这些部分明确标注其【独特价值点】。
    * **避免同质化：** 除非能提供显著不同的解释、更深层次的洞察或更新的信息，否则应避免重复参考内容中已经泛滥且无新意的信息点。

3.  **体现E-E-A-T (Experience, Expertise, Authoritativeness, Trustworthiness):**
    * **经验 (Experience - E):** 在主题适用的情况下（例如产品使用、服务体验、问题解决过程、地点探访等），在大纲中明确规划出需要融入**具体、生动的第一手经验**的部分。标注：`[E-E-A-T提示：此处需融入详细的第一手经验]`。
    * **专业性 (Expertise - E):** 规划展示对主题**深度理解和专业知识**的部分，例如：深入的原理解释、对复杂概念的清晰拆解、专业的对比分析、行业背景或趋势解读等。
    * **权威性 (Authoritativeness - A):**
        * **关键事实与数据支撑：** 对于涉及事实、数据、统计的论点，在相应大纲节点下，简要列出需要包含的**核心事实或数据要点**（可从参考内容提炼，但鼓励验证其时效性或寻找更优来源）。标注：`[E-E-A-T提示：需包含事实/数据 - 说明具体内容，如：市场份额数据、关键统计年份等]`。
    * **可信度 (Trustworthiness - T):**
        * **准确、无误导的标题：** 大纲中的各级标题应清晰、准确地反映该部分内容，避免使用夸张、耸人听闻或与内容不符的标题（Clickbait）。
        * **信息呈现优化（图表/列表）：** 识别大纲中至少一个适合通过**图表、表格、项目列表或步骤列表**等可视化或结构化形式呈现复杂信息、对比数据或操作流程的部分，以提高清晰度和用户体验。标注：`[E-E-A-T提示：建议使用图表/表格/列表展示 - 说明展示内容]`。

4.  **避免"搜索引擎优先"的陷阱 (Avoid Search Engine-First Content):**
    * **自然融入关键词：** `{main_keyword}` 和 `{secondary_keyword}` 应在文中**自然、有意义地使用**，作为表达相关概念的一部分，特别是在标题和讨论核心主题的部分。**无需强制规定每个段落的出现次数**，重点是语义相关和阅读流畅性。确保整体内容紧密围绕这些核心概念展开即可（例如，全文自然出现5次以上通常是合理的，但不必刻意追求）。
    * **价值驱动的结构与深度：** 大纲的结构（章节数量、层级）和各部分的详略程度应由**内容的逻辑、用户需求和信息的重要性**决定，而非固定的段落数或字数分配。

## 输出格式要求：

1.  **文章标题 (Title):** 提供1-2个建议的 {language} 标题，要求：吸引点击 (High CTR)，准确反映核心内容和用户价值，自然包含核心关键词。
2.  **目标受众与语言风格 (Audience & Tone):** 基于之前的分析，用1-2句话明确描述目标受众画像，并据此建议最合适的语言风格（例如：面向初学者的通俗易懂、面向专业人士的严谨精确、友好对话式、客观中立的教学式等）。
3.  **内容大纲 (Outline):**
    * 使用层级清晰的Markdown格式（例如 H1, H2, H3...）。
    * **H1:** 应为最终采纳的文章标题。
    * **H2 (主要章节):** 每个H2下应有简短说明，阐述该章节的**核心目的 (Purpose)** 和**为用户提供的关键价值 (Value for User)**。
    * **H3 (及以下子节):** 具体的论点、信息点、步骤等。
    * **篇幅/重要性指导:** 在每个H2章节旁，用括号标注其大致重要性或建议的深度（例如：(核心章节，需深度阐述)、(关键补充，中等篇幅)、(背景信息，简要介绍)），替代固定字数。
    * **嵌入E-E-A-T提示:** 在大纲的相应位置，清晰地插入之前定义的各种 `[E-E-A-T提示：...]` 标注。
    * **确保逻辑流畅:** 整体结构应能有效引导用户，从引出问题到深入分析再到提供解决方案或结论。
4.  核心关键词等不需要进行特殊的样式处理，保持和正文一致就可以
5.  确保文章最后的链接只给到前文的推荐产品上，不要给其他网站任何链接。

你必须在大纲的每一大段中告知这段的具体写作需求。
你不需要在文章中增加FAQ的部分。
在每一大段之后告知这段要写预估多少字！

不需要考虑写meta description，不需要写meta title。

不要采用"I"作为第一人称，需要的时候采用"We"

思考每一段可以用什么样的格式展示更清晰易懂，避免让文章出现多段纯文字摞在一起的情况。
大纲要求文章每段可以多使用富文本格式，多使用富文本格式（如列表、表格、粗体、斜体）目的是易于读者阅读，结构清晰。
注意，不要设计任何内部或者外部链接！
注意，不要设计任何内部或者外部链接！
注意，不要设计任何内部或者外部链接！
注意，不要设计任何内部或者外部链接！

---

## 参考的高排名内容 (用于理解、补充和超越)：
`{main_search_results}`
`{secondary_search_results}`
**重要使用说明：** 这些内容仅供您**理解当前已存在的信息格局、识别用户可能未被满足的需求（信息缺口）、提取可验证的事实与数据、以及寻找可引用的权威来源**。您的核心任务是基于这些理解，**创造出更新颖、更深入、更实用、或提供更独特视角的内容**，从而实现显著的附加价值。**严禁直接复制、简单改写或仅仅对这些内容进行摘要总结。**

---
**请直接用 {language} 输出完整大纲，不要添加任何额外的解释或开场白。**"""


# 推荐产品上下文（用户原版：三档详细度，由 Tavily/Exa 抓产品页得到信息）
PRODUCT_DETAIL_LEVELS = {
    "简短提及": "在文章中自然地简短提及产品，1-2句话即可",
    "中等介绍": "在相关章节中用3-4句话介绍产品的主要优势和特点",
    "详细介绍": "专门设置一个小节，用5-7句话详细介绍产品的功能、优势和适用场景",
}
DEFAULT_PRODUCT_LEVEL = "中等介绍"

PRODUCT_CONTEXT = """

## 产品推荐要求：
- 产品名称：{product_title}
- 产品URL：{product_url}
- 产品信息：{product_content}
- 推荐详细程度：{level} - {level_instruction}

请在大纲中合适的位置规划产品推荐内容，确保与文章主题自然融合。标注：[产品推荐：此处需要融入产品介绍和链接]
"""

# 正文写作时的产品指令（与大纲那份不同：这里要求真的写出锚文本链接）
PRODUCT_INSTRUCTIONS = """

## 产品推荐具体要求：
- 产品名称：{product_title}
- 产品URL：{product_url}
- 在文章中自然地融入产品推荐，使用Markdown锚文本格式
- 推荐详细程度：{level}
- 确保产品推荐与文章内容自然衔接，不要显得突兀
"""

# Reddit 真实讨论上下文（可选）：让大纲能吃到"真人到底在抱怨什么"
REDDIT_CONTEXT = """
## 社媒真实讨论（Reddit，用于挖未被满足的需求）
{reddit}
**使用说明：** 这是真实用户的原话。请从中识别 **高频痛点、被反复问但没被好好回答的问题、以及现有内容普遍忽略的角度**，
并在大纲里至少规划一个专门回应它们的章节（标注【独特价值点】）。严禁直接复制原话。
"""


IMAGE_CONTEXT = """
## 图片配图规划
本文将生成 {images_per_article} 张 AI 配图。
图片风格建议: {image_style_hint}
请在大纲中规划共 {images_per_article} 处配图位置,标注: [配图建议: 这里配什么内容的图]
"""


# ============================================================
# 大纲修改
# ============================================================
REVISE_SPECIFIC_BLOCK = """
## ‼️ 用户的特殊要求(最高优先级,必须严格执行)
{specific}
"""

REVISE_PROMPT = """# 指令: 根据用户反馈修改大纲

## 角色
你是一位资深的 SEO 内容策略师。用户已审阅了你之前的大纲,并提出了修改意见。

{specific_block}

## 文章基本信息
- 主关键词: {main_keyword}
- 次关键词: {secondary_keyword}
- 主题: {topic}
- 目标字数: {wordcounts}
- 语言: {language}

## 当前大纲
{current_outline}

## ‼️🔴 用户的修改意见(必须完全落实)
{user_feedback}

## 修改要求
1. 用户的修改意见是最高优先级,必须逐条落实
2. 保持用户没有提出异议的部分
3. 继续遵守硬约束(H 标签疑问句 + 黄金答案句)
4. 严禁出现年份
5. 输出完整修改后的大纲(不要只输出修改的部分)

请用中文输出完整修改后的大纲(便于用户审批),但所有 H1/H2/H3 标题本身必须用 {language} 书写。不要添加任何解释。
"""


# ============================================================
# 文章写作
# ============================================================
SPECIFIC_REMINDER_ARTICLE = """
---
## ⚠️ 最终检查: 特殊要求落实确认
在输出文章之前,请逐条确认以下特殊要求已在文章中完全落实:
{specific}
"""

REALISM_NOTE = (
    "\n⚠️ 本文主体是真实存在的实体产品。所有 [IMAGE: ...] 描述必须是"
    "写实产品摄影风格,严禁出现 futuristic / sci-fi / glowing 等词。"
)

IMAGE_INSTRUCTION = """
## 图片占位符要求
本文必须包含恰好 {images_per_article} 个图片占位符。

格式: [IMAGE: 用英文写的图片描述,40-80 字,具体且视觉化]

图片风格: {image_style_hint}{realism_note}

总数恰好 {images_per_article} 张
"""

ARTICLE_PROMPT = """你为{main_keyword}关键词写SEO文章

你需要知道写到这个字数：{wordcounts}

不要超过这个字数的30%以上！！！！

你的写作风格是简练，高信息密度，具备第一手经验的行业专家。你输出{language}。

你严格遵从这个大纲：{outline}同时 遵守特殊要求：{specific}

{product_instructions}

目前的这个关键词的排名靠前的内容供你参考:{main_search_results}{secondary_search_results}

把大纲中的属于你负责段落的事实性内容自然的融入。

你输出的总字数应该在{wordcounts}左右。

遵从大纲中的语言风格进行写作。遵从大纲中对关键词出现次数的需求。

你输出干净的可以直接发布的文本，所有不要有任何不适合发布的解释性内容。

不要采用"I"作为第一人称，需要的时候采用"We"

输出Markdown格式

核心关键词等不需要进行特殊的样式处理，保持和正文一致就可以

如果有的数据非常适合使用图表展示，建立图表，否则就不要建立。

直接开始正文

H2及以下的子标题不要过长，最好控制在五个英文单词以内。
H1文章标题可以更完整，充分体现文章核心内容和关键词。

不要在正文中使用加粗的markdown标记。
{image_instruction}
直接输出结果，不要添加任何的解释或说明。"""


POLISH_PROMPT = """**Task: Rewrite the following text in {language}.**

你的目标是让文章使12年纪学生可以流利阅读。 Use clear, accessible vocabulary for a general audience.

**Original Text (in {language}):**
{article}

**Instructions:**
1.  **Output Language: Strictly use {language}.** Do not switch to English or any other language.
2.  **Preserve Structure — this is the hardest rule:** Keep EVERY markdown heading exactly as it is.
    Same count, same level (`#`, `##`, `###`), same order, same position. Do not delete a single one,
    do not demote or promote levels, do not merge sections. Keep the original paragraph breaks too.
3.  **Retain Keywords:** Keep the keywords "{main_keyword}" and "{secondary_keyword}" in the text.
4.  **Keep Elements:** Preserve any links, tables and list items from the original text.
5.  **Format:** Output in clean Markdown format. Keep all existing headings (rule 2);
    just do not wrap the KEYWORDS themselves in headings or bold — the keywords stay as plain body text.
6.  **句式节奏：使用长短交错的句式** —— 不是严格一长一短，而是让长句与短句自然交替出现：
    用短句制造停顿和强调，用长句承载完整的因果与条件。避免通篇长度相近的句子，那读起来像机器写的。
{preserve_instructions}

**Begin rewriting in {language} directly. Do not add any explanations or introductory phrases.**"""

# 保留链接的附加指令（有产品链接时才加）
POLISH_PRESERVE_LINKS = "7.  **Preserve all links.**"

# 第一次润色破坏了结构时追加的强制指令
POLISH_STRICT_RETRY = """

---
⚠️ YOUR PREVIOUS ATTEMPT DELETED MARKDOWN HEADINGS. That is a fatal error.
Before you output, copy every heading line from the original text verbatim
(`# ...`, `## ...`, `### ...`) into your output at the same position, in the same order.
Rewrite only the paragraphs BETWEEN headings. The heading lines themselves must appear
in your output unchanged. Count them before you finish: the number of `##` lines in your
output must equal the number in the original."""


# ============================================================
# SEO 元数据
# ============================================================
SEO_PROMPT = """根据我给你的文章内容，输出 {language} 的SEO Title和Description。

要求：
- Title: 少于70字符，吸引人点击，必须包含关键词"{main_keyword}"
- Description: 少于170字符，准确描述文章内容，必须包含关键词"{main_keyword}"
请严格按照以下格式输出（不要添加任何其他内容）：

Title: [你的标题]
Description: [你的描述]

不要提及年份，需要非常吸引读者点开，避免俗套的题目，例如ultimate，guide这种词都要避免

这是文章：{excerpt}..."""

BANNED_DESC_STARTS = ["discover", "uncover", "unlock", "explore", "dive into", "delve into"]
