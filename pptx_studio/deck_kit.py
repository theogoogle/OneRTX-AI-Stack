"""Battle-tested 16:9 executive PowerPoint & matplotlib helper kit for Advanced Deck & pptx_studio.

Every shape, text frame, table, and image created by this kit is 100% native editable PowerPoint
(13.333 x 7.5 in widescreen) with automatic high-contrast text selection, exact aspect-ratio-preserving
image fitting, clean card padding, and zero python-pptx API footguns.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageOps
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

SLIDE_W = 13.333
SLIDE_H = 7.5
FONT_MAIN = "Calibri"

PALETTES: dict[str, dict[str, str]] = {
    "executive_navy": {
        "dark": "#0B132B",
        "header": "#0F172A",
        "dark_card": "#172540",
        "bg": "#F8FAFC",
        "card": "#FFFFFF",
        "border": "#E2E8F0",
        "accent": "#2563EB",
        "accent2": "#0EA5E9",
        "highlight": "#10B981",
        "text": "#0F172A",
        "muted": "#64748B",
        "text_light": "#F8FAFC",
        "muted_light": "#CBD5E1",
    },
    "obsidian_ai": {
        "dark": "#0D1117",
        "header": "#161B22",
        "dark_card": "#1F2937",
        "bg": "#F6F8FA",
        "card": "#FFFFFF",
        "border": "#DCE3EC",
        "accent": "#6366F1",
        "accent2": "#06B6D4",
        "highlight": "#10B981",
        "text": "#0F172A",
        "muted": "#64748B",
        "text_light": "#F8FAFC",
        "muted_light": "#CBD5E1",
    },
    "emerald_finance": {
        "dark": "#06281E",
        "header": "#0B3B2E",
        "dark_card": "#114B3B",
        "bg": "#F7FAF9",
        "card": "#FFFFFF",
        "border": "#D1E7DD",
        "accent": "#059669",
        "accent2": "#0D9488",
        "highlight": "#D97706",
        "text": "#0F172A",
        "muted": "#475569",
        "text_light": "#F8FAFC",
        "muted_light": "#CBD5E1",
    },
    "editorial_slate": {
        "dark": "#18181B",
        "header": "#27272A",
        "dark_card": "#323238",
        "bg": "#FAFAFA",
        "card": "#FFFFFF",
        "border": "#E4E4E7",
        "accent": "#E11D48",
        "accent2": "#F59E0B",
        "highlight": "#2563EB",
        "text": "#18181B",
        "muted": "#52525B",
        "text_light": "#FAFAFA",
        "muted_light": "#D4D4D8",
    },
    "editorial_luxe": {
        "dark": "#161311",
        "header": "#231E1B",
        "dark_card": "#2F2824",
        "bg": "#FAF7F2",
        "card": "#FFFFFF",
        "border": "#E7DFD5",
        "accent": "#8B2635",
        "accent2": "#B8860B",
        "highlight": "#2E5E4E",
        "text": "#1C1917",
        "muted": "#57534E",
        "text_light": "#FAF7F2",
        "muted_light": "#D6CEC3",
    },
    "nordic_winter": {
        "dark": "#0F1C22",
        "header": "#162730",
        "dark_card": "#203642",
        "bg": "#F7F9FA",
        "card": "#FFFFFF",
        "border": "#DAE3E8",
        "accent": "#3B6E8C",
        "accent2": "#7A344D",
        "highlight": "#2E5E4E",
        "text": "#0F1C22",
        "muted": "#4D606B",
        "text_light": "#F7F9FA",
        "muted_light": "#C7D5DC",
    },
}


def get_palette(name: str = "executive_navy") -> dict[str, str]:
    return dict(PALETTES.get(str(name or "").lower(), PALETTES["executive_navy"]))


def rgb(color, g: int | None = None, b: int | None = None) -> RGBColor:
    """Accept RGBColor, '#RRGGBB', 'RRGGBB', 0xRRGGBB, or (r, g, b) ints safely."""
    if isinstance(color, RGBColor):
        return color
    if g is not None and b is not None:
        return RGBColor(int(color) & 255, int(g) & 255, int(b) & 255)
    if isinstance(color, int):
        return RGBColor((color >> 16) & 255, (color >> 8) & 255, color & 255)
    if isinstance(color, (tuple, list)) and len(color) == 3:
        return RGBColor(int(color[0]) & 255, int(color[1]) & 255, int(color[2]) & 255)
    s = str(color or "#0F172A").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return RGBColor(0x0F, 0x17, 0x2A)
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def is_dark(color) -> bool:
    c = rgb(color)
    lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    return lum < 140


def new_deck() -> Presentation:
    """Create a 16:9 widescreen (13.333 x 7.5 in) Presentation."""
    os.makedirs("assets", exist_ok=True)
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    return prs


def set_slide_bg(slide, color) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = rgb(color)


def add_rect(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    fill=None,
    border=None,
    border_pt: float = 1.0,
    rounded: bool = False,
    radius: float = 0.04,
    **_extra,
):
    kind = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    if rounded and shp.adjustments:
        try:
            shp.adjustments[0] = radius
        except Exception:
            pass
    if fill is not None:
        shp.fill.solid()
        shp.fill.fore_color.rgb = rgb(fill)
    else:
        shp.fill.background()
    if border is not None:
        shp.line.color.rgb = rgb(border)
        shp.line.width = Pt(border_pt)
    else:
        shp.line.fill.background()
    return shp


def add_text(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    size: float = 13,
    color="#0F172A",
    bold: bool = False,
    italic: bool = False,
    align=PP_ALIGN.LEFT,
    valign=MSO_ANCHOR.TOP,
    font_name: str = FONT_MAIN,
    margin: tuple[float, float, float, float] = (0.06, 0.06, 0.04, 0.04),
    **_extra,
):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = valign
    tf.margin_left, tf.margin_right, tf.margin_top, tf.margin_bottom = (Inches(m) for m in margin)
    lines = str(text or "").split("\n")
    for idx, line in enumerate(lines):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(2)
        run = p.add_run()
        run.text = line
        run.font.name = font_name
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = rgb(color)
    return tb


def add_footer(slide, text: str, dark_bg: bool = False, page_num: int | None = None, **_extra) -> None:
    color = "#94A3B8" if dark_bg else "#64748B"
    if text:
        add_text(slide, 0.55, 7.04, 11.2, 0.3, text, size=9.5, color=color, margin=(0.0, 0.0, 0.0, 0.0))
    if page_num is not None:
        add_text(
            slide,
            11.9,
            7.04,
            0.88,
            0.3,
            f"{page_num}",
            size=9.5,
            color=color,
            bold=True,
            align=PP_ALIGN.RIGHT,
            margin=(0.0, 0.0, 0.0, 0.0),
        )


def add_cover_slide(
    prs: Presentation,
    title: str,
    subtitle: str,
    date_text: str = "",
    kicker: str = "",
    palette: dict[str, str] | None = None,
    cover_image: str | None = None,
    footer: str = "",
    **_extra,
):
    pal = palette or get_palette()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, pal["dark"])

    # Right decorative visual or sleek minimalist architectural strip
    has_img = bool(cover_image and Path(cover_image).is_file())
    if has_img:
        # Magazine-style split cover: crisp center-cropped photo filling the right half cleanly
        split_x = 7.75
        add_photo_card(
            slide,
            cover_image,
            split_x,
            0.0,
            SLIDE_W - split_x,
            SLIDE_H,
            crop=True,
            bg=None,
            border=None,
            pad=0.0,
        )
        add_rect(slide, 0.0, 0.0, split_x, SLIDE_H, fill=pal["dark"])
        add_rect(slide, split_x, 0.0, 0.08, SLIDE_H, fill=pal["accent"])
    else:
        # Clean minimalist architectural right accent strip (no empty card boxes)
        add_rect(slide, 12.15, 0.0, 1.183, SLIDE_H, fill=pal["dark_card"])
        add_rect(slide, 12.15, 0.0, 0.08, SLIDE_H, fill=pal["accent"])
        add_rect(slide, 12.42, 0.0, 0.04, SLIDE_H, fill=pal["accent2"])

    text_w = 6.55 if has_img else 10.6
    if kicker:
        add_text(
            slide,
            0.8,
            1.35,
            text_w,
            0.4,
            kicker.upper(),
            size=12,
            color=pal["accent2"],
            bold=True,
            margin=(0.0, 0.0, 0.0, 0.0),
        )

    title_size = 40 if len(title) <= 42 else 34
    add_text(
        slide,
        0.8,
        1.85,
        text_w,
        1.75,
        title,
        size=title_size,
        color=pal["text_light"],
        bold=True,
        valign=MSO_ANCHOR.MIDDLE,
        margin=(0.0, 0.0, 0.0, 0.0),
    )
    add_rect(slide, 0.8, 3.78, 2.4, 0.08, fill=pal["accent"])
    add_text(
        slide,
        0.8,
        4.05,
        text_w,
        1.1,
        subtitle,
        size=17,
        color=pal["muted_light"],
        margin=(0.0, 0.0, 0.0, 0.0),
    )
    if date_text:
        pill = add_rect(slide, 0.8, 5.45, 3.6, 0.46, fill=pal["dark_card"], border=pal["accent"], border_pt=1.0, rounded=True, radius=0.18)
        _ = pill
        add_text(
            slide,
            0.95,
            5.50,
            3.3,
            0.36,
            date_text,
            size=12,
            color=pal["text_light"],
            bold=True,
            valign=MSO_ANCHOR.MIDDLE,
            margin=(0.0, 0.0, 0.0, 0.0),
        )
    if footer:
        add_footer(slide, footer, dark_bg=True)
    return slide


def add_content_slide(
    prs: Presentation,
    title: str,
    kicker: str = "",
    palette: dict[str, str] | None = None,
    footer: str = "",
    page_num: int | None = None,
    **_extra,
):
    """Add a clean off-white content slide with a dark executive header band (title INSIDE the band)."""
    pal = palette or get_palette()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, pal["bg"])
    add_rect(slide, 0.0, 0.0, SLIDE_W, 1.12, fill=pal["header"])
    add_rect(slide, 0.0, 1.12, SLIDE_W, 0.05, fill=pal["accent"])
    t_len = len(str(title or ""))
    if kicker:
        t_sz = 24 if t_len <= 56 else (21 if t_len <= 78 else 18.5)
        add_text(
            slide,
            0.55,
            0.12,
            12.2,
            0.26,
            kicker.upper(),
            size=10,
            color=pal["accent2"],
            bold=True,
            margin=(0.0, 0.0, 0.0, 0.0),
        )
        add_text(
            slide,
            0.55,
            0.38,
            12.2,
            0.66,
            title,
            size=t_sz,
            color=pal["text_light"],
            bold=True,
            valign=MSO_ANCHOR.MIDDLE,
            margin=(0.0, 0.0, 0.0, 0.0),
        )
    else:
        t_sz = 26 if t_len <= 54 else (22 if t_len <= 78 else 19.5)
        add_text(
            slide,
            0.55,
            0.18,
            12.2,
            0.78,
            title,
            size=t_sz,
            color=pal["text_light"],
            bold=True,
            valign=MSO_ANCHOR.MIDDLE,
            margin=(0.0, 0.0, 0.0, 0.0),
        )
    add_footer(slide, footer, dark_bg=False, page_num=page_num)
    return slide


def add_card(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    title: str | None = None,
    bullets: Sequence[str] | None = None,
    body: str | None = None,
    bg: str = "#FFFFFF",
    border: str | None = "#E2E8F0",
    accent_top: str | None = None,
    accent_left: str | None = None,
    title_size: float = 17,
    body_size: float = 14.0,
    title_color: str | None = None,
    text_color: str | None = None,
    bullet_char: str = "▸",
    font_size: float | None = None,
    **_extra,
):
    if font_size is not None:
        body_size = font_size
    dark = is_dark(bg)
    t_col = title_color or ("#F8FAFC" if dark else "#0F172A")
    b_col = text_color or ("#E2E8F0" if dark else "#334155")
    brd = border if border is not None else ("#334155" if dark else "#E2E8F0")

    card = add_rect(slide, x, y, w, h, fill=bg, border=brd, border_pt=1.0, rounded=True, radius=0.04)
    pad_l = 0.24
    pad_t = 0.18
    if accent_top:
        add_rect(slide, x + 0.08, y, max(0.2, w - 0.16), 0.06, fill=accent_top)
        pad_t = 0.22
    if accent_left:
        add_rect(slide, x, y + 0.08, 0.07, max(0.2, h - 0.16), fill=accent_left)
        pad_l = 0.26

    inner_x = x + pad_l
    inner_w = max(0.5, w - pad_l - 0.20)
    cur_y = y + pad_t

    if title:
        title_h = 0.44 if len(title) <= 36 else 0.66
        add_text(
            slide,
            inner_x,
            cur_y,
            inner_w,
            title_h,
            title,
            size=title_size,
            color=t_col,
            bold=True,
            margin=(0.0, 0.0, 0.0, 0.0),
        )
        cur_y += title_h + 0.10

    rem_h = max(0.4, (y + h - 0.16) - cur_y)
    if bullets:
        cleaned_bullets = [str(b).strip() for b in bullets if str(b).strip()]
        total_chars = sum(len(b.replace("**", "")) for b in cleaned_bullets)
        area = inner_w * rem_h
        b_sz = body_size
        density = (total_chars / area) if area > 0 else 0
        if density > 52:
            b_sz = max(10.5, body_size - 3.0)
        elif density > 38:
            b_sz = max(11.5, body_size - 2.0)
        elif density > 26:
            b_sz = max(12.5, body_size - 1.0)
        tb = slide.shapes.add_textbox(Inches(inner_x), Inches(cur_y), Inches(inner_w), Inches(rem_h))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = Inches(0.02)
        lead_col = t_col if not dark else "#F8FAFC"
        for i, raw_item in enumerate(cleaned_bullets):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.space_after = Pt(7 if (len(cleaned_bullets) <= 4 and rem_h >= 2.2 and density < 38) else 4)
            # Parse executive bold-lead format: "**Lead**: rest", "**Lead** — rest", or "Lead: rest" (lead <= 52 chars)
            lead_txt, rest_txt = "", raw_item
            if raw_item.startswith("**") and "**" in raw_item[2:]:
                end_b = raw_item.index("**", 2)
                lead_txt = raw_item[2:end_b].strip()
                rest_txt = raw_item[end_b + 2 :].replace("**", "").strip()
            else:
                plain = raw_item.replace("**", "")
                for sep in (" : ", ": ", " — ", " – "):
                    if sep in plain:
                        idx_s = plain.index(sep)
                        if 2 <= idx_s <= 52:
                            lead_txt = plain[:idx_s].strip()
                            rest_txt = sep + plain[idx_s + len(sep) :].strip()
                            break
                if not lead_txt:
                    rest_txt = plain
            if lead_txt:
                r0 = p.add_run()
                r0.text = f"{bullet_char} {lead_txt}"
                r0.font.name = FONT_MAIN
                r0.font.size = Pt(b_sz)
                r0.font.bold = True
                r0.font.color.rgb = rgb(lead_col)
                if rest_txt:
                    if not rest_txt.startswith((":", " :", " —", " –", " -", ",", ".")):
                        rest_txt = " " + rest_txt
                    r1 = p.add_run()
                    r1.text = rest_txt
                    r1.font.name = FONT_MAIN
                    r1.font.size = Pt(b_sz)
                    r1.font.color.rgb = rgb(b_col)
            else:
                run = p.add_run()
                run.text = f"{bullet_char} {rest_txt}"
                run.font.name = FONT_MAIN
                run.font.size = Pt(b_sz)
                run.font.color.rgb = rgb(b_col)
    elif body:
        add_text(
            slide,
            inner_x,
            cur_y,
            inner_w,
            rem_h,
            body,
            size=body_size,
            color=b_col,
            margin=(0.0, 0.0, 0.0, 0.0),
        )
    return card


def add_kpi_card(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    label: str,
    value: str,
    detail: str = "",
    bg: str = "#FFFFFF",
    border: str = "#E2E8F0",
    accent: str = "#2563EB",
    **_extra,
):
    dark = is_dark(bg)
    add_rect(slide, x, y, w, h, fill=bg, border=border, border_pt=1.0, rounded=True, radius=0.05)
    add_rect(slide, x + 0.08, y, max(0.2, w - 0.16), 0.06, fill=accent)
    lbl_col = "#CBD5E1" if dark else "#64748B"
    val_col = "#F8FAFC" if dark else accent
    det_col = "#94A3B8" if dark else "#475569"

    add_text(slide, x + 0.2, y + 0.10, w - 0.4, 0.24, label.upper(), size=10.0, color=lbl_col, bold=True, margin=(0, 0, 0, 0))
    show_detail = bool(detail) and h >= 1.22
    v_sz = 32 if len(str(value)) <= 10 else (26 if len(str(value)) <= 16 else 20)
    if h < 1.45:
        v_sz = min(v_sz, 24)
    det_s = str(detail or "").strip()
    chars_per_line = max(18, int((w - 0.4) * 14))
    det_lines = max(1, (len(det_s) + chars_per_line - 1) // chars_per_line)
    det_h = 0.60 if (det_lines >= 2 and h >= 1.28) else 0.34
    val_top = y + 0.30
    val_bottom = (y + h - det_h - 0.04) if show_detail else (y + h - 0.10)
    val_h = max(0.32, val_bottom - val_top)
    add_text(
        slide,
        x + 0.2,
        val_top,
        w - 0.4,
        val_h,
        str(value),
        size=v_sz,
        color=val_col,
        bold=True,
        valign=MSO_ANCHOR.MIDDLE,
        margin=(0, 0, 0, 0),
    )
    if show_detail:
        add_text(
            slide,
            x + 0.2,
            y + h - det_h - 0.06,
            w - 0.4,
            det_h,
            det_s,
            size=10.0,
            color=det_col,
            margin=(0, 0, 0, 0),
        )


def add_table(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    col_widths: Sequence[float] | None = None,
    header_bg: str | None = None,
    stripe_bg: str = "#F1F5F9",
    font_size: float = 11.5,
    palette: dict[str, str] | None = None,
    **_extra,
):
    if header_bg is None:
        header_bg = (palette or {}).get("header", "#0F172A")
    n_rows = 1 + len(rows)
    n_cols = len(headers)
    tbl = slide.shapes.add_table(n_rows, n_cols, Inches(x), Inches(y), Inches(w), Inches(h)).table
    if col_widths and len(col_widths) == n_cols:
        total = sum(col_widths) or 1.0
        for c_i, cw in enumerate(col_widths):
            tbl.columns[c_i].width = Inches(w * (cw / total))
    else:
        for c_i in range(n_cols):
            tbl.columns[c_i].width = Inches(w / max(1, n_cols))

    all_rows = [list(headers)] + [list(r) for r in rows]
    for r_i, row in enumerate(all_rows):
        is_hdr = r_i == 0
        bg_col = header_bg if is_hdr else (stripe_bg if r_i % 2 == 0 else "#FFFFFF")
        txt_col = "#F8FAFC" if is_dark(bg_col) else "#0F172A"
        for c_i in range(n_cols):
            val = str(row[c_i]) if c_i < len(row) else ""
            cell = tbl.cell(r_i, c_i)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            cell.margin_left = Inches(0.10)
            cell.margin_right = Inches(0.10)
            cell.margin_top = Inches(0.05)
            cell.margin_bottom = Inches(0.05)
            cell.fill.solid()
            cell.fill.fore_color.rgb = rgb(bg_col)
            p = cell.text_frame.paragraphs[0]
            p.text = ""
            p.alignment = PP_ALIGN.LEFT if c_i == 0 else PP_ALIGN.CENTER
            run = p.add_run()
            run.text = val
            run.font.name = FONT_MAIN
            run.font.size = Pt(font_size if not is_hdr else font_size + 0.5)
            run.font.bold = is_hdr or (c_i == 0)
            run.font.color.rgb = rgb(txt_col)
    return tbl


def add_picture_fit(
    slide,
    image_path: str | Path,
    x: float,
    y: float,
    max_w: float,
    max_h: float,
    pad: float = 0.12,
    card_bg: str | None = None,
    card_border: str = "#E2E8F0",
    title: str | None = None,
    accent_top: str | None = None,
    **_extra,
):
    """Place an image inside (x, y, max_w, max_h) preserving exact natural aspect ratio (zero distortion)."""
    p = Path(image_path)
    if card_bg:
        add_rect(slide, x, y, max_w, max_h, fill=card_bg, border=card_border, border_pt=1.0, rounded=True, radius=0.04)
        if accent_top:
            add_rect(slide, x + 0.08, y, max(0.2, max_w - 0.16), 0.06, fill=accent_top)

    top_offset = pad
    if title:
        t_col = "#F8FAFC" if (card_bg and is_dark(card_bg)) else "#0F172A"
        add_text(slide, x + 0.2, y + 0.14, max_w - 0.4, 0.36, title, size=14.5, color=t_col, bold=True, margin=(0, 0, 0, 0))
        top_offset = 0.54

    avail_w = max(0.4, max_w - 2 * pad)
    avail_h = max(0.4, max_h - top_offset - pad)

    with Image.open(p) as im:
        iw, ih = im.size
    img_ar = iw / max(1, ih)
    box_ar = avail_w / max(0.01, avail_h)
    if img_ar >= box_ar:
        draw_w = avail_w
        draw_h = draw_w / img_ar
    else:
        draw_h = avail_h
        draw_w = draw_h * img_ar

    dx = x + pad + (avail_w - draw_w) / 2.0
    dy = y + top_offset + (avail_h - draw_h) / 2.0
    return slide.shapes.add_picture(str(p), Inches(dx), Inches(dy), width=Inches(draw_w))


def add_timeline(
    slide,
    x: float,
    y: float,
    w: float,
    events: Sequence[tuple],
    accent: str | None = None,
    line_color: str = "#CBD5E1",
    text_color: str = "#0F172A",
    muted_color: str = "#475569",
    palette: dict[str, str] | None = None,
    **_extra,
) -> None:
    if accent is None:
        accent = (palette or {}).get("accent", "#2563EB")
    """Crisp horizontal timeline with dates strictly above the rail and titles/descriptions strictly below."""
    n = len(events)
    if n == 0:
        return
    rail_y = y + 0.40
    add_rect(slide, x + 0.3, rail_y, max(0.5, w - 0.6), 0.05, fill=line_color)
    col_w = w / n
    for i, ev in enumerate(events):
        date_s = str(ev[0]) if len(ev) > 0 else ""
        title_s = str(ev[1]) if len(ev) > 1 else ""
        desc_s = str(ev[2]) if len(ev) > 2 else ""
        cx = x + col_w * (i + 0.5)
        bx = x + col_w * i + 0.08
        bw = max(0.6, col_w - 0.16)
        # Date above rail
        add_text(slide, bx, y, bw, 0.32, date_s, size=10.5, color=accent, bold=True, align=PP_ALIGN.CENTER, margin=(0, 0, 0, 0))
        # Circular node on rail
        dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(cx - 0.10), Inches(rail_y - 0.075), Inches(0.20), Inches(0.20))
        dot.fill.solid()
        dot.fill.fore_color.rgb = rgb(accent)
        dot.line.color.rgb = rgb("#FFFFFF")
        dot.line.width = Pt(1.5)
        # Label below rail
        add_text(slide, bx, rail_y + 0.18, bw, 0.36, title_s, size=11.5, color=text_color, bold=True, align=PP_ALIGN.CENTER, margin=(0, 0, 0, 0))
        if desc_s:
            add_text(slide, bx, rail_y + 0.52, bw, 0.50, desc_s, size=10, color=muted_color, align=PP_ALIGN.CENTER, margin=(0, 0, 0, 0))


def add_insight_bar(
    slide,
    text: str,
    y: float = 6.24,
    h: float = 0.58,
    bg: str | None = None,
    accent: str | None = None,
    x: float = 0.55,
    w: float = 12.23,
    palette: dict[str, str] | None = None,
    **_extra,
) -> None:
    if bg is None:
        bg = (palette or {}).get("header", "#0F172A")
    if accent is None:
        accent = (palette or {}).get("accent", "#2563EB")
    add_rect(slide, x, y, w, h, fill=bg, rounded=True, radius=0.08)
    add_rect(slide, x, y + 0.06, 0.09, max(0.1, h - 0.12), fill=accent)
    t_col = "#F8FAFC" if is_dark(bg) else "#0F172A"
    sz = 12 if len(text) <= 125 else 11
    add_text(slide, x + 0.24, y + 0.06, w - 0.38, h - 0.12, text, size=sz, color=t_col, valign=MSO_ANCHOR.MIDDLE, margin=(0, 0, 0, 0))


def add_closing_slide(
    prs: Presentation,
    title: str,
    pillars: Sequence[tuple[str, Sequence[str]]],
    palette: dict[str, str] | None = None,
    subtitle: str = "",
    kicker: str = "",
    footer: str = "",
    page_num: int | None = None,
    body_size: float = 13.5,
    font_size: float | None = None,
    **_extra,
):
    if font_size is not None:
        body_size = font_size
    """Dark executive closing slide with 3 or 4 evenly spaced pillars and zero overlap."""
    pal = palette or get_palette()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    set_slide_bg(slide, pal["dark"])

    if kicker:
        add_text(slide, 0.65, 0.42, 12.0, 0.32, kicker.upper(), size=11, color=pal["accent2"], bold=True, margin=(0, 0, 0, 0))
    add_text(slide, 0.65, 0.74, 12.0, 0.66, title, size=32, color=pal["text_light"], bold=True, margin=(0, 0, 0, 0))
    add_rect(slide, 0.65, 1.48, 2.2, 0.07, fill=pal["accent"])
    cards_top = 1.85
    if subtitle:
        add_text(slide, 0.65, 1.66, 12.0, 0.42, subtitle, size=14, color=pal["muted_light"], margin=(0, 0, 0, 0))
        cards_top = 2.22

    n = max(1, min(len(pillars), 4))
    gap = 0.28
    total_w = 12.03
    card_w = (total_w - gap * (n - 1)) / n
    card_h = 6.72 - cards_top
    accents = [pal["accent"], pal["accent2"], pal["highlight"], pal["accent"]]
    for i, (p_title, p_bullets) in enumerate(pillars[:n]):
        cx = 0.65 + i * (card_w + gap)
        add_card(
            slide,
            cx,
            cards_top,
            card_w,
            card_h,
            title=p_title,
            bullets=list(p_bullets),
            bg=pal["dark_card"],
            border="#334155",
            accent_top=accents[i % len(accents)],
            title_size=16,
            body_size=body_size,
            title_color=pal["text_light"],
            text_color=pal["muted_light"],
        )
    add_footer(slide, footer, dark_bg=True, page_num=page_num)
    return slide


def style_mpl_ax(
    ax,
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    grid_axis: str = "x",
    text_color: str = "#0F172A",
    muted_color: str = "#64748B",
    **_extra,
) -> None:
    """Clean editorial matplotlib styling: zero chart junk, soft grid behind bars, crisp labels."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#CBD5E1")
    ax.spines["bottom"].set_color("#CBD5E1")
    ax.tick_params(axis="both", colors=muted_color, labelsize=9.5)
    if grid_axis in ("x", "y", "both"):
        ax.set_axisbelow(True)
        ax.grid(axis=grid_axis, linestyle="--", alpha=0.35, color="#94A3B8")
    if title:
        ax.set_title(title, fontsize=13, fontweight="bold", color=text_color, pad=12, loc="left")
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=10, color=muted_color, labelpad=6)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=10, color=muted_color, labelpad=6)


