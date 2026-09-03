"""润色前的确定性文风体检（零 LLM 成本）。

为什么要有这一层：把「规则」写进 prompt 是最贵也最不可靠的做法 —— 模型要在长文里
自己找违规，找不找得到全看运气，而且规则越多越稀释注意力。能用正则数清楚的事
（破折号几个、哪句超 30 词、哪几句同一个词开头）就该在 Python 里数完，
只把**这篇文章的具体违规清单**交给模型去改。

分层参照 yzhao062/agent-style 的四级模型：
  Tier-1 机械判定  → 本模块，直接给出违规位置
  Tier-2 启发式度量 → 本模块，给出比例/密度
  Tier-3 语义判断  → 留在 prompt 里（比喻是否生造、句子有没有干活）

两条「加法规则」是本模块特有的，多数英文规则集反而要求砍掉它们：
  - 犹豫与限定语（multi/likely/大概/多半）：全知全能的语气才是 AI 指纹，
    StoryScope 实测 AI 的「叙述者直接讲道理」比例 52%→77%。一个都没有要提醒补。
  - 第一人称痕迹：老师傅口吻靠这个立住。

英文按词、中日韩按字，两套阈值。
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# 基础切分
# --------------------------------------------------------------------------- #
_CJK = re.compile(r"[一-鿿぀-ヿ가-힯]")
_IMAGE_TAG = re.compile(r"\[IMAGE:\s*[^\]]*\]")
_HEADING = re.compile(r"^#{1,6}\s", re.M)


def is_cjk(text: str) -> bool:
    """中日韩字符占比超 20% 就按中文规则走。"""
    t = text or ""
    return bool(t) and len(_CJK.findall(t)) / max(len(t), 1) > 0.2


def _body_only(text: str) -> str:
    """剥掉标题、表格、代码块、列表符号和图片占位符 —— 只留散文。

    这些结构本来就该长得整齐，拿它们去算句长和节奏必然误报。
    """
    t = _IMAGE_TAG.sub("", text or "")
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"^#{1,6} .*$", "", t, flags=re.M)
    t = re.sub(r"^\s*\|.*$", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*+]\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*\d+\.\s+", "", t, flags=re.M)
    return t


def split_sentences(text: str, cjk: bool) -> list[str]:
    body = _body_only(text)
    parts = re.split(r"(?<=[。！？])\s*", body) if cjk else re.split(r"(?<=[.!?])\s+", body)
    out = []
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if (len(s) if cjk else len(s.split())) >= (6 if cjk else 3):
            out.append(s)
    return out


def _slen(s: str, cjk: bool) -> int:
    return len(s) if cjk else len(s.split())


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _body_only(text).split("\n\n") if p.strip()]


# --------------------------------------------------------------------------- #
# 规则表
# --------------------------------------------------------------------------- #
# 过渡词（Tier-2 密度）
_TRANSITIONS_EN = re.compile(
    r"\b(Moreover|Furthermore|Additionally|In addition|Consequently|Therefore|"
    r"Nevertheless|Nonetheless|Thus|Hence)\b", re.I)
_TRANSITIONS_ZH = re.compile(r"(此外|而且|因此|然而|不仅如此|综上所述|由此可见|与此同时)")

# 段末总结句开头（agent-style RULE-E）
_SUMMARY_LEAD_EN = re.compile(
    r"^\s*(In short|In summary|In conclusion|Ultimately|Overall|All in all|"
    r"To sum up|That(?:'s| is) why|The bottom line)\b", re.I)
_SUMMARY_LEAD_ZH = re.compile(r"^\s*(总之|总的来说|综上|归根结底|说到底|这就是为什么)")

# 否定式对照（Wikipedia + agent-style RULE-07）
_NEG_PARALLEL_EN = re.compile(
    r"\b(?:it(?:'s| is)|this is|that(?:'s| is))\s+not\s+(?:just\s+)?[^.,;]{2,40}[,—-]\s*it(?:'s| is)\b|"
    r"\bnot\s+(?:just|only)\s+[^.,;]{2,40},\s*but\s+(?:also\s+)?|"
    # 同位语形式:"a marathon, not a sprint" / "data, not anxiety"。
    # 2026-09-02 漏过:旧正则只认 "it's not X, it's Y",而实测产出里六处全是这一种。
    r"\b\w+,\s*not\s+(?:a\s+|an\s+|the\s+|your\s+)?\w+(?=[\s.,;])", re.I)
_NEG_PARALLEL_ZH = re.compile(r"不是[^，。；]{1,20}[，,]\s*而是|不仅[^，。；]{1,20}[，,]\s*(?:而且|更)")

# 犹豫/限定语（加法规则：太少要补）
_HEDGE_EN = re.compile(
    r"\b(usually|often|typically|generally|in most cases|tends? to|likely|probably|"
    r"in my experience|I(?:'d| would) say|arguably|roughly|about|somewhat)\b", re.I)
_HEDGE_ZH = re.compile(r"(多半|大概|通常|一般来说|我个人|依我看|争议不小|不一定|未必|大致)")

# 第一人称痕迹（加法规则）
_FIRST_PERSON_EN = re.compile(r"\b(I|I've|I'm|my|we|we've|our)\b")
_FIRST_PERSON_ZH = re.compile(r"(我见过|我遇到|来找我|我个人|前阵子|我们)")

# P0 事故（必须零容忍 —— 实测漏进过成品）
_P0_PATTERNS = [
    ("unfilled_placeholder", re.compile(r"\[(?:insert|your|add|tbd|xx+)[^\]]{0,60}\]", re.I)),
    ("citation_leak", re.compile(r"(citeturn\w+|oai_citation|\[attached_file:\d+\])", re.I)),
    ("tracking_param", re.compile(r"utm_source=(?:chatgpt|grok|perplexity)\.com", re.I)),
    ("cutoff_disclaimer", re.compile(
        r"\b(as of my (?:last|knowledge)|my training data|I don't have access to real-?time)\b", re.I)),
]

# 事实清单的来源字段被原样带进正文（实测一篇 11 处）
_SOURCE_LEAK = re.compile(r"\((?:Source|来源)\s*[:：][^)]{2,80}\)|\br/[A-Za-z]\w{1,30}\b")

# 外来产品独占小节：标题是「When to Consider X / X as an alternative / A native baseline: X」
_FOREIGN_H2 = re.compile(
    r"(?im)^#{2,3}\s+.*\b(?:when to consider|as an alternative|alternatives? to|"
    r"a native baseline|other options?|instead of)\b.*$")

# 箭头/流程符号
_ARROW = re.compile(r"(→|➔|⇒|=>)")

# 合成案例 / 假经验口吻：声称经验却给不出可核实细节。
# ⚠️ 2026-09-02 教训：旧版只写了动词形式（we analyzed a…），实测 6 处假人设**一个都没抓到** ——
# 真实产出用的全是介词短语形式（In our work with clients / In my experience with new sellers）。
# 加规则时别只想一种句式，先去成品里数一遍实际长什么样。
_FAKE_CASE = re.compile(
    r"(?i)(?:\b(?:we|our team)\s+(?:recently\s+|once\s+|even\s+|also\s+)?"
    r"(?:analyzed|examined|tested|reviewed|audited|worked with|helped|consulted|saw|have seen|had|recommend)\b"
    r"|\bIn (?:our|my)\s+(?:work|audits?|experience|testing|tests|consulting)\b"
    r"|\bBased on (?:our|my)\s+(?:testing|experience|audits?|work)\b"
    r"|\bfrom (?:our|my)\s+experience\b"
    r"|\bwe(?:'ve| have)\s+(?:consistently\s+)?(?:found|seen|observed)\s+that\b)", re.I)

# 保留表：主语 we + 动词是「看到/观察」+ 宾语是社区/用户/多数卖家 —— 这是 hedge 不是造经验。
# 用户 2026-09-03 划的线：we + test/examine/audit/work with → 删；we see + 社区/用户 → 留。
_FAKE_CASE_KEEP = re.compile(
    r"(?i)\bwe\s+(?:see|notice|hear)\s+(?:across|from|among|in)\s+(?:the\s+)?"
    r"(?:seller|user|merchant|store owner|community|forum|reddit)")

# 自造术语：给常识现象起个名字再反复使用（同一个词不同段落定义还会漂移）
_INVENTED_TERM = re.compile(
    r"(?i)\bthe\s+([a-z]+(?:\s+[a-z]+)?)\s+(?:principle|effect|paradox|fallacy|dilemma|trap|blindness|rule)\b")

# 第三方官方话术：带数字的产品卖点，多半是从产品方官网搬来的
_VENDOR_PITCH = re.compile(
    r"(?i)\b\d{2,}\+?\s*(?:integrations|apps|templates|connectors|partners)\b"
    r"|\ball[- ]in[- ]one platform\b|\bindustry[- ]leading\b|\bbest[- ]in[- ]class\b")

# 主动推荐话术（用户没要求推荐时不该出现）
_ENDORSE = re.compile(
    r"(?i)\bwe\s+recommend\b|\bour\s+(?:top\s+)?pick\b|\bthe\s+best\s+choice\s+(?:is|for)\b"
    r"|\bworth\s+(?:the\s+)?investment\b|\bhighly\s+recommend")
_FAKE_CASE_ZH = re.compile(r"(我们|我)(最近|曾经|之前)?(分析|复盘|服务|接触|帮助)过(一个|一家|不少|很多)")

# 编造资历：用数字给自己贴权威
_FAKE_CRED = re.compile(
    r"(?i)\b(?:we(?:'ve| have)?\s+(?:watched|helped|served|worked with|managed)|"
    r"trusted by|used by)\s+(?:over\s+|more than\s+)?[\d,]{3,}\+?\s*\w*")

# 固定开场公式：每节都这么起手就是模板
_FORMULA_OPENER = re.compile(
    r"(?im)^(?:We need to\b|To build\b[^.]{0,40},\s*we\b|One of the most\b|"
    r"A (?:massive|common|huge) (?:misconception|mistake|myth)\b|"
    r"There is a (?:massive|major|constant|significant)\b)")

# 具体性：可验证的信息载体
_UNIT_NUM = re.compile(r"(?<![\w$])\$\s?\d[\d,.]*|(?<![\w])\d+(?:\.\d+)?\s?(?:%|ms|KB|MB|GB|s\b|"
                       r"seconds?|minutes?|hours?|days?|weeks?|months?|years?|x\b)", re.I)
_PROPER_NOUN = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-zA-Z]{2,}(?:[A-Z][a-zA-Z]*)?\b", re.M)


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #
def audit(text: str) -> dict:
    """返回体检结果。纯计算，不调用任何模型。"""
    cjk = is_cjk(text)
    sents = split_sentences(text, cjk)
    paras = paragraphs(text)
    n_sent, n_para = len(sents), len(paras)
    words = sum(_slen(s, cjk) for s in sents)
    findings: list[dict] = []

    def add(rule, severity, detail, samples=None):
        findings.append({"rule": rule, "severity": severity, "detail": detail,
                         "samples": samples or []})

    # ── P0：事故级，零容忍 ──────────────────────────────────────────
    for name, pat in _P0_PATTERNS:
        hits = [m.group(0) for m in pat.finditer(text or "")]
        if hits:
            add(name, "P0", f"{len(hits)} 处必须删除的残留", hits[:3])

    # ── Tier-1：机械判定 ────────────────────────────────────────────
    # 破折号配额：不超过段落数的 1/5（按文长自适应，比绝对值更合理）
    dashes = len(re.findall(r"—|(?<=\w)--(?=\w)| -- ", text or ""))
    quota = max(1, n_para // 5)
    if dashes > quota:
        add("em_dash_quota", "P1",
            f"破折号 {dashes} 个，超出配额（{n_para} 段 → 上限 {quota} 个）。多数改成句号断句")

    arrows = _ARROW.findall(text or "")
    if arrows:
        add("arrow_symbol", "P1", f"{len(arrows)} 处箭头符号，改成把话说完")

    # 超长句
    limit = 60 if cjk else 30
    long_s = [s for s in sents if _slen(s, cjk) > limit]
    if long_s:
        add("long_sentence", "P1",
            f"{len(long_s)} 句超过 {limit}{'字' if cjk else '词'}，拆开",
            [s[:110] for s in long_s[:4]])

    # 连续同词开头
    runs, i = [], 0
    while i < len(sents) - 2:
        head = " ".join(sents[i].split()[:2]).lower() if not cjk else sents[i][:3]
        j = i + 1
        while j < len(sents):
            h2 = " ".join(sents[j].split()[:2]).lower() if not cjk else sents[j][:3]
            if h2 != head:
                break
            j += 1
        if j - i >= 3:
            runs.append((head, j - i))
        i = j if j > i else i + 1
    for head, n in runs[:3]:
        add("same_opener_run", "P2", f"连续 {n} 句以「{head}」开头，换句式")

    # ── Tier-2：启发式度量 ──────────────────────────────────────────
    trans_pat = _TRANSITIONS_ZH if cjk else _TRANSITIONS_EN
    n_trans = len(trans_pat.findall(text or ""))
    if words and n_trans / words * 100 > 0.35:
        add("transition_density", "P2",
            f"过渡词 {n_trans} 个（每百{'字' if cjk else '词'} {100*n_trans/words:.2f}），删掉多数")

    lead_pat = _SUMMARY_LEAD_ZH if cjk else _SUMMARY_LEAD_EN
    closers = [p for p in paras
               if lead_pat.search(split_sentences(p, cjk)[-1] if split_sentences(p, cjk) else "")]
    if n_para >= 4 and len(closers) > max(2, n_para // 6):
        add("paragraph_summary_closer", "P2",
            f"{len(closers)}/{n_para} 段以总结句收尾。真人写字大部分段落是平的，留 2-3 处就够")

    neg_pat = _NEG_PARALLEL_ZH if cjk else _NEG_PARALLEL_EN
    negs = [m.group(0) for m in neg_pat.finditer(text or "")]
    if negs:
        add("negative_parallelism", "P1",
            f"{len(negs)} 处「不是 X 而是 Y」式对照，改成直接的正面陈述", negs[:3])

    # 句长节奏
    if n_sent >= 8:
        L = [_slen(s, cjk) for s in sents]
        avg = sum(L) / len(L)
        cv = (sum((x - avg) ** 2 for x in L) / len(L)) ** 0.5 / avg if avg else 0
        if cv < 0.30:
            add("uniform_rhythm", "P1",
                f"句长过于均匀（CV {cv:.2f}，均长 {avg:.0f}）。用短句制造停顿，长句承载因果")

    # ── 加法规则：少了要补（多数英文规则集反而要求砍掉这两项）─────────
    hedge_pat = _HEDGE_ZH if cjk else _HEDGE_EN
    n_hedge = len(hedge_pat.findall(text or ""))
    if words >= 600 and n_hedge <= 1:
        add("no_hedging", "P1",
            f"全文只有 {n_hedge} 处限定语。全知全能的语气正是 AI 指纹 —— "
            f"该不确定的地方就写「多半 / 通常 / 我个人偏向」")

    fp_pat = _FIRST_PERSON_ZH if cjk else _FIRST_PERSON_EN
    if words >= 600 and not fp_pat.search(text or ""):
        add("no_first_person", "P2",
            "全文没有第一人称痕迹。经验口吻靠「我见过 / 我们做过」立住")

    # ── 具体性：整篇零可验证信息是最致命的 AI 痕迹 ──────────────────
    # 2026-09-02 实测：一篇 2286 词的成品里 0 个工具名、0 个金额、0 个百分比，
    # 而它的搜索资料里 eRank/Etsy Ads/$0.20/7%/20%/29% 全都有 —— 写正文那步全丢了。
    # 这条是本模块唯一"内容级"规则，也是命中率最高的一条。
    if words >= 600:
        units = _UNIT_NUM.findall(text or "")
        density = 100 * len(units) / words
        if density < 0.15:
            add("no_concrete_specifics", "P1",
                f"全文只有 {len(units)} 处带单位的具体数字（每百词 {density:.2f}）。"
                f"读者拿不到任何可验证的东西 —— 从参考资料里取金额/百分比/期限补上，"
                f"**但资料没有的不许编**",
                units[:4])

    # ── 编造：比空洞更糟 ────────────────────────────────────────────
    fake_pat = _FAKE_CASE_ZH if cjk else _FAKE_CASE
    fake = [m.group(0).strip() for m in fake_pat.finditer(text or "")]
    if not cjk:
        # 剔除"we see across the seller community"这类 hedge —— 那是限定语，不是造经验
        keep_spans = [(m.start(), m.end()) for m in _FAKE_CASE_KEEP.finditer(text or "")]
        fake = [f for f in fake if not any(
            (text or "").find(f) >= a - 10 and (text or "").find(f) <= b for a, b in keep_spans)]
    if fake:
        # ⚠️ 措辞很重要：上一版写的是"要么换成真实例子，要么删掉举例"，
        # 给了模型选择余地，结果它两样都不选，三篇全部原样保留。
        # 改成单一、机械、无歧义的动作 —— 删前缀、留陈述。
        add("synthetic_case", "P0",
            f"{len(fake)} 处编造的经验声明，**逐句按下面三选一处理**（不要改写成别的说法）："
            "① 后半句的事实在事实清单里有 → 删掉前缀改挂来源："
            "「In our tests, new listings need 2 to 4 weeks」→"
            "「Sellers on Etsy forums report 2 to 4 weeks」；"
            "② 清单里没有但说法成立 → 删前缀并降成限定语："
            "「New listings usually need a few weeks」；"
            "③ 清单里没有、也无从判断 → 整句删掉。"
            "⚠️ 只删前缀留一个裸断言是**不合格**的 —— "
            "那只是把假权威换成了无来源硬断言",
            fake[:4])
    cred = _FAKE_CRED.findall(text or "")
    if cred:
        add("fabricated_credential", "P0",
            f"{len(cred)} 处用数字自我背书（「服务过 5 万个品牌」）。资料里没有就删掉")

    # ── 章节承诺 vs 兑现 ───────────────────────────────────────────
    # 标题写了「价格什么时候变贵」，节内却一个数字都没有 = 骗点击。
    # 实测命中：klaviyo 篇的「When does the pricing become too expensive?」零价格。
    PROMISE = (
        ("价格", re.compile(r"(?i)pric|cost|expensive|budget|\bfee|多少钱|价格|费用"),
         re.compile(r"\$\s?\d|\d+\s?(?:USD|EUR|€|元)|\d+\s?%")),
        ("数据", re.compile(r"(?i)\bdata\b|statistic|benchmark|number|数据|基准"),
         re.compile(r"\d")),
        # ⚠️ 步骤可以用 H3 小标题承载，不一定是编号列表 —— 只认列表会误报。
        # 实测把两节内容扎实的小节判成"骗点击"，差点用假警报淹掉真警报。
        ("步骤", re.compile(r"(?i)step[- ]by[- ]step|how to|步骤|怎么做"),
         re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+|^###\s|\bfirst\b|\bthen\b|\bnext\b|第一步|然后")),
    )
    for blk in re.split(r"^## ", text or "", flags=re.M)[1:]:
        title = blk.split("\n")[0].strip()
        for label, tpat, vpat in PROMISE:
            if tpat.search(title) and not vpat.search(blk[len(title):]):
                add("promise_not_kept", "P1",
                    f"小节「{title[:40]}」承诺了{label}，节内却没有 —— "
                    f"这是读者点进来最想看的地方，空着等于骗点击")

    # ── 编造术语 / 替人打广告 / 未经要求的推荐 ──────────────────────
    terms = _INVENTED_TERM.findall(text or "")
    if terms:
        add("invented_term", "P1",
            f"{len(terms)} 处自造术语（给常识起名再反复用，且定义容易漂移）。直接把现象描述清楚",
            [f"the {t} …" for t in terms[:3]])
    pitch = _VENDOR_PITCH.findall(text or "")
    if pitch:
        add("vendor_pitch", "P1",
            f"{len(pitch)} 处像是从产品方官网搬来的卖点话术。对比文里替一方念广告词会被当软文",
            [str(p) for p in pitch[:3]])
    endorse = _ENDORSE.findall(text or "")
    if endorse:
        add("unsolicited_endorsement", "P1",
            f"{len(endorse)} 处主动背书话术（我们推荐 / 最佳选择）。"
            f"用户没要求推荐产品时不该出现")

    # ── 来源字段泄漏 / 外来产品独占小节 ─────────────────────────────
    leaks = _SOURCE_LEAK.findall(text or "")
    if leaks:
        add("source_field_leak", "P0",
            f"{len(leaks)} 处事实清单的来源字段原样出现在正文（「(Source: …)」「r/Etsy」）。"
            f"官方来源改写成散文，第三方写机构+年份，社区来源不署名改限定语", leaks[:3])
    fh = _FOREIGN_H2.findall(text or "")
    if fh:
        add("foreign_product_section", "P1",
            f"{len(fh)} 个小节在给主题之外的产品独立立传（「When to Consider X」）。"
            f"没被用户点名的产品不许有自己的 H2/H3", [h.strip()[:70] for h in fh[:2]])

    # ── 固定开场公式 ───────────────────────────────────────────────
    if not cjk:
        openers = _FORMULA_OPENER.findall(text or "")
        if len(openers) >= 3:
            add("formula_openers", "P1",
                f"{len(openers)} 节用同一种「宣告问题→我们需要→解释」的公式开头，"
                f"通篇如此就是模板。留一处，其余直接从具体的事切入")

    order = {"P0": 0, "P1": 1, "P2": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return {
        "cjk": cjk, "words": words, "sentences": n_sent, "paragraphs": n_para,
        "findings": findings,
        "counts": {k: sum(1 for f in findings if f["severity"] == k) for k in ("P0", "P1", "P2")},
    }


def keyword_stuffing(article: str, keywords: list[str]) -> list[str]:
    """关键词硬塞检测 —— 看**句法结构**，不看大小写，不靠动词表。

    2026-09-03 二次教训：第一版用动词表（learning/using/finding…），是照着当轮的例子写的；
    下一轮模型换成 Figuring out / Implementing / When we evaluate / To reduce / True X works by，
    全部漏检、报了假 0。动词表永远追不上模型的换词，改成结构规则：

      关键词前 3 个 token 内出现「-ing 动词 / To / 形容词垫词 / When we|you 动词」 → 塞词
      关键词后紧跟「works by / starts with / requires / begins with」 → 塞词
      H2/H3 标题里出现完整关键词 → 塞词

    正确形式只有一种：关键词自己当独立名词短语（主语/宾语）。
    """
    hits: list[str] = []
    # H1 是唯一允许完整关键词的地方（用户定的：H1 / 首段 / 一个 H2），先剥掉再查，
    # 否则「The Complete Guide to <关键词>」会被不定式规则误报。
    body = re.sub(r"^#\s.*$", "", article or "", flags=re.M)
    for kw in keywords or []:
        kw = (kw or "").strip()
        if len(kw) < 6:
            continue
        k = re.escape(kw)
        pats = [
            # 前置包裹：任意 -ing 动词（含 figuring out / implementing）+ 最多两个垫词 + 关键词
            rf"(?i)\b\w+ing\s+(?:out\s+)?(?:\w+\s+){{0,2}}{k}\b",
            # 不定式：To + 动词 + 关键词（关键词本身以动词开头时 To 直接接）
            rf"(?i)\bTo\s+(?:\w+\s+)?{k}\b",
            # 形容词垫词：True / Effective / Proper / Real / Good / Smart + 关键词
            rf"(?i)\b(?:true|effective|proper|real|good|smart|solid|practical)\s+{k}\b",
            # 条件从句：When/If/While + we/you + 动词 + 关键词
            rf"(?i)\b(?:when|if|while|as)\s+(?:we|you)\s+\w+\s+(?:\w+\s+)?{k}\b",
            # 后置公式：关键词 + works by / starts with / requires / begins with
            rf"(?i)\b{k}\s+(?:works by|starts with|begins with|requires|means|involves)\b",
            # 标题里出现完整关键词
            rf"(?im)^#{{2,3}}\s.*{k}",
        ]
        for pat in pats:
            for mm in re.finditer(pat, body):
                frag = mm.group(0).strip()
                if frag not in hits:
                    hits.append(frag)
    return hits


def unlisted_numbers(article: str, facts: str) -> list[str]:
    """正文里出现、但事实清单里没有的数字 —— 交给用户在审批时处理。

    只查"带信息量"的数字：金额、百分比、四位以上的量。序数、年份、小整数（一到十）
    是行文自然会用的，查了全是噪音。

    这是**提示不是拦截** —— 清单不可能穷尽所有合理数字（"三个步骤"这类），
    所以返回列表给人看，不自动改稿。
    """
    if not facts:
        return []
    pat = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b\d{4,}\b")
    norm = lambda s: re.sub(r"[\s$%,]", "", s)
    listed = {norm(x) for x in pat.findall(facts)}
    seen, out = set(), []
    for m in pat.findall(article or ""):
        n = norm(m)
        if n and n not in listed and n not in seen:
            seen.add(n)
            out.append(m.strip())
    return out


def source_concentration(article: str, search_corpus: str) -> dict:
    """成品的用词有多集中在**单一**参考来源上 —— 查"是不是把一篇文章改写了一遍"。

    2026-09-02 由用户的一次警觉催生：speed 篇同时出现 RobotAlp、1800€、Yottaa，
    看起来像抄了某家欧洲 agency 的博客。实查是虚惊（三个标记分属三个来源，
    最大单源占比 16%，其余七源合计 34%），但这个查法值得常设 ——
    真发生一次就是抄袭风险，不是质量问题。

    参考资料按 "---" 分块（providers.search 的归一化格式）。
    返回 top1 单源占比;超过 0.5 该人工看一眼。
    """
    words = set(re.findall(r"[a-z]{5,}", (article or "").lower()))
    if not words or not search_corpus:
        return {"ratio": 0.0, "top_url": "", "blocks": 0}
    blocks = re.split(r"\n-{3,}\n", search_corpus)
    best, best_hits = "", 0
    for b in blocks:
        hits = len(words & set(re.findall(r"[a-z]{5,}", b.lower())))
        if hits > best_hits:
            best_hits, best = hits, b
    url = re.search(r"URL:\s*(\S+)", best)
    return {"ratio": round(best_hits / len(words), 3),
            "top_url": url.group(1) if url else "",
            "blocks": len(blocks)}


def to_prompt_block(result: dict, max_items: int = 12) -> str:
    """把体检结果渲染成 prompt 片段。没有违规就返回空串（那一段直接消失）。"""
    fs = result.get("findings") or []
    if not fs:
        return ""
    lines = []
    for f in fs[:max_items]:
        line = f"- [{f['severity']}] {f['detail']}"
        if f.get("samples"):
            shown = " / ".join(f'"{s}"' for s in f["samples"][:2])
            line += f"\n    例：{shown}"
        lines.append(line)
    return "\n".join(lines)


def format_report(result: dict) -> str:
    """给用户看的一行摘要。"""
    c = result["counts"]
    if not any(c.values()):
        return "文风体检：无问题"
    return (f"文风体检：{c['P0']} 项必修 / {c['P1']} 项建议 / {c['P2']} 项润色"
            f"（{result['sentences']} 句 · {result['paragraphs']} 段）")
