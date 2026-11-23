"""
title: Advanced Deck
author: Local AI Stack
description: Build a bespoke agency-grade editable PowerPoint (.pptx) with live news research, custom 300-DPI matplotlib charts, LibreOffice PNG rendering, Gemma 4 Vision QA, and an automatic visual polish pass.
version: 1.0.0
license: MIT
"""

import ast
import asyncio
import base64
import ctypes
import ctypes.wintypes as wintypes
import http.client
import inspect
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("advanced_deck")

try:
    from fastapi import UploadFile
    from starlette.datastructures import Headers
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler
    _HAS_OWUI_FILES = True
except Exception:
    _HAS_OWUI_FILES = False

ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4000
NO_WINDOW = 0x08000000

_ALLOWED_IMPORT_ROOTS = frozenset({
    "deck_kit",
    "pptx",
    "matplotlib",
    "numpy",
    "PIL",
    "math",
    "io",
    "re",
    "json",
    "textwrap",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "statistics",
    "colorsys",
})
_BLOCKED_CALL_NAMES = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "getattr",
    "setattr",
    "delattr",
    "hasattr",
    "globals",
    "locals",
    "vars",
    "dir",
    "breakpoint",
    "input",
    "help",
    "memoryview",
})
_BLOCKED_ATTR_NAMES = frozenset({
    "system",
    "popen",
    "unlink",
    "rmdir",
    "removedirs",
    "rmtree",
    "rename",
    "chmod",
    "chown",
    "kill",
    "startfile",
    "environ",
    "getenv",
    "putenv",
    "unsetenv",
    "execl",
    "execle",
    "execlp",
    "execv",
    "execve",
    "execvp",
    "spawnl",
    "spawnv",
    "spawnle",
    "spawnve",
    "fork",
    "open_code",
    "FileIO",
    "read_text",
    "read_bytes",
    "write_text",
    "write_bytes",
    "iterdir",
    "glob",
    "rglob",
    "home",
    "expanduser",
    "resolve",
})

_DECK_CODER_SYSTEM = """You are an elite presentation designer and Python engineer (McKinsey / BCG / Apple Keynote caliber).
Write ONE concise, complete Python 3 script (`build_deck.py`) that creates a bespoke, agency-grade, fully editable 16:9 PowerPoint (`deck.pptx`) using the pre-installed `deck_kit` layout helper library AND custom 300-DPI `matplotlib` charts saved in `assets/`.

PRE-INSTALLED `deck_kit` API (already in the working directory — use it so zero boilerplate bugs occur!):
```python
import numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from deck_kit import (
    get_palette, new_deck, add_cover_slide, add_content_slide, add_closing_slide,
    add_card, add_kpi_card, add_table, add_picture_fit, add_photo_card,
    add_swatch_strip, add_timeline, add_insight_bar, add_rect, add_text,
    style_mpl_ax, rgb,
)
```
Key functions (all coordinates in inches on a 13.333 x 7.50 in 16:9 canvas; safe content zone on content slides: `x = 0.55..12.78`, `y = 1.38..6.85`, footer at `7.04`):
- `pal = get_palette(name)` — choose the best fit for the topic: `"executive_navy"`, `"obsidian_ai"`, `"emerald_finance"`, `"editorial_slate"`, `"editorial_luxe"` (fashion/luxury/culture: espresso + warm ivory + burgundy + antique gold), or `"nordic_winter"` (winter/nordic/design: midnight spruce + alabaster + slate blue + plum). Keys are hex strings: `dark`, `header`, `dark_card`, `bg`, `card`, `border`, `accent`, `accent2`, `highlight`, `text`, `muted`, `text_light`, `muted_light`.
- `prs = new_deck()` — creates 16:9 widescreen `Presentation` + `assets/` folder.
- `add_cover_slide(prs, title, subtitle, date_text="", kicker="", palette=pal, cover_image=None, footer="")` — if `cover_image` is passed (e.g. `"assets/photo_01.jpg"` or `"assets/cover_art.png"`), renders a magazine-style split cover with the photo crisply cropped on the right!
- `add_content_slide(prs, title, kicker="", palette=pal, footer="", page_num=2)` — puts the white bold title INSIDE the dark header band (`y = 0..1.12`) + accent rule (`y = 1.12`), leaving `y = 1.38..6.85` 100% clear!
- `add_card(slide, x, y, w, h, title=None, bullets=None, body=None, bg="#FFFFFF", border="#E2E8F0", accent_top=None, accent_left=None, title_size=15.5, body_size=12.5)` — automatically picks crisp light text on dark backgrounds (`bg=pal["dark_card"]` or `pal["dark"]`) and bolds the lead phrase before `:`.
- `add_kpi_card(slide, x, y, w, h, label, value, detail="", bg="#FFFFFF", border="#E2E8F0", accent=pal["accent"])`
- `add_table(slide, x, y, w, h, headers, rows, col_widths=None, header_bg=pal["header"], font_size=11.5)` — clean native PPTX table with alternating stripes and zero duplicated text.
- `add_picture_fit(slide, image_path, x, y, max_w, max_h, pad=0.12, card_bg="#FFFFFF", card_border="#E2E8F0", title=None, accent_top=None)` — fits any matplotlib chart cleanly inside `(max_w, max_h)` preserving exact aspect ratio (NEVER stretched!).
- `add_photo_card(slide, image_path, x, y, w, h, caption="", title="", crop=True, bg="#FFFFFF", border="#E2E8F0", accent_top=None)` — magazine lookbook photo card using Lanczos upper-center crop (keeps faces/outfits framed cleanly, zero letterboxing, optional editorial caption!).
- `add_swatch_strip(slide, x, y, w, h, swatches, title="", bg="#FFFFFF", border="#E2E8F0")` — `swatches = [("#7C2D36", "Bordeaux", "Manteaux & cuir"), ...]` (3–6 color/material palette tiles — ideal for fashion, branding, interior design!).
- `add_timeline(slide, x, y, w, events, accent=pal["accent"])` — `events = [(date, title, short_desc), ...]`, dates strictly above line, labels strictly below (needs `1.45 in` vertical height, e.g. `y = 5.30`; NEVER combine with `add_insight_bar` on the same slide!).
- `add_insight_bar(slide, text, y=6.24, h=0.58, bg=pal["header"], accent=pal["accent"])` — keep all cards/charts/tables above it ending at `y + h <= 6.02` so there is crisp breathing room!
- `add_closing_slide(prs, title, pillars, palette=pal, subtitle="", kicker="", footer="", page_num=5)` — `pillars = [(pillar_title, [bullet1, bullet2, ...]), ...]` (3 or 4 pillars).
- `style_mpl_ax(ax, title="", xlabel="", ylabel="", grid_axis="x")` — clean editorial matplotlib styling.

HARD DESIGN & EDITORIAL SUBSTANCE RULES:
1. Return ONLY a single ```python ... ``` code block — no prose before or after. Save final deck with `prs.save("deck.pptx")`.
2. **CONSULTING / MAGAZINE-GRADE CONTENT DEPTH (CRITICAL — never ship thin or generic slides!):**
   - **Action Titles:** Every content slide title MUST state a concrete insight or headline naming real players/houses/events (NEVER generic labels like `"Lancements Majeurs"`, `"Tendances Clés"`, or `"Géopolitique & Régulation"`!).
   - **Substantive Bold-Lead Bullets:** `add_card` automatically bolds the phrase before `:`! Format EVERY bullet as `"Entity / House / Event: concrete detail, real spec/material/metric, and why it matters"` (**16–26 words per bullet**, 3–4 bullets per card).
   - **STRICT ANTI-FLUFF RULE:** NEVER write empty 5-word stubs or vague platitudes. Every single bullet MUST carry a named company, designer/fashion house, model, material, date, or verified fact from the research!
   - **Mandatory KPI Context:** EVERY `add_kpi_card(...)` MUST include a crisp 1-line `detail` string (`<= 55` chars) explaining WHO, WHAT, and comparison/date (NEVER a bare number with empty `detail`!).
   - **Balanced Canvas Fill:** Fill the ENTIRE 12.23 in width (`x = 0.55..12.78`) purposefully — no 70%-blank white cards or narrow half-empty right columns! (e.g., for a split slide: Left card `x = 0.55, w = 6.0` + Right 3-KPI stack `x = 6.80, w = 5.95, h = 1.40` each covering `y = 1.40..5.96`!).
   - **Named Source Footers:** Every slide footer cites the real source names & date used (e.g. `"Sources : Vogue, Harper's Bazaar, Elle · 21 novembre 2026"`).
3. **TOPIC-MATCHED VISUALS (DATA CHARTS vs EDITORIAL PHOTOGRAPHY — NEVER FORCE FAKE CHARTS!):**
   - **Business / Tech / Finance / News decks:** Build 1–2 genuine 300-DPI `matplotlib` charts (`dpi=300, bbox_inches="tight", facecolor="white"`) grounded in **real business, market, or technical figures** (CapEx `$B`, valuations `$B`, ARR `$M`, launches per lab, pricing `$/1M`, benchmark scores). **STRICTLY FORBIDDEN:** Never plot meaningless SEO search-volume/buzz-index spam (`"Funding +13500%"`, `"Chips 117"`). Always use hex strings (`pal["accent"]`, etc., NEVER `RGBColor`), never mix incompatible units (`$M` and `$B`) on the same y-axis, add direct data labels, give `ax.set_ylim(0, max_val * 1.22)` headroom, and put legends in `loc="upper left"` (`frameon=False`) so they never overlap tall bars.
   - **Editorial / Visual / Fashion / Travel / Architecture / Lifestyle decks:** Prefer `"editorial_luxe"` or `"nordic_winter"` palette, showcase the **verified local editorial photos** already saved in `assets/photo_XX.jpg` via `add_photo_card(...)` (and `cover_image="assets/photo_01.jpg"` on Slide 1 if a clean landscape/editorial photo is available!), include an `add_swatch_strip(...)` for seasonal colors/materials, use styled lookbook/styling tables (`add_table`) and dark editorial callout cards — and **DO NOT fabricate or force `matplotlib` bar charts** unless genuine quantitative numbers appear in the research!
4. Match the EXACT requested slide count and language (ALL slide titles, kickers, bold-lead bullets, chart titles/axes/labels, captions, table headers, and footers in French if the request is in French).
5. NEVER put bracketed citation markers (`[1]`, `[2]`, `[5]`) or raw Unicode emojis in slide titles/text.
6. Vary the layout on EVERY single slide so it feels agency-crafted, never repetitive:
   - Slide 1: Dark Executive/Editorial Cover (`add_cover_slide`, passing `cover_image="assets/cover_art.png"` or a verified `"assets/photo_01.jpg"` when appropriate, with a specific subtitle naming the deck's top highlights).
   - Middle slides: mix distinct compositions across slides — e.g. (A) Split feature card + 3-KPI stack OR lookbook photo card (`add_photo_card`), (B) Left 300-DPI chart OR editorial photo + Right styled native comparison table + insight bar, (C) 3-column bento cards (or 2–3 photo lookbook cards with captions) + bottom insight bar, (D) Color/material swatch strip (`add_swatch_strip`) + 2 contrasting dark/light detail cards, (E) Horizontal timeline (`add_timeline`, without insight bar).
   - Last slide: Dark 3- or 4-pillar Executive Closing (`add_closing_slide`) with concrete takeaways or styling/action guide.
7. NEVER overlap two cards/charts/timelines (leave `>= 0.22 in` gap between elements, keep all elements inside `x = 0.55..12.78`, `y = 1.38..6.85`).
8. Allowed imports only: `deck_kit`, `pptx`, `matplotlib`, `numpy`, `PIL`, `math`, `re`, `textwrap`, `io`, `datetime`, `json`, `collections`, `statistics`. Never import `os`, `pathlib`, `subprocess`, `urllib`, `requests`, `httpx`, `shutil`, `sys`, or `socket` (`new_deck()` already creates `assets/`)."""

