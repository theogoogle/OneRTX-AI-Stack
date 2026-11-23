"""
Clean Markdown -> styled Word (.docx) & editorial A4 PDF exporter for Open WebUI.
Strips model reasoning blocks (<think>...</think> and <details type="reasoning">...</details>) automatically.

Usage:
  python -I -X utf8 exporter.py --format docx|pdf --title "..." --input-file input.md --output out.docx
"""
from __future__ import annotations

import argparse
import html as html_lib
from html.parser import HTMLParser
import json
import os
import re
import subprocess
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_INLINE_TOKEN_RE = re.compile(r"(\*\*\*[^*]+\*\*\*|\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def strip_reasoning(md: str) -> str:
    text = str(md or "")
    text = re.sub(r"(?is)<think>.*?</think>", "", text)
    text = re.sub(r'(?is)<details\b[^>]*type=["\']reasoning["\'][^>]*>.*?</details>', "", text)
    text = re.sub(r"(?im)^\[TOOL_RESULT[^\]]*\]\s*$", "", text)
    return text.strip()


def _find_chrome() -> str | None:
    cands = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for p in cands:
        if p.is_file():
            return str(p)
    return None


# ------------------------------------------------------------------ DOCX builder
def _add_inline_runs(paragraph, text: str, base_color=None) -> None:
    from docx.shared import Pt, RGBColor

    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            r = paragraph.add_run(text[pos : m.start()])
            if base_color:
                r.font.color.rgb = base_color
        tok = m.group(0)
        if tok.startswith("***") and tok.endswith("***"):
            r = paragraph.add_run(tok[3:-3])
            r.bold = True
            r.italic = True
        elif tok.startswith("**") and tok.endswith("**"):
            r = paragraph.add_run(tok[2:-2])
            r.bold = True
        elif tok.startswith("*") and tok.endswith("*"):
            r = paragraph.add_run(tok[1:-1])
            r.italic = True
        elif tok.startswith("`") and tok.endswith("`"):
            r = paragraph.add_run(tok[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
            r.font.color.rgb = RGBColor(0x8B, 0x1E, 0x3F)
        elif tok.startswith("[") and "](" in tok:
            lbl, _url = tok[1:-1].split("](", 1)
            r = paragraph.add_run(lbl)
            r.underline = True
            r.font.color.rgb = RGBColor(0x1B, 0x4F, 0x72)
        if base_color and not (tok.startswith("`") or tok.startswith("[")):
            r.font.color.rgb = base_color
        pos = m.end()
    if pos < len(text):
        r = paragraph.add_run(text[pos:])
        if base_color:
            r.font.color.rgb = base_color


def _set_cell_shading(cell, hex_color: str) -> None:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{hex_color}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _add_bottom_border(paragraph, hex_color: str = "1B365D", size: str = "12") -> None:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    pBdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}><w:bottom w:val="single" w:sz="{size}" w:space="4" w:color="{hex_color}"/></w:pBdr>'
    )
    paragraph._p.get_or_add_pPr().append(pBdr)


def _add_code_block(doc, lines: list[str]) -> None:
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls
    from docx.shared import Inches, Pt, RGBColor

    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.18)
    p.paragraph_format.right_indent = Inches(0.18)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="F4F6F8"/>')
    bdr = parse_xml(
        f'<w:pBdr {nsdecls("w")}><w:left w:val="single" w:sz="18" w:space="8" w:color="CBD5E1"/></w:pBdr>'
    )
    p._p.get_or_add_pPr().append(shd)
    p._p.get_or_add_pPr().append(bdr)
    r = p.add_run("\n".join(lines))
    r.font.name = "Consolas"
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor(0x1E, 0x29, 0x3B)


