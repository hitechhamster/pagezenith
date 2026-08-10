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

OUTLINE_PROMPT = """# 指令: 创建一份 {language} 长文大纲

## 角色
你是一位资深的 SEO 内容策略师。你的大纲将被写手严格按照执行。

{specific_block}

## 核心关键词
- **主关键词(文章核心,标题/H1/全文都围绕它)**: {main_keyword}
- **次关键词(辅助)**: {secondary_keyword}
- **主题**: {topic}
- **预估总字数**: {wordcounts}(合格区间 {wc_min}-{wc_max})
- **建议 H2 主章节数**: {h2_count}

{image_context}

---

## ⚠️ 必须严格遵守的两条硬性结构约束

### 硬约束 1: 所有 H1/H2/H3 标题必须是疑问句
- **H1 主标题**: 8-15 个英文单词,围绕主关键词
- **H2/H3 子标题**: 简洁问句,4-7 个英文单词以内

### 硬约束 2: 黄金答案句(每个 H2/H3 下方必须规划)
- 紧跟标题之后的第一句话必须用 1-2 句直接回答标题提出的问题
- 在大纲中标注: `[黄金答案句:......]`
- 文章正文除此之外不使用粗体

---

## 其他方面全部自由发挥

---

## 字数分配
- 在每个 H2 下标注 `[预估字数: XXX 字]`
- 各章节字数加起来接近 {wordcounts}

---

## 大纲格式
1. **文章标题 (H1)**: 1-2 个候选,8-15 个英文单词,围绕主关键词,**无年份**
2. **目标受众与语言风格**: 1-2 句话
3. **内容大纲**: Markdown 格式 (H1, H2, H3)
4. **结尾**: 行动建议或行业展望收尾

---

## 严格约束
- 严禁出现年份(2024/2025/2026 等)
- 不需要 FAQ 部分
- 不需要 meta description 和 meta title

---

## 红海参考(避开同质化)

{main_search_results}

{secondary_search_results}

严禁直接复制这些内容。

{specific_reminder}

---

请用中文输出完整大纲(便于用户审批),但所有 H1/H2/H3 标题本身必须用 {language} 书写(因为标题会直接用于最终文章)。不要添加任何额外解释或开场白。
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

ARTICLE_PROMPT = """# 任务: 根据大纲撰写 {language} SEO 文章

{specific_block}

## 关键词配置
- **主关键词(文章核心)**: {main_keyword}
- **次关键词**: {secondary_keyword}(自然出现 2-3 次)

## 基本参数
- 主题: {topic}
- 目标字数: {wordcounts}
- 输出语言: {language}

## 必须严格遵循的大纲
{outline}

## 可参考的搜索结果(仅用于事实补充,不要直接复制)
{main_search_results}

{secondary_search_results}

---

## 字数控制
- 目标: {wordcounts},下限 {wc_min},上限 {wc_max}
- 宁可写长也不要写短

---

## 硬性结构约束
### 硬约束 1: 所有 H1/H2/H3 标题必须是疑问句
### 硬约束 2: 黄金答案句 — 每个 H2/H3 下第一句用 `**...**` 粗体,正文其他部分不用粗体

---

## 通用禁止事项
- 严禁出现年份
- 禁止 FAQ 格式
- 禁止外部链接(特殊要求中明确指定的链接除外)
- 直接从 H1 标题开始,不要前言

{image_instruction}

{specific_reminder}

---

**现在,直接用 {language} 输出完整文章正文(Markdown 格式),从 H1 标题开始。**
"""


# ============================================================
# 润色（独立环节，写完之后单独跑）
# ============================================================
# 目标是"美国 12 年级学生能读懂"：Flesch-Kincaid 年级 9-12。
# 不往更低压 —— 压太低会把专业内容写成小学作文，反而伤 SEO 与可信度。
POLISH_PROMPT = """# 任务: 把下面这篇 {language} 文章润色到"美国 12 年级学生能读懂"的水平

## 角色
你是一位擅长把专业内容写得好读的编辑。你只改表达,不改事实、不改结构。

## 可读性目标(核心)
- 目标 Flesch-Kincaid 阅读年级: **9-12**(高中生能顺畅读完)
- 不要压到 9 以下 —— 过度简化会让专业内容显得幼稚,反而降低可信度

## 具体手法
1. 拆长句: 超过 25 个词的句子拆成两三句。一句话只讲一件事
2. 换词: 能用常见词就不用生僻词/学术词(utilize→use, facilitate→help)
3. 主动语态优先: 被动句改成"谁做了什么"
4. 术语必须解释: 第一次出现的行业术语、缩写,就地用一个短句说清楚
5. 删冗余: 去掉套话、同义反复、"值得注意的是"这类废话铺垫
6. 段落变短: 一段不超过 4 句

## ‼️ 必须原样保留(改动这些等于毁掉这篇文章)
1. **所有 H1/H2/H3 标题保持疑问句**,数量、顺序、层级都不能变(用词可微调,语义不能变)
2. **每个 H2/H3 下面的第一句黄金答案句仍然用 `**...**` 粗体**,正文其他地方不许出现粗体
3. **`[IMAGE: ...]` 占位符一字不改,位置不动**
4. **Markdown 链接 `[锚文本](URL)` 全部保留**,URL 不能改
5. 表格、列表等 Markdown 结构保留
6. 严禁出现年份
7. 输出语言仍然是 {language}

## 字数
原文约 {actual} 词,润色后保持在 {wc_min}-{wc_max} 之间。是改写不是缩写,不要删内容,把话说清楚就行。

## 原文
{article}

---

**直接输出润色后的完整文章(Markdown),从 H1 标题开始,不要任何解释或开场白。**
"""


# ============================================================
# SEO 元数据
# ============================================================
SEO_PROMPT = """根据下面的文章内容,输出 {language} 的 SEO Title 和 Description。

- **文章主标题 H1**: {h1}
- **核心关键词**: {main_keyword}
- **文章开头节选**: {excerpt}...

Title: 少于 70 字符,包含 "{main_keyword}"
Description: 少于 170 字符,包含 "{main_keyword}"
禁用开头词: Discover / Uncover / Unlock / Explore / Dive into / Delve into
严禁出现年份

输出格式:
Title: [你的标题]
Description: [你的描述]
"""

BANNED_DESC_STARTS = ["discover", "uncover", "unlock", "explore", "dive into", "delve into"]
