"""单页抽取 prompt（目标页与竞品页共用，见规格 §1）。

要点：
  - claims 语义归一化：同一事实不同措辞算一条；去掉修辞/填充词，只留可判真伪的命题。
  - is_firsthand 只在有具体第一手证据（实测/原创数据/原创图）时为 true。
  - 只输出 JSON，无前言、无 Markdown 代码块。
  - 这一步禁止给任何分数。
"""

EXTRACTION_SYSTEM = """\
You are an information-extraction engine for SEO content analysis.
You DO NOT score anything. You only extract structured, evidence-backed facts.
Output ONLY a single JSON object. No preface, no markdown fences, no commentary.
"""

EXTRACTION_USER_TEMPLATE = """\
Extract a structured profile of the page below.

Rules:
- "claims": list of normalized propositions. SEMANTICALLY DEDUPE: the same fact in
  different wording counts once. Strip rhetoric/filler; keep only verifiable propositions.
  Each claim: {{"text", "type", "has_source", "is_firsthand"}}.
  type ∈ fact | data_point | opinion | how_to_step | definition.
  has_source = the page cites a source for this claim.
  is_firsthand = true ONLY with concrete first-hand evidence (own test, original data,
  original image). Generic statements are NOT first-hand.
- "subtopics": short phrases the page covers.
- "eeat_signals": author_named, author_credentials (or null), firsthand_markers (concrete
  evidence strings), outbound_citations (int), published_date, updated_date,
  has_about_or_contact, red_flags (exaggerated promises / contradicts common sense /
  strong claims with no source).
- "readability_notes": qualitative issues (undefined jargon, long paragraphs, intro not
  answering intent, etc.).

- "page_kind": classify the page into ONE of:
  article (informational content), product_list (collection/listing of many products),
  product_detail (single product), category, homepage, forum (Q&A/thread), review,
  tool, other. Judge by structure: many product links + thin prose = product_list.

Output JSON ONLY, matching this shape exactly:
{{
  "url": "{url}",
  "page_kind": "article",
  "claims": [{{"text": "...", "type": "fact", "has_source": false, "is_firsthand": false}}],
  "subtopics": ["..."],
  "eeat_signals": {{
    "author_named": false, "author_credentials": null, "firsthand_markers": [],
    "outbound_citations": 0, "published_date": null, "updated_date": null,
    "has_about_or_contact": false, "red_flags": []
  }},
  "readability_notes": ["..."]
}}

{lang_rule}

URL: {url}
TITLE: {title}
CONTENT:
{content}
"""

# 强制中文输出：所有文本字段用简体中文，但保留数字/专有名词/品牌/监管机构缩写原样。
ZH_LANG_RULE = (
    "LANGUAGE: Output ALL text fields (claims.text, subtopics, readability_notes, "
    "eeat_signals.author_credentials, firsthand_markers, red_flags) in 简体中文, "
    "REGARDLESS of the page's language. Translate facts into natural Chinese, but keep "
    "numbers, proper nouns, brand names, regulator acronyms (FCA/ASIC/NFA), and URLs "
    "AS-IS. Still semantically normalize each claim into one short Chinese proposition."
)
EN_LANG_RULE = "LANGUAGE: Output all text fields in English."


def build_extraction_user(
    url: str, title: str | None, content: str, output_lang: str = "zh", max_chars: int = 16000
) -> str:
    lang_rule = ZH_LANG_RULE if output_lang == "zh" else EN_LANG_RULE
    return EXTRACTION_USER_TEMPLATE.format(
        url=url, title=title or "", content=content[:max_chars], lang_rule=lang_rule
    )
