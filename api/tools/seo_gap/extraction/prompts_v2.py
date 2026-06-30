"""四部分报告(v2)用的 LLM prompt：关键词语义 / LSI 覆盖 / 增补段落。
全部要求只输出 JSON、文本用简体中文。"""

import json

# 一 · 关键词语义分析 ------------------------------------------------------- #
SEMANTICS_SYSTEM = """\
你是资深中文 SEO 意图分析师。只输出一个 JSON 对象，不要前言、不要 markdown 包裹。
所有文本用简体中文。"""


def build_semantics_user(keyword: str, competitor_titles: list[str], subtopics: list[str]) -> str:
    return f"""\
分析这个搜索关键词背后的用户意图与内容期望。

关键词：{keyword}
前10标题：{json.dumps(competitor_titles, ensure_ascii=False)}
前10覆盖的子话题：{json.dumps(subtopics[:40], ensure_ascii=False)}

输出 JSON（中文）：
{{
  "intent_type": "信息型 | 商业型 | 交易型 | 导航型 中之一",
  "user_wants": ["用户真正想知道/获得的内容点，4-6条"],
  "expected_format": "用户期望的内容形态，如 识别清单+案例 / 对比测评 / 步骤指南",
  "summary": "一句话总结搜索意图"
}}"""


# 视觉理解页面类型（截图）----------------------------------------------------- #
VISION_PROMPT = """\
这是网页首屏截图。只输出一个 JSON 对象（中文），不要前言、不要 markdown：
{
  "page_kind": "article|product_list|product_detail|category|homepage|forum|review|tool|other 之一",
  "visual_summary": "一句话描述：视觉主体是什么（如商品图墙/长文/视频/表格），版式特征",
  "text_adequacy": "充足 | 偏少 | 缺乏（指作为内容页参与排名的正文是否够）"
}
判断要点：满屏商品图片+缩略图网格、正文很少 → product_list；大段文字+配图 → article。"""


# 三 · LSI 覆盖 + 相关性判定 ------------------------------------------------- #
LSI_SYSTEM = """\
你对每个语义词/问题做两件事：
1) relevant：它是否与「目标页面的主题」相关。关键词常有歧义（如 D Prime 既是外汇券商，
   又是 D.Prime 牛排店），与本页主题无关的（如券商页里的 steakhouse/menu/restaurant）判 relevant=false。
2) covered：页面是否实质性覆盖了它的意思（不要求字面一致）。
只输出 JSON 数组，不要前言、不要 markdown。"""


def build_lsi_user(page_text: str, terms: list[str]) -> str:
    return f"""\
目标页面正文（节选，据此判断本页主题）：
{page_text[:8000]}

对下列每个词判断 relevant 与 covered。输出 JSON 数组：
[{{"term": "原词", "relevant": true 或 false, "covered": true 或 false}}]

待判定：
{json.dumps(terms, ensure_ascii=False)}"""


# GEO 分析（面向生成式 AI 搜索引擎）------------------------------------------- #
GEO_SYSTEM = """\
你是 GEO（Generative Engine Optimization，生成式引擎优化）专家，评估页面被 AI 答案引擎
（Google AI Overviews / ChatGPT / Perplexity 等）抓取、理解并引用的能力——这与传统 SEO 不同，
重在“能否被 AI 直接提取成答案并引用”。只输出一个 JSON 对象（中文），不要前言、不要 markdown。"""


def build_geo_user(keyword: str, page_text: str, has_schema: bool,
                   author_named: bool, citations: int, updated: str) -> str:
    return f"""\
关键词：{keyword}
结构化数据(JSON-LD Schema)：{"有" if has_schema else "无"}
署名作者：{"有" if author_named else "无"} ｜ 外部引用数：{citations} ｜ 更新日期：{updated or "无"}
页面正文（节选）：
{page_text[:7000]}

从以下维度评估页面对 AI 生成式搜索引擎的友好度，各 0-100：
- 可直接提取的答案（开门见山、可被引用的明确结论/定义）
- 结构化（问句式标题 / 列表 / 表格 / FAQ，便于 LLM 解析）
- 可引用的事实与数据（具体数字、统计、可核实来源——AI 偏爱引用这类）
- 权威与可信信号（作者资质 / 引用来源 / 时效）
- 结构化数据 Schema（FAQ/HowTo/Article 等标记）
- 语义完整度（覆盖该问题相关实体与子问题）

输出 JSON：
{{
  "score": 综合 0-100,
  "summary": "一句话总评",
  "dimensions": [{{"name":"维度名","score":0-100,"note":"简短说明"}}],
  "recommendations": ["针对 AI 答案引擎的具体优化建议，3-6条"]
}}"""


# AI 总结（页面质量评测 + 差异）---------------------------------------------- #
SUMMARY_SYSTEM = """\
你是资深 SEO 总监。基于已有分析，给目标页一份高管摘要：整体质量评分、亮点、与前10的主要差异、
结论与最该做的事。客观、可执行。只输出一个 JSON 对象（中文），不要前言、不要 markdown。"""


