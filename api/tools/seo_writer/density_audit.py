"""交付后的内容打分：意图覆盖 / 信息增益 / 证据密度 / 信息密度 / 结构可读性。

与 prose_audit 的分工：
  prose_audit   管**文风**（像不像人写的）—— 破折号、句长、假案例、塞词。
  density_audit 管**内容值不值**（读者拿走了什么）—— 有多少硬信息、多少是竞品没有的。

五个维度按判据排序，不是并列打分：

  1. 意图覆盖 —— **一票否决**。SERP 上排在前面的是什么形态（教程 / 清单 / 对比 / 工具），
     你是不是同一形态；People Also Ask 的子问题覆盖了几个。形态不对，后面四项再高也白搭。
  2. 信息增益 —— **权重最高**，而且**必须对着真实 SERP 算**，不能凭感觉。
     算法：本文的硬信息单元里，有多少个在竞品全文语料里找不到。
  3. 证据密度 —— 参数 + 专名的密度（`fetchpriority="high"`、settings_data.json、Web Pixels）。
     ⚠️ 它是**一手证据的代理指标，不是一手证据本身**。真正的一手证据（自己跑的数据、
     自己截的图）AI 写手没有，检测器也造不出来。这里只测「读者能不能照着做」，
     测不了「作者是不是真做过」。
  4. 信息密度 —— 绝对值：每 100 词几个硬信息单元（含论据统计）。
  5. 结构可读性 —— **相对，不是绝对**。Flesch 对 SEO 近乎无用，只当次要区间校验；
     主判据是：到第一个直接答案有多远、每个 H2 能不能单独回答一个子问题、能不能扫读。

**不测 AI 率。** 检测器误报率太高，Google 也不查它 —— 伪指标，不进表。

⚠️ 这五项全是**代理指标**。真指标是发布后的行为数据（跳出、AIO 引用、转化），
只能从 GSC / 日志拿。这里的分数只回答"这篇比竞品多给了什么"，不回答"它会不会排上去"。
"""

from __future__ import annotations

import gzip
import re
from typing import Any

from . import prose_audit

# --------------------------------------------------------------------------- #
# 硬信息单元识别
# --------------------------------------------------------------------------- #
#: 功能词。参与专名判断时要排除，否则句中的 "The"/"This" 会被当专名。
_STOP = frozenset("""the a an and or of to in on for with is are was were be been by at from
as that this these those it its into than then so if not no but can will your you we our
their they them what when where which who how why all any each more most other some such
only own same very just also over under out up down off about after before between during
without within here there now new one two first second third next last many much every both
few does do did has have had why""".split())

#: 反引号里的东西一律算参数 —— 作者特意标出来的就是让读者照抄的。
_CODE = re.compile(r"`[^`\n]+`")

#: 参数：读者能照着设的值。
#: **不含**光秃秃的连字符形容词 —— 早期版本把 render-blocking / one-size-fits-all /
#: high-resolution 当成标识符，结果三篇文章的增益全算到 85% 上下，指标失去判别力。
#: 连字符词只有在反引号里、或作为 CSS 属性名出现时才算。
_PARAM = re.compile(
    r"""(?:[A-Za-z_][\w-]*\s*=\s*["'][^"'\n]{1,40}["'])"""              # fetchpriority="high"
    r"""|(?:\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\s*:\s*['"]?[\w.#%-]{2,20})"""  # font-display: swap
    r"""|(?:\b[a-z]+_[a-z_]{2,}\b)"""                                   # settings_data
    r"""|(?:\b[\w-]+\.(?:js|css|liquid|json|html|png|jpe?g|webp|avif|svg|mp4|txt|xml)\b)"""
)

#: 带单位的数值 = 参数（"2.5 seconds"、"1600 pixels"、"40 kHz"、"1 tbsp"）。读者照着设。
#:
#: 2026-09-05 实测三篇非网页题材（工业超声波清洗机 / 3PL / 白鞋清洗）：单位表只认
#: ms/KB/px 这类 web 单位，"204 gallons""40kHz""60°C""90 PSI""1 tbsp""30 minutes"
#: 一个都不算，工业买家文密度算出 1.2，开头空转算成整篇 —— 那是检测器瞎，不是文章空。
#: 所以单位表按题材分组补全：数据/网页、物理量、商业量、厨房量、时间。
_NUM_UNIT = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?"
    r"("
    # 数据 / 网页
    r"ms|kb|mb|gb|tb|px|pixels?|seconds?|milliseconds?|megabytes?|kilobytes?|gigabytes?|"
    r"items?|monitors?|columns?|rows?|fps|dpi|"
    # 物理量：频率、功率、电、体积、长度、温度、压力、重量、速度
    # 单字母单位只留不会撞上普通英文的：a（"1 a day"）和 in（"3 in 10 sellers"）不收
    r"k?hz|k?w|kwh|v|amps?|watts?|volts?|"
    r"l|ml|liters?|litres?|gal|gallons?|fl\.? ?oz|oz|ounces?|cc|m3|"
    r"mm|cm|m|km|meters?|metres?|inch|inches|ft|feet|foot|yd|yards?|"
    r"°\s?[cf]|degrees?(?: [cf])?|celsius|fahrenheit|"
    r"psi|bar|kpa|mpa|"
    r"mg|g|kg|grams?|kilograms?|lbs?|pounds?|tons?|tonnes?|"
    r"mph|km/h|rpm|"
    # 商业 / 运营量
    r"orders?|skus?|units?|pallets?|packages?|shipments?|pieces?|pcs|"
    r"words?|characters?|pages?|"
    # 厨房 / 家用量
    r"tbsp|tsp|tablespoons?|teaspoons?|cups?|drops?|scoops?|"
    # 时间
    r"mins?|minutes?|hours?|hrs?|days?|weeks?|months?|years?|"
    # 中文
    r"秒|毫秒|像素|个|条|列|升|毫升|克|千克|公斤|米|厘米|毫米|度|分钟|小时|天|周|月|年"
    r")\b", re.I)

