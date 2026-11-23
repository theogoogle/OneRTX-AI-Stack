"""pptx_studio: an MCP tool server (stdio) for bespoke executive PowerPoint creation & visual QA.

Used by the local coding agents (Open Code, Claude Code) to produce Claude-style bespoke editable `.pptx`
presentations:
  1. `search_slide_facts`: pulls fresh live news/facts (Tavily `topic=news` + `time_range`) and verified editorial photo URLs.
  2. `generate_slide_visual`: generates a custom editorial cover/hero illustration via local Qwen-Image-2.1 (ComfyUI).
  3. `inspect_pptx_visually`: converts `.pptx` -> PDF via LibreOffice headless -> PNGs via PyMuPDF, runs programmatic
     geometry/overlap/font/citation checks, and runs local Gemma 4 Vision QA to critique every slide visually.
  4. `publish_pptx`: copies the verified `.pptx` + contact-sheet preview PNG to `%USERPROFILE%\\AI\\generated\\decks\\`
     and returns clickable PC + phone (Tailscale `:8443`) download links.

Started by the agents themselves (stdio): no port, no API key. Strictly confined to `PPTX_STUDIO_ROOT`
(default `%USERPROFILE%\\AI\\projects`) for project reads/writes. Log: `pptx_studio.log` next to this file.
"""
import base64
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

ROOT = Path(os.environ.get("PPTX_STUDIO_ROOT") or Path.home() / "AI" / "projects").resolve()
SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import html_files  # noqa: E402
import web_search  # noqa: E402

ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4000
LOG = Path(__file__).with_name("pptx_studio.log")
NO_WINDOW = 0x08000000
DECKS_DIR = html_files.GENERATED_DIR / "decks"
SOFFICE_CANDIDATES = (
    Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
    Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
)
_lock = threading.Lock()
_CITE_BRACKET_RE = re.compile(r"\[\d{1,2}\]")

TOOLS = [
    {
        "name": "search_slide_facts",
        "description": (
            "Search the live web (Tavily) for up-to-date facts, figures, and verified editorial photo URLs to use "
            "in a presentation. Use this whenever a deck covers current events, recent launches, or live market data. "
            "Write concise English search keywords with exact month/year dates for global tech/AI/finance topics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Clean search query with dates (e.g. 'AI model launches Google Gemini OpenAI Anthropic November 2026')."},
                "topic": {"type": "string", "enum": ["news", "general"], "description": "Use 'news' for recent news/launches, 'general' for reference/background."},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year", "none"], "description": "Recency filter (default 'week' for news)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "generate_slide_visual",
        "description": (
            "Generate a bespoke high-resolution editorial illustration or cover artwork using the local Qwen-Image-2.1 "
            "model (ComfyUI, ~55 s) and save it as a PNG inside the project folder so your python-pptx script can embed it. "
            "Use sparingly (1-2 key visuals per deck: e.g. cover hero or section graphic). Describe clean, modern, "
            "text-free editorial artwork (never ask for text inside generated images)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed English visual prompt for clean, text-free editorial artwork."},
                "file": {"type": "string", "description": "Output PNG path inside the project folder (e.g. my-deck/cover_art.png)."},
                "aspect": {"type": "string", "enum": ["16:9", "4:3", "1:1", "3:4"], "description": "Aspect ratio (default '16:9')."},
            },
            "required": ["prompt", "file"],
        },
    },
    {
        "name": "inspect_pptx_visually",
        "description": (
            "Render a `.pptx` file to PNG slides via LibreOffice headless + PyMuPDF, run programmatic geometry checks "
            "(blank slides, text-box overlaps, out-of-bounds shapes, tiny fonts, leaked `[1]` citation brackets), and "
            "optionally run local Gemma 4 Vision QA to critique layout, contrast, whitespace, and visual polish slide by slide. "
            "ALWAYS call this after generating a bespoke `.pptx`, fix every issue reported, and re-check before publishing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to the `.pptx` file inside the projects folder."},
                "vision_qa": {"type": "boolean", "description": "Run local vision model critique on rendered slide PNGs (default true)."},
            },
            "required": ["file"],
        },
    },
    {
        "name": "publish_pptx",
        "description": (
            "Publish the final verified `.pptx` deck so the user can download it immediately from both PC and phone "
            "(copies `.pptx` + renders a visual contact-sheet preview PNG to the local file server on :4001 / :8443). "
            "Always call this as the final step and include the returned download link in your answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path to the final `.pptx` file inside the projects folder."},
            },
            "required": ["file"],
        },
    },
]


