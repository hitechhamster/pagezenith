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

  function render(md) {
    const lines = String(md || "").split("\n");
    const out = [];
    let i = 0, inList = false;
    const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };

    while (i < lines.length) {
      const line = lines[i].trim();

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

      const h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { closeList(); const lv = h[1].length; out.push(`<h${lv}>${inline(h[2])}</h${lv}>`); i++; continue; }

      if (/^[-*]\s+/.test(line)) {
        if (!inList) { out.push("<ul>"); inList = true; }
        out.push(`<li>${inline(line.replace(/^[-*]\s+/, ""))}</li>`);
        i++; continue;
      }

      const ol = line.match(/^\d+\.\s+(.*)$/);
      if (ol) {
        if (!inList) { out.push("<ul>"); inList = true; }
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

  window.MD = { render, escape: esc };
})();