#: 论据统计 = 用来论证、不是用来照着设的数（百分比、金额、拼写数字、无单位大数）。
#: 计入信息密度，**不计入证据密度** —— 用户判「B 开头 320 词是立场铺垫、无硬信息」，
#: 而那段里恰好全是 60 percent / four seconds 这类论据统计。口径必须对得上。
_STAT_PCT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:%|percent)\b", re.I)
_STAT_MONEY = re.compile(r"[$€£¥]\s?\d[\d,]*(?:\.\d+)?(?:\s?[-–—]\s?\d[\d,]*)?")
_STAT_BIG = re.compile(r"(?<![\w.$])(?:\d{1,3}(?:,\d{3})+|\d{3,})(?![\w.])")
_NUMWORD = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|dozen)"
    r"(?:-(?:second|minute|hour|day|week|month|year|pixel|percent|megabyte|item|step))?\b", re.I)
_LIST_NUM = re.compile(r"^\s*\d+[.)]\s", re.M)

#: CamelCase / 全大写缩写 = 专名（PageSpeed、WebP、LCP、TTFB），归 entity 不归 param。
_CAMEL = re.compile(r"\b[A-Z][a-z]+[A-Z][A-Za-z]+\b|\b[A-Z]{2,6}\b")

#: 中文正文里的专名容器（英文靠大写识别，中文只能靠引号 / 书名号）。
_CJK_NAMED = re.compile(r"[《「『【]([^》」』】\n]{2,20})[》」』】]")

#: 段首指代词 —— H2 首句以它开头 = 这一节不能被单独引用（AIO / LLM 摘录的前提）。
_ANAPHORA = re.compile(
    r"^\s*(?:this|that|these|those|it|they|there|here|so|but|and|however|"
    r"as (?:mentioned|noted|we|discussed)|now that|once you|with that|"
    r"因此|所以|这样|这些|上面|前面|如上|接下来)\b", re.I)

#: 直接答案的信号：祈使句、定义句。
_DIRECT_ANSWER = re.compile(
    r"^\s*(?:[A-Z][A-Za-z]+\s+(?:is|are|means|refers)\b"
    r"|(?:Use|Set|Enable|Disable|Remove|Delete|Check|Measure|Add|Avoid|Test|Run|Install|"
    r"Replace|Compress|Defer|Open|Go|Click|Upload|Keep|Limit|Switch|Choose|Pick|Start|Stop)\b)")