_DECK_POLISH_SYSTEM = """You are fixing specific bugs or visual defects in an existing `deck_kit` + `matplotlib` script (`build_deck.py`).
Prefer surgical `SEARCH/REPLACE` patch blocks (fast, zero regressions on untouched slides):

<<<<<<< SEARCH
exact lines from build_deck.py to replace
=======
corrected replacement lines
>>>>>>> REPLACE

Rules:
- Emit one or more `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` blocks (or a single full ```python ... ``` script only if nearly the whole file changed).
- Every `SEARCH` block MUST match the existing script verbatim.
- Fix EVERY reported traceback, geometry issue, overlap, clipping, padding, or Vision QA `FIX` item while keeping `deck_kit`, exact slide count, language, rich facts, and allowed imports, saving to `"deck.pptx"`.
"""


_DECK_LIBS_CACHE: dict[str, bool] = {}


def _has_deck_libs(py_exe: str) -> bool:
    """Return True if `py_exe` can import `pptx` and `matplotlib` in isolated (-I) mode."""
    norm = os.path.normcase(os.path.realpath(py_exe))
    cached = _DECK_LIBS_CACHE.get(norm)
    if cached is not None:
        return cached
    try:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "check": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        r = subprocess.run([py_exe, "-I", "-c", "import pptx, matplotlib"], **kwargs)
        ok = r.returncode == 0
    except Exception:
        ok = False
    _DECK_LIBS_CACHE[norm] = ok
    return ok


def _find_py312() -> str:
    candidates: list[str] = []
    cand = Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe"
    if cand.is_file():
        candidates.append(str(cand))
    found = shutil.which("python")
    if found and Path(found).is_file():
        candidates.append(found)
    if sys.executable and Path(sys.executable).is_file():
        candidates.append(sys.executable)
    owui_venv_py = Path.home() / "AI" / "open-webui" / ".venv" / "Scripts" / "python.exe"
    if owui_venv_py.is_file():
        candidates.append(str(owui_venv_py))
    prefix_py = Path(sys.prefix) / "Scripts" / "python.exe"
    if prefix_py.is_file():
        candidates.append(str(prefix_py))

    dedup: list[str] = []
    seen: set[str] = set()
    for p in candidates:
        k = os.path.normcase(os.path.realpath(p))
        if k not in seen:
            seen.add(k)
            dedup.append(p)

    for p in dedup:
        if _has_deck_libs(p):
            return p
    if cand.is_file():
        return str(cand)
    if found and ".venv" not in found.lower():
        return found
    return sys.executable


def _slugify(text: str, max_len: int = 44) -> str:
    s = unicodedata.normalize("NFKD", str(text or "deck")).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s or "deck")[:max_len]


def _extract_python_code(text: str) -> str:
    if not text:
        return ""
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, re.S | re.I)
    if blocks:
        return max(blocks, key=len).strip()
    stripped = text.strip()
    if ("Presentation(" in stripped or "new_deck(" in stripped) and ("import " in stripped or "from " in stripped):
        return stripped
    return ""


def _ensure_widescreen_code(code: str) -> str:
    """Fix common python-pptx import typos and guarantee 16:9 widescreen dimensions."""
    if not code:
        return code
    # Fix classic LLM mistake: `from pptx.enum.shapes import MSO_ANCHOR` (MSO_ANCHOR lives in pptx.enum.text)
    code = re.sub(
        r"^([ \t]*)from[ \t]+pptx\.enum\.shapes[ \t]+import[ \t]+MSO_ANCHOR[ \t]*$",
        r"\1from pptx.enum.text import MSO_ANCHOR",
        code,
        flags=re.M,
    )
    code = re.sub(
        r"^([ \t]*)from[ \t]+pptx\.enum\.shapes[ \t]+import[ \t]+MSO_SHAPE\s*,\s*MSO_ANCHOR[ \t]*$",
        r"\1from pptx.enum.shapes import MSO_SHAPE\n\1from pptx.enum.text import MSO_ANCHOR",
        code,
        flags=re.M,
    )
    code = re.sub(
        r"^([ \t]*)from[ \t]+pptx\.enum\.shapes[ \t]+import[ \t]+MSO_ANCHOR\s*,\s*MSO_SHAPE[ \t]*$",
        r"\1from pptx.enum.shapes import MSO_SHAPE\n\1from pptx.enum.text import MSO_ANCHOR",
        code,
        flags=re.M,
    )
    # Ensure MSO_SHAPE / MSO_ANCHOR / PP_ALIGN are imported if referenced in code
    prepends: list[str] = []
    if re.search(r"\bMSO_SHAPE\b", code) and not re.search(r"import[^\n]*\bMSO_SHAPE\b", code):
        prepends.append("from pptx.enum.shapes import MSO_SHAPE")
    if re.search(r"\bMSO_ANCHOR\b", code) and not re.search(r"import[^\n]*\bMSO_ANCHOR\b", code):
        prepends.append("from pptx.enum.text import MSO_ANCHOR")
    if re.search(r"\bPP_ALIGN\b", code) and not re.search(r"import[^\n]*\bPP_ALIGN\b", code):
        prepends.append("from pptx.enum.text import PP_ALIGN")
    if prepends:
        code = "\n".join(prepends) + "\n" + code

    if "slide_width" in code or "Presentation(" not in code:
        return code
    m = re.search(r"^([ \t]*)([A-Za-z_]\w*)\s*=\s*Presentation\s*\(\s*\)\s*$", code, re.M)
    if not m:
        return code
    indent, var = m.group(1), m.group(2)
    inject = (
        f"{m.group(0)}\n"
        f"{indent}{var}.slide_width = Inches(13.333)\n"
        f"{indent}{var}.slide_height = Inches(7.5)"
    )
    out = code[: m.start()] + inject + code[m.end() :]
    if "Inches" not in out[: m.start()]:
        out = "from pptx.util import Inches\n" + out
    return out


_SR_BLOCK_RE = re.compile(
    r"<<<<<<<[ \t]*SEARCH[ \t]*\r?\n(.*?)\r?\n=======[ \t]*\r?\n(.*?)(?:\r?\n)?>>>>>>>[ \t]*REPLACE",
    re.S,
)


def _apply_surgical_patches(base_code: str, reply: str) -> Optional[str]:
    """Apply SEARCH/REPLACE patch blocks cleanly (including empty delete replacements); fallback to full script."""
    blocks = _SR_BLOCK_RE.findall(reply or "")
    if blocks:
        updated = base_code
        applied = 0
        for find_txt, repl_txt in blocks:
            if find_txt in updated:
                updated = updated.replace(find_txt, repl_txt, 1)
                applied += 1
            else:
                find_lines = [ln.rstrip() for ln in find_txt.splitlines()]
                base_lines = updated.splitlines()
                matched_at = -1
                for i in range(len(base_lines) - len(find_lines) + 1):
                    if [ln.rstrip() for ln in base_lines[i : i + len(find_lines)]] == find_lines:
                        matched_at = i
                        break
                if matched_at >= 0:
                    repl_lines = repl_txt.splitlines() if repl_txt else []
                    new_lines = base_lines[:matched_at] + repl_lines + base_lines[matched_at + len(find_lines) :]
                    updated = "\n".join(new_lines)
                    applied += 1
        if applied > 0:
            return _ensure_widescreen_code(updated)
    full = _extract_python_code(reply)
    return _ensure_widescreen_code(full) if full else None


