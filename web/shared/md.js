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

      // 表格：|a|b| 后面跟 |---|---|
      if (line.startsWith("|") && line.endsWith("|")) {
        const block = [];
        while (i < lines.length && lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) {
          block.push(lines[i].trim()); i++;
        }
        const rows = block.filter(l => !/^[|\s:-]+$/.test(l));
        if (rows.length) {
          closeList();
          const cells = r => r.split("|").slice(1, -1).map(c => c.trim());
          out.push("<table><thead><tr>" + cells(rows[0]).map(c => `<th>${inline(c)}</th>`).join("") + "</tr></thead><tbody>");
          rows.slice(1).forEach(r => out.push("<tr>" + cells(r).map(c => `<td>${inline(c)}</td>`).join("") + "</tr>"));
          out.push("</tbody></table>");
        }
        continue;
      }

      if (!line) { closeList(); i++; continue; }

      // 图片占位符：单独标出来，让用户一眼看到配图会插在哪
      const img = line.match(/^\[IMAGE:\s*([\s\S]+?)\s*\]$/);
      if (img) { closeList(); out.push(`<div class="md-img" data-ph="${esc(line)}">🖼 ${esc(img[1])}</div>`); i++; continue; }

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