def _body(text: str) -> str:
    """只留能承载信息的正文。表格单元格保留（那里全是硬信息），只丢分隔行。"""
    t = re.sub(r"\[IMAGE:\s*[^\]]*\]", " ", text or "")
    t = re.sub(r"^\s*\|?\s*:?-{2,}[-\s:|]*$", "", t, flags=re.M)      # 表格分隔行
    t = re.sub(r"^\s*\|", "", t, flags=re.M)
    t = re.sub(r"\s*\|\s*", ". ", t)                                   # 单元格 → 独立短句
    t = re.sub(r"^#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    return t


def _prose(text: str) -> str:
    """去掉标题行的正文。专名只从这里抽 ——
    标题本身是 Title Case，整行大写会被连成一个巨型「专名」，把密度撑虚。"""
    return _body(re.sub(r"^#{1,6}\s+.*$", "", text or "", flags=re.M))


def word_count(text: str) -> int:
    """与 workflow.count_words 同口径：英文按词、中日韩按字。"""
    t = _body(text)
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", t))
    latin = len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", t))
    return cjk + latin


#: 大写但不是专名的词尾：副词 / 动名词 / 抽象名词。
#: 实测误报：Historically、Performance、Interaction、Evaluating、Recommended 全被当成专名，
#: 于是「开头空转」算出 2 词 —— 其实是把 "Every second your storefront…" 里的
#: Storefront 当成了硬信息。单词专名必须过这道筛。
_NOT_NAME_SUFFIX = re.compile(
    r"(?:ly|ing|ed|tion|sion|ance|ence|ment|ness|ity|ism|able|ible|ive|ous)$", re.I)

#: 全大写缩写照收（LCP / CLS / INP / TTFB / API / CDN）。
_ACRONYM = re.compile(r"^[A-Z]{2,6}$")


def _proper_nouns(sentence: str) -> list[str]:
    """句中（非句首）的专名。**连续大写词合并成一个单元** ——
    "Web Pixels" 是一个专有名称不是两个，分开数会把密度算成两倍。

    单个孤立的大写词要过 _NOT_NAME_SUFFIX：Dawn / Shopify / Yottaa 留下，
    Historically / Performance 剔除。
    """
    out: list[str] = []
    run: list[str] = []
    for i, raw in enumerate(sentence.split()):
        w = re.sub(r"^[\"'(\[]+|[\"')\].,:;!?]+$", "", raw)
        if (i > 0 and len(w) >= 2 and w[0].isupper() and w.lower() not in _STOP
                and re.fullmatch(r"[A-Z][A-Za-z0-9+.\-]*", w)):
            run.append(w)
            # 原词尾带标点就断开：枚举 "Meta Pixel, Google Tag Manager, TikTok" 是三个专名，
            # 不断开会连成一个巨型单元，密度和增益全被这一条撑起来。
            if raw[-1:] in ",;:.!?)":
                out.append(" ".join(run))
                run = []
            continue
        if run:
            out.append(" ".join(run))
            run = []
    if run:
        out.append(" ".join(run))
    keep: list[str] = []
    for p in out:
        # 超过 4 词的连续大写多半是 Title Case 的一整行，不是专名 —— 拆开逐词判断
        parts = p.split()
        for q in ([p] if len(parts) <= 4 else parts):
            if " " in q or _ACRONYM.match(q) or not _NOT_NAME_SUFFIX.search(q):
                keep.append(q)
    return keep


def hard_units(text: str) -> dict[str, set[str]]:
    """把文本拆成三档硬信息单元。

    param  —— 读者能照着设的值：fetchpriority="high"、settings_data.json、2.5 seconds
    entity —— 工具 / 功能 / 格式名：PageSpeed Insights、Web Pixels、WebP、Dawn
    stat   —— 论据统计：63%、four seconds、$50–200。用来论证，不是用来照做。

    形容词、论断、过渡词都不算硬信息 —— 那是稀释剂。
    同一个词重复出现只算一次：**说三遍不等于给了三条信息**。
    """
    body = _body(text)
    param: set[str] = set()
    entity: set[str] = set()
    stat: set[str] = set()

    for m in _CODE.findall(body):
        param.add(m.strip("` ").lower())
    rest = _CODE.sub(" ", body)

    for m in _PARAM.findall(rest):
        m = m.strip().lower()
        if len(m) > 2 and m not in _STOP:
            param.add(m)
    for num, unit in _NUM_UNIT.findall(rest):
        param.add(f"{num} {unit}".lower())

    numeric = _LIST_NUM.sub("", rest)
    for m in _STAT_PCT.findall(numeric):
        stat.add(m.strip().lower())
    for m in _STAT_MONEY.findall(numeric):
        stat.add(m.strip().lower())
    for m in _STAT_BIG.findall(numeric):
        stat.add(m.strip().lower())
    for m in _NUMWORD.findall(numeric):
        stat.add(m.lower())

    for m in _CJK_NAMED.findall(rest):
        entity.add(m.strip().lower())
    if not prose_audit.is_cjk(text):
        for m in _CAMEL.findall(rest):
            if m.lower() not in _STOP:
                entity.add(m.lower())
        prose = _CODE.sub(" ", _prose(text))
        for sent in prose_audit.split_sentences(prose, False):
            for p in _proper_nouns(sent):
                entity.add(p.lower())

    entity -= param
    stat -= (param | entity)
    return {"param": param, "entity": entity, "stat": stat}


def _all_units(text: str) -> set[str]:
    u = hard_units(text)
    return u["param"] | u["entity"] | u["stat"]


#: 引用信号：这句话里的数字是**带出处的**，不是凭空的行业传说。
#: "According to the Yottaa 2025 Web Performance Index…" 里的 63% 与
#: "user abandonment spikes past 60 percent" 里的 60% 不是一个东西 ——
#: 前者可核实，后者是吓唬人。E-E-A-T 认前者。
_SOURCED = re.compile(
    r"(?i)\b(according to|per the|reports?|study|studies|index|survey|research|benchmark|"
    r"data (?:from|by|shows)|analyz\w+ a sample|documentation|官方|据|报告|白皮书)\b")


def sourced_stats(text: str) -> set[str]:
    """带出处的统计。归入证据档 —— 它是「可核实证据」，只是不可照抄。"""
    out: set[str] = set()
    cjk = prose_audit.is_cjk(text)
    for sent in prose_audit.split_sentences(_body(text), cjk):
        if not _SOURCED.search(sent):
            continue
        u = hard_units(sent)
        out |= u["stat"]
    return out


def squash(s: str) -> str:
    """去空白小写化，用于"这个单元还在不在文里"的判断。
    单元是按 "40 khz" 归一化存的，正文里可能写成 "40kHz" —— 不压掉空白就会误报丢失
    （实测两处护栏都因此误拒过：只加不减的补写被判"丢了 1.0 mm"）。"""
    return re.sub(r"\s+", "", (s or "").lower())


_NUM_LEAD = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*[a-z°%]")


def lost_units(before: str, after: str) -> list[str]:
    """before 里有、after 里找不到的证据单元。

    数值型单元（"1 tablespoon"、"15 minutes"、"0.2 mm"）只认**数字还在不在**：
    改写成 "1 tbsp"、"15 min"、折进 "0.1–0.3 mm" 的区间都算保留。
    实测这条闸把 4 次补写拒了 3 次，拒的理由全是这种同义改写 —— 那是误杀，不是保护。
    名称型单元（产品名、路径、文件名）仍按去空白子串严格比。
    """
    low = squash(after)
    nums = set(re.findall(r"\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?", after or ""))
    out = []
    for u in _evidence_units(before):
        if squash(u) in low:
            continue
        m = _NUM_LEAD.match(u)
        if m and m.group(1) in nums:
            continue
        out.append(u)
    return sorted(out)


_DIGITS = re.compile(r"\d[\d,]*(?:\.\d+)?")


def material_numbers(material: str) -> set[str]:
    """素材（事实清单 + 搜索页语料）里出现过的所有数字，去逗号。"""
    return {m.replace(",", "") for m in _DIGITS.findall(material or "")}


def _evidence_units(text: str, material: str | None = None) -> set[str]:
    """证据档 = 参数 + 专名 + **带出处的统计**。

    无出处的论据统计不算 —— 它证明「问题严重」，既不告诉读者该设成多少，
    也没给读者核实的入口。

    material 给了的话，**带数字的参数只有在素材里出现过才算**：密度是优化目标，
    任何数字都计分的话模型就编数字凑分（实测 `Bill_of_Materials.xlsx`、"45–60 days lead time"）。
    竞品页不传 material —— 它们自己就是语料。
    """
    u = hard_units(text)
    params = u["param"]
    if material is not None:
        known = material_numbers(material)
        params = {p for p in params
                  if not _DIGITS.search(p)
                  or all(d.replace(",", "") in known for d in _DIGITS.findall(p))}
    return params | u["entity"] | (sourced_stats(text) & u["stat"])


# --------------------------------------------------------------------------- #
# 0. SERP 解析
# --------------------------------------------------------------------------- #
def parse_serp(search_text: str) -> dict[str, Any]:
    """从 providers.search 返回的文本块里拆出竞品标题、正文语料、PAA 问题。

    search() 的返回是字符串（改签名要动整条链路），所以约定用标记行传结构化信息：
    `Q:` = People Also Ask，`Rel:` = 相关搜索。见 providers._fmt。
    拿不到就返回空列表 —— 调用方必须能接受「这一维测不了」，而不是拿默认值当结论。
    """
    titles = [t.strip() for t in re.findall(r"^Title:\s*(.+)$", search_text or "", re.M)]
    questions = [q.strip() for q in re.findall(r"^Q:\s*(.+)$", search_text or "", re.M)]
    related = [r.strip() for r in re.findall(r"^Rel:\s*(.+)$", search_text or "", re.M)]
    # Content 后面跟的是抓来的整页正文（多行，到下一个 --- 为止）。
    # 早期版本用 ^Content:\s*(.*)$ 只取了第一行，竞品语料被截掉九成，增益因此虚高。
    corpus = "\n".join(
        m.group(1) for m in re.finditer(
            r"^Content:\s*(.*?)(?=^---\s*$|^Title:|^Q:|^Rel:|\Z)",
            search_text or "", re.M | re.S)) or (search_text or "")
    dedup = lambda xs: list(dict.fromkeys(xs))
    return {"titles": dedup(titles), "questions": dedup(questions),
            "related": dedup(related), "corpus": corpus}


# --------------------------------------------------------------------------- #
# 1. 意图覆盖（一票否决）
# --------------------------------------------------------------------------- #
_SHAPE_PATTERNS = [
    ("how_to", re.compile(r"(?i)\bhow to\b|\bguide\b|\bstep[- ]by[- ]step\b|\btutorial\b|"
                          r"\bways to\b|怎么|如何|教程|指南")),
    ("listicle", re.compile(r"(?i)^\s*\d+\s|\b\d+\s+(?:ways|tips|tools|apps|ideas|best|reasons)\b|"
                            r"\btop \d+\b|\bchecklist\b")),
    ("comparison", re.compile(r"(?i)\bvs\.?\b|\bversus\b|\bcompar\w*\b|\balternatives?\b|对比|区别")),
    ("tool", re.compile(r"(?i)\b(?:app|tool|calculator|checker|generator|software|plugin)s?\s*$|工具")),
    ("definition", re.compile(r"(?i)^\s*what (?:is|are)\b|\bmeaning\b|\bdefinition\b|是什么|含义")),
]


def _shapes_of(title: str) -> set[str]:
    """一个标题可以同时是多种形态（"10 Ways to Speed Up..." 既是清单又是教程）。"""
    return {name for name, pat in _SHAPE_PATTERNS if pat.search(title or "")}


def _on_topic(question: str, serp: dict[str, Any], keyword: str = "") -> bool:
    """PAA 里混着与本题无关的问题 —— 一篇讲速度的 SERP 上会出现
    "Is Shopify still worth it in 2026?"、"How much does Shopify take from a $20 sale?"。
    把它们当成"必须回答"喂给写手，会直接写出跑题的小节。

    判据用**竞品标题**而不是正文：标题是紧扣主题的，正文什么都可能顺带提一句。
    问题的实词（去掉主关键词自带的词）至少有一个出现在竞品标题里，
    或者在正文里高频（≥8 次）才算相关。
    """
    kw = set(re.findall(r"[a-z]{3,}", (keyword or "").lower()))
    terms = [w for w in re.findall(r"[a-z]{4,}", question.lower())
             if w not in _STOP and w not in kw]
    if not terms:
        return True                     # 问题完全由主关键词构成 → 必然相关
    heads = " ".join(serp.get("titles") or []).lower()
    low = (serp.get("corpus") or "").lower()
    return any(t in heads or low.count(t) >= 8 for t in terms)


def _answers(article: str, question: str) -> bool:
    """这篇文章有没有回答这个问题。

    判据：问题的实词有多少落在**同一个小节内**。整篇都出现不算 ——
    词散在六个小节里，说明这问题被顺带提过一句，没有被回答。
    """
    terms = [w for w in re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]{2,}", question.lower())
             if w not in _STOP]
    if not terms:
        return False
    need = max(2, int(len(terms) * 0.6))
    return any(sum(1 for t in terms if t in body.lower()) >= need
               for _, body in sections(article))