def log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _project_path(file_arg: str, exts: tuple[str, ...]) -> Path:
    p = Path(str(file_arg or "").strip().strip('"'))
    if not p.name:
        raise ValueError("file path is required")
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if p.suffix.lower() not in exts:
        raise ValueError(f"file must end with one of: {', '.join(exts)}")
    if ROOT != p and ROOT not in p.parents:
        raise ValueError(f"file must be inside the projects folder {ROOT}")
    return p


def _find_soffice() -> Path | None:
    for cand in SOFFICE_CANDIDATES:
        if cand.is_file():
            return cand
    found = shutil.which("soffice") or shutil.which("soffice.exe")
    return Path(found) if found else None


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "deck")[:50]


def render_pptx_to_pngs(pptx_path: Path, out_dir: Path, dpi: int = 150) -> tuple[list[Path], Path | None]:
    """Convert `.pptx` -> PDF (LibreOffice headless) -> slide PNGs (PyMuPDF) + 2-column contact sheet PNG."""
    import pymupdf  # PyMuPDF
    from PIL import Image, ImageDraw

    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice (soffice.exe) is not installed")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo_pptx_") as tmp_str:
        tmp = Path(tmp_str)
        profile_dir = tmp / "profile"
        profile_uri = profile_dir.resolve().as_uri()
        cmd = [
            str(soffice),
            f"-env:UserInstallation={profile_uri}",
            "--headless",
            "--norestore",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            str(tmp),
            str(pptx_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        pdf_path = tmp / (pptx_path.stem + ".pdf")
        if r.returncode != 0 or not pdf_path.is_file():
            raise RuntimeError(f"LibreOffice conversion failed (rc={r.returncode}): {(r.stderr or r.stdout)[:300]}")

        doc = pymupdf.open(str(pdf_path))
        slide_pngs: list[Path] = []
        zoom = dpi / 72.0
        mat = pymupdf.Matrix(zoom, zoom)
        try:
            for i, page in enumerate(doc, 1):
                pix = page.get_pixmap(matrix=mat, alpha=False)
                png_path = out_dir / f"slide_{i:02d}.png"
                pix.save(str(png_path))
                slide_pngs.append(png_path)
        finally:
            doc.close()

    if not slide_pngs:
        return [], None

    # Build a crisp 2-column contact sheet PNG for fast overview
    cols = 2 if len(slide_pngs) > 1 else 1
    rows = (len(slide_pngs) + cols - 1) // cols
    thumb_w, thumb_h = 640, 360
    pad, label_h = 20, 26
    sheet = Image.new(
        "RGB",
        (cols * thumb_w + (cols + 1) * pad, rows * (thumb_h + label_h) + (rows + 1) * pad),
        (18, 22, 32),
    )
    draw = ImageDraw.Draw(sheet)
    for idx, png in enumerate(slide_pngs):
        r_i, c_i = divmod(idx, cols)
        x = pad + c_i * (thumb_w + pad)
        y = pad + r_i * (thumb_h + label_h + pad)
        with Image.open(png) as im:
            im_r = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            sheet.paste(im_r, (x, y + label_h))
        draw.rectangle([x - 1, y + label_h - 1, x + thumb_w, y + label_h + thumb_h], outline=(70, 82, 105), width=1)
        draw.text((x + 4, y + 4), f"Slide {idx + 1}", fill=(226, 232, 240))
    sheet_path = out_dir / "contact_sheet.png"
    sheet.save(str(sheet_path), optimize=True)
    return slide_pngs, sheet_path


def audit_pptx_geometry(pptx_path: Path) -> dict:
    """Programmatic OOXML checks: dimensions, blank slides, text overlaps, bounds, tiny fonts, leaked citations."""
    from pptx import Presentation

    prs = Presentation(str(pptx_path))
    emu_in = 914400.0
    sw = round(prs.slide_width / emu_in, 3)
    sh = round(prs.slide_height / emu_in, 3)
    issues: list[str] = []
    if abs(sw - 13.333) > 0.2 or abs(sh - 7.5) > 0.2:
        issues.append(
            f"CRITICAL: Deck is {sw}x{sh} in (NOT 16:9)! Set prs.slide_width = Inches(13.333) and "
            "prs.slide_height = Inches(7.5) immediately after Presentation()."
        )

    slide_summaries: list[dict] = []
    for idx, slide in enumerate(prs.slides, 1):
        text_boxes = []
        total_chars = 0
        min_font_pt = None
        cited = False
        has_visual = False

        for shape in slide.shapes:
            left = shape.left / emu_in
            top = shape.top / emu_in
            width = shape.width / emu_in
            height = shape.height / emu_in
            right, bottom = left + width, top + height
            area_frac = (width * height) / max(sw * sh, 1.0)

            if shape.shape_type == 13 or shape.has_chart or shape.has_table:  # PICTURE=13
                has_visual = True
            if shape.shape_type == 13 and width > 0.1 and height > 0.1:
                try:
                    iw, ih = shape.image.size
                    if iw > 0 and ih > 0:
                        img_ar = iw / ih
                        box_ar = width / height
                        ratio = max(box_ar / img_ar, img_ar / box_ar)
                        if ratio > 1.35:
                            issues.append(
                                f"Slide {idx}: image is visibly stretched/squashed (natural ratio {img_ar:.2f} vs "
                                f"placed box {box_ar:.2f}); pass ONLY width= OR ONLY height= to add_picture() and "
                                "match matplotlib figsize to the card aspect ratio."
                            )
                except Exception:
                    pass

            txt = ""
            if shape.has_text_frame:
                txt = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()).strip()
                total_chars += len(txt)
                if _CITE_BRACKET_RE.search(txt):
                    cited = True
                for p in shape.text_frame.paragraphs:
                    for run in p.runs:
                        if run.font.size is not None and top < sh - 0.7:
                            pt = round(run.font.size.pt, 1)
                            if min_font_pt is None or pt < min_font_pt:
                                min_font_pt = pt
            elif shape.has_table:
                dup_cells = []
                cell_texts = []
                for row in shape.table.rows:
                    for c in row.cells:
                        ct = c.text.strip()
                        if ct:
                            cell_texts.append(ct)
                            if len(ct) >= 4 and len(ct) % 2 == 0 and ct[: len(ct) // 2] == ct[len(ct) // 2 :]:
                                dup_cells.append(ct[:24])
                if dup_cells:
                    issues.append(
                        f"Slide {idx}: table cells have duplicated text ({', '.join(dup_cells[:3])}) — do NOT set "
                        "both cell.text = val AND p.add_run().text = val; clear p.text = '' before p.add_run()."
                    )
                txt = " ".join(cell_texts)
                total_chars += len(txt)

            # Bounds check on meaningful content shapes (ignore intentional full-bleed backgrounds & bottom footer strip)
            if txt and area_frac < 0.75:
                if right > sw + 0.15 or bottom > sh + 0.15 or left < -0.15 or top < -0.15:
                    issues.append(f"Slide {idx}: text box '{txt[:35]}' extends outside slide bounds ({left:.2f},{top:.2f}..{right:.2f},{bottom:.2f}).")
                if top < sh - 0.7:
                    text_boxes.append((left, top, right, bottom, txt[:40], area_frac))

        # Check pairwise overlaps between text boxes (ignoring container cards that fully enclose a smaller label)
        for i in range(len(text_boxes)):
            l1, t1, r1, b1, n1, _ = text_boxes[i]
            for j in range(i + 1, len(text_boxes)):
                l2, t2, r2, b2, n2, _ = text_boxes[j]
                ix = max(0.0, min(r1, r2) - max(l1, l2))
                iy = max(0.0, min(b1, b2) - max(t1, t2))
                if ix > 0.15 and iy > 0.12:
                    inter = ix * iy
                    a1 = max((r1 - l1) * (b1 - t1), 1e-6)
                    a2 = max((r2 - l2) * (b2 - t2), 1e-6)
                    if inter / min(a1, a2) > 0.18:
                        issues.append(f"Slide {idx}: overlapping text boxes '{n1}' and '{n2}' ({ix:.2f}x{iy:.2f} in).")

        if idx > 1 and total_chars < 45 and not has_visual:
            issues.append(f"Slide {idx}: nearly empty ({total_chars} chars, no picture/chart/table).")
        if min_font_pt is not None and min_font_pt < 10.0:
            issues.append(f"Slide {idx}: very small font ({min_font_pt} pt); keep body text >= 12 pt and footers >= 9-10 pt.")
        if cited:
            issues.append(f"Slide {idx}: raw citation bracket like [1]/[5] found in slide text — remove inline brackets.")

        slide_summaries.append({
            "slide": idx,
            "shapes": len(slide.shapes),
            "chars": total_chars,
            "has_visual": has_visual,
            "min_font_pt": min_font_pt,
        })

    return {
        "slide_count": len(prs.slides),
        "dimensions_in": f"{sw}x{sh}",
        "slides": slide_summaries,
        "issues": issues,
    }


def _encode_png_for_vision(png_path: Path, max_w: int = 960) -> str:
    from PIL import Image

    with Image.open(png_path) as im:
        im = im.convert("RGB")
        if im.width > max_w:
            h = max(1, int(im.height * (max_w / im.width)))
            im = im.resize((max_w, h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def vision_critique_slides(slide_pngs: list[Path]) -> str:
    """Send rendered slide images to local Gemma 4 12B Vision via router :4000 (`fast` tier) for art-director QA."""
    content = [
        {
            "type": "text",
            "text": (
                "You are a ruthless presentation art director reviewing rendered PowerPoint slides (in order: Slide 1, "
                "Slide 2, ...). For EACH slide, inspect carefully for:\n"
                "1) Text overflow, clipped words, or text overlapping borders/icons/other boxes\n"
                "2) Poor contrast (dark text on dark bg or faint text)\n"
                "3) Excessive empty space or monotonous wall-of-text layout\n"
                "4) Misaligned cards, awkward wrapping, or clipped charts/labels\n"
                "Reply concisely slide by slide: either 'Slide N: PASS' or 'Slide N: FIX — <exact concrete visual fix>'. "
                "Finish with an overall score out of 10 and the top 1-2 highest-impact visual upgrades."
            ),
        }
    ]
    # Cap to 8 individual slides so vision context stays sharp and fast
    for idx, png in enumerate(slide_pngs[:8], 1):
        content.append({"type": "text", "text": f"Slide {idx}:"})
        content.append({"type": "image_url", "image_url": {"url": _encode_png_for_vision(png)}})

    body = {
        "model": "fast",
        "stream": False,
        "temperature": 0.2,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": content}],
    }
    conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=180)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            return f"(vision QA skipped: router HTTP {resp.status}: {raw[:200].decode('utf-8', 'replace')})"
        data = json.loads(raw)
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        return f"(vision QA skipped: {e.__class__.__name__}: {e})"
    finally:
        conn.close()


def tool_search_slide_facts(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    topic = str(args.get("topic") or "news").strip().lower()
    if topic not in ("news", "general"):
        topic = "news"
    tr = str(args.get("time_range") or ("week" if topic == "news" else "none")).strip().lower()
    time_range = tr if tr in ("day", "week", "month", "year") else None
    results, images = web_search.search_rich(query, count=6, topic=topic, time_range=time_range)
    lines = [f"Search ({topic}, time_range={time_range or 'any'}) for: {query}\n"]
    for i, r in enumerate(results, 1):
        pub = web_search._format_pub_date(r.get("published"))
        lines.append(f"[{i}] ({pub or 'undated'}) {r.get('title', '')}\nURL: {r.get('url', '')}\n{(r.get('content') or '')[:1600]}\n")
    if images:
        lines.append("VERIFIED EDITORIAL PHOTOS (direct HTTPS URLs you can download via urllib/requests and embed in slides):")
        for idx, img in enumerate(images, 1):
            lines.append(f"  - Photo {idx}: {img.get('description', '')}\n    URL: {img['url']}")
    log(f"search_slide_facts: {query!r} ({topic}/{time_range}) -> {len(results)} results, {len(images)} photos")
    return "\n".join(lines)


def tool_generate_slide_visual(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    out_path = _project_path(args.get("file"), (".png", ".jpg", ".jpeg"))
    aspect = str(args.get("aspect") or "16:9").strip()
    if aspect not in ("16:9", "4:3", "1:1", "3:4"):
        aspect = "16:9"
    body = {
        "model": "qwen-image-2.1",
        "prompt": prompt,
        "aspect_ratio": aspect,
        "megapixels": 1.0,
        "response_format": "b64_json",
    }
    if not _lock.acquire(timeout=300):
        raise RuntimeError("another pptx_studio job is running")
    try:
        t0 = time.time()
        conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=300)
        try:
            conn.request("POST", "/v1/images/generations", json.dumps(body).encode(), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        if resp.status != 200:
            raise RuntimeError(f"image generation failed (HTTP {resp.status}): {raw[:300].decode('utf-8', 'replace')}")
        data = json.loads(raw)
        b64 = ((data.get("data") or [{}])[0]).get("b64_json")
        if not b64:
            raise RuntimeError("no image data returned")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(b64))
        secs = round(time.time() - t0, 1)
    finally:
        _lock.release()
    log(f"generate_slide_visual: {out_path} ({out_path.stat().st_size} B, {secs}s)")
    return f"Saved bespoke visual to `{out_path}` ({out_path.stat().st_size // 1024} KB, {secs} s, aspect {aspect})."


def tool_inspect_pptx_visually(args: dict) -> str:
    pptx_path = _project_path(args.get("file"), (".pptx",))
    if not pptx_path.is_file():
        raise FileNotFoundError(f"File not found: {pptx_path}")
    vision_qa = bool(args.get("vision_qa", True))
    if not _lock.acquire(timeout=300):
        raise RuntimeError("another pptx_studio job is running")
    try:
        t0 = time.time()
        audit = audit_pptx_geometry(pptx_path)
        preview_dir = pptx_path.parent / f".{pptx_path.stem}_preview"
        slide_pngs, sheet_path = render_pptx_to_pngs(pptx_path, preview_dir)
        critique = vision_critique_slides(slide_pngs) if (vision_qa and slide_pngs) else "(vision QA disabled)"
        secs = round(time.time() - t0, 1)
    finally:
        _lock.release()

    lines = [
        f"Inspected `{pptx_path.name}` ({audit['slide_count']} slides, {audit['dimensions_in']} in) in {secs} s.",
        f"Rendered {len(slide_pngs)} slide PNGs in `{preview_dir}`" + (f" (contact sheet: `{sheet_path}`)" if sheet_path else "") + ".",
        "",
        "### Programmatic Geometry Audit",
    ]
    if audit["issues"]:
        lines.extend(f"- ISSUE: {iss}" for iss in audit["issues"])
    else:
        lines.append("- PASS: No geometry overlaps, out-of-bounds text, tiny fonts, or raw citations detected.")
    for s in audit["slides"]:
        lines.append(
            f"  - Slide {s['slide']}: {s['shapes']} shapes, {s['chars']} chars, "
            f"visual={'yes' if s['has_visual'] else 'no'}, min_font={s['min_font_pt']} pt"
        )
    lines.extend(["", "### Local Vision QA Critique (Gemma 4 Vision)", critique])
    log(f"inspect_pptx_visually: {pptx_path.name} -> {len(audit['issues'])} geo issues, {secs}s")
    return "\n".join(lines)


def tool_publish_pptx(args: dict) -> str:
    pptx_path = _project_path(args.get("file"), (".pptx",))
    if not pptx_path.is_file():
        raise FileNotFoundError(f"File not found: {pptx_path}")
    DECKS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"{_slug(pptx_path.stem)}_{stamp}"
    dest_pptx = DECKS_DIR / f"{base_name}.pptx"
    shutil.copy2(pptx_path, dest_pptx)

    # Render contact sheet preview alongside the published deck for instant visual inspection
    preview_url = None
    slide_count = 0
    try:
        with tempfile.TemporaryDirectory(prefix="pub_pptx_") as tmp_str:
            pngs, sheet = render_pptx_to_pngs(dest_pptx, Path(tmp_str), dpi=130)
            slide_count = len(pngs)
            if sheet and sheet.is_file():
                dest_preview = DECKS_DIR / f"{base_name}_preview.png"
                shutil.copy2(sheet, dest_preview)
                preview_url = f"{html_files.public_base()}/decks/{quote(dest_preview.name)}"
    except Exception as e:
        log(f"publish_pptx preview warning: {e}")

    base = html_files.public_base()
    pptx_url = f"{base}/decks/{quote(dest_pptx.name)}"
    kb = max(1, dest_pptx.stat().st_size // 1024)
    log(f"publish_pptx: {dest_pptx.name} ({kb} KB, {slide_count} slides)")
    out = [
        f"Published editable PowerPoint ({slide_count or '?'} slides, {kb} KB):",
        f"- **Download `.pptx` (PC & phone):** [{dest_pptx.name}]({pptx_url})",
        f"- **Local file:** `{dest_pptx}`",
    ]
    if preview_url:
        out.append(f"- **Slide overview preview:** ![Slide preview]({preview_url})")
    return "\n".join(out)


HANDLERS = {
    "search_slide_facts": tool_search_slide_facts,
    "generate_slide_visual": tool_generate_slide_visual,
    "inspect_pptx_visually": tool_inspect_pptx_visually,
    "publish_pptx": tool_publish_pptx,
}


def reply(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:
            continue
        if method == "initialize":
            reply(
                msg_id,
                {
                    "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pptx_studio", "version": "1.0.0"},
                },
            )
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            fn = HANDLERS.get(name)
            if not fn:
                reply(msg_id, {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True})
                continue
            try:
                text, is_error = fn(params.get("arguments") or {}), False
            except Exception as e:
                text, is_error = f"{name} failed: {e}", True
                log(f"{name} error: {e}")
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
        else:
            reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()