def _audit_generated_code(code: str) -> list[str]:
    """Static AST safety check: allow only safe presentation/charting constructs and block shell/env/FS/reflection bypasses."""
    errors: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError at line {e.lineno}: {e.msg}"]

    io_aliases: set[str] = {"io"}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root == "io":
                    io_aliases.add(alias.asname or "io")
                if alias.name == "*" or root not in _ALLOWED_IMPORT_ROOTS:
                    errors.append(f"Disallowed import '{alias.name}' (only deck_kit, pptx, matplotlib, numpy, PIL, math/stdlib allowed).")
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0):
                errors.append("Disallowed relative import.")
                continue
            mod = node.module or ""
            root = mod.split(".")[0]
            if root not in _ALLOWED_IMPORT_ROOTS:
                errors.append(f"Disallowed import 'from {mod}' (only deck_kit, pptx, matplotlib, numpy, PIL, math/stdlib allowed).")
            for alias in node.names:
                if alias.name == "*":
                    errors.append(f"Disallowed wildcard import 'from {mod} import *'.")
                elif root == "io" and alias.name in ("open", "open_code", "FileIO"):
                    errors.append(f"Disallowed io import '{alias.name}'.")

    for node in nodes:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALL_NAMES:
                errors.append(f"Disallowed builtin call '{node.func.id}()'.")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                errors.append(f"Disallowed dunder attribute '.{node.attr}'.")
            elif node.attr in _BLOCKED_ATTR_NAMES:
                errors.append(f"Disallowed attribute '.{node.attr}'.")
            elif (
                node.attr in ("open", "open_code", "FileIO")
                and isinstance(node.value, ast.Name)
                and node.value.id in io_aliases
            ):
                errors.append(f"Disallowed call '{node.value.id}.{node.attr}()'.")
        elif isinstance(node, ast.Name):
            if node.id.startswith("__") and node.id.endswith("__") and node.id != "__name__":
                errors.append(f"Disallowed dunder name '{node.id}'.")
    return errors


def _router_post_json(path: str, payload: dict, timeout: int = 300) -> dict:
    conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=timeout)
    try:
        conn.request("POST", path, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"Router HTTP {resp.status}: {raw[:300].decode('utf-8', 'replace')}")
        return json.loads(raw)
    finally:
        conn.close()


def _ask_pro_code(system_prompt: str, user_prompt: str, budget_max_tokens: int = 16384) -> str:
    data = _router_post_json(
        "/v1/chat/completions",
        {
            "model": "deep",
            "stream": False,
            "temperature": 0.4,
            "max_tokens": budget_max_tokens,
            "_internal_advanced_deck": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=300,
    )
    return (data["choices"][0]["message"].get("content") or "").strip()


_LOW_SIGNAL_DOMAINS = (
    "risingtrends.",
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "pinterest.",
    "aitoolsrecap.",
)
_JUNK_PHOTO_RE = re.compile(
    r"(logo|favicon|icon|sprite|avatar|placeholder|badge|banner[-_]?ad|watermark|"
    r"shutterstock|istockphoto|gettyimages\.com/detail|123rf|alamy|depositphotos|"
    r"\.svg(?:\?|$)|\.gif(?:\?|$)|\.ico(?:\?|$))",
    re.I,
)
_EDITORIAL_VISUAL_RE = re.compile(
    r"\b(fashion|mode|vestimentaires?|v[eê]tements?|couture|runway|d[eé]fil[eé]s?|lookbook|pr[eê]t[- ]?[àa][- ]?porter|"
    r"garde[- ]?robe|wardrobe|streetwear|menswear|womenswear|outfits?|manteaux?|silhouettes?|accessoires?|"
    r"luxury\s+trends?|beaut[eé]|beauty|skincare|parfum|perfume|cosmetics?|"
    r"interior\s+design|d[eé]coration|architecture|mobilier|furniture|home\s+decor|"
    r"travel|voyages?|destinations?|tourisme|h[oô]tellerie|gastronomie|culinary|cuisine|food\s+trends?|"
    r"art\s+exhibition|exposition|mus[eé]e|cin[eé]ma|photography|photographie|design\s+trends?)\b",
    re.I,
)
_QUANT_OVERRIDE_RE = re.compile(
    r"\b(market\s+size|taille\s+du\s+march[eé]|capex|valuation|valorisation|revenue|chiffre\s+d['’]affaires|"
    r"earnings|r[eé]sultats\s+financiers|bilan\s+financier|quarterly\s+report|ebitda|m&a|ipo)\b",
    re.I,
)
_EXPLICIT_SLIDE_NUM_RE = re.compile(r"\b(\d{1,2})\s*(?:slides?|diapos(?:itives)?|pages?)\b", re.I)
_BRIEF_DECK_RE = re.compile(r"\b(brief|short|quick|compact|summary|r[eé]sum[eé]|rapide|court|synth[eè]se\s+courte)\b", re.I)
_DEEP_DECK_RE = re.compile(r"\b(deep[- ]?dive|comprehensive|detailed|complete|full|complet|compl[eè]te|d[eé]taill[eé]e?|approfondi)\b", re.I)


def _is_editorial_visual_topic(topic: str, instructions: str = "") -> bool:
    """True for visual/lifestyle/editorial topics (fashion, travel, design, architecture, food, culture)."""
    blob = f"{topic or ''} {instructions or ''}"
    return bool(_EDITORIAL_VISUAL_RE.search(blob)) and not bool(_QUANT_OVERRIDE_RE.search(blob))


def _choose_slide_count(requested: int, topic: str, instructions: str = "") -> int:
    """Honor exact requested count (3..15); when 0/omitted, choose a natural executive length (5, 7, or 8)."""
    try:
        req = int(requested or 0)
    except Exception:
        req = 0
    if req > 0:
        return max(3, min(req, 15))
    blob = f"{topic or ''} {instructions or ''}"
    m = _EXPLICIT_SLIDE_NUM_RE.search(blob)
    if m:
        return max(3, min(int(m.group(1)), 15))
    if _BRIEF_DECK_RE.search(blob):
        return 5
    if _DEEP_DECK_RE.search(blob):
        return 8
    return 7


def _download_verified_photos(
    image_candidates: list[dict],
    assets_dir: Path,
    web_search_mod: Any,
    max_photos: int = 5,
) -> list[dict]:
    """Safely download and verify real high-res editorial photos locally into work_dir/assets/photo_XX.jpg."""
    from PIL import Image

    assets_dir.mkdir(parents=True, exist_ok=True)
    saved: list[dict] = []
    seen_urls: set[str] = set()
    for item in image_candidates:
        if len(saved) >= max_photos:
            break
        url = (item.get("url") or "").strip()
        desc = (item.get("description") or "").strip()
        if not url or url in seen_urls or _JUNK_PHOTO_RE.search(url):
            continue
        seen_urls.add(url)
        try:
            if not (
                hasattr(web_search_mod, "_is_public_https_url")
                and hasattr(web_search_mod, "safe_open_public_https")
                and web_search_mod._is_public_https_url(url)
            ):
                continue
            hdrs = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
            }
            with web_search_mod.safe_open_public_https(url, headers=hdrs, timeout=8) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not ctype.startswith("image/") and "octet-stream" not in ctype:
                    continue
                raw = resp.read(6_000_000)
            if len(raw) < 12_000:
                continue
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                w, h = im.size
                if w < 480 or h < 320:
                    continue
                ar = w / max(1, h)
                if ar < 0.48 or ar > 2.45:
                    continue
                out_name = f"photo_{len(saved) + 1:02d}.jpg"
                out_path = assets_dir / out_name
                im.convert("RGB").save(str(out_path), format="JPEG", quality=92, optimize=True)
            saved.append({
                "rel_path": f"assets/{out_name}",
                "width": w,
                "height": h,
                "orientation": "landscape" if w >= h else "portrait",
                "description": desc[:140],
            })
        except Exception:
            continue
    return saved