def intent_coverage(article: str, serp: dict[str, Any], keyword: str = "") -> dict[str, Any]:
    """一票否决项：形态对不对、子问题覆盖了几个。

    形态判定只看**能识别出形态的标题**。八条标题里六条是泛标题时，
    早期版本会把众数算成 "article" 兜底值，反而把一篇标准 how-to 判成形态不符 ——
    那是误报，不是发现。
    """
    titles = serp.get("titles") or []
    corpus = serp.get("corpus", "")
    counts: dict[str, int] = {}
    for t in titles:
        for sh in _shapes_of(t):
            counts[sh] = counts.get(sh, 0) + 1
    # 出现在 ≥25% 标题里的形态，算这条 SERP 的主流形态（可以有多个）
    dominant = {sh for sh, n in counts.items() if titles and n / len(titles) >= 0.25}
    mine = _shapes_of(article.split("\n", 1)[0]) or {"article"}
    shape_match = (not dominant) or bool(mine & dominant)

    raw_q = serp.get("questions") or []
    questions = [q for q in raw_q if _on_topic(q, serp, keyword)]
    dropped = [q for q in raw_q if q not in questions]
    hit = [q for q in questions if _answers(article, q)]
    miss = [q for q in questions if q not in hit]
    ratio = len(hit) / len(questions) if questions else None

    reasons = []
    if not shape_match:
        reasons.append(f"SERP 主流形态是 {sorted(dominant)}，本文是 {sorted(mine)}")
    # 一票否决只在"问题不少于两个、答到的不到一半"时触发。只有一个问题没答到，
    # 那是漏了一处，不是文章写错了方向（用户：差不多就算过关）。
    if ratio is not None and ratio < 0.5 and len(questions) >= 2:
        reasons.append(f"SERP 相关子问题只覆盖 {len(hit)}/{len(questions)}")
    return {"serp_shapes": sorted(dominant), "article_shapes": sorted(mine),
            "shape_match": shape_match, "questions": questions, "off_topic": dropped,
            "covered": hit, "missing": miss,
            "coverage": None if ratio is None else round(ratio, 2),
            "veto": bool(reasons), "reasons": reasons,
            "measurable": bool(titles or questions)}