def build_summary_user(keyword, semantics, page_match_verdict, target_info,
                       top_missing, lsi_missing_count, geo_score,
                       reddit_summary: str = "", reddit_unmet=None) -> str:
    reddit_line = ""
    if reddit_summary or reddit_unmet:
        reddit_line = (f"\nReddit 真实讨论：{reddit_summary}\n"
                       f"真实用户反复关心但常被忽略的点：{json.dumps((reddit_unmet or [])[:6], ensure_ascii=False)}")
    return f"""\
关键词：{keyword}
搜索意图：{semantics.get('intent_type','')} / {semantics.get('expected_format','')}
页面类型匹配：{page_match_verdict}
我们页面：类型={target_info.get('page_kind')} 字数={target_info.get('word_count')} 图片={target_info.get('image_count')} 论点数={target_info.get('claim_count')} 正文={target_info.get('text_adequacy','')}
竞品有我们缺的要点(节选)：{json.dumps(top_missing[:15], ensure_ascii=False)}
缺失 LSI 词数：{lsi_missing_count}
GEO(生成式引擎)得分：{geo_score}{reddit_line}

输出 JSON：
{{
  "score": 综合质量分 0-100,
  "grade": "优秀|良好|一般|较差",
  "quality": "页面质量评测，2-3句",
  "strengths": ["亮点，1-3条"],
  "gaps": ["与前10的主要差异/不足，2-4条"],
  "verdict": "结论：这页面就当前关键词的竞争力如何 + 最该优先做的1-2件事"
}}"""


# Reddit 真实讨论洞察 ------------------------------------------------------- #
REDDIT_SYSTEM = """\
你是面向"做这个关键词的内容/SEO"的策略师。下面给你关键词、目标页面正文摘录，以及围绕
该词从 Reddit 抓到的真实帖子标题、正文与高赞评论。请**只基于这些真实讨论**分析：真实用户
在关心什么、抱怨什么、反复问什么，以及这些里**目标页面还没覆盖**的内容角度。
真实用户需求权重最高——这是判断该写什么、补什么的第一依据。
只输出一个 JSON 对象（除 quotes 外用简体中文），不要前言、不要 markdown。"""


def build_reddit_user(keyword: str, page_sample: str, corpus: str) -> str:
    return f"""\
关键词：{keyword}

目标页面正文摘录（据此判断哪些角度页面已覆盖、哪些没有）：
\"\"\"{page_sample[:1500]}\"\"\"

=== Reddit 真实讨论（标题 / 正文 / 高赞评论）===
{corpus}

输出 JSON：
{{
  "summary": "一段：Reddit 用户围绕这个词整体在讨论/关心什么（中文）",
  "themes": [{{"name":"讨论主题(中文)","summary":"大家在说什么(中文)","pain_points":["痛点(中文)"],"quotes":["1-2句英文原话"]}}],
  "content_angles": ["真实用户想要、且目标页面应覆盖的内容角度（中文，6-10条，按真实需求强度排序）"],
  "unmet_needs": ["用户反复问/抱怨、但现有内容普遍没讲清的点（中文，3-6条）"]
}}"""


# 四 · 增补段落生成 --------------------------------------------------------- #
SUPPLEMENT_SYSTEM = """\
你是 SEO 内容写手。根据缺口，直接写出可粘贴进页面的正文段落。
关键：heading 和 body 必须用【目标页面的语言】撰写（页面是英文就写英文、中文就写中文），
以便作者直接粘贴使用；reason 始终用简体中文（这是给中文用户看的分析说明）。
每段约 80-160 字/词，准确、不夸张、不编造数据；保留专有名词与品牌名。
每个对象都必须有非空的 reason。只输出 JSON 数组，不要前言、不要 markdown。"""

_LANG_ZH = {"zh": "简体中文", "en": "英文", "other": "页面所用语言"}


def build_supplement_user(
    keyword: str, user_wants: list[str], missing_points: list[str],
    missing_lsi: list[str], our_main: list[str], page_lang: str = "zh",
    page_sample: str = "", reddit_demand: list[str] | None = None,
) -> str:
    lang_name = _LANG_ZH.get(page_lang, "页面所用语言")
    reddit_block = ""
    if reddit_demand:
        reddit_block = (
            "\nReddit 真实用户需求（**最高优先级**——这是真实读者反复关心/追问的，"
            "优先据此补内容）：\n"
            + json.dumps(reddit_demand[:15], ensure_ascii=False) + "\n"
        )
    return f"""\
【最重要】heading 与 body 必须与下面「目标页面原文摘录」**同一种语言**（页面是英文就写英文、
中文就写中文）；以页面原文为准（约为 {lang_name}）。reason 始终用简体中文。

目标页面原文摘录：
\"\"\"{page_sample[:1200]}\"\"\"

关键词：{keyword}
用户期望内容：{json.dumps(user_wants, ensure_ascii=False)}
竞品有、我们缺的要点：{json.dumps(missing_points[:25], ensure_ascii=False)}
缺失的语义词/问题(LSI)：{json.dumps(missing_lsi[:20], ensure_ascii=False)}{reddit_block}
针对最重要的缺口生成 3-6 个建议新增的小节，每节写好可直接粘贴的正文。
**若 Reddit 真实用户需求里有页面没覆盖的点，优先为它们各写一节**（这些是经过真实讨论验证的需求）。
在 reason 里注明该节是否回应了 Reddit 上的真实关切。
输出 JSON 数组：
[{{
  "heading": "建议新增的小标题（与页面同语言）",
  "body": "写好的正文段落（80-160字/词，与页面同语言）",
  "reason": "为什么要补这段（用简体中文）"
}}]"""
