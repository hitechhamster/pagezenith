"use strict";
// 极简 Markdown → HTML。站里不引任何前端库（Docker 镜像也没打包 CDN 资源），
// 大纲/文章的渲染需求就是标题、列表、表格、加粗、链接、[IMAGE:] 占位符这几样，够用即可。
// 所有文本先转义再拼接，模型输出里的 <script> 不会被执行。
(function () {
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // 行内：先转义，再处理 **粗体** 和 [文字](链接)
  function inline(s) {
    let t = esc(s);
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      (m, txt, url) => `<a href="${url}" target="_blank" rel="noopener">${txt}</a>`);
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    return t;
  }

  const fenceStart = s => s.match(/^ {0,3}(`{3,}|~{3,})([^\r\n]*)$/);
  const fenceEnd = (s, f) => new RegExp("^ {0,3}" + f[1][0] + "{" + f[1].length + ",}\\s*$").test(s);

  // Only settle blank-line boundaries outside fenced code. Keep internal whitespace intact.
  function splitBlocks(md) {
    const blocks = [], lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
    let current = [], fence = null;
    for (const line of lines) {
      if (fence) {
        current.push(line);
        if (fenceEnd(line, fence)) fence = null;
      } else if (fenceStart(line)) {
        fence = fenceStart(line); current.push(line);
      } else if (!line.trim()) {
        if (current.length) { blocks.push(current.join("\n")); current = []; }
      } else current.push(line);
    }
    blocks.push(current.join("\n"));
    return blocks;
  }

  function render(md) {
    const lines = String(md || "").split("\n");
    const out = [];
    let i = 0, inList = false;
    const closeList = () => { if (inList) { out.push(`</${inList}>`); inList = false; } };

    while (i < lines.length) {
      const line = lines[i].trim();

      const fence = fenceStart(lines[i]);
      if (fence) {
        closeList();
        const code = []; i++;
        while (i < lines.length && !fenceEnd(lines[i], fence)) code.push(lines[i++]);
        if (i < lines.length) i++;
        out.push(`<pre class="md-code"><code>${esc(code.join("\n"))}</code></pre>`);
        continue;
      }

      // Legacy, unfenced ASCII boxes: display faithfully, never interpret them as tables.
      if (/^\+(?:[-=]{3,}\+)+$/.test(line)) {
        closeList();
        const diagram = [lines[i++]];
        while (i < lines.length && (/^\s*[|+]/.test(lines[i]) || !lines[i].trim())) diagram.push(lines[i++]);
        out.push(`<pre class="md-code"><code>${esc(diagram.join("\n").trimEnd())}</code></pre>`);
        continue;
      }

      // 表格：靠「分隔行」认，不要求首尾竖线。
      // 2026-09-03 实测：模型有时输出松散写法（`列一 | 列二` + `--- | --- | ---`，行首尾没有竖线），
      // 旧逻辑要求 startsWith("|") && endsWith("|")，整块表格直接降级成一堆纯文本段落。
      const isSep = s => /\|/.test(s) && /^[|\s:-]+$/.test(s) && /-{2,}/.test(s);
      if (line && /\|/.test(line) && i + 1 < lines.length && isSep(lines[i + 1].trim())) {
        closeList();
        const cells = r => {
          let t = r.trim();
          if (t.startsWith("|")) t = t.slice(1);
          if (t.endsWith("|")) t = t.slice(0, -1);
          return t.split("|").map(c => c.trim());
        };
        const head = cells(line);
        i += 2;                                   // 跳过表头和分隔行
        const body = [];
        while (i < lines.length) {
          const r = lines[i].trim();
          if (!r || !/\|/.test(r)) break;         // 空行或没有竖线 = 表格结束
          body.push(cells(r)); i++;
        }
        out.push("<table><thead><tr>" + head.map(c => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>");
        body.forEach(r => out.push("<tr>" + r.map(c => `<td>${inline(c)}</td>`).join("") + "</tr>"));
        out.push("</tbody></table>");
        continue;
      }

      if (!line) { closeList(); i++; continue; }

      // 图片占位符：单独标出来，让用户一眼看到配图会插在哪
      const img = line.match(/^\[IMAGE:\s*([\s\S]+?)\s*\]$/);
      if (img) { closeList(); out.push(`<div class="md-img" data-ph="${esc(line)}"><span class="md-img-tag">配图</span>${esc(img[1])}</div>`); i++; continue; }

      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { closeList(); const lv = h[1].length; out.push(`<h${lv}>${inline(h[2])}</h${lv}>`); i++; continue; }

      if (/^[-*]\s+/.test(line)) {
        if (inList !== "ul") { closeList(); out.push("<ul>"); inList = "ul"; }
        out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
        i++; continue;
      }

      const ol = line.match(/^\d+\.\s+(.*)$/);
      if (ol) {
        if (inList !== "ol") { closeList(); out.push("<ol>"); inList = "ol"; }
        out.push(`<li>${inline(ol[1])}</li>`);
        i++; continue;
      }

      closeList();
      out.push(`<p>${inline(line)}</p>`);
      i++;
    }
    closeList();
    return out.join("\n");
  }

  window.MD = { render, splitBlocks, escape: esc };
})();