# --------------------------------------------------------------------------- #
# 2. 信息增益（权重最高，必须对着真实 SERP 算）
# --------------------------------------------------------------------------- #
def information_gain(article: str, corpus: str) -> dict[str, Any]:
    """本文的硬信息单元里，有多少在竞品全文里找不到。

    这是**相对**指标：一篇写满常识的文章密度可以很高，增益却接近零。

    ⚠️ 语料只有 SERP 前几名的抓取正文（各 4000 字符封顶），**不是全网**。
    所以绝对值偏高、只可同题横比，不可跨题比、更不可当作"独家内容占比"对外说。
    没有语料时返回 measurable=False —— 宁可说测不了，也不给一个编出来的分。
    """
    mine = _evidence_units(article)
    if not mine:
        return {"measurable": False, "reason": "本文没有可识别的硬信息单元"}
    if not (corpus or "").strip():
        return {"measurable": False, "reason": "没有竞品语料，增益无法计算"}

    low = corpus.lower()
    theirs = _evidence_units(corpus)
    new = {u for u in mine if u not in theirs and u not in low}
    return {"measurable": True, "total": len(mine), "new": len(new),
            "ratio": round(len(new) / len(mine), 3),
            "samples": sorted(new, key=len, reverse=True)[:12]}


# --------------------------------------------------------------------------- #
# 3-4. 证据密度 / 信息密度
# --------------------------------------------------------------------------- #
def sections(text: str) -> list[tuple[str, str]]:
    """按 H2 切；开头没有 H2 的部分归为 (intro)。"""
    chunks = re.split(r"^(##\s+.*)$", text or "", flags=re.M)
    out: list[tuple[str, str]] = []
    if chunks and chunks[0].strip():
        out.append(("(intro)", chunks[0]))
    for i in range(1, len(chunks), 2):
        out.append((chunks[i].lstrip("# ").strip(),
                    chunks[i + 1] if i + 1 < len(chunks) else ""))
    return out


def density(text: str, floor: float = 3.0, material: str | None = None) -> dict[str, Any]:
    """绝对密度 + 分节明细 + 开头空转 + 跨节重复。

    分节明细才是能拿来改的东西：总分 8.0 但某一节 0.9，要改的是那一节，
    不是让模型「整体再具体一点」。

    floor = "薄节"的判定线。默认 3.0；audit() 会按竞品基线传一个相对值进来 ——
    固定 3.0 的问题是补写救到 3.0 就停手，而竞品在 3.5–6。
    """
    u = hard_units(text)
    words = max(word_count(text), 1)
    evid = len(_evidence_units(text, material))
    total = len(u["param"]) + len(u["entity"]) + len(u["stat"])

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    stat_where: dict[str, list[str]] = {}
    for head, body in sections(text):
        bu = _evidence_units(body, material)
        n = word_count(body)
        if n < 20:
            continue
        rows.append({"head": head, "words": n, "units": len(bu),
                     "per100": round(len(bu) / n * 100, 2),
                     "repeat": round(len(bu & seen) / max(len(bu), 1), 2)})
        seen |= bu
        # 论据统计单独记出现过的小节。它不进证据档，所以上面那条跨节重复度看不见它 ——
        # 实测漏过：同一条「Yottaa 63% / 5 亿访问 / 1300 站」被写进了三个小节，
        # 读者读到第三遍，而信息量一个单位都没增加。
        for v in hard_units(body)["stat"]:
            stat_where.setdefault(v, []).append(head)

    # 开头空转：第一个**参数级**单元之前有多少词。读者容忍度最低的一段。
    # 只认 param：专名（"Online retail"）和无出处统计（"60 percent bounce"）
    # 恰恰是立场铺垫的典型成分 —— 拿它们当锚点会把 320 词的铺垫算成 0 词。
    low = _body(text).lower()
    pos = [low.find(v) for v in u["param"] if low.find(v) >= 0]
    gap = len(re.findall(r"\S+", low[:min(pos)])) if pos else words

    gz = len(gzip.compress((text or " ").encode("utf-8"))) * 8 / max(len(text or " "), 1)
    return {"words": words, "units": total, "per100": round(total / words * 100, 2),
            "param": len(u["param"]), "entity": len(u["entity"]), "stat": len(u["stat"]),
            "evidence": evid, "evidence_per100": round(evid / words * 100, 2),
            "opening_gap": gap, "gzip_bits": round(gz, 2), "sections": rows,
            "floor": floor,
            "thin": [r["head"] for r in rows if r["per100"] < floor and r["words"] >= 100],
            # FAQ 天然是把正文答过的东西再答一遍给搜索页看，重复度高是设计不是缺陷，
            # 不能把它标成"该删"。
            "repetitive": [r["head"] for r in rows
                           if r["repeat"] >= 0.5 and r["units"] >= 4
                           and not re.search(r"(?i)^(frequently asked|faq|常见问题|preguntas frecuentes|"
                                             r"questions fréquentes|häufige fragen|よくある質問|"
                                             r"perguntas frequentes|자주 묻는|domande frequenti|"
                                             r"pertanyaan umum)", r["head"])],
            "repeated_stats": [{"value": v, "sections": sorted(set(w))}
                               for v, w in stat_where.items() if len(set(w)) >= 2]}


def competitor_density(search_text: str) -> dict[str, Any]:
    """竞品页面的证据密度中位数 —— 给用户看的**锚点**。

    "每 100 词 4.9 条硬信息"用户看不懂，"竞品 2.8、你 4.9"一眼就懂。
    同一套 _evidence_units 口径，所以可以直接比。只算抓到全文的页面（>150 词），
    只有摘要的那几条比出来没意义。
    """
    pages = [m.group(1) for m in re.finditer(
        r"^Content:\s*(.*?)(?=^---\s*$|^Title:|^Q:|^Rel:|\Z)", search_text or "", re.M | re.S)]
    vals = []
    for p in pages:
        n = word_count(p)
        if n < 150:
            continue
        # 只比散文页。应用商店列表页抓下来全是导航和功能名堆砌（实测 69 行里 39 行不足 6 词，
        # 密度算出 14.09），拿它当基线会让每一篇正经文章都显得"低于竞品"。
        lines = [l for l in p.splitlines() if l.strip()]
        if lines and sum(1 for l in lines if len(l.split()) < 6) / len(lines) > 0.4:
            continue
        vals.append(len(_evidence_units(p)) / n * 100)
    # 少于三页不给中位数 —— 两个数的"中位数"就是随机挑一个，没资格当锚点
    if len(vals) < 3:
        return {"measurable": False, "pages": len(vals)}
    vals.sort()
    mid = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    return {"measurable": True, "pages": len(vals), "median": round(mid, 2),
            "max": round(vals[-1], 2)}


