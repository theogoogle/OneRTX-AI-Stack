"""
Deterministic Smart Visuals engine (Infographics, Mind Maps, and Timelines).
Generates structured JSON spec via local Pro (or accepts a pre-built --spec-file),
renders zero-overlap vector SVG (for mindmaps) or editorial HTML grids (for infographics/timelines),
and exports a crisp 2x high-DPI PNG via Playwright Chromium.

Usage:
  python -I -X utf8 renderer.py --topic "..." --type auto|infographic|mindmap|timeline --language auto --out-dir <dir>
  python -I -X utf8 renderer.py --spec-file spec.json --out-dir <dir>
"""
from __future__ import annotations

import argparse
import html as html_lib
import http.client
import json
import math
import re
import textwrap
from pathlib import Path
from typing import Any

ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4000

BRANCH_COLORS = [
    ("#0EA5E9", "#E0F2FE", "#0369A1"),  # sky
    ("#10B981", "#D1FAE5", "#047857"),  # emerald
    ("#F59E0B", "#FEF3C7", "#B45309"),  # amber
    ("#8B5CF6", "#EDE9FE", "#6D28D9"),  # violet
    ("#F43F5E", "#FFE4E6", "#BE123C"),  # rose
    ("#06B6D4", "#CFFAFE", "#0E7490"),  # cyan
]

SPEC_SYSTEM_PROMPT = """You are an information designer creating crystal-clear visual diagrams for busy readers and families.
Given a topic, outline, or dense text, return ONLY ONE valid JSON object (no markdown fences, no commentary) matching ONE of these 3 schemas:

1) If visual_type == "mindmap" (or topic is a concept tree, brainstorm, study map, or breakdown):
{
  "type": "mindmap",
  "title": "Short crisp title (max 8 words)",
  "subtitle": "1-line subtitle",
  "center": "Central Idea (2-4 words)",
  "branches": [
    { "label": "Branch Name (2-4 words)", "items": ["Concise leaf 1 (3-8 words)", "Concise leaf 2", "Concise leaf 3"] }
  ]
}
(Include 4 to 6 branches, 2 to 4 concise items per branch.)

2) If visual_type == "timeline" (or topic is chronological steps, history, roadmap, or schedule):
{
  "type": "timeline",
  "kicker": "TIMELINE",
  "title": "Short crisp title",
  "subtitle": "1-line overview",
  "steps": [
    { "badge": "Step 1 / Year", "heading": "Short heading", "detail": "1-2 clear factual sentences." }
  ],
  "takeaway": "1-sentence key takeaway."
}
(Include 4 to 7 chronological steps.)

3) If visual_type == "infographic" (or default for visual summaries, comparisons, study guides, stats):
{
  "type": "infographic",
  "kicker": "VISUAL SUMMARY",
  "title": "Short crisp title",
  "subtitle": "1-line overview",
  "stats": [
    { "value": "Short stat / key metric", "label": "Short label" }
  ],
  "cards": [
    { "badge": "01 · Category", "heading": "Card heading", "bullets": ["Crisp bullet 1", "Crisp bullet 2", "Crisp bullet 3"] }
  ],
  "takeaway": "1-sentence bottom summary."
}
(Include 3 stats and 4 or 6 cards.)"""


def _strip_json(raw: str) -> dict[str, Any]:
    text = re.sub(r"(?is)<think>.*?</think>", "", (raw or "")).strip()
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    if m:
        text = m.group(1).strip()
    else:
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j > i:
            text = text[i : j + 1]
    return json.loads(text)


