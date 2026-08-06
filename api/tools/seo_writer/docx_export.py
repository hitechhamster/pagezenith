"""Markdown 文章 → Word（.docx）字节流。

移植自线下工作流：标题层级、无序列表、表格、加粗、Markdown 超链接（真·可点蓝色下划线），
以及把正文里的 `[IMAGE: ...]` 占位符替换成生成好的图片。

不落盘：直接返回 bytes，由 router 以 base64 随 SSE done 事件发给浏览器下载。
"""

from __future__ import annotations

import io
import re
from typing import Optional

import docx
import docx.enum.text
import docx.opc.constants
import docx.shared
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

IMAGE_PLACEHOLDER = re.compile(r"^\[IMAGE:\s*[^\]]+?\s*\]$")


def add_hyperlink(paragraph, text: str, url: str) -> None:
    """python-docx 没有原生超链接 API，只能手搓 w:hyperlink 元素。"""
    part = paragraph.part
    r_id = part.relate_to(url, docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    rPr.append(color)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(u)
    new_run.append(rPr)
    text_elem = OxmlElement("w:t")
    text_elem.text = text
    new_run.append(text_elem)
    hyperlink.append(new_run)
    paragraph._element.append(hyperlink)


def _add_formatted_runs(paragraph, text: str) -> None:
    """把一行 Markdown 拆成 [链接 / 加粗 / 普通] 三种 run。"""
    for part in re.split(r"(\[.*?\]\(.*?\)|\*\*.*?\*\*)", text):
        link = re.match(r"\[(.*?)\]\((.*?)\)", part)
        if link:
            add_hyperlink(paragraph, link.group(1), link.group(2))
        elif part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part:
            paragraph.add_run(part)


def build_docx(markdown_text: str, image_map: Optional[dict[str, bytes]] = None) -> bytes:
    """返回 .docx 的字节内容。image_map: {"[IMAGE: xxx]": png_bytes}"""
    doc = docx.Document()
    image_map = image_map or {}

    lines = (markdown_text or "").strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 图片占位符 → 真图
        if IMAGE_PLACEHOLDER.match(line):
            png = image_map.get(line)
            if png:
                try:
                    doc.add_picture(io.BytesIO(png), width=docx.shared.Inches(6.0))
                    doc.paragraphs[-1].alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER
                except Exception:
                    pass  # 单张图插不进去不该让整篇导出失败
            i += 1
            continue

        # Markdown 表格
        if line.startswith("|") and line.endswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            data_rows = [l for l in table_lines if not re.match(r"^[|\s:-]+$", l)]
            if not data_rows:
                continue
            header_cols = [c.strip() for c in data_rows[0].split("|")[1:-1]]
            if not header_cols:
                continue
            table = doc.add_table(rows=1, cols=len(header_cols))
            table.style = "Table Grid"
            for j, h in enumerate(header_cols):
                p = table.rows[0].cells[j].paragraphs[0]
                _add_formatted_runs(p, h)
                p.alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER
            for row_text in data_rows[1:]:
                cells = [c.strip() for c in row_text.split("|")[1:-1]]
                row = table.add_row().cells
                for j, ct in enumerate(cells):
                    if j < len(row):
                        _add_formatted_runs(row[j].paragraphs[0], ct)
            continue

        if not line:
            i += 1
            continue

        if line.startswith("# "):
            _add_formatted_runs(doc.add_heading(level=1), line[2:].strip())
        elif line.startswith("## "):
            _add_formatted_runs(doc.add_heading(level=2), line[3:].strip())
        elif line.startswith("### "):
            _add_formatted_runs(doc.add_heading(level=3), line[4:].strip())
        elif line.startswith("#### "):
            _add_formatted_runs(doc.add_heading(level=4), line[5:].strip())
        elif line.startswith("* ") or line.startswith("- "):
            _add_formatted_runs(doc.add_paragraph(style="List Bullet"), line[2:].strip())
        else:
            _add_formatted_runs(doc.add_paragraph(), line)
        i += 1

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name or "").strip()[:100] or "article"