# --------------------------------------------------------------------------- #
# 5. 结构可读性（相对，不是绝对）
# --------------------------------------------------------------------------- #
#: Flesch-Kincaid 可接受区间：**7–13，所有类型一视同仁**（用户 2026-09-05 定）。
#:
#: 这里原来按文章类型分了五档（how_to 7–10、conceptual 10–13 等），全是【推断】值。
#: 实测把它否掉了：一篇技术教程光是 optimization / configuration / JavaScript /
#: Largest Contentful Paint 这些词就把 FK 顶到 11–12，要压进 7–10 只能删术语，
#: 而删术语正好砍掉信息密度和增益 —— 而那两项才是要最大化的目标。
#: 同一批样本里也能看到这个对冲：FK 9.4 的那篇密度只有 3.34，FK 13.2 的那篇密度 5.63。
#:
#: 所以按类型细分推迟到有发布后行为数据再说，现在只留一条宽区间当护栏：
#: 低于 7 说明话说得太碎、专业感没了；高于 13 说明句子真的绕，该拆。
#: 中间不给意见 —— 拿一个没依据的推断值去逼模型改稿，比不管更糟。
FK_BAND: dict[str, tuple[float, float]] = {}
_DEFAULT_BAND = (7.0, 13.0)


def structural_readability(text: str, topic_type: str = "",
                           language: str = "English") -> dict[str, Any]:
    """三条主判据 + FK 区间校验。

    主判据都是「能不能被摘录」的问题，不是「好不好懂」：
      a. 到第一个直接答案有多远 —— 前面铺垫越长，跳出越早。
      b. 每个 H2 能不能单独回答一个子问题 —— 首句靠 This/因此 接上文的，摘出来读不懂。
      c. 扫读密度 —— 列表 / 表格行占比。
    """
    heads = re.findall(r"^##\s+(.+)$", text or "", re.M)
    cjk = prose_audit.is_cjk(text)
    orphan = []
    for head, body in sections(text):
        if head == "(intro)":
            continue
        sents = prose_audit.split_sentences(body, cjk)
        if sents and _ANAPHORA.match(sents[0]):
            orphan.append(head)

    lead = word_count(text)
    run = 0
    for s in prose_audit.split_sentences(text, cjk):
        if _DIRECT_ANSWER.match(s) or len(_evidence_units(s)) >= 2:
            lead = run
            break
        run += len(s) if cjk else len(s.split())

    lines = (text or "").splitlines()
    scan = sum(1 for l in lines if re.match(r"^\s*(?:[-*+]\s|\d+[.)]\s|\|)", l))

    grade = None
    if (language or "").lower().startswith("english"):
        try:
            import textstat
            grade = round(textstat.flesch_kincaid_grade(_body(text)), 1)
        except Exception:  # noqa: BLE001  textstat 不该拖垮打分
            grade = None

    lo, hi = FK_BAND.get(topic_type, _DEFAULT_BAND)
    return {"h2": len(heads), "orphan_h2": orphan, "lead_to_answer": lead,
            "scan_lines": scan, "scan_ratio": round(scan / max(len(lines), 1), 2),
            "fk": grade, "fk_band": [lo, hi],
            "fk_state": (None if grade is None else
                         "ok" if lo <= grade <= hi else ("hard" if grade > hi else "shallow"))}


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
#: 四项权重。**【推断】** —— 按「增益最高 > 证据 > 密度 > 可读性」的排序设的起点，
#: 没有发布后行为数据校准过。改权重前先拿 GSC 数据验证，别凭手感调。
_WEIGHTS = {"gain": 0.40, "evidence": 0.25, "density": 0.20, "readability": 0.15}

#: 满分线。**【临时基线，n=3】** —— 来源是同一关键词下三篇的实测区间
#: （增益 0.33 / 0.57 / 0.66，证据密度 1.6 / 2.5 / 4.7，信息密度 2.1 / 3.3 / 5.6），
#: **不是行业基准**。样本太少，只够把三篇拉开档次；积累到几十篇后必须重定。
#: 在此之前，分数只可同题横比，不可当作绝对质量值对外说。
_FULL = {"gain": 0.70, "evidence": 5.0, "density": 6.5}


def audit(article: str, *, search_text: str = "", topic_type: str = "",
          keyword: str = "", language: str = "English",
          material: str | None = None) -> dict[str, Any]:
    """跑完五项，返回可直接 emit 给前端的结构。

    意图覆盖**不参与加权** —— 它是闸门。形态不对或子问题覆盖不到一半，
    分数照给但必须带上否决理由，别让「85 分」盖过「你写的根本不是 SERP 要的东西」。
    """
    if word_count(article) < 50:
        # 空文章的 lead_to_answer=0、orphan=0，可读性会拿满分 —— 必须先短路，
        # 否则"什么都没写"能拿到 20 分。
        return {"score": 0, "parts": {k: 0 for k in _WEIGHTS}, "intent": {},
                "gain": {"measurable": False, "reason": "文章太短"},
                "density": {}, "readability": {}, "unmeasured": list(_WEIGHTS)}

    serp = parse_serp(search_text)
    intent = intent_coverage(article, serp, keyword)
    gain = information_gain(article, serp.get("corpus", ""))
    bench = competitor_density(search_text)
    tgt = density_target(search_text)
    # 薄节线相对竞品：目标的 80%，下限 3.0。补写按这条线救，不是救到 3.0 就停。
    den = density(article, floor=max(3.0, round(tgt["per100"] * 0.8, 1)), material=material)
    read = structural_readability(article, topic_type, language)

    clamp = lambda x: max(0.0, min(1.0, x))
    parts: dict[str, float | None] = {
        "gain": clamp(gain["ratio"] / _FULL["gain"]) if gain.get("measurable") else None,
        "evidence": clamp(den["evidence_per100"] / _FULL["evidence"]),
        "density": clamp(den["per100"] / _FULL["density"]),
        # 「开头多少词内给答案」不再计分（用户 2026-09-05：不要求这个）；只看 H2 自足 + 可扫读
        "readability": clamp(0.6 * (1 - clamp(len(read["orphan_h2"]) / max(read["h2"], 1)))
                             + 0.4 * clamp(read["scan_ratio"] / 0.25)),
    }
    live = {k: v for k, v in parts.items() if v is not None}
    wsum = sum(_WEIGHTS[k] for k in live) or 1
    score = round(sum(v * _WEIGHTS[k] for k, v in live.items()) / wsum * 100)

    return {"score": score,
            "parts": {k: (None if v is None else round(v * 100)) for k, v in parts.items()},
            "intent": intent, "gain": gain, "density": den, "readability": read,
            "benchmark": bench, "target": tgt,
            "unmeasured": [k for k, v in parts.items() if v is None]}