def generate_spec_on_pro(topic_or_content: str, visual_type: str = "auto", language: str = "auto") -> dict[str, Any]:
    lang_note = (
        f"Write all labels and text in {language}."
        if language and language.lower() not in ("auto", "")
        else "Match the exact language of the user's input."
    )
    vt_note = (
        f"MUST use schema type='{visual_type}'."
        if visual_type in ("infographic", "mindmap", "timeline")
        else "Choose the most natural schema ('mindmap', 'infographic', or 'timeline') for the content."
    )
    payload = {
        "model": "deep",
        "temperature": 0.35,
        "max_tokens": 5000,
        "stream": False,
        "_internal_smart_visuals": True,
        "messages": [
            {"role": "system", "content": f"{SPEC_SYSTEM_PROMPT}\n\n{vt_note} {lang_note}"},
            {"role": "user", "content": topic_or_content[:8000]},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=300)
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Router HTTP {resp.status}: {raw.decode('utf-8', 'ignore')[:300]}")
        data = json.loads(raw.decode("utf-8"))
    finally:
        conn.close()
    content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    return _strip_json(content)


# ------------------------------------------------------------------ Deterministic SVG Mind Map (balanced L/R tree)
def build_mindmap_svg(spec: dict[str, Any]) -> tuple[str, int, int]:
    title = str(spec.get("title") or "Mind Map").strip()
    subtitle = str(spec.get("subtitle") or "").strip()
    center = str(spec.get("center") or title).strip()
    branches = list(spec.get("branches") or [])[:6]
    if not branches:
        branches = [{"label": "Overview", "items": ["No items provided"]}]

    left_branches = [b for i, b in enumerate(branches) if i % 2 == 1]
    right_branches = [b for i, b in enumerate(branches) if i % 2 == 0]
    if not left_branches and len(right_branches) > 1:
        left_branches.append(right_branches.pop())

    def _side_height(side: list[dict[str, Any]]) -> int:
        if not side:
            return 220
        total = 0
        for b in side:
            items = list(b.get("items") or [])[:4] or [""]
            h = max(62, sum(max(36, 18 * len(textwrap.wrap(str(it), 28)) + 16) + 10 for it in items))
            total += h + 28
        return max(260, total)

    h_body = max(_side_height(left_branches), _side_height(right_branches), 360)
    width = 1440
    top_pad = 108
    height = top_pad + h_body + 56
    cx, cy = width // 2, top_pad + h_body // 2

    elems: list[str] = [
        f'<rect width="{width}" height="{height}" fill="#0F172A"/>',
        f'<text x="{cx}" y="48" text-anchor="middle" fill="#F8FAFC" font-family="Segoe UI, Arial, sans-serif" font-size="28" font-weight="700">{html_lib.escape(title)}</text>',
    ]
    if subtitle:
        elems.append(
            f'<text x="{cx}" y="76" text-anchor="middle" fill="#94A3B8" font-family="Segoe UI, Arial, sans-serif" font-size="14">{html_lib.escape(subtitle[:110])}</text>'
        )

    def _render_side(side: list[dict[str, Any]], direction: int, color_offset: int) -> None:
        if not side:
            return
        # Compute vertical slots evenly
        heights = []
        for b in side:
            items = list(b.get("items") or [])[:4] or [""]
            item_heights = [max(36, 18 * max(1, len(textwrap.wrap(str(it), 28))) + 16) for it in items]
            heights.append((b, items, item_heights, max(54, sum(item_heights) + 10 * max(0, len(items) - 1)) + 26))
        total_h = sum(x[3] for x in heights)
        y_cursor = top_pad + (h_body - total_h) / 2

        bx = cx + direction * 255
        lx = cx + direction * 490
        for idx, (b, items, item_heights, block_h) in enumerate(heights):
            accent, soft_bg, dark_txt = BRANCH_COLORS[(idx + color_offset) % len(BRANCH_COLORS)]
            by = y_cursor + block_h / 2
            y_cursor += block_h

            # Center -> Branch cubic curve
            c1x = cx + direction * 115
            c2x = bx - direction * 115
            elems.append(
                f'<path d="M {cx + direction * 96} {cy} C {c1x} {cy}, {c2x} {by:.1f}, {bx - direction * 95} {by:.1f}" '
                f'fill="none" stroke="{accent}" stroke-width="3.2" stroke-opacity="0.88"/>'
            )

            # Branch node
            b_lines = textwrap.wrap(str(b.get("label") or "Topic"), 18)[:2] or ["Topic"]
            bh = 22 + 18 * len(b_lines)
            elems.append(
                f'<rect x="{bx - 95}" y="{by - bh / 2:.1f}" width="190" height="{bh}" rx="12" '
                f'fill="{soft_bg}" stroke="{accent}" stroke-width="2.2"/>'
            )
            for li, line in enumerate(b_lines):
                ty = by - (len(b_lines) - 1) * 9 + li * 18 + 5
                elems.append(
                    f'<text x="{bx}" y="{ty:.1f}" text-anchor="middle" fill="{dark_txt}" font-family="Segoe UI, Arial, sans-serif" font-size="14.5" font-weight="700">{html_lib.escape(line)}</text>'
                )

            # Leaves
            leaves_total = sum(item_heights) + 10 * max(0, len(items) - 1)
            ly_cur = by - leaves_total / 2
            for it, ih in zip(items, item_heights):
                ly = ly_cur + ih / 2
                ly_cur += ih + 10
                x_branch_edge = bx + direction * 95
                x_leaf_edge = lx - direction * 125
                mcx = (x_branch_edge + x_leaf_edge) / 2
                elems.append(
                    f'<path d="M {x_branch_edge} {by:.1f} C {mcx:.1f} {by:.1f}, {mcx:.1f} {ly:.1f}, {x_leaf_edge} {ly:.1f}" '
                    f'fill="none" stroke="{accent}" stroke-width="1.8" stroke-opacity="0.65"/>'
                )
                rx = lx - 125
                elems.append(
                    f'<rect x="{rx}" y="{ly - ih / 2:.1f}" width="250" height="{ih}" rx="9" '
                    f'fill="#1E293B" stroke="#334155" stroke-width="1.2"/>'
                )
                wlines = textwrap.wrap(str(it), 28)[:3] or [""]
                for li, wline in enumerate(wlines):
                    ty = ly - (len(wlines) - 1) * 9 + li * 18 + 5
                    elems.append(
                        f'<text x="{lx}" y="{ty:.1f}" text-anchor="middle" fill="#E2E8F0" font-family="Segoe UI, Arial, sans-serif" font-size="13">{html_lib.escape(wline)}</text>'
                    )

    _render_side(right_branches, +1, 0)
    _render_side(left_branches, -1, len(right_branches))

    # Central node drawn last on top
    c_lines = textwrap.wrap(center, 16)[:3] or [center[:16]]
    ch = max(72, 26 + 22 * len(c_lines))
    elems.append(
        f'<rect x="{cx - 98}" y="{cy - ch / 2:.1f}" width="196" height="{ch}" rx="18" '
        f'fill="#1E3A8A" stroke="#60A5FA" stroke-width="3"/>'
    )
    for li, line in enumerate(c_lines):
        ty = cy - (len(c_lines) - 1) * 11 + li * 22 + 6
        elems.append(
            f'<text x="{cx}" y="{ty:.1f}" text-anchor="middle" fill="#FFFFFF" font-family="Segoe UI, Arial, sans-serif" font-size="17" font-weight="700">{html_lib.escape(line)}</text>'
        )

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
        + "\n".join(elems)
        + "</svg>"
    )
    return svg, width, height