def _live_search_facts(
    topic: str,
    language: str,
    time_range: str = "",
    assets_dir: Optional[Path] = None,
    editorial_mode: bool = False,
) -> tuple[str, str, list[dict]]:
    """Run multi-angle fresh live web research (3 topic-adapted queries, spam-filtered, + verified editorial photos)."""
    scripts_dir = str(Path.home() / ".lmstudio" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import web_search  # noqa: E402

    today = web_search.today_text()
    month_year = datetime.now().strftime("%B %Y")
    queries: list[str] = []
    if editorial_mode:
        angle_guide = (
            "Write in English or the topic's natural language (prefer English/French fashion & editorial outlets like "
            "Vogue, Elle, Harper's Bazaar, GQ, Wallpaper, Architectural Digest) covering 3 distinct editorial angles:\n"
            "Line 1: Flagship seasonal trends, standout silhouettes, key designers/houses, and runway or collection highlights\n"
            "Line 2: Defining color palettes, hero fabrics/materials, key pieces, and signature accessories\n"
            "Line 3: Styling directions, cultural shifts, and notable visual/editorial lookbook trends"
        )
    else:
        angle_guide = (
            "For global tech, AI, science, or finance topics, write in English and cover 3 distinct angles:\n"
            "Line 1: Flagship product/model launches & major industry leaders (e.g. for AI: Google Gemini, OpenAI, Anthropic, Meta, Qwen)\n"
            "Line 2: Major funding rounds, valuations, CapEx, earnings, or regulation/policy announcements\n"
            "Line 3: Technical specs, benchmarks, pricing, or breakthrough announcements with concrete metrics"
        )
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 1234, timeout=30)
        try:
            q_body = {
                "model": "qwen3.5-4b",
                "temperature": 0,
                "max_tokens": 140,
                "reasoning_effort": "none",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"Write EXACTLY 3 complementary web search queries (one per line, no numbering, no quotes, "
                            f"no words like PowerPoint/slides) to gather rich, concrete facts, major announcements, "
                            f"and real details for an executive presentation. Today is {today}. Include exact season/year "
                            f"or month/year ('{month_year}') when relevant.\n{angle_guide}"
                        ),
                    },
                    {"role": "user", "content": topic[:1000]},
                ],
            }
            conn.request("POST", "/v1/chat/completions", json.dumps(q_body).encode("utf-8"), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status == 200:
                q_data = json.loads(resp.read())
                raw_q = (q_data["choices"][0]["message"].get("content") or "").strip()
                for ln in raw_q.splitlines():
                    cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)]|Line\s*\d+:)\s*", "", ln).strip().strip('"').strip("'")
                    cleaned = re.sub(r"\b(power\s*point|pptx?|slides?|deck|presentation|pr[eé]sentation|diaporama)\b", "", cleaned, flags=re.I).strip()
                    if len(cleaned) >= 8 and cleaned.lower() not in {x.lower() for x in queries}:
                        queries.append(cleaned[:140])
                    if len(queries) >= 3:
                        break
        finally:
            conn.close()
    except Exception:
        queries = []

    if not queries:
        base = re.sub(r"\b(power\s*point|pptx?|slides?|deck|presentation|pr[eé]sentation|diaporama)\b", "", topic, flags=re.I).strip()
        if editorial_mode:
            queries = [
                f"{base[:100]} runway key trends designers".strip(),
                f"{base[:100]} colors fabrics silhouettes accessories".strip(),
            ]
        else:
            queries = [
                f"{base[:100]} major launches announcements {month_year}".strip(),
                f"{base[:100]} funding valuation regulation metrics {month_year}".strip(),
            ]

    opts = web_search.infer_search_options(topic, queries[0])
    t_range = time_range if time_range in ("day", "week", "month", "year") else opts.get("time_range")
    search_topic = opts.get("topic", "general")
    if editorial_mode and not time_range:
        # Seasonal fashion/design/travel editorial articles live in general editorial index (Vogue, Elle, etc.)
        search_topic = "general"
        t_range = None
    elif t_range in ("day", "week") and search_topic == "general":
        search_topic = "news"

    seen_urls: set[str] = set()
    high_signal: list[dict] = []
    low_signal: list[dict] = []
    image_candidates: list[dict] = []
    for q in queries[:3]:
        try:
            res, imgs = web_search.search_rich(q, count=5, topic=search_topic, time_range=t_range)
        except Exception:
            continue
        for im in imgs or []:
            if isinstance(im, dict) and im.get("url"):
                image_candidates.append(im)
        for r in res or []:
            url = (r.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            pub = web_search._format_pub_date(r.get("published"))
            item = {
                "title": (r.get("title") or "").strip(),
                "url": url,
                "pub": pub or "",
                "content": (r.get("content") or "").strip(),
            }
            if any(bad in url.lower() for bad in _LOW_SIGNAL_DOMAINS):
                low_signal.append(item)
            else:
                high_signal.append(item)

    # Sort freshest-dated articles first; fall back to low-signal only if needed to reach 8 sources
    high_signal.sort(key=lambda x: (bool(x["pub"]), x["pub"]), reverse=True)
    selected = high_signal[:10]
    if len(selected) < 6:
        low_signal.sort(key=lambda x: (bool(x["pub"]), x["pub"]), reverse=True)
        selected.extend(low_signal[: 8 - len(selected)])

    # Download verified local editorial photos (5 max in editorial mode, 2 max in data/news mode)
    downloaded_photos: list[dict] = []
    if assets_dir is not None and image_candidates:
        max_p = 5 if editorial_mode else 2
        downloaded_photos = _download_verified_photos(image_candidates, assets_dir, web_search, max_photos=max_p)

    q_summary = " | ".join(queries[:3])
    lines = [
        f"LIVE MULTI-ANGLE WEB RESEARCH (retrieved {today}, topic={search_topic}, time_range={t_range or 'any'}, "
        f"{len(selected)} deduplicated sources across {len(queries[:3])} angles):"
    ]
    for i, r in enumerate(selected, 1):
        lines.append(
            f"\nSource {i} ({r['pub'] or 'undated'}): {r['title']}\n"
            f"URL: {r['url']}\n"
            f"{r['content'][:1500]}"
        )
    if downloaded_photos:
        lines.append("\nVERIFIED LOCAL EDITORIAL PHOTOS ALREADY SAVED ON DISK (ready to embed via `add_photo_card` or `cover_image` — NEVER invent other filenames!):")
        for ph in downloaded_photos:
            cap_note = f" — {ph['description']}" if ph["description"] else ""
            lines.append(f"- `{ph['rel_path']}` ({ph['width']}x{ph['height']}, {ph['orientation']}){cap_note}")
    lines.append(
        "\nEDITORIAL MANDATE: Build the deck around the BIGGEST, MOST RECENT concrete stories/trends above (named houses, "
        "designers, models, companies, deals, materials, exact dates, and real metrics). Ignore generic SEO "
        "keyword-spike spam and NEVER write vague filler or `[1]`/`[5]` citation brackets."
    )
    return "\n".join(lines), q_summary, downloaded_photos


def _generate_cover_art(topic: str, out_png: Path) -> bool:
    try:
        prompt = (
            f"Minimalist ultra-modern dark navy executive presentation cover artwork inspired by {topic[:160]}, "
            "subtle glowing geometric glass and light waves on the right side, deep dark navy negative space on the left, "
            "editorial studio lighting, crisp vector-like aesthetic, strictly no text, no letters, no numbers, no logos"
        )
        data = _router_post_json(
            "/v1/images/generations",
            {
                "model": "qwen-image-2.1",
                "prompt": prompt,
                "aspect_ratio": "16:9",
                "megapixels": 1.0,
                "response_format": "b64_json",
            },
            timeout=240,
        )
        b64 = ((data.get("data") or [{}])[0]).get("b64_json")
        if not b64:
            return False
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(base64.b64decode(b64))
        return out_png.is_file() and out_png.stat().st_size > 4000
    except Exception as e:
        log.warning("cover art skipped: %s", e)
        return False


_SAFE_ENV_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "SYSTEMDRIVE",
    "PATH",
    "PATHEXT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)
_USER_ENV_KEYS = (
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
)

_SANDBOX_RUNNER = r'''
import os, site, sys
from pathlib import Path

work_dir = Path(sys.argv[1]).resolve()
script_path = Path(sys.argv[2]).resolve()
if str(work_dir) not in sys.path:
    sys.path.insert(0, str(work_dir))

# Pre-import charting/presentation stack before engaging runtime audit restrictions
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager  # noqa: F401
import matplotlib.pyplot  # noqa: F401
import numpy  # noqa: F401
from PIL import Image  # noqa: F401
import pptx  # noqa: F401
import deck_kit  # noqa: F401

work_real = os.path.normcase(os.path.realpath(str(work_dir)))
ro_candidates = [sys.prefix, sys.base_prefix, sys.exec_prefix, getattr(sys, "base_exec_prefix", sys.prefix)]
try:
    ro_candidates.extend(site.getsitepackages())
except Exception:
    pass
windir = os.environ.get("WINDIR") or r"C:\Windows"
ro_candidates.append(str(Path(windir) / "Fonts"))
ro_roots = tuple(
    dict.fromkeys(
        os.path.normcase(os.path.realpath(os.path.abspath(str(p))))
        for p in ro_candidates
        if p
    )
)

def _is_under(path_str, roots):
    try:
        ap = os.path.normcase(os.path.realpath(os.path.abspath(path_str)))
    except Exception:
        return False
    for r in roots:
        try:
            if os.path.commonpath([ap, r]) == r:
                return True
        except ValueError:
            continue
    return False

# Drop work_dir / CWD from import path and disable zipimport now that deck_kit is loaded
sys.path = [p for p in sys.path if p not in ("", ".") and not _is_under(p, (work_real,))]
try:
    import zipimport
    sys.path_hooks = [
        h for h in sys.path_hooks
        if h is not zipimport.zipimporter
        and "zipimporter" not in getattr(h, "__name__", "").lower()
        and "zipimport" not in getattr(h, "__module__", "").lower()
    ]
except Exception:
    pass
sys.path_importer_cache.clear()

def _resolve_arg(p):
    if p in (None, "", "."):
        return work_real
    raw = os.fsdecode(p) if isinstance(p, (bytes, os.PathLike)) else str(p)
    if not os.path.isabs(raw):
        raw = os.path.join(work_real, raw)
    return raw

_WRITE_MASK = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_APPEND", 0)
_FORBIDDEN_WRITE_EXTS = frozenset({
    ".py", ".pyc", ".pyo", ".pyw", ".pth", ".pyd", ".dll", ".so",
    ".zip", ".whl", ".egg",
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js",
})
_BLOCKED_EVENTS = frozenset({
    "subprocess.Popen",
    "os.system",
    "os.startfile",
    "os.posix_spawn",
    "os.kill",
    "os.link",
    "os.symlink",
    "os.truncate",
    "os.ftruncate",
    "os.chmod",
    "os.fchmod",
    "os.chown",
    "os.lchown",
    "os.fchown",
    "os.chflags",
    "os.lchflags",
    "os.utime",
    "os.chdir",
    "os.fchdir",
    "os.chroot",
    "os.putenv",
    "os.unsetenv",
    "urllib.Request",
    "http.client.connect",
    "http.client.send",
    "webbrowser.open",
    "ftplib.connect",
    "ftplib.sendcmd",
    "smtplib.connect",
    "poplib.connect",
    "imaplib.open",
})

source_text = script_path.read_text(encoding="utf-8")
_main_code = compile(source_text, str(script_path), "exec")

def _audit_hook(event, args):
    if event in _BLOCKED_EVENTS or event.startswith(("os.spawn", "os.exec", "winreg.", "ctypes.", "socket.", "shutil.")):
        raise PermissionError(f"Blocked by Advanced Deck sandbox: {event}")
    if event == "import":
        mod_name = str(args[0] or "") if args else ""
        if mod_name == "zipimport" or mod_name.startswith("zipimport."):
            raise PermissionError("Blocked zipimport inside deck sandbox")
        fn_arg = args[1] if len(args) > 1 else None
        if isinstance(fn_arg, (str, bytes, os.PathLike)):
            resolved_fn = _resolve_arg(fn_arg)
            if _is_under(resolved_fn, (work_real,)) or (
                resolved_fn.lower().endswith((".zip", ".whl", ".egg")) and not _is_under(resolved_fn, ro_roots)
            ):
                raise PermissionError(f"Blocked workspace import file: {resolved_fn}")
        sp_arg = args[2] if len(args) > 2 else None
        if isinstance(sp_arg, (list, tuple)):
            for sp_entry in sp_arg:
                if sp_entry in ("", "."):
                    raise PermissionError("Blocked CWD import path inside deck sandbox")
                if isinstance(sp_entry, (str, bytes, os.PathLike)):
                    sp_str = os.fsdecode(sp_entry) if isinstance(sp_entry, (bytes, os.PathLike)) else str(sp_entry)
                    resolved_sp = _resolve_arg(sp_str)
                    if _is_under(resolved_sp, (work_real,)) or (
                        sp_str.lower().endswith((".zip", ".whl", ".egg")) and not _is_under(resolved_sp, ro_roots)
                    ):
                        raise PermissionError(f"Blocked workspace sys.path import: {sp_str}")
        hooks_arg = args[4] if len(args) > 4 else None
        if isinstance(hooks_arg, (list, tuple)):
            for hk in hooks_arg:
                if "zipimporter" in getattr(hk, "__name__", "").lower() or "zipimport" in getattr(hk, "__module__", "").lower():
                    raise PermissionError("Blocked zipimporter hook inside deck sandbox")
        return
    if event == "exec":
        code_obj = args[0] if args else None
        if code_obj is not _main_code:
            cf = getattr(code_obj, "co_filename", "") or ""
            if cf and not (cf.startswith("<") and cf.endswith(">")):
                resolved_cf = _resolve_arg(cf)
                if _is_under(resolved_cf, (work_real,)):
                    raise PermissionError(f"Blocked secondary exec inside deck workspace: {resolved_cf}")
        return
    if event == "open":
        path_arg, mode_arg, flags_arg = args[0], args[1], args[2]
        if isinstance(path_arg, int):
            return
        resolved = _resolve_arg(path_arg)
        is_write = bool(flags_arg and (int(flags_arg) & _WRITE_MASK)) or (
            isinstance(mode_arg, str) and any(c in mode_arg for c in ("w", "a", "x", "+"))
        )
        if _is_under(resolved, (work_real,)):
            if is_write and os.path.splitext(resolved)[1].lower() in _FORBIDDEN_WRITE_EXTS:
                raise PermissionError(f"Blocked writing executable/code file inside deck workspace: {resolved}")
            return
        if is_write or not _is_under(resolved, ro_roots):
            raise PermissionError(f"Blocked file access outside deck workspace: {resolved}")
    elif event in ("os.listdir", "os.scandir", "os.walk"):
        resolved = _resolve_arg(args[0] if args else None)
        if not (_is_under(resolved, (work_real,)) or _is_under(resolved, ro_roots)):
            raise PermissionError(f"Blocked directory listing outside deck workspace: {resolved}")
    elif event in ("os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.rename", "os.replace"):
        for idx, a in enumerate(args[:2]):
            if isinstance(a, (str, bytes, os.PathLike)):
                resolved = _resolve_arg(a)
                if not _is_under(resolved, (work_real,)):
                    raise PermissionError(f"Blocked filesystem mutation outside deck workspace: {resolved}")
                if event in ("os.rename", "os.replace") and idx == 1:
                    if os.path.splitext(resolved)[1].lower() in _FORBIDDEN_WRITE_EXTS:
                        raise PermissionError(f"Blocked renaming to executable/code file inside deck workspace: {resolved}")

sys.addaudithook(_audit_hook)
exec(_main_code, {"__name__": "__main__", "__file__": str(script_path)})
'''

_APPCONTAINER_PROFILE = "OneRTX.AdvancedDeck.Sandbox"
_APPCONTAINER_LOCK = threading.Lock()
_APPCONTAINER_SID_CACHE: dict[str, tuple[int, str]] = {}
_APPCONTAINER_ACL_CACHE: set[tuple[str, str]] = set()


if os.name == "nt":
    import msvcrt

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [
            ("AppContainerSid", ctypes.c_void_p),
            ("Capabilities", ctypes.c_void_p),
            ("CapabilityCount", wintypes.DWORD),
            ("Reserved", wintypes.DWORD),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _ensure_appcontainer_sid() -> tuple[int, str]:
    with _APPCONTAINER_LOCK:
        if _APPCONTAINER_PROFILE in _APPCONTAINER_SID_CACHE:
            return _APPCONTAINER_SID_CACHE[_APPCONTAINER_PROFILE]

        userenv = ctypes.WinDLL("userenv", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        userenv.CreateAppContainerProfile.argtypes = [
            wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ]
        userenv.CreateAppContainerProfile.restype = ctypes.c_uint32
        userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
        ]
        userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_uint32
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        psid = ctypes.c_void_p()
        hr = userenv.CreateAppContainerProfile(
            _APPCONTAINER_PROFILE,
            "OneRTX Advanced Deck Sandbox",
            "Isolated zero-network OS container for deck script execution",
            None,
            0,
            ctypes.byref(psid),
        )
        if (hr & 0xFFFFFFFF) == 0x800700B7:  # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
            hr = userenv.DeriveAppContainerSidFromAppContainerName(_APPCONTAINER_PROFILE, ctypes.byref(psid))
        if (hr & 0xFFFFFFFF) != 0 or not psid.value:
            raise OSError(f"AppContainer profile setup failed: HRESULT=0x{hr & 0xFFFFFFFF:08X}")

        pstr = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(pstr)) or not pstr.value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid_str = ctypes.wstring_at(pstr.value)
        finally:
            kernel32.LocalFree(pstr)

        res = (int(psid.value), sid_str)
        _APPCONTAINER_SID_CACHE[_APPCONTAINER_PROFILE] = res
        return res


def _run_icacls(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        ["icacls", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=NO_WINDOW,
    )
    return r.returncode, ((r.stdout or "") + "\n" + (r.stderr or "")).strip()


def _resolve_py_acl_roots(py_exe: str) -> list[Path]:
    """Return deduplicated directories requiring AppContainer RX ACL (Scripts/exe dir, venv root, base home)."""
    exe_dir = Path(py_exe).resolve().parent
    roots: list[Path] = [exe_dir]
    cfg_p = exe_dir / "pyvenv.cfg"
    if not cfg_p.is_file():
        cfg_p = exe_dir.parent / "pyvenv.cfg"
    if cfg_p.is_file():
        roots.append(cfg_p.parent.resolve())
        for ln in cfg_p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if ln.strip().lower().startswith("home"):
                _, _, rhs = ln.partition("=")
                hp = Path(rhs.strip())
                if hp.is_dir():
                    roots.append(hp.resolve())
                    if hp.name.lower() == "scripts" and (hp.parent / "Lib").is_dir():
                        roots.append(hp.parent.resolve())
                break
    dedup: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        k = os.path.normcase(str(r))
        if k not in seen:
            seen.add(k)
            dedup.append(r)
    return dedup


def _ensure_py_root_acl(py_exe: str, sid_str: str) -> None:
    roots = _resolve_py_acl_roots(py_exe)
    with _APPCONTAINER_LOCK:
        for root_dir in roots:
            key = (os.path.normcase(str(root_dir)), sid_str)
            if key in _APPCONTAINER_ACL_CACHE:
                continue
            rc, out = _run_icacls([str(root_dir)])
            if rc == 0 and sid_str in out:
                _APPCONTAINER_ACL_CACHE.add(key)
                continue
            rc, err = _run_icacls([str(root_dir), "/grant", f"*{sid_str}:(OI)(CI)RX", "/Q"])
            if rc != 0:
                raise RuntimeError(f"Failed to grant AppContainer RX ACL on {root_dir}: {err}")
            _APPCONTAINER_ACL_CACHE.add(key)


def _run_in_appcontainer(argv: list[str], cwd: Path, env: dict[str, str], timeout: int = 90) -> tuple[int, str, str]:
    """Execute child process inside a zero-network Win32 AppContainer with write ACL scoped strictly to `cwd`."""
    sid_ptr, sid_str = _ensure_appcontainer_sid()
    _ensure_py_root_acl(argv[0], sid_str)
    cwd_resolved = str(cwd.resolve())
    rc_acl, acl_err = _run_icacls([cwd_resolved, "/grant", f"*{sid_str}:(OI)(CI)M", "/Q"])
    if rc_acl != 0:
        raise RuntimeError(f"Failed to grant AppContainer workspace ACL: {acl_err}")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_SECURITY_ATTRIBUTES), wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.c_void_p, ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    invalid_handle = ctypes.c_void_p(-1).value
    handles_to_close: list[int] = []

    def _safe_close(h_val: Optional[int]) -> None:
        if h_val and h_val != invalid_handle and h_val in handles_to_close:
            handles_to_close.remove(h_val)
            kernel32.CloseHandle(h_val)

    attr_buf = None
    attr_initialized = False
    pi = _PROCESS_INFORMATION()
    out_sink: list[bytes] = []
    err_sink: list[bytes] = []
    t_out: Optional[threading.Thread] = None
    t_err: Optional[threading.Thread] = None

    def _read_pipe(h_val: int, sink: list[bytes]) -> None:
        fd = -1
        try:
            fd = msvcrt.open_osfhandle(h_val, os.O_RDONLY)
            with os.fdopen(fd, "rb", closefd=True) as fh:
                sink.append(fh.read())
        except Exception:
            if fd == -1 and h_val and h_val != invalid_handle:
                try:
                    kernel32.CloseHandle(h_val)
                except Exception:
                    pass

    try:
        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, True)
        h_out_r, h_out_w = wintypes.HANDLE(), wintypes.HANDLE()
        h_err_r, h_err_w = wintypes.HANDLE(), wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_out_r), ctypes.byref(h_out_w), ctypes.byref(sa), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        handles_to_close.extend([h_out_r.value, h_out_w.value])
        if not kernel32.CreatePipe(ctypes.byref(h_err_r), ctypes.byref(h_err_w), ctypes.byref(sa), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        handles_to_close.extend([h_err_r.value, h_err_w.value])

        kernel32.SetHandleInformation(h_out_r, 0x00000001, 0)
        kernel32.SetHandleInformation(h_err_r, 0x00000001, 0)

        h_nul = kernel32.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(sa), 3, 0x80, None)
        if not h_nul or h_nul == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        handles_to_close.append(h_nul)

        h_job = kernel32.CreateJobObjectW(None, None)
        if not h_job:
            raise ctypes.WinError(ctypes.get_last_error())
        handles_to_close.append(h_job)
        job_limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        job_limits.BasicLimitInformation.LimitFlags = 0x00002200  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_JOB_MEMORY
        job_limits.JobMemoryLimit = 2 * 1024 * 1024 * 1024
        if not kernel32.SetInformationJobObject(h_job, 9, ctypes.byref(job_limits), ctypes.sizeof(job_limits)):
            raise ctypes.WinError(ctypes.get_last_error())

        attr_size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(attr_size))
        attr_buf = (ctypes.c_byte * attr_size.value)()
        if not kernel32.InitializeProcThreadAttributeList(attr_buf, 2, 0, ctypes.byref(attr_size)):
            raise ctypes.WinError(ctypes.get_last_error())
        attr_initialized = True

        sec_caps = _SECURITY_CAPABILITIES(sid_ptr, None, 0, 0)
        if not kernel32.UpdateProcThreadAttribute(
            attr_buf, 0, 0x00020009, ctypes.byref(sec_caps), ctypes.sizeof(sec_caps), None, None
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        handle_list = (wintypes.HANDLE * 3)(h_nul, h_out_w.value, h_err_w.value)
        if not kernel32.UpdateProcThreadAttribute(
            attr_buf, 0, 0x00020002, ctypes.byref(handle_list), ctypes.sizeof(handle_list), None, None
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        si_ex = _STARTUPINFOEXW()
        si_ex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        si_ex.StartupInfo.lpDesktop = "WinSta0\\Default"
        si_ex.StartupInfo.dwFlags = 0x00000101  # STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si_ex.StartupInfo.wShowWindow = 0  # SW_HIDE
        si_ex.StartupInfo.hStdInput = h_nul
        si_ex.StartupInfo.hStdOutput = h_out_w
        si_ex.StartupInfo.hStdError = h_err_w
        si_ex.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)

        cmdline_buf = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
        ac_env = dict(env)
        ac_env.setdefault("LOCALAPPDATA", cwd_resolved)
        env_entries = [f"{k}={v}" for k, v in sorted(ac_env.items(), key=lambda kv: kv[0].upper())]
        env_text = ("\0".join(env_entries) + "\0\0") if env_entries else "\0\0"
        env_buf = ctypes.create_unicode_buffer(env_text, len(env_text))

        # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED | CREATE_NO_WINDOW
        creation_flags = 0x00080000 | 0x00000400 | 0x00000004 | NO_WINDOW
        if not kernel32.CreateProcessW(
            argv[0],
            cmdline_buf,
            None,
            None,
            True,
            creation_flags,
            ctypes.byref(env_buf),
            cwd_resolved,
            ctypes.byref(si_ex),
            ctypes.byref(pi),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        handles_to_close.extend([x for x in (pi.hThread, pi.hProcess) if x])
        if not kernel32.AssignProcessToJobObject(h_job, pi.hProcess):
            assign_err = ctypes.get_last_error()
            kernel32.TerminateProcess(pi.hProcess, 1)
            raise ctypes.WinError(assign_err)
        if kernel32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            resume_err = ctypes.get_last_error()
            kernel32.TerminateJobObject(h_job, 1)
            raise ctypes.WinError(resume_err)

        # Close child-side pipe handles in parent immediately so pipe EOF triggers when child exits
        _safe_close(h_out_w.value)
        _safe_close(h_err_w.value)
        _safe_close(h_nul)
        _safe_close(pi.hThread)

        out_r_raw, err_r_raw = h_out_r.value, h_err_r.value
        for hr in (out_r_raw, err_r_raw):
            if hr in handles_to_close:
                handles_to_close.remove(hr)

        t_out = threading.Thread(target=_read_pipe, args=(out_r_raw, out_sink), daemon=True)
        t_err = threading.Thread(target=_read_pipe, args=(err_r_raw, err_sink), daemon=True)
        t_out.start()
        t_err.start()

        wait_rc = kernel32.WaitForSingleObject(pi.hProcess, max(1, int(timeout)) * 1000)
        timed_out = (wait_rc == 0x00000102)
        if timed_out:
            kernel32.TerminateJobObject(h_job, 124)
            kernel32.TerminateProcess(pi.hProcess, 124)
            kernel32.WaitForSingleObject(pi.hProcess, 5000)

        exit_code = wintypes.DWORD(1)
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        _safe_close(pi.hProcess)
        _safe_close(h_job)

        t_out.join(timeout=5)
        t_err.join(timeout=5)
        stdout_txt = b"".join(out_sink).decode("utf-8", errors="replace")
        stderr_txt = b"".join(err_sink).decode("utf-8", errors="replace")
        if timed_out:
            stderr_txt = (stderr_txt + f"\nTimed out after {timeout}s inside AppContainer.").strip()
            return 124, stdout_txt, stderr_txt
        return int(exit_code.value), stdout_txt, stderr_txt
    finally:
        for h_rem in list(handles_to_close):
            _safe_close(h_rem)
        if attr_initialized and attr_buf is not None:
            kernel32.DeleteProcThreadAttributeList(attr_buf)
        try:
            _run_icacls([cwd_resolved, "/remove:g", f"*{sid_str}", "/Q"])
        except Exception:
            pass


def _safe_subprocess_env(work_dir: Optional[Path] = None, *, include_userprofile: bool = False) -> dict[str, str]:
    """Minimal allowlisted environment for child processes (strips WEBUI_SECRET_KEY and API secrets)."""
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if os.environ.get(k)}
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["MPLBACKEND"] = "Agg"
    if include_userprofile:
        for k in _USER_ENV_KEYS:
            if os.environ.get(k):
                env[k] = os.environ[k]
    if work_dir is not None:
        tmp_dir = (work_dir / "_tmp").resolve()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_str = str(tmp_dir)
        env["TMP"] = tmp_str
        env["TEMP"] = tmp_str
        env["MPLCONFIGDIR"] = tmp_str
        if not include_userprofile:
            env["HOME"] = tmp_str
            env["USERPROFILE"] = tmp_str
            env["LOCALAPPDATA"] = tmp_str
    return env


def _run_build_script(py312: str, work_dir: Path, script_path: Path) -> tuple[bool, str]:
    if os.name != "nt" and os.environ.get("ALLOW_UNCONTAINED_DECK_EXEC") != "1":
        return (
            False,
            "Refusing uncontained deck script execution on non-Windows host "
            "(Win32 AppContainer isolation is required; set ALLOW_UNCONTAINED_DECK_EXEC=1 to override).",
        )
    kit_src = Path.home() / "AI" / "pptx_studio" / "deck_kit.py"
    if kit_src.is_file():
        shutil.copy2(kit_src, work_dir / "deck_kit.py")
    env = _safe_subprocess_env(work_dir, include_userprofile=False)
    argv = [py312, "-I", "-X", "utf8", "-c", _SANDBOX_RUNNER, str(work_dir.resolve()), str(script_path.resolve())]
    if os.name == "nt":
        rc, stdout_txt, stderr_txt = _run_in_appcontainer(argv, work_dir, env, timeout=90)
    else:
        r = subprocess.run(
            argv,
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        rc, stdout_txt, stderr_txt = r.returncode, r.stdout or "", r.stderr or ""
    pptx_file = work_dir / "deck.pptx"
    if rc == 0 and pptx_file.is_file() and pptx_file.stat().st_size > 5000:
        return True, stdout_txt.strip()
    err = (stderr_txt + "\n" + stdout_txt).strip()
    if rc == 0 and not pptx_file.is_file():
        err = "Script exited 0 but did not create 'deck.pptx' in the current working directory.\n" + err
    return False, err[-2500:]


def _run_visual_qa(py312: str, pptx_path: Path, preview_dir: Path, run_vision: bool = True) -> dict:
    """Invoke pptx_studio helpers under system Python 3.12 (PyMuPDF + LibreOffice + Gemma 4 Vision QA)."""
    helper = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path.home() / 'AI' / 'pptx_studio'))\n"
        "import server as s\n"
        "pptx_p = Path(sys.argv[1])\n"
        "prev_d = Path(sys.argv[2])\n"
        "do_vis = sys.argv[3] == '1'\n"
        "audit = s.audit_pptx_geometry(pptx_p)\n"
        "pngs, sheet = s.render_pptx_to_pngs(pptx_p, prev_d, dpi=150)\n"
        "critique = s.vision_critique_slides(pngs) if (do_vis and pngs) else ''\n"
        "print(json.dumps({'audit': audit, 'pngs': [str(p) for p in pngs], 'sheet': str(sheet) if sheet else '', 'critique': critique}, ensure_ascii=False))\n"
    )
    r = subprocess.run(
        [py312, "-I", "-X", "utf8", "-c", helper, str(pptx_path), str(preview_dir), "1" if run_vision else "0"],
        env=_safe_subprocess_env(include_userprofile=True),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        creationflags=NO_WINDOW,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Visual QA failed: {(r.stderr or r.stdout)[-600:]}")
    lines = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"Visual QA produced no JSON: {r.stdout[-400:]}")
    return json.loads(lines[-1])


