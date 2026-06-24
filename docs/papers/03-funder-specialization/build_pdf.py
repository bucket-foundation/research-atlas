#!/usr/bin/env python3
"""Render paper.md -> paper.pdf (preprint style) with weasyprint.

Pure-stdlib markdown-to-HTML (headings, paragraphs, lists, tables, images,
inline code/emphasis, fenced code blocks) + a print stylesheet, then weasyprint
to PDF. Resolves the relative figure paths against the repo so images embed.

Usage:  python docs/papers/03-funder-specialization/build_pdf.py
"""

from __future__ import annotations

import html
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
MD = HERE / "paper.md"
PDF = HERE / "paper.pdf"

CSS = """
@page { size: A4; margin: 20mm 18mm; @bottom-center {
    content: counter(page) " / " counter(pages); font-size: 8pt; color: #888; } }
body { font-family: 'Georgia','Times New Roman',serif; font-size: 10.5pt;
       line-height: 1.5; color: #1b2a4a; }
h1 { font-size: 19pt; line-height: 1.25; color: #11203f; margin: 0 0 4pt; }
h2 { font-size: 13.5pt; color: #11203f; margin: 18pt 0 4pt;
     border-bottom: 1.5px solid #c0392b; padding-bottom: 2pt; }
h3 { font-size: 11.5pt; color: #1b2a4a; margin: 12pt 0 3pt; }
p { margin: 4pt 0; text-align: justify; }
strong { color: #11203f; }
code { font-family: 'DejaVu Sans Mono',monospace; font-size: 9pt;
       background: #f3f4f7; padding: 0.5pt 2pt; border-radius: 2px; }
pre { background: #f3f4f7; border: 1px solid #e0e3ea; border-radius: 4px;
      padding: 8pt; font-size: 8.5pt; font-family: 'DejaVu Sans Mono',monospace;
      white-space: pre-wrap; line-height: 1.35; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 6pt 0; }
th, td { border: 1px solid #d4d8e2; padding: 3pt 6pt; text-align: left;
         vertical-align: top; }
th { background: #11203f; color: #fff; }
tr:nth-child(even) td { background: #f6f7fa; }
img { max-width: 88%; display: block; margin: 8pt auto; }
em { color: #555; }
hr { border: none; border-top: 1px solid #d4d8e2; margin: 12pt 0; }
.meta { color: #555; font-size: 9.5pt; }
"""


def md_inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"\$([^$]+)\$", r"<em>\1</em>", text)  # render math as emphasis
    return text


def md_to_html(md: str) -> str:
    out, lines, i = [], md.split("\n"), 0
    in_table = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            i += 1
            continue
        m = re.match(r"!\[.*?\]\((.*?)\)", line.strip())
        if m:
            src = m.group(1)
            p = (HERE / src).resolve()
            out.append(f'<img src="file://{p}" />')
            i += 1
            continue
        if line.strip().startswith("|") and "|" in line:
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            cells = [r for r in cells if not all(re.fullmatch(r"-{2,}|:?-+:?", c or "-") for c in r)]
            if cells:
                out.append("<table>")
                out.append("<tr>" + "".join(f"<th>{md_inline(c)}</th>" for c in cells[0]) + "</tr>")
                for r in cells[1:]:
                    out.append("<tr>" + "".join(f"<td>{md_inline(c)}</td>" for c in r) + "</tr>")
                out.append("</table>")
            continue
        if re.match(r"^#{1,6} ", line):
            lvl = len(line) - len(line.lstrip("#"))
            out.append(f"<h{lvl}>{md_inline(line[lvl:].strip())}</h{lvl}>")
        elif line.strip() == "---":
            out.append("<hr/>")
        elif re.match(r"^\s*[-*] ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*] ", lines[i]):
                items.append(f"<li>{md_inline(re.sub(r'^\\s*[-*] ', '', lines[i]))}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif re.match(r"^\s*\d+\. ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+\. ", lines[i]):
                items.append(f"<li>{md_inline(re.sub(r'^\\s*\\d+\\. ', '', lines[i]))}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue
        elif line.strip():
            # gather consecutive prose lines into a single paragraph (standard
            # markdown) so inline emphasis/bold can span wrapped source lines.
            para = [line.strip()]
            j = i + 1
            while (j < len(lines) and lines[j].strip()
                   and not re.match(r"^#{1,6} ", lines[j])
                   and not lines[j].strip().startswith("|")
                   and not lines[j].startswith("```")
                   and not re.match(r"^!\[.*?\]\(", lines[j].strip())
                   and not re.match(r"^\s*[-*] ", lines[j])
                   and not re.match(r"^\s*\d+\. ", lines[j])
                   and lines[j].strip() != "---"):
                para.append(lines[j].strip())
                j += 1
            text = " ".join(para)
            cls = ' class="meta"' if text.startswith("**Author") or text.startswith("**Version") or text.startswith("**Corpus") or text.startswith("**Reproducibility") else ""
            out.append(f"<p{cls}>{md_inline(text)}</p>")
            i = j
            continue
        i += 1
    return "\n".join(out)


def main() -> int:
    from weasyprint import HTML, CSS as WCSS

    body = md_to_html(MD.read_text())
    doc = f"<html><head><meta charset='utf-8'></head><body>{body}</body></html>"
    HTML(string=doc, base_url=str(HERE)).write_pdf(
        str(PDF), stylesheets=[WCSS(string=CSS)])
    print(f"wrote {PDF}  ({PDF.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
