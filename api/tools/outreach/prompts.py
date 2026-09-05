"""外链拓客的 LLM 提示词：分类机会 + 生成外联邮件（合并一次调用）。"""

from __future__ import annotations

# 语言码 → 给 LLM 看的语言名。取主语言段（zh-CN → zh），未命中回落英语。
_LANG_NAMES = {
    "en": "英语", "fr": "法语", "es": "西班牙语", "de": "德语",
    "it": "意大利语", "pt": "葡萄牙语", "ja": "日语", "zh": "中文",
    "nl": "荷兰语", "ru": "俄语", "ko": "韩语", "pl": "波兰语",
    "tr": "土耳其语", "ar": "阿拉伯语", "id": "印尼语", "th": "泰语",
    "vi": "越南语",
}


def lang_name(lang: str) -> str:
    base = (lang or "en").strip().lower().replace("_", "-").split("-")[0]
    return _LANG_NAMES.get(base, "英语")


def build_classify_system(lang: str = "en") -> str:
    """分类 + 写外联邮件。邮件语言跟目标站语言走——给法国站长发英文信回复率低。"""
    ln = lang_name(lang)
    return f"""\
你是跨境 SEO 外链拓客策略师。给你一个"候选网站"的标题与正文摘录，以及用户的目标
（主题词 + 用户简介）。请判断这个站作为外链对象的价值，并（若要求）写一封简短、
不像群发、能提高回复率的**{ln}**外联邮件。

分类要点：
- site_type：博客 / 资源页 / 新闻媒体 / 目录 / 论坛 / 公司站 / 其它（中文）
- relevance：与主题词的相关度 0-100
- opportunity：投稿 / 资源位加链 / 合作 / 断链替换 / 跳过（中文之一）。
  若明显是直接竞品、或与主题无关、或是大平台(维基/亚马逊等)不可能给链 → opportunity="跳过"
- reason：一句中文，说明为什么是机会或为什么跳过

邮件要点（仅当 need_email=是 且 opportunity≠跳过 时才写，否则两字段留空）：
- **用{ln}写**（主题行和正文都是），母语者口吻，不要机翻腔；
  主题行具体、不夸张；正文 60-110 词；先真诚提到对方内容的一个具体点，
  再说明你能提供什么价值，最后一个轻量、明确的 ask；不要过度恭维、不要群发感。

只输出一个 JSON 对象，不要前言/markdown。"""


# 向后兼容：老调用点若还 import CLASSIFY_SYSTEM，拿到的是英文版。
CLASSIFY_SYSTEM = build_classify_system("en")


def build_classify_user(keyword: str, your_url: str, your_brief: str,
                        need_email: bool, site_title: str, site_text: str,
                        lang: str = "en") -> str:
    ln = lang_name(lang)
    return f"""\
目标主题词：{keyword}
用户网站/页面：{your_url or "（未提供）"}
用户简介（能提供什么）：{your_brief or "（未提供）"}
need_email：{"是" if need_email else "否"}
邮件语言：{ln}

=== 候选网站 ===
标题：{site_title}
正文摘录：
\"\"\"{site_text[:4000]}\"\"\"

输出 JSON：
{{
  "site_type": "...",
  "relevance": 0-100,
  "opportunity": "投稿|资源位加链|合作|断链替换|跳过",
  "reason": "中文一句",
  "email_subject": "{ln}主题（不写则空串）",
  "email_body": "{ln}正文（不写则空串）"
}}"""