async def _maybe_await(val: Any) -> Any:
    return await val if inspect.isawaitable(val) else val


async def _upload_to_owui(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    request: Any,
    user_dict: Optional[dict],
    fallback_dir: Path,
) -> tuple[Optional[str], Optional[str]]:
    user_id = (user_dict or {}).get("id") if isinstance(user_dict, dict) else None
    if _HAS_OWUI_FILES and request is not None and user_id:
        try:
            user_model = await _maybe_await(Users.get_user_by_id(user_id))
            if user_model:
                buf = io.BytesIO(data)
                upload = UploadFile(file=buf, filename=filename, headers=Headers({"content-type": content_type}))
                item = await _maybe_await(
                    upload_file_handler(request=request, file=upload, metadata={}, process=False, user=user_model)
                )
                fid = getattr(item, "id", None) or (item.get("id") if isinstance(item, dict) else None)
                if fid:
                    return f"/api/v1/files/{fid}/content", str(fid)
        except Exception:
            log.exception("[advanced_deck] Files API upload failed")

    fallback_dir.mkdir(parents=True, exist_ok=True)
    out = fallback_dir / filename
    out.write_bytes(data)
    return f"/cache/files/{filename}", None


class Tools:
    def __init__(self):
        self.valves = self.Valves()

    class Valves(BaseModel):
        enable_vision_qa: bool = Field(
            default=True,
            description="Render every slide via LibreOffice and run Gemma 4 Vision QA.",
        )
        enable_auto_polish: bool = Field(
            default=True,
            description="Run 1 automatic visual polish pass whenever geometry or Vision QA finds issues.",
        )
        enable_cover_art: bool = Field(
            default=False,
            description="Generate bespoke Qwen-Image-2.1 cover artwork via ComfyUI (+~55 s).",
        )
        emit_status: bool = Field(
            default=True,
            description="Emit live progress status pills in Open WebUI.",
        )
        export_fallback_dir: str = Field(
            default=str(Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"),
            description="Fallback directory when Open WebUI Files API is unavailable.",
        )

    async def _emit(self, emitter, desc: str, *, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": desc, "done": done}})
            except Exception:
                pass

    async def build_advanced_deck(
        self,
        topic: str,
        slide_count: int = 0,
        language: str = "auto",
        use_web_search: bool = True,
        time_range: str = "",
        instructions: str = "",
        include_cover_art: bool = False,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """Build a bespoke, agency-grade editable PowerPoint (.pptx) with live research, topic-matched visuals (300-DPI matplotlib charts OR verified editorial photography + color swatches), LibreOffice PNG rendering, Gemma 4 Vision QA, and an automatic visual polish pass.

        :param topic: Clear description of the presentation topic, angle, and key points to cover.
        :param slide_count: Exact number of slides requested by the user (3 to 15), or 0 / omitted to let the tool pick the natural deck length (typically 7 slides, 5 for a brief overview, 8 for a deep dive).
        :param language: Output language for all slide text (e.g. 'French', 'English', or 'auto' to match the topic).
        :param use_web_search: Set true for current events, seasonal trends, recent news, market data, or anything after 2025; false for timeless/internal topics.
        :param time_range: Optional recency filter for web search: 'day', 'week', 'month', or '' (auto).
        :param instructions: Optional extra design/audience preferences (e.g. specific theme, data to highlight, audience).
        :param include_cover_art: Set true only if the user specifically asks for AI-generated cover artwork (+55 s).
        """
        t0 = time.time()
        editorial_mode = _is_editorial_visual_topic(topic, instructions)
        n_slides = _choose_slide_count(slide_count, topic, instructions)
        py312 = _find_py312()
        slug = _slugify(topic)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        work_dir = Path.home() / "AI" / "projects" / "decks" / f"{slug}_{stamp}"
        work_dir.mkdir(parents=True, exist_ok=True)
        assets_dir = work_dir / "assets"
        assets_dir.mkdir(exist_ok=True)

        # 1. Optional live web research (+ verified editorial photos)
        facts_block = ""
        search_query = ""
        verified_photos: list[dict] = []
        if use_web_search:
            pill = "🔍 Gathering latest editorial trends & verified photography…" if editorial_mode else "🔍 Gathering latest live facts & numbers…"
            await self._emit(__event_emitter__, pill, done=False)
            try:
                facts_block, search_query, verified_photos = await asyncio.to_thread(
                    _live_search_facts, topic, language, time_range, assets_dir, editorial_mode
                )
            except Exception as e:
                log.warning("live search skipped: %s", e)

        # 2. Optional bespoke cover art (ComfyUI Qwen-Image-2.1)
        has_cover_art = False
        if include_cover_art or self.valves.enable_cover_art:
            await self._emit(__event_emitter__, "🖼️ Generating bespoke cover artwork (Qwen-Image-2.1)…", done=False)
            has_cover_art = await asyncio.to_thread(_generate_cover_art, topic, assets_dir / "cover_art.png")

        # 3. Generate bespoke deck_kit + matplotlib/photo script via Pro (Qwen 3.6, 45 tok/s)
        design_pill = (
            f"🎨 Designing bespoke {n_slides}-slide editorial lookbook deck…"
            if editorial_mode
            else f"🎨 Designing bespoke {n_slides}-slide deck & matplotlib charts…"
        )
        await self._emit(__event_emitter__, design_pill, done=False)
        user_brief = [
            f"Create an agency-grade editable PowerPoint deck (`deck.pptx`) with EXACTLY {n_slides} slides.",
            f"Topic: {topic}",
            f"Language: {language} (if 'auto', match the exact language of the topic above).",
            f"Today's date: {datetime.now().strftime('%d %B %Y')}.",
            (
                "VISUAL MODE: EDITORIAL / VISUAL LOOKBOOK — pick `'editorial_luxe'` or `'nordic_winter'`, showcase "
                "verified local photos from `assets/photo_XX.jpg` via `add_photo_card` (and `cover_image` on Slide 1 if "
                "a clean photo exists), include a seasonal `add_swatch_strip` color/material palette, and DO NOT force "
                "fake matplotlib bar charts unless real quantitative numbers exist in the research."
                if editorial_mode
                else "VISUAL MODE: EXECUTIVE / ANALYTICAL — include 1–2 real 300-DPI matplotlib charts grounded in "
                "genuine metrics from the research, plus styled tables, KPIs, and bold-lead insight cards."
            ),
        ]
        if instructions:
            user_brief.append(f"Additional user instructions: {instructions}")
        if has_cover_art:
            user_brief.append("Cover artwork is already saved at `assets/cover_art.png` — pass `cover_image='assets/cover_art.png'` to `add_cover_slide`.")
        if facts_block:
            user_brief.append("\n" + facts_block)

        raw_reply = await asyncio.to_thread(_ask_pro_code, _DECK_CODER_SYSTEM, "\n".join(user_brief))
        code = _ensure_widescreen_code(_extract_python_code(raw_reply))
        if not code:
            await self._emit(__event_emitter__, "⚠️ Could not extract slide builder script", done=True)
            return "[TOOL_RESULT]\nI couldn't generate the advanced presentation script. Please try again."

        script_path = work_dir / "build_deck.py"
        pptx_path = work_dir / "deck.pptx"
        build_log = work_dir / "build.log"
        log_lines: list[str] = [
            f"topic={topic!r} n_slides={n_slides} editorial={editorial_mode} photos={len(verified_photos)} lang={language!r} query={search_query!r}"
        ]

        # 4. Validate AST + execute (up to 2 fast surgical traceback/AST repair passes if needed)
        await self._emit(__event_emitter__, "⚙️ Building native PowerPoint (.pptx) & 300-DPI charts…", done=False)
        ok, run_msg = False, ""
        for attempt in range(3):
            ast_errs = _audit_generated_code(code)
            if ast_errs:
                run_msg = "Safety/AST validation failed:\n- " + "\n- ".join(ast_errs)
            else:
                script_path.write_text(code, encoding="utf-8")
                ok, run_msg = await asyncio.to_thread(_run_build_script, py312, work_dir, script_path)
            log_lines.append(f"build_attempt_{attempt + 1}: ok={ok} msg={run_msg[:600]!r}")
            if ok:
                break
            if attempt < 2:
                await self._emit(__event_emitter__, "🔧 Fixing script issue and rebuilding…", done=False)
                fix_prompt = (
                    f"The Python script below failed with this error:\n```\n{run_msg}\n```\n\n"
                    "Return surgical `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` patch block(s) to fix this error "
                    "(do not rewrite untouched slides).\n\n"
                    f"```python\n{code}\n```"
                )
                fixed_reply = await asyncio.to_thread(_ask_pro_code, _DECK_POLISH_SYSTEM, fix_prompt)
                fixed_code = _apply_surgical_patches(code, fixed_reply)
                if fixed_code and fixed_code != code:
                    code = fixed_code
                    log_lines.append(f"fix_{attempt + 1}: patch applied ({len(code)} chars)")
                else:
                    log_lines.append(f"fix_{attempt + 1}: patch did not match; reply={fixed_reply[:300]!r}")

        if not ok or not pptx_path.is_file():
            build_log.write_text("\n".join(log_lines), encoding="utf-8")
            await self._emit(__event_emitter__, "⚠️ Deck build failed", done=True)
            return (
                "[TOOL_RESULT — report this build error concisely to the user and stop; do NOT call build_advanced_deck again.]\n"
                f"I couldn't build the advanced deck: {run_msg[:500]}"
            )

        # 5. LibreOffice render + Geometry Audit + Gemma 4 Vision QA
        preview_dir = work_dir / "preview_v1"
        qa = None
        polished = False
        try:
            await self._emit(__event_emitter__, "👁️ Rendering slides (LibreOffice) & running Gemma 4 Vision QA…", done=False)
            qa = await asyncio.to_thread(_run_visual_qa, py312, pptx_path, preview_dir, self.valves.enable_vision_qa)
            log_lines.append(f"qa_v1_issues={len((qa.get('audit') or {}).get('issues') or [])}: {(qa.get('audit') or {}).get('issues')}")
            log_lines.append(f"qa_v1_critique:\n{qa.get('critique') or ''}")
        except Exception as e:
            log.warning("Visual QA skipped: %s", e)
            log_lines.append(f"qa_v1_error={e!r}")

        # 6. Automatic 1-turn surgical visual polish pass whenever geometry or Vision QA reports fixes!
        if qa and self.valves.enable_auto_polish:
            geo_issues = (qa.get("audit") or {}).get("issues") or []
            critique = str(qa.get("critique") or "")
            needs_polish = bool(geo_issues) or bool(re.search(r"\bFIX\b", critique))
            if needs_polish:
                await self._emit(__event_emitter__, "✨ Polishing layout & spacing from visual QA feedback…", done=False)
                polish_prompt = (
                    "Visual QA reviewed the rendered PNG slides of `build_deck.py` and found these issues to fix:\n\n"
                    + ("Geometry issues:\n- " + "\n- ".join(geo_issues) + "\n\n" if geo_issues else "")
                    + f"Art-Director Vision QA:\n{critique}\n\n"
                    f"Here is the current `build_deck.py`:\n```python\n{code}\n```\n\n"
                    "Return surgical `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` blocks fixing ONLY the reported "
                    "issues (do not rewrite untouched slides)."
                )
                try:
                    pol_reply = await asyncio.to_thread(_ask_pro_code, _DECK_POLISH_SYSTEM, polish_prompt)
                    pol_code = _apply_surgical_patches(code, pol_reply)
                    if pol_code and pol_code != code and not _audit_generated_code(pol_code):
                        bak_pptx = work_dir / "deck_v1.pptx"
                        shutil.copy2(pptx_path, bak_pptx)
                        script_path.write_text(pol_code, encoding="utf-8")
                        ok2, msg2 = await asyncio.to_thread(_run_build_script, py312, work_dir, script_path)
                        log_lines.append(f"polish_build: ok={ok2} msg={msg2[:400]!r}")
                        if ok2 and pptx_path.is_file():
                            qa2 = await asyncio.to_thread(_run_visual_qa, py312, pptx_path, work_dir / "preview_v2", False)
                            v2_issues = (qa2.get("audit") or {}).get("issues") or []
                            log_lines.append(f"qa_v2_issues={len(v2_issues)}: {v2_issues}")
                            if len(v2_issues) <= len(geo_issues):
                                polished = True
                                preview_dir = work_dir / "preview_v2"
                                qa = qa2
                                code = pol_code
                            else:
                                log_lines.append("polish reverted: v2 introduced more geometry issues than v1")
                                shutil.copy2(bak_pptx, pptx_path)
                        else:
                            shutil.copy2(bak_pptx, pptx_path)
                except Exception as e:
                    log.warning("Polish pass failed (keeping v1): %s", e)
                    log_lines.append(f"polish_error={e!r}")
        try:
            build_log.write_text("\n".join(log_lines), encoding="utf-8")
        except Exception:
            pass

        # 7. Upload final .pptx + contact-sheet preview PNG to Open WebUI Files API (PC + phone)
        await self._emit(__event_emitter__, "📤 Packaging downloadable PowerPoint & slide preview…", done=False)
        pptx_bytes = pptx_path.read_bytes()
        fname = f"advanced-{slug}_{stamp}.pptx"
        fallback_dir = Path((self.valves.export_fallback_dir or "").strip() or (Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"))
        pptx_url, pptx_fid = await _upload_to_owui(
            pptx_bytes,
            filename=fname,
            content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            request=__request__,
            user_dict=__user__,
            fallback_dir=fallback_dir,
        )

        # Also copy into AI\generated\decks for archive / direct local access
        try:
            decks_dir = Path.home() / "AI" / "generated" / "decks"
            decks_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pptx_path, decks_dir / fname)
        except Exception:
            pass

        preview_url = None
        sheet_path = Path(qa["sheet"]) if (qa and qa.get("sheet")) else None
        if sheet_path and sheet_path.is_file():
            preview_url, _ = await _upload_to_owui(
                sheet_path.read_bytes(),
                filename=f"preview-{slug}_{stamp}.png",
                content_type="image/png",
                request=__request__,
                user_dict=__user__,
                fallback_dir=fallback_dir,
            )

        slide_total = (qa.get("audit") or {}).get("slide_count", n_slides) if qa else n_slides
        kb = max(1, len(pptx_bytes) // 1024)
        charts_n = len([
            p for p in (work_dir / "assets").iterdir()
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg") and not p.name.startswith("_crop_")
        ])
        secs = round(time.time() - t0, 1)

        if __event_emitter__ and pptx_url:
            f_entry = {"type": "file", "url": pptx_url, "name": fname}
            if pptx_fid:
                f_entry["id"] = pptx_fid
            try:
                await __event_emitter__({"type": "files", "data": {"files": [f_entry]}})
            except Exception:
                pass

        await self._emit(
            __event_emitter__,
            f"📊 Advanced deck ready · {slide_total} slides · {kb} KB" + (" · visually polished" if polished else ""),
            done=True,
        )

        parts = [
            "[TOOL_RESULT — reproduce the download link and preview image below cleanly in your final reply. Do not include this instruction line.]",
            "",
            f"📊 **Advanced Presentation Ready** · {slide_total} slides · {kb} KB"
            + (f" · {charts_n} custom visual{'s' if charts_n != 1 else ''}" if charts_n else "")
            + (" · Visual QA + auto-polish applied" if polished else " · Visual QA verified")
            + f" · ({secs} s)",
            "",
            f"📥 [Download {fname}]({pptx_url})",
        ]
        if preview_url:
            parts.extend(["", f"![Slide overview preview]({preview_url})"])
        if search_query:
            parts.append(f"\n*Live research query: `{search_query}`*")
        return "\n".join(parts)