def density_target(search_text: str, wordcount: int = 0) -> dict[str, Any]:
    """这篇该写多密：竞品中位数 × 1.2，下限 4 条/100 词。

    为什么必须量化：旧规则"每个 H2 至少 3 条具体信息"换算下来是每 100 词 1 条，
    而实测竞品是 3.5–6 条 —— 目标定得比竞品低，产出当然低于竞品。
    没有竞品基线时用 4.0（同题四篇实测里"高于竞品"那篇是 4.7）。
    """
    bench = competitor_density(search_text)
    base = bench.get("median") if bench.get("measurable") else None
    per100 = max(4.0, round((base or 0) * 1.2, 1))
    total = int(per100 * wordcount / 100) if wordcount else 0
    return {"per100": per100, "competitor": base, "pages": bench.get("pages", 0),
            "total": total}


def gap_brief(search_text: str, keyword: str = "", max_items: int = 14,
              wordcount: int = 0, h2_count: int = 5) -> str:
    """写作前的**素材**块：竞品的信息基线 + 搜索页上没人答好的问题。

    为什么是素材不是规则：让 prompt 写「要提高信息增益」等于没说 —— 模型不知道
    竞品已经写了什么。把竞品共同覆盖的硬信息**列出来**，它才知道哪些是基线、
    哪些说出来算不上独家。这一步全是确定性计算，零 LLM、零额外 API。
    """
    serp = parse_serp(search_text)
    # 每页只取 Content 正文 —— 连 Title:/URL:/SourceType: 一起算，
    # 会把 url、content、sourcetype 这些标记词当成"行业基线"喂给写手。
    pages = [m.group(1) for m in re.finditer(
        r"^Content:\s*(.*?)(?=^---\s*$|^Title:|^Q:|^Rel:|\Z)",
        search_text or "", re.M | re.S)]
    seen: dict[str, int] = {}
    for page in pages:
        for u in _evidence_units(page):
            seen[u] = seen.get(u, 0) + 1
    # 两家以上都写了 = 行业基线；只有一家写 = 不够稳，不当基线用
    baseline = sorted((u for u, n in seen.items() if n >= 2), key=lambda u: -seen[u])
    questions = [q for q in serp["questions"] if _on_topic(q, serp, keyword)]

    parts: list[str] = []

    # SERP 形态：排在前面的是什么体裁，你就得写成什么体裁。形态不对是一票否决项。
    counts: dict[str, int] = {}
    for t in serp["titles"]:
        for sh in _shapes_of(t):
            counts[sh] = counts.get(sh, 0) + 1
    n_titles = len(serp["titles"])
    dominant = [sh for sh, n in counts.items() if n_titles and n / n_titles >= 0.25]
    if dominant:
        names = {"how_to": "操作教程（How to / 指南）", "listicle": "编号清单（N 个方法）",
                 "comparison": "对比评测（A vs B）", "tool": "工具页",
                 "definition": "概念解释（什么是 X）"}
        parts.append("**搜索页排前面的是这些体裁：** "
                     + "、".join(names.get(s, s) for s in dominant)
                     + f"（{n_titles} 条结果里统计出来的）。\n"
                       "**本文必须写成同一体裁** —— 标题和结构都要对得上。"
                       "体裁不对，内容再好也接不住这批搜索的人。"
                     + ("\n搜索页是清单 / 对比 / 工具形态：读者来是要**看到具体选项的名字**的。"
                        "本文必须点名列出主要选项（产品、服务商、工具），写成事实（它是什么、"
                        "规格 / 价格 / 适用范围），**不背书、不排名、不说「最佳」**。"
                        "不列名字 = 既丢密度也丢意图。"
                        if set(dominant) & {"listicle", "comparison", "tool"} else ""))

    # 密度：不再给"每 100 词 x 条 / 每节一表"的量化目标（用户 2026-09-05）——
    # 那两条把模型压成了报关手册和编造的规格表。只留一句人设；补不补写由后台按竞品基线决定。
    parts.append("你非常喜欢列出数据和事实，非常在乎文章的信息密度。")

    if questions:
        parts.append("**搜索页上读者正在问的（People Also Ask，真实数据不是推测）：**\n"
                     + "\n".join(f"- {q}" for q in questions)
                     + "\n**这几个问题一个都不能漏。** 每个问题要么直接拿去当 H2 标题，"
                       "要么在正文里有一处专门回答它的段落。判据是：把那一段单独摘出来，"
                       "它本身就是这个问题的完整答案 —— 不能靠上文才读得懂，"
                       "也不能只是顺带提了一句相关的词。")
    if serp["related"]:
        parts.append("**相关搜索（次级意图，能覆盖就覆盖）：** "
                     + "、".join(serp["related"][:8]))
    # 竞品都在点名的实体：多家竞品都提到的产品 / 服务商 / 工具名。读者搜到这个词，
    # 预期看到这些名字 —— 3PL 那篇竞品密度 6.25 我们只有 4.3，差的就是每家服务商
    # 一行参数（价格档、仓库数、起订量）。这是给素材：点名 + 给参数，不背书。
    kw_words = set(re.findall(r"[a-z]{3,}", (keyword or "").lower()))
    named = [u for u in baseline
             if u.replace(" ", "").isalpha() and not any(w in kw_words for w in u.split())
             and 3 <= len(u) <= 32 and not _NOT_NAME_SUFFIX.search(u.split()[-1])
             and u not in _STOP and seen.get(u, 0) >= 2]
    # 品牌在句中是大写的（ShipBob、Flexport），普通词不是（scale、connect、improve）。
    # 在竞品正文里数一下：大写形态占比不到六成的，不是名字，是词。
    corpus_raw = serp.get("corpus", "")
    def _mostly_capitalized(u: str) -> bool:
        # 不区分大小写地找，再看命中里首字母大写的占几成（"ShipBob" 内部还有大写，
        # 拼一个 "Shipbob" 去精确匹配是匹配不到的）
        first = u.split()[0]
        if len(first) < 5 and " " not in u:          # DTC / SLA 这类缩写是术语不是产品
            return False
        cap = low = 0
        for m in re.finditer(r"\b" + re.escape(first) + r"\b", corpus_raw, re.I):
            pre = corpus_raw[max(0, m.start() - 2):m.start()]
            if not pre.strip() or pre.rstrip()[-1:] in ".!?:|#-":   # 句首 / 表头 / 列表首
                continue                                          # 这里的大写不说明是名字
            if m.group(0)[:1].isupper():
                cap += 1
            else:
                low += 1
        return cap >= 3 and cap / max(cap + low, 1) >= 0.6
    named = [u for u in named
             if u not in {"google", "amazon", "shopify", "reddit", "youtube"} and _mostly_capitalized(u)][:10]
    if len(named) >= 3:
        parts.append("**多家竞品都点名的产品 / 服务商 / 工具：** " + "、".join(named)
                     + "\n读者搜这个词就是预期看到这些名字。本文要覆盖它们，"
                       "**每个给 2–3 条具体参数**（价格档位、规格、起订量、适用范围）"
                       "—— 写成事实对照（表格最好），不背书、不排名。")
    if baseline:
        parts.append("**竞品已经写透的（≥2 家都写了，属于行业基线）：**\n"
                     + "、".join(baseline[:max_items])
                     + "\n这些**该写还得写**（读者要看到），但**不许当成本文的独特价值** —— "
                       "别在这些点上花大段篇幅论证自己有多懂。真正的加分在基线之外："
                       "具体的参数值、文件名、设置路径、失败表现、判断门槛。")
    return "\n\n".join(parts)


