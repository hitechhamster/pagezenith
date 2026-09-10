/* Structured long-form research; raw model text is never treated as HTML. */
function renderDeepResearch(r) {
  const out = $("out");
  out.innerHTML = "";
  out.classList.add("deep-report");
  renderSteps(r.steps);
  const sources = new Map((r.threads || []).map((t, i) => [`T${String(i + 1).padStart(3, "0")}`, t]));
  const urlAllowed = url => {
    try {
      const u = new URL(url);
      return u.protocol === "https:" && (u.hostname === "reddit.com" || u.hostname.endsWith(".reddit.com"));
    } catch { return false; }
  };
  const card = (title, id) => {
    const node = E("section", "card report-section");
    if (id) node.id = id;
    node.append(E("h2", null, title));
    return node;
  };
  const paragraph = (parent, label, text, cls = "") => {
    if (!text) return;
    const p = E("p", "report-prose " + cls);
    if (label) p.append(E("strong", null, label + "："));
    p.append(document.createTextNode(text));
    parent.append(p);
  };
  const refs = (parent, ids) => {
    const box = E("div", "report-refs");
    for (const id of new Set(ids || [])) {
      const source = sources.get(id);
      if (!source || !urlAllowed(source.url)) continue;
      const a = E("a", null, id + " · r/" + source.subreddit);
      a.href = source.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      a.title = source.title; box.append(a);
    }
    if (box.children.length) parent.append(box);
  };
  const finding = (f, index) => {
    const node = E("article", "report-finding");
    node.append(E("h3", null, `${index + 1}. ${f.title}`));
    if (!f.source_ids?.length) node.append(E("span", "report-hypothesis", "待验证假设"));
    paragraph(node, "样本观察", f.observation);
    paragraph(node, "分析与推断", f.interpretation);
    paragraph(node, "行动建议", f.action);
    paragraph(node, "反例与边界", f.caveat, "report-caveat");
    refs(node, f.source_ids);
    for (const q of f.quote_evidence || []) {
      if (!urlAllowed(q.source_url)) continue;
      const a = E("a", "quote", "“" + q.text + "” · 查看原文");
      a.href = q.source_url; a.target = "_blank"; a.rel = "noopener noreferrer";
      node.append(a);
    }
    return node;
  };
  const top = card("市场判断与适用边界", "report-summary");
  paragraph(top, "研究对象", r.market || r.question);
  paragraph(top, "", r.overview);
  paragraph(top, "样本人群", r.audience);
  const metrics = E("div", "metrics");
  metrics.append(metric(r.query_count || 0, "搜索词"), metric(r.rounds || 1, "检索轮次"),
    metric(r.thread_count || 0, "来源帖子"), metric(r.comment_count || 0, "抓取评论"));
  top.append(metrics);
  paragraph(top, "", "抓取评论数不等于逐条分析数；原始材料按帖子均衡截取供分析。", "note");
  paragraph(top, "", `正文已逐字核验 ${r.quotes_verified || 0} 条引文；删除 ${r.quotes_dropped || 0} 条无法匹配的引文。市场表格的依据另附于各单元格。`, "note");
  if (r.depth_note) paragraph(top, "深度说明", r.depth_note, "report-caveat");
  out.append(top);

  const contents = E("nav", "report-contents");
  contents.setAttribute("aria-label", "报告目录");
  const entries = [...(r.market_tables || []).map((t, i) => [`report-matrix-${i}`, t.title]),
    ["report-decision", "进入判断"], ...(r.sections || []).map((s, i) => [`report-chapter-${i}`, s.title]),
    ["report-cross-checks", "交叉审查"], ["report-actions", "行动计划"], ["report-sources", "来源"]];
  for (const [id, title] of entries) { const a = E("a", null, title); a.href = "#" + id; contents.append(a); }
  out.append(contents);

  (r.market_tables || []).forEach((matrix, index) => {
    const section = card(matrix.title, `report-matrix-${index}`);
    paragraph(section, "", "逐格区分样本描述、分析推断与未提及；样本描述可展开核对原句，来源匹配不等于推断已获证实。", "note");
    if (matrix.rows?.length) {
      const scroll = E("div", "market-table-scroll");
      scroll.tabIndex = 0;
      scroll.setAttribute("role", "region");
      scroll.setAttribute("aria-label", matrix.title + "，可横向滚动");
      const table = E("table", "market-table");
      table.append(E("caption", "sr-only", matrix.title));
      const head = E("thead"), heading = E("tr"), body = E("tbody");
      const columns = Object.entries(matrix.columns || {});
      for (const [, label] of columns) {
        const th = E("th", null, label); th.scope = "col"; heading.append(th);
      }
      head.append(heading); table.append(head, body);
      for (const row of matrix.rows) {
        const tr = E("tr");
        columns.forEach(([key], i) => {
          const cell = row.cells?.[key] || {basis: "unknown", text: "样本未提及"};
          const td = E(i === 0 ? "th" : "td");
          if (i === 0) td.scope = "row";
          const label = cell.basis === "sample" ? "样本描述" : cell.basis === "inference" ? "分析推断" : "证据缺口";
          td.append(E("span", "market-basis " + (cell.basis === "sample" ? "sample" : cell.basis === "inference" ? "inference" : "unknown"), label),
            E("div", "market-cell-text", cell.text || "样本未提及"));
          refs(td, cell.source_ids);
          if (cell.quote_evidence?.length) {
            const evidence = E("details", "market-cell-evidence");
            evidence.append(E("summary", null, "核对原句"));
            for (const quote of cell.quote_evidence) {
              if (!urlAllowed(quote.source_url)) continue;
              const a = E("a", null, "“" + quote.text + "”");
              a.href = quote.source_url; a.target = "_blank"; a.rel = "noopener noreferrer";
              evidence.append(a);
            }
            td.append(evidence);
          }
          tr.append(td);
        });
        body.append(tr);
      }
      scroll.append(table); section.append(scroll);
    } else {
      paragraph(section, "", "当前样本不足以形成可靠的横向对照。", "report-caveat");
    }
    paragraph(section, "待补充证据", matrix.evidence_gap, "report-caveat");
    out.append(section);
  });

  if (r.concern_answers?.length) {
    const s = card("你另外关心的问题");
    for (const answer of r.concern_answers) {
      const node = E("article", "report-finding"); node.append(E("h3", null, answer.question));
      paragraph(node, "", answer.answer);
      paragraph(node, "证据缺口", answer.evidence_gap || (!answer.answer ? "当前样本不足以回答。" : ""), "report-caveat");
      s.append(node);
    }
    out.append(s);
  }
  const decision = card("机会优先级与进入判断", "report-decision");
  paragraph(decision, "", r.decision || "当前样本尚不足以形成进入判断。"); out.append(decision);

  const coverage = card("六个专题的引用覆盖");
  const counts = (r.sections || []).map(s => new Set(s.findings.flatMap(f => (f.source_ids || []).filter(id => sources.has(id)))).size);
  const max = Math.max(1, ...counts);
  (r.sections || []).forEach((s, i) => {
    const row = E("div", "chartrow"); const bar = E("div", "chartbar"); const fill = E("i");
    fill.style.width = `${counts[i] / max * 100}%`; bar.append(fill);
    row.append(E("span", null, s.title), bar, E("span", null, counts[i])); coverage.append(row);
  });
  paragraph(coverage, "", "数字是该专题引用的不同帖子数，同一帖可以跨专题引用；不代表市场份额、满意度或赞同比例。", "note");
  out.append(coverage);

  (r.sections || []).forEach((section, i) => {
    const node = card(`${String(i + 1).padStart(2, "0")} · ${section.title}`, `report-chapter-${i}`);
    paragraph(node, "", section.introduction);
    (section.findings || []).forEach((f, n) => node.append(finding(f, n)));
    if (section.evidence_gaps?.length) paragraph(node, "本专题待验证", section.evidence_gaps.join("\n"), "report-caveat");
    out.append(node);
  });
  const checks = card("交叉审查：矛盾、反例与结论修正", "report-cross-checks");
  (r.cross_checks || []).forEach((f, i) => checks.append(finding(f, i)));
  if (!r.cross_checks?.length) paragraph(checks, "", "本次未形成具体的跨专题审查结论，不能据此认为不存在矛盾。");
  out.append(checks);
  const plan = card("先验证，再试点：行动计划", "report-actions");
  paragraph(plan, "", "以下为待验证的经营建议。试验门槛应结合自己的成本、退货与获客数据确定，不能将 AI 提议的数字视为已核实的行业标准。", "note");
  (r.action_plan || []).forEach((action, i) => {
    const node = E("article", "report-finding");
    node.append(E("span", "report-priority", action.priority), E("h3", null, `${i + 1}. ${action.task}`));
    paragraph(node, "理由", action.rationale); paragraph(node, "如何验证", action.test);
    paragraph(node, "继续信号", action.success_signal); paragraph(node, "停止信号", action.stop_signal, "report-caveat");
    refs(node, action.source_ids); plan.append(node);
  });
  if (!r.action_plan?.length) paragraph(plan, "", "尚未形成可执行计划，需要补充证据。");
  out.append(plan);
  if (r.evidence_gaps?.length) {
    const gaps = card("仍未解决的问题");
    const list = E("ul", "gaps"); r.evidence_gaps.forEach(x => list.append(E("li", null, x))); gaps.append(list); out.append(gaps);
  }
  if (r.article_ideas?.length) {
    const ideas = card("可验证的内容与 SEO 切入点");
    for (const idea of r.article_ideas) {
      const node = E("article", "report-finding"); node.append(E("h3", null, idea.title));
      paragraph(node, "关键词", idea.target_keyword); paragraph(node, "切入方式", idea.angle);
      paragraph(node, "回应的需求", idea.addresses); ideas.append(node);
    }
    out.append(ideas);
  }
  const trace = E("details", "card report-searches"); trace.append(E("summary", null, "实际执行的搜索"));
  for (const q of r.searches || []) paragraph(trace, `第${q.round}轮 · ${q.thread_count}帖`, q.query + "\n" + q.purpose);
  out.append(trace);
  const allSources = card("来源帖子", "report-sources");
  for (const [id, source] of sources) {
    if (!urlAllowed(source.url)) continue;
    const a = E("a", "source"); a.href = source.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.append(E("small", null, `${id} · r/${source.subreddit}`), E("span", null, source.title)); allSources.append(a);
  }
  out.append(allSources);
}
