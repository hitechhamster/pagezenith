"""文章质量检测的 LLM prompt：标题评分+改写 / 信息密度 / 难句改写。
要求只输出 JSON、中文说明，但 title/rewrite 正文跟随文章语言。"""

import json

_LANG = {"zh": "简体中文", "en": "英文"}


# 标题吸引力 -------------------------------------------------------------- #
TITLE_SYSTEM = """\
你是资深内容编辑，评估文章标题的吸引力并给出更好的改写。
只输出一个 JSON 对象，不要前言/markdown。dims 的 note、issues、why 用简体中文；
suggestions 的 title 用【文章原语言】撰写（与下面正文同语言）。"""


def build_title_user(title: str, body_excerpt: str, lang: str) -> str:
    lang_name = _LANG.get(lang, "文章语言")
    return f"""\
文章语言：{lang_name}
当前标题：{title or "（未检测到明确标题）"}
正文开头摘录：\"\"\"{body_excerpt[:800]}\"\"\"

从 清晰度 / 具体性 / 吸引力(情绪或好奇) / 利益点 / 与正文匹配度 评估标题，各 0-100。
再生成 4-5 个更好的标题（用{lang_name}）。输出 JSON：
{{
  "score": 综合 0-100,
  "dims": [{{"name":"维度名","score":0-100,"note":"简短中文说明"}}],
  "issues": ["标题的问题，中文"],
  "suggestions": [{{"title":"改写标题（用{lang_name}）","why":"为什么更好，中文"}}]
}}"""


# 信息密度 ---------------------------------------------------------------- #
DENSITY_SYSTEM = """\
你评估文章的信息密度（实质信息 vs 注水/废话）。只输出一个 JSON 对象，不要前言/markdown。
说明用简体中文；watery 的 quote 摘自原文。"""


def build_density_user(body: str, lang: str) -> str:
    return f"""\
判断这篇文章的信息密度：数出有多少条**实质信息点**（事实/数据/步骤/具体洞见，空泛铺垫不算），
并找出**注水/冗余**的段落或句子（正确的废话、空泛、重复、过度铺垫）。

正文：
\"\"\"{body[:9000]}\"\"\"

输出 JSON：
{{
  "score": 信息密度 0-100,
  "info_points": 实质信息点数(整数),
  "summary": "一句话总评（中文）",
  "watery": [{{"quote":"注水原文摘录","issue":"问题(中文)","suggestion":"精简建议(中文)"}}]
}}"""


# 难句改写 ---------------------------------------------------------------- #
REWRITE_SYSTEM = """\
你把难读的句子改写得更简洁易读，保持原意与语言不变（原文英文就改成更易读的英文，中文就中文）。
只输出 JSON 数组，不要前言/markdown。"""


def build_rewrite_user(sentences: list[str], lang: str) -> str:
    lang_name = _LANG.get(lang, "原语言")
    return f"""\
把下列难句改写得更短、更易读（用{lang_name}，保持原意）。输出 JSON 数组：
[{{"original":"原句","rewrite":"改写后"}}]

难句：
{json.dumps(sentences, ensure_ascii=False)}"""