def _add_table_block(doc, raw_rows: list[str]) -> None:
    from docx.shared import Pt, RGBColor

    parsed: list[list[str]] = []
    for idx, line in enumerate(raw_rows):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if idx == 1 and all(re.match(r"^:?-{2,}:?$", c or "") for c in cells):
            continue
        parsed.append(cells)
    if not parsed:
        return
    cols = max(len(r) for r in parsed)
    table = doc.add_table(rows=len(parsed), cols=cols)
    table.style = "Table Grid"
    for r_idx, row in enumerate(parsed):
        for c_idx in range(cols):
            cell = table.cell(r_idx, c_idx)
            val = row[c_idx] if c_idx < len(row) else ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            if r_idx == 0:
                _set_cell_shading(cell, "1B365D")
                _add_inline_runs(p, val, base_color=RGBColor(0xFF, 0xFF, 0xFF))
                for run in p.runs:
                    run.bold = True
                    run.font.size = Pt(9.5)
            else:
                if r_idx % 2 == 0:
                    _set_cell_shading(cell, "F8FAFC")
                _add_inline_runs(p, val, base_color=RGBColor(0x1F, 0x29, 0x37))
                for run in p.runs:
                    run.font.size = Pt(9.5)
    sp = doc.add_paragraph()
    sp.paragraph_format.space_after = Pt(2)


def export_docx(title: str, md_text: str, out_path: Path) -> dict[str, Any]:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    clean = strip_reasoning(md_text)
    doc = Document()
    for sec in doc.sections:
        sec.top_margin = Inches(0.8)
        sec.bottom_margin = Inches(0.8)
        sec.left_margin = Inches(0.85)
        sec.right_margin = Inches(0.85)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)

    # Header title + subtitle
    p_title = doc.add_paragraph()
    p_title.paragraph_format.space_after = Pt(2)
    r_t = p_title.add_run(title.strip() or "Document Export")
    r_t.bold = True
    r_t.font.name = "Calibri"
    r_t.font.size = Pt(20)
    r_t.font.color.rgb = RGBColor(0x1B, 0x36, 0x5D)

    p_sub = doc.add_paragraph()
    p_sub.paragraph_format.space_after = Pt(10)
    r_sub = p_sub.add_run(f"Generated {datetime.now():%A %d %B %Y · %H:%M}")
    r_sub.font.size = Pt(9)
    r_sub.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
    _add_bottom_border(p_sub, "1B365D", "12")

    lines = clean.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            _add_code_block(doc, code_lines)
            i += 1
            continue

        # Markdown table
        if "|" in stripped and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
            t_rows = []
            while i < len(lines) and "|" in lines[i].strip():
                t_rows.append(lines[i])
                i += 1
            _add_table_block(doc, t_rows)
            continue

        # Horizontal rule
        if re.match(r"^[-*_]{3,}$", stripped):
            p_hr = doc.add_paragraph()
            p_hr.paragraph_format.space_after = Pt(6)
            _add_bottom_border(p_hr, "CBD5E1", "6")
            i += 1
            continue

        # Headings
        m_h = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if m_h:
            lvl = len(m_h.group(1))
            txt = m_h.group(2).strip()
            p_h = doc.add_paragraph()
            p_h.paragraph_format.space_before = Pt(10 if lvl <= 2 else 7)
            p_h.paragraph_format.space_after = Pt(3)
            _add_inline_runs(p_h, txt, base_color=RGBColor(0x1B, 0x36, 0x5D))
            sz = {1: 15, 2: 13, 3: 11.5, 4: 10.5}[lvl]
            for r in p_h.runs:
                r.bold = True
                r.font.size = Pt(sz)
            i += 1
            continue

        # Blockquote
        if stripped.startswith(">"):
            quote_text = re.sub(r"^>\s*", "", stripped)
            p_q = doc.add_paragraph()
            p_q.paragraph_format.left_indent = Inches(0.25)
            p_q.paragraph_format.space_after = Pt(4)
            _add_inline_runs(p_q, quote_text, base_color=RGBColor(0x47, 0x55, 0x69))
            for r in p_q.runs:
                r.italic = True
            i += 1
            continue

        # Bullets / numbered lists
        m_b = re.match(r"^[-*•]\s+(.+)$", stripped)
        if m_b:
            p_b = doc.add_paragraph(style="List Bullet")
            p_b.paragraph_format.space_after = Pt(2)
            _add_inline_runs(p_b, m_b.group(1).strip())
            i += 1
            continue
        m_n = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if m_n:
            p_n = doc.add_paragraph(style="List Number")
            p_n.paragraph_format.space_after = Pt(2)
            _add_inline_runs(p_n, m_n.group(1).strip())
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(5)
        _add_inline_runs(p, stripped)
        i += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return {"ok": True, "format": "docx", "path": str(out_path), "filename": out_path.name, "bytes": out_path.stat().st_size}