def readability_note(topic_type: str) -> str:
    """写作阶段的可读性目标。绝对分只是护栏，结构才是主判据。"""
    lo, hi = FK_BAND.get(topic_type, _DEFAULT_BAND)
    return (f"目标阅读年级 FK {lo:.0f}–{hi:.0f}。这是一条**宽护栏不是靶心** —— "
            f"落在区间内就别为了挪动这个数字去改稿，更不许为了压低它删掉术语和具体参数。"
            f"比分数更重要的是两条结构要求："
            f"①每个 H2 的首句不许用 This/因此 接上文 —— 它要能被单独摘出来读；"
            f"②能列表就别写成段落。")


def format_report(r: dict[str, Any]) -> str:
    """一行摘要，给 SSE 日志和前端徽章用。"""
    g, i, d = r["gain"], r["intent"], r["density"]
    gtxt = f"增益 {g['ratio']:.0%}" if g.get("measurable") else "增益 无语料"
    itxt = f"意图 {len(i['covered'])}/{len(i['questions'])}" if i["questions"] else "意图 无 PAA"
    return (f"内容分 {r['score']}／100 · {gtxt} · {itxt} · "
            f"密度 {d['per100']}/100词 · 证据 {d['evidence_per100']}/100词"
            + ("　⛔ " + "；".join(i["reasons"]) if i["veto"] else ""))


def to_prompt_block(r: dict[str, Any], max_items: int = 8) -> str:
    """把不合格项变成**这篇文章的具体清单**交给模型改。

    和 prose_audit.to_prompt_block 一个思路：能用代码数清楚的就别写进 prompt 当规则，
    只把这一篇的违规位置递过去。
    """
    lines: list[str] = []
    i, d, read = r["intent"], r["density"], r["readability"]

    for q in (i.get("missing") or [])[:max_items]:
        lines.append(f"- 搜索页上读者反复问、本文却没回答：「{q}」——"
                     f"补一个能独立回答它的小节或段落。")
    for h in (d.get("thin") or [])[:max_items]:
        lines.append(f"- 「{h}」这一节几乎没有硬信息（参数 / 文件名 / 功能名），全是立场铺垫——"
                     f"要么补具体的，要么删掉并入别节。")
    for h in (d.get("repetitive") or [])[:max_items]:
        lines.append(f"- 「{h}」这一节一半以上的信息前文已经给过——只留新增的部分。")
    # 跨节重复的统计**不进用户清单**（用户 2026-09-05 定）：炼钢文里 1,600°C / 400 / 1,800
    # 这类工艺参数本来就该在多个小节出现，六条"只在一节保留"全是噪音。
    # 真正该删的（带引用信号的重复统计句）由 postfix.dedupe_repeated_stats 用代码收口，不用提示。
    for h in (read.get("orphan_h2") or [])[:max_items]:
        lines.append(f"- 「{h}」首句靠上文接续（This / 因此…），单独摘出来读不懂——"
                     f"改成自足的开头。")
    if read.get("fk_state") == "hard":
        lo, hi = read["fk_band"]
        lines.append(f"- 阅读年级 FK {read['fk']}，这类文章的目标是 {lo}–{hi}——拆长句，别改术语。")

    return "\n".join(lines[: max_items * 2])