# ------------------------------------------------------------------ HTML Infographic & Timeline
_VISUAL_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:1100px;background:#0b1120;color:#e2e8f0;font-family:"Segoe UI",-apple-system,BlinkMacSystemFont,Roboto,Arial,sans-serif;padding:34px 38px 36px}
.hero{background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);border:1px solid #334155;border-top:5px solid #38bdf8;border-radius:14px;padding:24px 28px;margin-bottom:20px}
.kicker{font-size:11px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:#38bdf8;margin-bottom:6px}
.hero h1{font-size:30px;line-height:1.18;color:#f8fafc;margin-bottom:6px}
.hero p{font-size:15px;color:#94a3b8;line-height:1.45}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}
.stat{background:#111c33;border:1px solid #1e3a5f;border-radius:12px;padding:14px 18px}
.stat .v{font-size:24px;font-weight:700;color:#38bdf8;line-height:1.15;margin-bottom:4px}
.stat .l{font-size:12.5px;color:#cbd5e1}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.card{background:#111827;border:1px solid #273449;border-radius:12px;padding:18px 20px}
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:3px 9px;border-radius:99px;margin-bottom:8px}
.card h2{font-size:17px;color:#f8fafc;margin-bottom:8px;line-height:1.25}
.card ul{padding-left:18px;font-size:13.5px;color:#cbd5e1;line-height:1.48}
.card li{margin-bottom:5px}
.timeline{display:flex;flex-direction:column;gap:12px;margin-bottom:20px;border-left:3px solid #38bdf8;margin-left:14px;padding-left:22px}
.step{background:#111827;border:1px solid #273449;border-radius:12px;padding:15px 18px;position:relative}
.step:before{content:"";position:absolute;left:-30px;top:18px;width:13px;height:13px;border-radius:50%;background:#38bdf8;border:3px solid #0b1120}
.step .sbadge{font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#38bdf8;margin-bottom:3px}
.step h2{font-size:16.5px;color:#f8fafc;margin-bottom:4px}
.step p{font-size:13.5px;color:#cbd5e1;line-height:1.48}
.takeaway{background:#0f2922;border:1px solid #10b981;border-radius:12px;padding:14px 18px;font-size:14px;color:#d1fae5;line-height:1.45}
.takeaway b{color:#34d399;text-transform:uppercase;letter-spacing:.08em;font-size:11px;margin-right:8px}
"""


def build_infographic_or_timeline_html(spec: dict[str, Any]) -> str:
    vtype = str(spec.get("type") or "infographic").lower()
    kicker = html_lib.escape(str(spec.get("kicker") or ("TIMELINE" if vtype == "timeline" else "VISUAL BRIEF")))
    title = html_lib.escape(str(spec.get("title") or "Visual Summary"))
    subtitle = html_lib.escape(str(spec.get("subtitle") or ""))
    takeaway = html_lib.escape(str(spec.get("takeaway") or ""))

    body_parts = [
        f"<div class='hero'><div class='kicker'>{kicker}</div><h1>{title}</h1>"
        + (f"<p>{subtitle}</p>" if subtitle else "")
        + "</div>"
    ]

    if vtype == "timeline":
        steps_html = []
        for st in (spec.get("steps") or [])[:8]:
            sb = html_lib.escape(str(st.get("badge") or ""))
            sh = html_lib.escape(str(st.get("heading") or ""))
            sd = html_lib.escape(str(st.get("detail") or ""))
            steps_html.append(f"<div class='step'><div class='sbadge'>{sb}</div><h2>{sh}</h2><p>{sd}</p></div>")
        body_parts.append(f"<div class='timeline'>{''.join(steps_html)}</div>")
    else:
        stats = (spec.get("stats") or [])[:3]
        if stats:
            s_cards = "".join(
                f"<div class='stat'><div class='v'>{html_lib.escape(str(s.get('value') or ''))}</div>"
                f"<div class='l'>{html_lib.escape(str(s.get('label') or ''))}</div></div>"
                for s in stats
            )
            body_parts.append(f"<div class='stats'>{s_cards}</div>")
        cards = (spec.get("cards") or [])[:6]
        c_html = []
        for idx, c in enumerate(cards):
            accent, soft_bg, dark_txt = BRANCH_COLORS[idx % len(BRANCH_COLORS)]
            badge = html_lib.escape(str(c.get("badge") or f"0{idx + 1}"))
            head = html_lib.escape(str(c.get("heading") or ""))
            buls = "".join(f"<li>{html_lib.escape(str(b))}</li>" for b in (c.get("bullets") or [])[:4])
            c_html.append(
                f"<div class='card' style='border-top:3px solid {accent}'>"
                f"<span class='badge' style='background:{soft_bg};color:{dark_txt}'>{badge}</span>"
                f"<h2>{head}</h2><ul>{buls}</ul></div>"
            )
        body_parts.append(f"<div class='grid'>{''.join(c_html)}</div>")

    if takeaway:
        body_parts.append(f"<div class='takeaway'><b>Key Takeaway</b>{takeaway}</div>")

    return f"<!DOCTYPE html><html><head><meta charset='utf-8'><style>{_VISUAL_CSS}</style></head><body>{''.join(body_parts)}</body></html>"


def render_spec(spec: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    vtype = str(spec.get("type") or "infographic").lower()
    if vtype not in ("mindmap", "infographic", "timeline"):
        vtype = "infographic"
    title = str(spec.get("title") or "Visual Summary").strip()

    svg_path: Path | None = None
    html_path: Path
    png_path = out_dir / "visual.png"

    if vtype == "mindmap":
        svg_str, vw, vh = build_mindmap_svg(spec)
        svg_path = out_dir / "visual.svg"
        svg_path.write_text(svg_str, encoding="utf-8")
        html_path = out_dir / "visual.html"
        html_path.write_text(
            f"<!DOCTYPE html><html><body style='margin:0;background:#0F172A;width:{vw}px'>{svg_str}</body></html>",
            encoding="utf-8",
        )
        vp = {"width": vw, "height": max(400, vh)}
    else:
        html_str = build_infographic_or_timeline_html(spec)
        html_path = out_dir / "visual.html"
        html_path.write_text(html_str, encoding="utf-8")
        vp = {"width": 1100, "height": 760}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport=vp, device_scale_factor=2)
            page.goto(html_path.resolve().as_uri(), wait_until="load")
            box = page.evaluate("() => { const r = document.body.getBoundingClientRect(); return {w: Math.ceil(r.width), h: Math.ceil(r.height)}; }")
            page.screenshot(path=str(png_path), full_page=True)
        finally:
            browser.close()

    return {
        "ok": True,
        "visual_type": vtype,
        "title": title,
        "png_path": str(png_path),
        "svg_path": str(svg_path) if svg_path else None,
        "html_path": str(html_path),
        "width": box.get("w"),
        "height": box.get("h"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Smart Infographic & Mind Map renderer")
    ap.add_argument("--topic", default="")
    ap.add_argument("--type", default="auto", choices=("auto", "infographic", "mindmap", "timeline"))
    ap.add_argument("--language", default="auto")
    ap.add_argument("--spec-file", default="")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    try:
        if args.spec_file:
            spec = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
        elif args.topic.strip():
            spec = generate_spec_on_pro(args.topic, args.type, args.language)
        else:
            raise ValueError("Either --topic or --spec-file is required.")
        res = render_spec(spec, Path(args.out_dir))
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