# ------------------------------------------------------------------ PDF builder
_PDF_CSS = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm; }
body { font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif; color: #1f2937; font-size: 10.5pt; line-height: 1.52; }
header.doc-head { border-bottom: 2px solid #1b365d; padding-bottom: 8px; margin-bottom: 14px; }
header.doc-head h1 { margin: 0 0 3px 0; font-size: 19pt; color: #1b365d; line-height: 1.2; }
header.doc-head .meta { font-size: 8.5pt; color: #64748b; }
h1, h2, h3, h4 { color: #1b365d; margin: 14px 0 6px; page-break-after: avoid; }
h1 { font-size: 14.5pt; } h2 { font-size: 12.5pt; border-bottom: 1px solid #e2e8f0; padding-bottom: 3px; } h3 { font-size: 11pt; }
p { margin: 0 0 8px 0; }
ul, ol { margin: 4px 0 10px 0; padding-left: 22px; }
li { margin-bottom: 3px; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 14px; font-size: 9.5pt; page-break-inside: avoid; }
th { background: #1b365d; color: #ffffff; text-align: left; padding: 6px 8px; font-weight: 600; }
td { border-bottom: 1px solid #e2e8f0; padding: 6px 8px; vertical-align: top; }
tr:nth-child(even) td { background: #f8fafc; }
pre { background: #f4f6f8; border-left: 3px solid #cbd5e1; padding: 8px 10px; font-family: Consolas, monospace; font-size: 8.5pt; white-space: pre-wrap; word-break: break-word; border-radius: 3px; }
code { background: #f1f5f9; color: #8b1e3f; padding: 1px 4px; border-radius: 3px; font-family: Consolas, monospace; font-size: 9pt; }
pre code { background: transparent; color: #1e293b; padding: 0; }
blockquote { border-left: 3px solid #1b365d; margin: 8px 0; padding: 4px 12px; color: #475569; background: #f8fafc; }
hr { border: none; border-top: 1px solid #cbd5e1; margin: 14px 0; }
a { color: #1b4f72; }
"""

_SAFE_HTML_TAGS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr", "ul", "ol", "li",
    "strong", "b", "em", "i", "u", "s", "del",
    "code", "pre", "blockquote",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
    "a", "span", "sub", "sup", "div",
})
_VOID_HTML_TAGS = frozenset({"br", "hr"})
_DROP_CONTENT_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed",
    "svg", "math", "form", "textarea", "noscript",
})


class _SafeHTMLSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._drop_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _DROP_CONTENT_TAGS:
            self._drop_stack.append(t)
            return
        if self._drop_stack or t not in _SAFE_HTML_TAGS:
            return
        clean_attrs: list[str] = []
        for k, v in attrs:
            kl = (k or "").lower()
            val = "" if v is None else str(v)
            if t == "a" and kl == "href":
                try:
                    scheme = urllib.parse.urlsplit(val.strip()).scheme.lower()
                except ValueError:
                    scheme = ""
                if scheme in ("http", "https"):
                    clean_attrs.append(f'href="{html_lib.escape(val.strip(), quote=True)}"')
            elif t in ("th", "td") and kl in ("colspan", "rowspan") and val.isdigit():
                clean_attrs.append(f'{kl}="{val}"')
            elif t in ("th", "td") and kl == "align" and val.lower() in ("left", "center", "right"):
                clean_attrs.append(f'align="{val.lower()}"')
        attr_str = (" " + " ".join(clean_attrs)) if clean_attrs else ""
        self._out.append(f"<{t}{attr_str}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _VOID_HTML_TAGS and not self._drop_stack:
            self._out.append(f"<{t}>")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if self._drop_stack:
            if t == self._drop_stack[-1]:
                self._drop_stack.pop()
            elif t in self._drop_stack:
                idx = len(self._drop_stack) - 1 - self._drop_stack[::-1].index(t)
                del self._drop_stack[idx:]
            return
        if t in _SAFE_HTML_TAGS and t not in _VOID_HTML_TAGS:
            self._out.append(f"</{t}>")

    def handle_data(self, data: str) -> None:
        if not self._drop_stack:
            self._out.append(html_lib.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if not self._drop_stack:
            self._out.append(html_lib.escape(html_lib.unescape(f"&{name};"), quote=False))

    def handle_charref(self, name: str) -> None:
        if not self._drop_stack:
            self._out.append(html_lib.escape(html_lib.unescape(f"&#{name};"), quote=False))

    def handle_comment(self, data: str) -> None:
        return


def _sanitize_html(raw_html: str) -> str:
    """Strip unsafe tags (script/iframe/img/style), event handlers, and non-HTTP(S) links."""
    parser = _SafeHTMLSanitizer()
    parser.feed(str(raw_html or ""))
    parser.close()
    return "".join(parser._out)


def export_pdf(title: str, md_text: str, out_path: Path) -> dict[str, Any]:
    import markdown as md_lib

    clean = strip_reasoning(md_text)
    body_html = _sanitize_html(md_lib.markdown(clean, extensions=["tables", "fenced_code", "sane_lists"]))
    full_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline';\">"
        f"<title>{html_lib.escape(title)}</title><style>{_PDF_CSS}</style></head><body>"
        f"<header class='doc-head'><h1>{html_lib.escape(title.strip() or 'Document Export')}</h1>"
        f"<div class='meta'>Generated {datetime.now():%A %d %B %Y · %H:%M}</div></header>"
        f"{body_html}</body></html>"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = out_path.with_suffix(".html")
    html_path.write_text(full_html, encoding="utf-8")

    try:
        chrome = _find_chrome()
        if chrome:
            subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-pdf-header-footer",
                    f"--print-to-pdf={out_path}",
                    html_path.resolve().as_uri(),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=45,
                creationflags=NO_WINDOW,
            )
        else:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch()
                try:
                    page = browser.new_page(java_script_enabled=False)
                    page.goto(html_path.resolve().as_uri(), wait_until="load")
                    page.pdf(path=str(out_path), format="A4", print_background=True)
                finally:
                    browser.close()
    finally:
        try:
            html_path.unlink(missing_ok=True)
        except OSError:
            pass

    if not out_path.is_file() or out_path.stat().st_size < 1000:
        raise RuntimeError("PDF rendering failed to produce a valid output file.")
    return {"ok": True, "format": "pdf", "path": str(out_path), "filename": out_path.name, "bytes": out_path.stat().st_size}


def main() -> int:
    ap = argparse.ArgumentParser(description="Markdown to DOCX/PDF exporter")
    ap.add_argument("--format", choices=("docx", "pdf"), required=True)
    ap.add_argument("--title", default="Document Export")
    ap.add_argument("--input-file", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    md_text = Path(args.input_file).read_text(encoding="utf-8", errors="replace")
    out_p = Path(args.output)
    res = export_docx(args.title, md_text, out_p) if args.format == "docx" else export_pdf(args.title, md_text, out_p)
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
