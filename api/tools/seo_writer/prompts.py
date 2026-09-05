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

# ⚠️ 必须加在**每一张**图上。
# 实测 gemini-3-pro-image 会自作主张把真实品牌 logo 画到产品上（而且画糊），
# 用户是拿去商用站的，带真商标 = 实打实的侵权风险。
NEGATIVE_BRANDS = (
    "CRITICAL: Do not depict any real brand, logo, trademark, wordmark, or company name. "
    "Products must be generic and unbranded — blank where a logo would be. "
    "Do not render any legible text, lettering, captions, labels or watermarks anywhere "
    "in the image. Do not depict recognisable real people."
)


def image_style_suffix(topic_type: str, style_key: str = "auto") -> str:
    """配图 prompt 的风格后缀。

    style_key 由用户选（见 voices.IMAGE_STYLES）；"auto"（默认）走原来按
    topic_type 自动判断的逻辑，保证不选风格时行为跟以前一致。
    """
    from .voices import IMAGE_STYLES          # 局部导入，避免与 voices 循环引用

    preset = IMAGE_STYLES.get(style_key or "auto", IMAGE_STYLES["auto"])
    chosen = preset.get("suffix", "")

    if chosen:
        # 用户明确选了风格：写实档仍然要带上"别搞科幻"那组负面词
        negative = NEGATIVE_STYLE_REALISTIC if style_key == "photo" else NEGATIVE_STYLE_BASIC
        return f"{chosen} {negative} {NEGATIVE_BRANDS} High quality."

    if topic_type == "realistic_product":
        return (
            f"{NEGATIVE_STYLE_REALISTIC} "
            "Realistic product photography, high resolution, natural lighting, "
            "accurate materials and proportions, clean neutral background, "
            "commercial catalog quality. "
            f"{NEGATIVE_BRANDS}"
        )
    return (
        "Professional, clean composition suitable for a blog article. "
        f"{NEGATIVE_STYLE_BASIC} {NEGATIVE_BRANDS} High quality."
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
# --------------------------------------------------------------------------- #
# 事实清单（搜索之后、大纲之前）
# --------------------------------------------------------------------------- #
# 为什么要单独一步：不锁定事实，同一个系统对同一件事每次给的答案都不一样 ——
# 实测同一主题三版产出里，标题可见字符 40→50-60、Mailchimp 分群门槛 2000→1000、
# 「Shopify Email」写成了不存在的「Shopify Messaging」。原因是数字在**生成时现编**。
# 先把可核实的事实连同出处抽出来，正文只准用清单里的，清单外的数字一律标疑。
FACTS_PROMPT = """从下面的参考资料里，抽出所有**可核实的事实**，供后续写作使用。

主题：{topic}
关键词：{main_keyword} / {secondary_keyword}

## 只抽这几类
1. **数字**：价格、费率、限制值、免费额度、时长、尺寸、百分比、样本量
2. **专有名称**：产品名、功能名、工具名、报告名（**必须与资料里的写法完全一致**）
3. **门槛与条件**：达到什么条件才会怎样（如"回复率 95% 以上才有某徽章"）
4. **平台官方口径**：官方文档明确说了什么

## 输出格式（每行一条，严格照抄这个结构）
`值` — 它是什么 — 来源：<域名或报告名> [来源类型]

来源类型直接抄资料里的 SourceType（厂商官方 / 社区讨论 / 第三方内容）。

## 硬规则
- **只抄资料里真实出现的**。资料没有的，一个字都不许补，不许根据常识"合理推断"。
- **专有名称逐字照抄**。资料写 "Shopify Email" 就不许写成 "Shopify Messaging"。
- 同一件事若不同来源说法冲突，**两条都列出来**，并在末尾注明「⚠️ 冲突」。
- **带年份的统计必须标注「截至 X 年」**，写进描述里。实测漏标过：`96.6 million active buyers`
  其实是 2023 年的数，正文却写成 "currently"。**查不到年份的统计，标注「年份不明」。**
- 只有社区讨论支持、官方未确认的说法，放进「待核实」区，别混进事实区。
- **平台规格类数字（字数上限、时长上限、尺寸、数量限制、费率）必须有 `[厂商官方]` 来源才能进事实区。**
  只有第三方或社区说的规格，一律进「待核实」并注明「无官方来源」。
  实测反例：视频时长写成 30 秒（AWeber 博客），Etsy 官方规格是 5–15 秒；
  「首屏 4 个产品」来自社区讨论却被当成规范。规格是平台定的，第三方转述不算数。
- **官方优先**：同一件事若厂商官方页面写了、第三方或社区又有别的说法，
  **以官方原文为准**并注明「官方页面已明确」。不许因为社区吵得凶就把官方说法降级成"未明确" ——
  实测出过反例：Klaviyo 按 active profile 与发送量双重计费，官网定价页写得清清楚楚，
  文章却写成「官方没明说，但用户反映…」。**hedge 之前先在资料里搜一遍官方怎么说。**

## 两类必须排除的东西（实测都混进来过）

**一、与本文主题无关的数字。** 抽之前先问：这个数字能帮读者做关于「{topic}」的决定吗？
不能就不要。反面实测：一篇 Klaviyo vs Mailchimp 的清单里混进了某个 App 的
5 星占比 81%、4 星 13%、3 星 2%…… 连着五条，对读者的选择毫无帮助，
还会把真正有用的条目挤掉。**宁可只给 8 条有用的，不要凑满 25 条。**

**二、厂商的自夸数字。** `[厂商官方]` 来源里的「50K+ 品牌已迁移至我们」
「350+ 集成」「行业领先」这类**用来自我推销的数字**，一律不收 ——
抄进对比文等于替一方打广告。
厂商官方来源里**只收**这几类：价格、免费额度、限制值、功能名称、明确的规则说明。
判据：这个数字是**用来让读者做判断的**（月费 $20、上限 500 个联系人），
还是**用来让厂商显得厉害的**（服务过 5 万品牌）？后者不要。

## 输出结构
### 事实
（上述格式的条目，按重要性排序，最多 25 条）

### 待核实
（社区传说 / 可能过期 / 来源冲突的条目，同样格式，末尾注明原因；没有就写「无」）

## 参考资料
{main_search_results}
{secondary_search_results}
{reddit_context}

直接输出清单，不要任何前言或解释。"""


# 事实清单注入大纲/正文时的包装。清单为空则整段消失。
#: 搜索页实况简报。由 density_audit.gap_brief 确定性算出来，零 LLM、零额外 API。
#:
#: 为什么这块比再写十条 prompt 规则管用：写「要提高信息增益」等于没说 ——
#: 模型不知道竞品已经写了什么，只能凭感觉猜「什么算独特」。把竞品的信息基线
#: **列出来**，它才知道哪些说了不算数。素材 > 规则。
GAP_BRIEF_BLOCK = """

## 搜索页实况（这一节全部来自真实抓取，不是推测）

{gap_brief}
"""

#: 可读性目标随文章类型变。旧版对所有类型都要求 FK 9–12，
#: 但操作教程的读者是边做边读的，12 年级的句子会拖慢他们。
READABILITY_BLOCK = """

## 可读性目标

{readability_note}
"""

FACTS_BLOCK = """

## 已锁定的事实清单（写作时的唯一数字来源）

{facts}

**使用规则：**
- **每条事实都要分配到具体的某一节，并且服务于那一节的论点。**
  大纲阶段就在对应小节下标注要用清单里的哪几条；**没被分配到的事实，正文不许用。**
  ⚠️ 实测反例：一篇讲 Shopify 提速的文章里冒出「Shopify app 按 30 天周期以美元计费」，
  只因为它在清单里。**清单是可用范围，不是必须用完的任务。**
- **数字分两类，规矩不一样**（2026-09-05 改：以前一刀切"所有数字必须来自清单"，
  结果正文只敢复述竞品都有的官方文档内容，信息增益实测掉到 0.22，四篇里最低）：
  · **断言型 —— 必须来自清单，逐字一致。** 凡是声称"世界上发生了什么"的数：
    百分比、跳出率、转化率提升、市场份额、金额、样本量、"X 家品牌"。
    清单里没有就不许写，宁可说定性（"多数"、"少量"）。**编一个统计 = 事故。**
  · **建议型 —— 用你自己的专业知识给具体值，不需要清单授权。** 凡是"读者该把它设成多少"
    的数：上传尺寸、压缩阈值、每页条数、超时秒数、目标指标值、版本号、路径与文件名。
    这类值是**建议**不是**发现**，写成"上传不超过 1600 像素宽"而不是"研究表明 1600 像素最优"。
    **这是本文信息增益的主要来源，该给就给，别缩回泛泛而谈。**
- **专有名称**（产品名 / 功能名 / 菜单路径 / 文件名）：清单里有的照抄清单写法；
  清单没有但你确知的，可以写 —— 但只写你有把握的，拿不准的宁可不写具体名字。
- **清单的「来源」字段只给你看，绝不能原样带进正文。** 实测一篇文章里出现了 11 处
  「(Source: AWeber, as of 2023)」「(Source: r/Etsy community discussion)」——
  读起来像调研笔记，还把弱来源暴露给读者。正文里引用只允许两种写法：
  · 官方来源 → 写成散文："Etsy's help center puts the tag limit at 13."
  · 第三方统计 → 写机构名 + 年份："an AWeber survey in 2023 found…"
  · **社区来源一律不署名**，改用限定语："many sellers report…"。绝不写 r/Etsy、Reddit。
- 清单里没有的**断言型**数字，一个都不许写。建议型具体值见上一条，该给就给。
- 「待核实」区的条目可以用，但必须写成「多数卖家反映 X，官方文档未明确」这种口径，
  不许当成确定事实。
"""


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

# ============================================================
# 写作声音（写手）
# ============================================================
# 三个阶段各一个包装：大纲要按声音规划结构，正文按声音落笔，润色不能把声音抹平。
# 规则正文在 voices.py，这里只负责包装成 prompt 片段。

OUTLINE_VOICE_BLOCK = """
## 本文的写作声音
{voice_outline_rules}
（下面「输出格式要求」里的语言风格一节，要照这个声音来描述，不要另外发明。）
"""

ARTICLE_VOICE_BLOCK = """## 写作声音（文风的最终依据，与大纲中的风格描述冲突时以此为准）

{voice_article_rules}
"""

# 没选写手时的默认文风 = 改造前 ARTICLE_PROMPT 里那两句原文，一字不改地搬进来：
#   ① 硬编码的那句文风
#   ② 「遵从大纲中的语言风格进行写作」
# ②必须留在这里。正文模板下方有一句静态的「语言风格以上面「写作声音」一节为准」，
# 不选写手时如果这里不写②，那句原始指令就凭空消失了，而且模板还会引用一个
# 不存在的小节 —— 等于悄悄改了老用户的产出。加上小标题让那句引用也有着落。
# ⚠️ 2026-09-02 改：原文是「具备**第一手经验的**行业专家」。
# 这句话每篇必注入，而模型没有第一手经验，于是每篇都编出
# 「In our work with clients…」「In my experience…」这类声称经验却给不出细节的句子
# （七篇产出无一幸免）。要专家感就靠信息密度和判断力，不靠假造经历。
ARTICLE_VOICE_DEFAULT = """## 写作声音

你的写作风格是简练、高信息密度的行业专家：直接给判断、给条件、给取舍，不绕弯。
**但你没有任何第一手经历可讲。** 不许写「我们服务过的客户」「以我的经验」
「我们审计时发现」这类声称亲身经验的句子 —— 你没有这些经历，写出来就是编的。
权威感来自两处：引用带出处的数据，以及把机制讲透。
另外，遵从大纲中的语言风格进行写作。"""


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

{facts_block}
{gap_brief_block}
{product_context}
{image_context}
{reddit_context}
{voice_outline}

## 大纲核心要求 (深度融合 Helpful Content & E-E-A-T):

1.  **以用户为中心 & 满足核心意图 (People-First & Intent Fulfillment):**
    * **快速解答核心问题：** 大纲的开篇部分（如引言或第一主要章节）必须直接、清晰地回应用户最可能关心的核心问题（基于上述意图分析）。避免不必要的冗长引入。
    * **逻辑清晰的用户旅程：** 设计一个自然的、符合用户思考路径的内容流。结构应引导读者从基础认知到深入理解，最终有效满足其搜索意图，让用户读完感觉"问题解决了"。
    * **全面性与实质性内容 (Completeness & Substance):** 确保大纲覆盖了用户围绕该主题可能关心的所有关键方面，提供足够深入、实质性的信息，避免内容过于肤浅或遗漏关键点。

2.  **原创性 & 显著附加价值 (Originality & Significant Added Value):**
    * **超越参考，创造独特：** 大纲中必须明确规划出 **至少1-2个核心部分**，其目标是提供现有高排名内容（见下文参考）**所缺乏的独特价值**。这可以是：**原创的深入分析、独特的视角/观点、把机制讲透（别人只说"要做X"，你说清"为什么X有效、什么情况下无效"）、实用的、非泛泛而谈的操作步骤、判断标准与取舍条件、或者对现有信息的批判性整合与提炼**。请在这些部分明确标注其【独特价值点】。
      ⚠️ **不要规划「第一手经验分享」「我们的客户案例」这类需要亲身经历的内容** ——
      写手没有经历，只会编（实测七篇产出全部编出了假案例或假经验句）。
    * **避免同质化：** 除非能提供显著不同的解释、更深层次的洞察或更新的信息，否则应避免重复参考内容中已经泛滥且无新意的信息点。

3.  **体现E-E-A-T (Experience, Expertise, Authoritativeness, Trustworthiness):**
    * **经验 (Experience - E):** ⚠️ 写手**没有第一手经验**，要求它"融入生动的亲身经历"只会得到
      编造的假案例（实测产出过「We recently analyzed a failing jewelry shop…」这种听着像真事、
      实际全是虚构、没有任何可核实细节的段落 —— 这是**减分**不是加分）。
      所以这里改为规划**经验型的信息密度**：具体的操作步骤、常见的失败方式和它的具体表现、
      不同情况下的取舍条件、容易踩的坑。标注：`[E-E-A-T提示：此处需具体到可操作，
      给条件/门槛/失败表现，不要编造亲身案例]`。
      只有当参考资料里存在**真实且可指名的例子**时，才规划案例，并标注其出处。
    * **专业性 (Expertise - E):** 规划展示对主题**深度理解和专业知识**的部分，例如：深入的原理解释、对复杂概念的清晰拆解、专业的对比分析、行业背景或趋势解读等。
    * **权威性 (Authoritativeness - A):**
        * **关键事实与数据支撑：** 对于涉及事实、数据、统计的论点，在相应大纲节点下，简要列出需要包含的**核心事实或数据要点**（可从参考内容提炼，但鼓励验证其时效性或寻找更优来源）。标注：`[E-E-A-T提示：需包含事实/数据 - 说明具体内容，如：市场份额数据、关键统计年份等]`。
    * **可信度 (Trustworthiness - T):**
        * **准确、无误导的标题：** 大纲中的各级标题应清晰、准确地反映该部分内容，避免使用夸张、耸人听闻或与内容不符的标题（Clickbait）。
          **标题写成人话短句，不要把关键词硬拼成名词词组。** 反面（实测产出）：
          「Algorithm Tag Reading」「Inventory Size Power」「Mockup Blindness」；
          正面：「How Etsy actually reads your tags」「Why 10 listings isn't enough」。
        * **不要安排空洞的收尾章节。** 实测最后一节常退化成「坚持就会赢 / 别信一夜暴富 /
          专业地对待你的店」这类零信息量的鸡汤。结尾要么给可执行的下一步，
          要么给判断标准（什么情况选 A、什么情况选 B），要么就不单独设收尾节。
        * **没有「产品推荐要求」时，不要规划任何推荐章节。** 上文若未给出产品信息，
          就不许安排「替代方案 / 推荐工具 / 其他值得考虑的选择」这类章节 ——
          实测大纲自己长出过一节推荐 Omnisend、MailerLite，用户根本没要求。
          把工具名当事实写进论据是可以的，单独设节推荐不行。
        * **主题之外的任何产品不得拥有独立 H2/H3。** 实测一篇 Klaviyo vs Mailchimp 里
          冒出整节「When to Consider Shopify Messaging」加 21 种语言列表 —— 它靠"替代方案"
          的名义混过了过滤。硬规则：只有标题/主题里点名的产品可以有自己的小节；
          **产品的语言列表、地区列表、集成清单这类枚举字段一律不进正文。**
        * **同一件事只讲一处。** 实测出现过开头的用户画像、中间的对比表格、结尾的选择矩阵
          在讲同一组区别。规划时逐节自问：这一节的信息在别处出现过吗？出现过就合并，
          留信息最全的那一处。
        * **结构不许照搬任何单一参考来源。** 你的 H2 顺序不得与下方任何一篇参考内容的
          小标题顺序一致；论据要横跨至少三个不同来源。只跟着一篇的骨架走，
          产出就是那篇的改写版 —— 这是抄袭风险，不是质量问题。
        * **章节标题承诺什么，节内就必须给什么。** 标题写了「什么时候价格变贵」，
          节内就必须有具体价位和档位；写了「数据怎么说」，就必须有数字。
          实测出现过整节叫「When does the pricing become too expensive?」却一个价格都没有 ——
          这是读者点进来最想看的地方，空着等于骗点击。
        * **信息呈现优化（图表/列表）：** 识别大纲中至少一个适合通过**图表、表格、项目列表或步骤列表**等可视化或结构化形式呈现复杂信息、对比数据或操作流程的部分，以提高清晰度和用户体验。标注：`[E-E-A-T提示：建议使用图表/表格/列表展示 - 说明展示内容]`。

4.  **避免"搜索引擎优先"的陷阱 (Avoid Search Engine-First Content):**
    * **关键词只规划位置，绝不规划次数。** `{main_keyword}` 和 `{secondary_keyword}`
      出现在 **H1、首段、以及最多一个 H2** 即可，正文其余部分**不做任何次数要求**，
      也**不许在大纲里写"本节需出现关键词 N 次"**。
      ⚠️ 实测教训：一旦大纲写了次数，正文就会硬塞出
      「searching for the best email marketing for shopify」
      「learning practical etsy seo tips」这种小写、语法多余、删掉不影响句意的从句 ——
      七篇产出每篇都有。判据很简单：**把那半句删掉后段落意思不变，就是塞词。**
      ⚠️ **但"少提关键词"绝不等于"少提产品名"。** 实测这条规则反噬过：
      为了避开重复，模型把对比表的表头写成了「First Platform / Second Platform」，
      读者根本不知道谁是谁。**产品名、工具名、平台名该出现多少次就出现多少次，
      表格表头必须写真实名称。** 要淡化的只是"把整个关键词短语硬拼进句子"这个动作。
    * **价值驱动的结构与深度：** 大纲的结构（章节数量、层级）和各部分的详略程度应由**内容的逻辑、用户需求和信息的重要性**决定，而非固定的段落数或字数分配。

## 输出格式要求：

1.  **文章标题 (Title):** 提供1-2个建议的 {language} 标题（标题用 {language}，后面的说明用中文），要求：吸引点击 (High CTR)，准确反映核心内容和用户价值，自然包含核心关键词。
2.  **目标受众与语言风格 (Audience & Tone):** 基于之前的分析，用1-2句话明确描述目标受众画像。
    语言风格：**如果上文给了「本文的写作声音」，就照它写，不要另外发明一套风格**；只有在没给的情况下，才由你建议最合适的语言风格（例如：面向初学者的通俗易懂、面向专业人士的严谨精确、友好对话式、客观中立的教学式等）。
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

**人称固定用 "We"，全文不许出现 "I / my / in my experience"。**
⚠️ 实测同一篇里同时出现 "In my experience" 和 "In our tests across multiple stores" ——
人称混用比单一人称更假。参考资料里的作者用 "I" 是他们的事，你不许跟着学。

思考每一段可以用什么样的格式展示更清晰易懂，避免让文章出现多段纯文字摞在一起的情况。
大纲要求文章每段可以多使用富文本格式，多使用富文本格式（如列表、表格、粗体、斜体）目的是易于读者阅读，结构清晰。
**每个 H2 下面必须规划一个结构化信息块**，并在大纲里写明它的形式和要装什么：
参数表（列头是什么、几行）/ 规格对照表 / 检查清单 / 带具体值的步骤表。
实测排在前面的竞品赢在这一点：一张 60 词的规格表装 15 条具体信息，60 词散文只装 2 条。
大纲里没规划结构化块的小节，正文写出来一定是立场铺垫。
注意，不要设计任何内部或者外部链接！
注意，不要设计任何内部或者外部链接！
注意，不要设计任何内部或者外部链接！
注意，不要设计任何内部或者外部链接！

---

## 参考的高排名内容 (用于理解、补充和超越)：
`{main_search_results}`
`{secondary_search_results}`
**每条参考都带 `SourceType` 标记，按类型区别对待：**
- **厂商官方**：只取事实字段（价格、限制值、功能名称、免费额度），**形容词和定位话术一律不要**。
  「整合 AI、营销与服务于一个平台」「350+ 集成」这类是广告词，抄进对比文等于替一方带货。
- **社区讨论**：是真人原话，价值在于暴露真实痛点；但**属个人经验，不等于平台官方口径**。
  与官方文档冲突时必须并列写出（「多数卖家观察到 X，官方文档说的是 Y」），不许只写一边。
- **第三方内容**：正常参考。

**重要使用说明：** 这些内容仅供您**理解当前已存在的信息格局、识别用户可能未被满足的需求（信息缺口）、提取可验证的事实与数据、以及寻找可引用的权威来源**。您的核心任务是基于这些理解，**创造出更新颖、更深入、更实用、或提供更独特视角的内容**，从而实现显著的附加价值。**严禁直接复制、简单改写或仅仅对这些内容进行摘要总结。**

---
**输出语言：正文说明、章节purpose、E-E-A-T 提示、字数指导等**全部用简体中文**（这份大纲是给
用户审批用的，不是给读者看的）。但 **H1/H2/H3 标题本身必须用 {language} 书写** ——
标题会原样进入正文，语言必须与文章一致。
（与「改大纲」环节保持一致；改大纲一直是中文输出，初版大纲不该是英文。）
不要添加任何额外的解释或开场白。**"""


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

你输出{language}。
{voice_article}

你严格遵从这个大纲：{outline}同时 遵守特殊要求：{specific}

{product_instructions}
{facts_block}
{gap_brief_block}
{readability_block}

目前的这个关键词的排名靠前的内容供你参考:{main_search_results}{secondary_search_results}

把大纲中的属于你负责段落的事实性内容自然的融入。

## 具体性（最重要的一条，实测最容易违反）

**具体信息的数量目标见上面「搜索页实况」里的密度目标 —— 那是按竞品算出来的，不是拍的。**
什么算一条：一个带单位的设定值（像素 / KB / 秒 / 毫米 / 千克 / 条数）、一个真实的工具或
产品型号、一条菜单路径或文件名、一个判断门槛、一种失败的具体表现、一条带出处的数据。
同一个值说三遍只算一条。达不到目标的小节就是在讲立场，不是在给东西 —— 删掉比留着强。
**每个 H2 至少一个结构化块**（参数表 / 规格表 / 对照表 / 检查清单 / 带具体值的步骤表），
密度住在表格里，不住在段落里。

- **两类数字，规矩不一样。这是本文能不能比竞品多给东西的关键。**
  · **断言型（世界上发生了什么）**：百分比、跳出率、转化提升、市场份额、金额、样本量、
    "超过 X 家品牌"。**只能用参考资料或事实清单里出现过的，一个都不许编。**
    没有就把话说定性（"多数卖家"、"明显下降"）。**编一个统计 = 事故。**
  · **建议型（读者该把它设成多少）**：上传尺寸、压缩阈值、每页条数、超时秒数、
    目标指标值、版本号、菜单路径、文件名、设置项名称。
    **用你自己的专业知识直接给具体值，不需要资料授权。**
    写成建议而不是发现：「hero 图上传不超过 1600 像素宽」✅，
    「研究表明 1600 像素是最优宽度」❌。
    **别缩回泛泛而谈** —— "优化你的图片尺寸"这种话竞品全都写过，等于没写。
- **禁止编造第一手案例。** 不许出现「We recently analyzed a shop that…」
  「We worked with a brand who…」这种听起来像真事、实际是编的场景 ——
  它没有任何可核实的细节，读者一眼看穿。要举例就用参考资料里真实存在的例子；
  没有就直接讲道理，不举例。
- 同理不许编造资历（「我们服务过 5 万个品牌」）。你没有资历。

## 不要主动推荐产品

上面如果**没有**给「产品推荐具体要求」，就说明用户没有要推荐任何东西。这种情况下：

- **不许新增「替代方案」「推荐工具」「值得考虑的其他选择」这类章节**，也不许在结尾
  塞一份工具清单。实测产出过整节推荐 Omnisend / MailerLite 的情况，用户根本没要求。
- **不许使用背书话术**：「我们推荐」「最佳选择是」「值得投资」「不容错过」。
- **不许搬产品方的官方定位文案**（「整合 AI、营销与服务于一个平台，350+ 集成」这种）。
  对比类文章里替一方念广告词，读者会怀疑你收了钱。要写就写可核实的功能差异。
- ✅ **允许**把工具名当**事实**提及：「多数卖家用 eRank 查 tag 的搜索量」
  「PageSpeed Insights 给的是实验室分」。这是具体性，不是推荐 —— 区别在于
  你是在**说明机制**还是在**劝人去买**。
- ✅ 文章主题本身就是评测/对比某些产品时（例如「A vs B」），围绕主题客观陈述当然可以，
  但不要额外引入主题之外的第三个产品。

## 句式（这三条实测每篇都犯）

- **不要每节都用同一个公式开头。** 「We need to address…」「To build a sustainable
  business, we first need to…」「One of the most destructive habits is…」——
  这类"宣告问题→我们需要→解释"的开场，全文最多用一次。其余小节直接从具体的事、
  具体的数、具体的做法切入。
- **少用「X, not Y」对照句式**（"a marathon, not a sprint"、"data, not anxiety"）。
  全文最多一处。改成直接的正面陈述。
- **强断言必须有依据。** 「mathematically expected」「the algorithm actively favors」
  「no longer optional」这类确定式语气，只有资料支持时才写，并且写清依据。
  资料没说的就用「通常」「多数情况下」「据卖家反馈」这类限定语 ——
  全知全能的语气是最明显的 AI 痕迹。
- **民间说法不许升级成定论。** 圈内流传但平台官方未确认的说法（「改 tag 会掉排名」
  「分几天上架显得活跃」），写成「多数卖家观察到 X，平台官方文档说的是 Y，
  保守起见建议 Z」。**直接断言"会导致排名严重下跌"会被懂行的读者当场打脸** ——
  这是实测被专业读者抓到的原话。
- **不许自造术语。** 给一个常识现象起个名字再反复使用（「the lazy buyer principle」
  「mockup blindness」「failure manifestation of low-effort inventory」）是典型 AI 手法，
  而且同一个自造词在不同段落定义还会漂移。直接把现象描述清楚即可。
- **不许编造经验口吻。** 「In our work with clients…」「In our audits of…」
  「In my experience with new sellers…」——这些声称经验却给不出任何可核实细节的句子，
  说得越多越假。要么给资料里的真实数据，要么直接讲事实，不要加人设前缀。

你输出的总字数应该在{wordcounts}左右。

**关键词不追求出现次数。** 让 {main_keyword} / {secondary_keyword} 出现在 H1、首段
和最多一个 H2 就够了。**但产品名、工具名、平台名不受此限** ——
该提多少次提多少次，对比表的表头必须是真实名称，不许写成
「First Platform / Second Platform」这种匿名列（实测发生过）。
大纲里若写了"本节需出现关键词 N 次"，**忽略它**。
**关键词只能以独立名词短语的形式出现**（主语、宾语、或标题里），前面**不许接**
动名词、不定式、形容词或从句引导词。下面五句是实测产出，**全是塞词，一句都不许写**：
  ✗ Figuring out how to get more sales on etsy is…
  ✗ Implementing effective etsy seo tips requires…
  ✗ When we evaluate the best email marketing for shopify, …
  ✗ To reduce shopify load time for good, …
  ✗ True shopify store speed optimization works by…
共同点：关键词被一个动词或形容词"包"起来硬塞进句子。正确的写法是关键词自己当主语或宾语：
  ✓ Shopify store speed optimization comes down to three things.
自检：**把含关键词的那半句删掉后段落意思不变，就是塞词，删掉重写。**
语言风格以上面「写作声音」一节为准；大纲里的风格描述与之冲突时，以「写作声音」为准。

你输出干净的可以直接发布的文本，所有不要有任何不适合发布的解释性内容。

**人称固定用 "We"，全文不许出现 "I / my / in my experience"。**
⚠️ 实测同一篇里同时出现 "In my experience" 和 "In our tests across multiple stores" ——
人称混用比单一人称更假。参考资料里的作者用 "I" 是他们的事，你不许跟着学。

输出Markdown格式

核心关键词等不需要进行特殊的样式处理，保持和正文一致就可以

如果有的数据非常适合使用表格展示，就建表格，否则不要硬建。
**表格必须用标准 Markdown 语法，每行首尾都带竖线**，并且第二行是分隔行：
| 列一 | 列二 |
| --- | --- |
| 值 | 值 |
不许写成 `列一 | 列二` 这种省略首尾竖线的松散写法（实测前端渲染不出来，会变成一堆纯文本）。

直接开始正文

**标题层级不许跳级**：H2 下面要分小节就用 H3，不许直接跳到 H4
（跳级对 SEO 的大纲解析不利，前端和 Word 的层级也会错位）。最深到 H3 就够了。
H2 及以下的子标题写成**人话短句**，不要名词堆。
反面（实测产出，全是把关键词硬拼成词组）：「Algorithm Tag Reading」「Inventory Size Power」
「Impactful Video」「Mockup Blindness」——真人不会这么起标题。
正面：「How Etsy actually reads your tags」「Why 10 listings isn't enough」
「What to fix before you touch SEO」。
长度 4-9 个英文单词，能带动词或疑问词就带上。
H1文章标题可以更完整，充分体现文章核心内容和关键词。

不要在正文中使用加粗的markdown标记。
{image_instruction}
直接输出结果，不要添加任何的解释或说明。"""


POLISH_PROMPT = """**Task: Rewrite the following text in {language}.**

{readability_note}

拆长句、换掉绕的词来达到这个区间。**但不许为了压年级而删掉具体信息** ——
参数值、文件名、菜单路径、工具名一个都不能少，它们才是这篇文章的价值所在。
句子可以变短变多，硬信息只能原样搬过去。

**Original Text (in {language}):**
{article}

**Instructions:**
1.  **Output Language: Strictly use {language}.** Do not switch to English or any other language.
2.  **Preserve Structure — this is the hardest rule:** Keep EVERY markdown heading exactly as it is.
    Same count, same level (`#`, `##`, `###`), same order, same position. Do not delete a single one,
    do not demote or promote levels, do not merge sections. Keep the original paragraph breaks too.
3.  **Retain Keywords:** Keep the keywords "{main_keyword}" and "{secondary_keyword}" in the text.
    关键词在这里是全小写的，那只是检索写法 —— **正文里的品牌名、产品名、专有名词一律保持
    原文的大小写**（Shopify / Klaviyo / Etsy，不是 shopify / klaviyo / etsy）。
    实测三个模型都会照抄小写关键词进正文，别犯。
4.  **Keep Elements:** Preserve any links, tables and list items from the original text.
5.  **Format:** Output in clean Markdown format. Keep all existing headings (rule 2);
    just do not wrap the KEYWORDS themselves in headings or bold — the keywords stay as plain body text.
6.  **句式节奏：使用长短交错的句式** —— 不是严格一长一短，而是让长句与短句自然交替出现：
    用短句制造停顿和强调，用长句承载完整的因果与条件。避免通篇长度相近的句子，那读起来像机器写的。
7.  **每句话都要干活。** 逐句问：这句给了新信息吗？推进论证了吗？引出下文了吗？
    如果只是把上一句换个漂亮说法重说一遍，删掉。真人写字大部分句子是平的 ——
    不要每段结尾都留一个"响一下"的东西，也不要为了做金句而倒装或砍短。
8.  **不许强行造比喻。** 为了显文采把 A 嫁接成 B 的句子，一律直说。
    商业黑话（护城河 / 赛道 / 生态 / 引擎 / DNA，moat / playbook / north star / superpower）全部禁用。
    自检一句：这话会有人在饭桌上说出来吗？说不出来就改成大白话。
    唯一例外：原文本身贯穿始终的核心意象可以保留。
9.  **你只能做四件事：删、拆、重排、换更浅的说法。不许新增任何信息。**
    ⚠️ 这条取代了旧的"具体压过抽象"规则 —— 那条给了润色"加信息"的权限，
    实测直接催生出「That 3 to 4 months window」「that 20 characters limit」这种
    把数字硬塞进指代短语的病句（正文 0 处 → 润色 4 处）。
    具体性是正文阶段的职责（那边有事实清单），润色只负责让它好读。
    **输出里不许出现原文没有的数字、产品名或事实。**
10. **篇幅下限：输出不得少于原文的 90%。** 简化句子 ≠ 删内容。实测全量润色把一篇
    2110 词砍到 1650 词（78%），客户买的字数就没了。删掉的只能是重复和套话，
    每一个事实、数字、步骤、条件都必须留在输出里。
11. **不许自造术语。** 给常识现象起个名字再反复用（"mockup blindness"
    "the lazy buyer principle"），尤其不许加引号把它装成行业术语。直接描述现象。
12. **人称：原文有 "I / my / in my experience" 才改成 we 或删掉，不许 I 和 we 混用。
    原文没有第一人称的句子，不许改成 "We …"。** 润色不是作者，不能替作者声称
    "我们控制 / 我们审核 / 我们做过" —— 实测把 "Controlling these specs on the line is what
    determines quality" 润成 "We control these exact specifications directly on the shared
    assembly lines"，凭空多出一个假工厂主。被动句、无主语句保持原样。
13. **反引号里的文件名 / 代码 / 路径 / 菜单路径原样搬。** 不许在里面断句、拆开、改大小写。
    实测把 `Bill_of_Materials.xlsx` 从中间拆成两句，文件名当场碎掉。
13. **结尾必须落地。** 最后一段不许写"策略比工具更重要""专注长期价值""保持专业"
    这类零信息量的收束。要么给一个具体动作，要么给一个数字或判断标准。
    写不出来就直接在最后一个实质段落结束，不要硬加收尾段。
{audit_findings}{preserve_instructions}
{voice_polish}

**Begin rewriting in {language} directly. Do not add any explanations or introductory phrases.**"""

# 写手文风在润色阶段的包装。
# 润色是整篇重写，不把文风带进来会被抹平成通用腔 —— 这是加它的全部理由。
# 它凌驾于规则 6（长短交错）之上：有的写手就是要通篇短句或通篇匀速。
POLISH_VOICE_BLOCK = """
**Voice — preserve it (this overrides instruction 6 wherever they disagree):**
{voice_polish_rules}
The original text was written in this voice on purpose. Simplifying for readability must not
flatten it into generic prose."""

# 保留链接的附加指令（有产品链接时才加）
POLISH_PRESERVE_LINKS = "10. **Preserve all links.**"

# 体检块：由 prose_audit 算出「这篇文章的具体违规」，只在有违规时才拼进去。
# 为什么给清单而不是给规则：规则要模型自己在长文里找违规，找不找得到看运气；
# 清单直接把位置摆在它面前，token 还更少。
POLISH_AUDIT_BLOCK = """

**这篇文章的体检结果 —— 逐条修掉（P0 必须修）：**
{audit_findings}
修的时候只动被点名的地方，其余段落保持原样。"""

# 第一次润色破坏了结构时追加的强制指令
# 轻润色：原文可读性**本来就达标**时走这条，只修体检点名的地方，不做整篇简化。
# 为什么需要它（2026-09-02 实测）：全量润色不看输入水平，一律往「12 年级能读懂」压。
# etsy 那篇原文 FK 10.6 已在 9-12 目标带内，全量润色把它压到 7.4（判定「偏浅，专业感不足」）
# 并砍掉 27% 的字数（触发「偏少」告警）—— 客户买 2000 词，交付 1628 词。
POLISH_LIGHT_PROMPT = """**Task: Make targeted fixes to the following text in {language}.**

这篇文章的可读性**已经达标**，不需要整篇改写。你的任务是**只修下面点名的问题**，
其余内容原样保留。

**Original Text (in {language}):**
{article}

**Instructions:**
1.  **Output Language: Strictly use {language}.**
2.  **不要缩写、不要概括、不要删段落。** 输出的篇幅必须与原文基本相当 ——
    这是最容易犯的错：一"润色"就越改越短。没被点名的句子请**逐字保留**。
3.  **Preserve Structure：** 每一个 markdown 标题原样保留（数量、层级、顺序、位置都不变）。
    链接、表格、列表项同样不动。
4.  **Retain Keywords：** 保留关键词 "{main_keyword}" 和 "{secondary_keyword}"。
    关键词在这里是全小写的，那只是检索写法 —— **正文里的品牌名、专有名词一律保持原文的
    大小写**（Shopify / Klaviyo / Etsy，不是 shopify / klaviyo / etsy）。
5.  **不许强行造比喻**，不许为了做金句而倒装或砍短，不许添加原文没有的事实或数字。
{audit_findings}{preserve_instructions}
{voice_polish}

**直接输出修改后的完整全文，不要任何解释或前言。**"""


#: 润色重试的第二种触发：上一轮把硬信息弄丢了。列出来点名保留。
#: 为什么不在正文规则里写"别删信息"就完事：写了（见上面"不许为了压年级删掉具体信息"），
#: 实测 3PL 那篇照删 15%。规则不收敛，清单才收敛 —— 和文风体检一个思路。
POLISH_KEEP_UNITS = """

## 上一轮润色把下面这些具体信息删掉了 —— 这一轮必须逐字保留，一条都不能少

{units}

它们是读者能照做的参数、能核实的名称、带出处的数据，是这篇文章的价值本身。
拆句、换说法都可以，但这些字符串必须原样出现在输出里。宁可句子长一点，也不许丢。
"""

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