def add_photo_card(
    slide,
    image_path: str | Path,
    x: float,
    y: float,
    w: float,
    h: float,
    caption: str = "",
    title: str = "",
    crop: bool = True,
    bg: str | None = "#FFFFFF",
    border: str | None = "#E2E8F0",
    accent_top: str | None = None,
    pad: float = 0.08,
    **_extra,
):
    """Place a verified editorial photo cleanly inside (x, y, w, h) with optional Lanczos center-crop and caption."""
    p = Path(image_path)
    if not p.is_file():
        return None
    if bg is not None:
        add_rect(slide, x, y, w, h, fill=bg, border=border, border_pt=1.0, rounded=True, radius=0.04)
        if accent_top:
            add_rect(slide, x + 0.08, y, max(0.2, w - 0.16), 0.06, fill=accent_top)

    dark = is_dark(bg) if bg else False
    t_col = "#F8FAFC" if dark else "#0F172A"
    c_col = "#CBD5E1" if dark else "#475569"

    top_off = pad
    if title:
        add_text(slide, x + 0.16, y + 0.12, w - 0.32, 0.34, title, size=14.5, color=t_col, bold=True, margin=(0, 0, 0, 0))
        top_off = 0.48
    cap_h = 0.44 if caption else 0.0
    bot_off = pad + cap_h

    inner_x = x + pad
    inner_y = y + top_off
    inner_w = max(0.4, w - 2 * pad)
    inner_h = max(0.4, h - top_off - bot_off)

    if crop:
        target_px_w = max(240, int(round(inner_w * 200)))
        target_px_h = max(240, int(round(inner_h * 200)))
        key = hashlib.sha1(f"{p.resolve()}|{target_px_w}x{target_px_h}".encode("utf-8")).hexdigest()[:10]
        crop_dir = p.parent
        crop_path = crop_dir / f"_crop_{p.stem}_{key}.jpg"
        with Image.open(p) as im:
            im_rgb = im.convert("RGB")
            # Bias crop slightly above center (0.5, 0.32) so portraits/fashion subjects keep heads framed!
            fitted = ImageOps.fit(im_rgb, (target_px_w, target_px_h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.32))
            fitted.save(str(crop_path), format="JPEG", quality=92, optimize=True)
        pic = slide.shapes.add_picture(str(crop_path), Inches(inner_x), Inches(inner_y), width=Inches(inner_w))
    else:
        pic = add_picture_fit(slide, p, inner_x, inner_y, inner_w, inner_h, pad=0.0)

    if caption:
        add_text(
            slide,
            x + 0.14,
            y + h - cap_h + 0.02,
            w - 0.28,
            cap_h - 0.04,
            caption,
            size=10.5,
            color=c_col,
            italic=True,
            valign=MSO_ANCHOR.MIDDLE,
            margin=(0, 0, 0, 0),
        )
    return pic


def add_swatch_strip(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    swatches: Sequence[tuple],
    title: str = "",
    bg: str = "#FFFFFF",
    border: str = "#E2E8F0",
    **_extra,
):
    """Render an editorial color/material palette swatch strip: swatches=[(hex_color, name, note), ...]."""
    add_rect(slide, x, y, w, h, fill=bg, border=border, border_pt=1.0, rounded=True, radius=0.04)
    dark = is_dark(bg)
    t_col = "#F8FAFC" if dark else "#0F172A"
    m_col = "#CBD5E1" if dark else "#57534E"

    cur_y = y + 0.14
    if title:
        add_text(slide, x + 0.20, cur_y, w - 0.40, 0.34, title, size=14.5, color=t_col, bold=True, margin=(0, 0, 0, 0))
        cur_y += 0.42

    items = list(swatches)[:6]
    if not items:
        return
    gap = 0.16
    avail_w = w - 0.40
    col_w = (avail_w - gap * (len(items) - 1)) / len(items)
    rem_h = max(0.8, (y + h - 0.14) - cur_y)
    swatch_h = max(0.45, rem_h * 0.54)
    for idx, sw in enumerate(items):
        hex_c = str(sw[0]) if len(sw) > 0 else "#3B6E8C"
        name = str(sw[1]) if len(sw) > 1 else ""
        note = str(sw[2]) if len(sw) > 2 else ""
        sx = x + 0.20 + idx * (col_w + gap)
        add_rect(slide, sx, cur_y, col_w, swatch_h, fill=hex_c, border="#CBD5E1", border_pt=0.75, rounded=True, radius=0.08)
        lbl_y = cur_y + swatch_h + 0.06
        add_text(slide, sx, lbl_y, col_w, 0.28, name, size=11.0, color=t_col, bold=True, align=PP_ALIGN.CENTER, margin=(0, 0, 0, 0))
        if note and (lbl_y + 0.28) < (y + h - 0.08):
            add_text(
                slide,
                sx,
                lbl_y + 0.28,
                col_w,
                max(0.24, (y + h - 0.08) - (lbl_y + 0.28)),
                note,
                size=10.0,
                color=m_col,
                align=PP_ALIGN.CENTER,
                margin=(0, 0, 0, 0),
            )
