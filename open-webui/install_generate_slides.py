"""Idempotent installer/updater for both PowerPoint tools in Open WebUI:
  1. `Simple Deck`   (id: `generate_slides`) — fast (~1-2 min) native PPTX via JSON layouts, template inspect, quick edits.
  2. `Advanced Deck` (id: `advanced_deck`)   — bespoke (~2.5-3.5 min) python-pptx + 300-DPI matplotlib charts +
                                                LibreOffice render + Gemma 4 Vision QA + automatic visual polish pass.

Registers tools in `data/webui.db` using Open WebUI's own spec parser, strips any `requirements:` frontmatter
line (after verifying required libs are installed so cold loads never spawn `pip`), keeps all tools strictly
**Private** (owner-only — removes any `principal_id = '*'` public access grant), and attaches `Simple Deck`
(`generate_slides`) to main chat models' `meta.toolIds` while leaving `Advanced Deck` and `The Family Table`
as clean 1-click toggles right next to it in Open WebUI's `+ -> Tools` menu (router.py gives each toggled tool
automatic priority and strips slide tools on non-slide turns for zero token overhead).

Usage (from AI\\open-webui):
  .\\.venv\\Scripts\\python.exe -X utf8 install_generate_slides.py
"""
import importlib.util
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "webui.db"
EXPORT_DIR = HERE / "data" / "cache" / "files"
ATTACH_SIMPLE_MODELS = ("auto", "deep", "think", "fast", "code")

TOOLS_CONFIG = [
    {
        "id": "generate_slides",
        "name": "Simple Deck",
        "file": HERE / "tools" / "generate_slides.py",
        "fallback_desc": "Fast native PowerPoint (.pptx) generator (~1-2 min) from structured JSON layouts, template inspection, and quick slide edits.",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "image_generation": False,
            "pptx_export_dir": str(EXPORT_DIR),
            "template_mode_enabled": True,
            "inspect_slides_enabled": True,
            "presentation_edit_enabled": True,
            "emit_status": True,
        },
        "preserve_valves": ("unsplash_access_key",),
    },
    {
        "id": "advanced_deck",
        "name": "Advanced Deck",
        "file": HERE / "tools" / "advanced_deck.py",
        "fallback_desc": "Bespoke agency-grade PowerPoint (.pptx) studio (~3 min): live news search, custom python-pptx + 300-DPI matplotlib charts, LibreOffice PNG rendering, Gemma 4 Vision QA + automatic visual polish pass, inline preview & downloadable file.",
        "attach_models": (),
        "valves_overrides": {
            "enable_vision_qa": True,
            "enable_auto_polish": True,
            "enable_cover_art": False,
            "emit_status": True,
            "export_fallback_dir": str(EXPORT_DIR),
        },
        "preserve_valves": ("enable_cover_art",),
    },
    {
        "id": "family_table",
        "name": "The Family Table",
        "file": HERE / "tools" / "family_table.py",
        "fallback_desc": "100% local daily family broadsheet PDF generator (Met Office + Google Calendar/Gmail/Sheets OAuth + Tavily research + Pro + Gemma 4 Vision QA + Gmail draft).",
        "attach_models": (),
        "valves_overrides": {
            "enable_vision_qa": True,
            "default_create_draft": True,
            "default_update_memory": True,
            "emit_status": True,
            "export_fallback_dir": str(EXPORT_DIR),
        },
        "preserve_valves": (),
    },
    {
        "id": "google_workspace",
        "name": "Google Workspace",
        "file": HERE / "tools" / "google_workspace.py",
        "fallback_desc": "Private owner-only Google Calendar & Gmail assistant (upcoming events, search emails, read full threads, save Gmail drafts — zero overhead on unrelated chats).",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "emit_status": True,
        },
        "preserve_valves": (),
    },
    {
        "id": "youtube_transcript",
        "name": "YouTube Transcript",
        "file": HERE / "tools" / "youtube_transcript.py",
        "fallback_desc": "Fetch clean timestamped transcripts and metadata from any YouTube video URL locally (EN/FR + auto-translation).",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "emit_status": True,
        },
        "preserve_valves": (),
    },
    {
        "id": "export_document",
        "name": "Export Word & PDF",
        "file": HERE / "tools" / "export_document.py",
        "fallback_desc": "Export Markdown summaries, tables, and reports into cleanly styled Word (.docx) or A4 PDF documents.",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "emit_status": True,
            "export_fallback_dir": str(EXPORT_DIR),
        },
        "preserve_valves": (),
    },
    {
        "id": "deep_research",
        "name": "Deep Research",
        "file": HERE / "tools" / "deep_research.py",
        "fallback_desc": "Multi-step autonomous research agent (4 sub-queries, parallel full-page reading, cited synthesis on Pro, and PDF export).",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "emit_status": True,
            "export_pdf": True,
        },
        "preserve_valves": (),
    },
    {
        "id": "smart_visuals",
        "name": "Smart Visuals (Infographic & Mind Map)",
        "file": HERE / "tools" / "smart_visuals.py",
        "fallback_desc": "Generate publication-grade high-DPI infographics, balanced SVG+PNG mind maps, and timelines offline.",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "emit_status": True,
        },
        "preserve_valves": (),
    },
    {
        "id": "market_finance",
        "name": "Live Market & Finance",
        "file": HERE / "tools" / "market_finance.py",
        "fallback_desc": "Real-time stocks, ETFs, indices, crypto, and FX quotes with 300-DPI price & comparison charts.",
        "attach_models": ATTACH_SIMPLE_MODELS,
        "valves_overrides": {
            "emit_status": True,
        },
        "preserve_valves": (),
    },
]

FUNCTIONS_CONFIG = [
    {
        "id": "export_message_action",
        "name": "Export Word / PDF",
        "type": "action",
        "file": HERE / "functions" / "export_message_action.py",
        "fallback_desc": "1-click message buttons to export any reply as a formatted Word (.docx) or PDF file (strips thinking blocks).",
        "is_global": 1,
        "valves_overrides": {
            "emit_status": True,
        },
    },
    {
        "id": "adaptive_memory",
        "name": "Adaptive Long-Term Memory",
        "type": "filter",
        "file": HERE / "functions" / "adaptive_memory.py",
        "fallback_desc": "Privately injects relevant user memories into chats and asynchronously extracts durable preferences on CPU qwen3.5-4b.",
        "is_global": 1,
        "valves_overrides": {
            "priority": 10,
            "max_injected_memories": 15,
            "auto_extract": True,
            "extract_model": "qwen3.5-4b",
            "lmstudio_url": "http://127.0.0.1:1234/v1/chat/completions",
        },
    },
]


def _extract_frontmatter(content: str) -> dict:
    import ast
    fm = {}
    try:
        doc = ast.get_docstring(ast.parse(content)) or ""
    except Exception:
        doc = ""
    for line in doc.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if k and v and " " not in k:
                fm[k] = v
    return fm


def _build_specs_and_valves(tool_id: str, tool_path: Path, valves_overrides: dict):
    spec = importlib.util.spec_from_file_location(f"owui_tool_{tool_id}", str(tool_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools_obj = mod.Tools()
    has_user_valves = hasattr(tools_obj, "UserValves")

    from open_webui.utils.plugin import get_function_module_from_cache  # noqa: F401
    from open_webui.utils.tools import convert_function_to_pydantic_model, convert_pydantic_model_to_openai_function_spec

    specs = []
    for attr in dir(tools_obj):
        if attr.startswith("_") or attr in ("Valves", "UserValves"):
            continue
        fn = getattr(tools_obj, attr)
        if not callable(fn) or isinstance(fn, type):
            continue
        pyd = convert_function_to_pydantic_model(fn)
        specs.append(convert_pydantic_model_to_openai_function_spec(pyd))

    valves_obj = tools_obj.Valves(**valves_overrides)
    return specs, valves_obj.model_dump(), has_user_valves


def _upsert_tool(con, owner_id: str, tool_cols: set, cfg: dict, now_ts: int) -> None:
    tool_id = cfg["id"]
    tool_name = cfg["name"]
    tool_file = cfg["file"]
    if not tool_file.is_file():
        raise FileNotFoundError(f"tool source missing: {tool_file}")

    (HERE / "data" / "cache" / "tools" / tool_id).mkdir(parents=True, exist_ok=True)
    content = tool_file.read_text(encoding="utf-8")
    content = re.sub(r"(?m)^requirements:\s*[^\r\n]*\r?\n", "", content, count=1)
    frontmatter = _extract_frontmatter(content)
    frontmatter["title"] = tool_name

    specs, valves_dict, has_user_valves = _build_specs_and_valves(tool_id, tool_file, cfg["valves_overrides"])
    spec_names = [s.get("name") for s in specs]

    meta = {
        "description": cfg["fallback_desc"],
        "manifest": frontmatter,
        "has_user_valves": has_user_valves,
    }

    existing = con.execute("SELECT id, valves FROM tool WHERE id = ?", (tool_id,)).fetchone()
    if existing:
        try:
            old_valves = json.loads(existing[1]) if existing[1] else {}
        except Exception:
            old_valves = {}
        if isinstance(old_valves, dict):
            for k in cfg.get("preserve_valves", ()):
                if k in old_valves and old_valves[k] not in (None, ""):
                    valves_dict[k] = old_valves[k]
        con.execute(
            "UPDATE tool SET name = ?, content = ?, specs = ?, meta = ?, valves = ?, "
            "updated_at = ?, is_active = 1 WHERE id = ?",
            (
                tool_name,
                content,
                json.dumps(specs, ensure_ascii=False),
                json.dumps(meta, ensure_ascii=False),
                json.dumps(valves_dict, ensure_ascii=False),
                now_ts,
                tool_id,
            ),
        )
        action = "updated"
    else:
        if "version_id" in tool_cols and "is_active" in tool_cols:
            con.execute(
                "INSERT INTO tool (id, user_id, name, content, specs, meta, valves, updated_at, created_at, version_id, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)",
                (
                    tool_id,
                    owner_id,
                    tool_name,
                    content,
                    json.dumps(specs, ensure_ascii=False),
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(valves_dict, ensure_ascii=False),
                    now_ts,
                    now_ts,
                ),
            )
        else:
            con.execute(
                "INSERT INTO tool (id, user_id, name, content, specs, meta, valves, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tool_id,
                    owner_id,
                    tool_name,
                    content,
                    json.dumps(specs, ensure_ascii=False),
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(valves_dict, ensure_ascii=False),
                    now_ts,
                    now_ts,
                ),
            )
        action = "inserted"

    # Privacy: keep all custom tools strictly Private (owner-only; never grant public '*' read access)
    con.execute(
        "DELETE FROM access_grant WHERE resource_type = 'tool' AND resource_id = ? AND principal_id = '*'",
        (tool_id,),
    )

    attached = []
    for mid in cfg.get("attach_models", ()):
        row = con.execute("SELECT meta FROM model WHERE id = ?", (mid,)).fetchone()
        if not row:
            continue
        m = json.loads(row[0]) if row[0] else {}
        tids = list(m.get("toolIds") or [])
        if tool_id not in tids:
            tids.append(tool_id)
            m["toolIds"] = tids
            con.execute(
                "UPDATE model SET meta = ?, updated_at = ? WHERE id = ?",
                (json.dumps(m, ensure_ascii=False), now_ts, mid),
            )
            attached.append(mid)

    print(f"Tool '{tool_id}' ({tool_name}) {action}: specs={spec_names}, newly_attached={attached or 'none'}")


def _upsert_function(con, owner_id: str, fn_cols: set, cfg: dict, now_ts: int) -> None:
    fn_id = cfg["id"]
    fn_name = cfg["name"]
    fn_type = cfg["type"]
    fn_file = cfg["file"]
    if not fn_file.is_file():
        raise FileNotFoundError(f"function source missing: {fn_file}")

    (HERE / "data" / "cache" / "functions" / fn_id).mkdir(parents=True, exist_ok=True)
    content = fn_file.read_text(encoding="utf-8")
    content = re.sub(r"(?m)^requirements:\s*[^\r\n]*\r?\n", "", content, count=1)
    frontmatter = _extract_frontmatter(content)
    frontmatter["title"] = fn_name

    spec = importlib.util.spec_from_file_location(f"owui_fn_{fn_id}", str(fn_file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cls_name = {"action": "Action", "filter": "Filter", "pipe": "Pipe"}.get(fn_type, "Filter")
    inst = getattr(mod, cls_name)()
    valves_dict = inst.Valves(**(cfg.get("valves_overrides") or {})).model_dump() if hasattr(inst, "Valves") else {}

    meta = {
        "description": cfg["fallback_desc"],
        "manifest": frontmatter,
    }
    is_global = int(cfg.get("is_global", 1))
    existing = con.execute("SELECT id, valves FROM function WHERE id = ?", (fn_id,)).fetchone()
    if existing:
        con.execute(
            "UPDATE function SET name = ?, type = ?, content = ?, meta = ?, valves = ?, "
            "updated_at = ?, is_active = 1, is_global = ? WHERE id = ?",
            (
                fn_name,
                fn_type,
                content,
                json.dumps(meta, ensure_ascii=False),
                json.dumps(valves_dict, ensure_ascii=False),
                now_ts,
                is_global,
                fn_id,
            ),
        )
        action = "updated"
    else:
        if "version_id" in fn_cols:
            con.execute(
                "INSERT INTO function (id, user_id, name, type, content, meta, valves, is_active, is_global, updated_at, created_at, version_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, NULL)",
                (
                    fn_id,
                    owner_id,
                    fn_name,
                    fn_type,
                    content,
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(valves_dict, ensure_ascii=False),
                    is_global,
                    now_ts,
                    now_ts,
                ),
            )
        else:
            con.execute(
                "INSERT INTO function (id, user_id, name, type, content, meta, valves, is_active, is_global, updated_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    fn_id,
                    owner_id,
                    fn_name,
                    fn_type,
                    content,
                    json.dumps(meta, ensure_ascii=False),
                    json.dumps(valves_dict, ensure_ascii=False),
                    is_global,
                    now_ts,
                    now_ts,
                ),
            )
        action = "inserted"

    print(f"Function '{fn_id}' ({fn_name}, type={fn_type}, global={is_global}) {action}")


def main() -> int:
    if not DB_PATH.is_file():
        print(f"ERROR: Open WebUI database missing: {DB_PATH}", file=sys.stderr)
        return 1

    import PIL  # noqa: F401
    import pptx  # noqa: F401

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    secret_file = HERE / ".webui_secret_key"
    if secret_file.is_file() and not os.environ.get("WEBUI_SECRET_KEY"):
        os.environ["WEBUI_SECRET_KEY"] = secret_file.read_text(encoding="utf-8").strip()
    os.environ.setdefault("DATA_DIR", str(HERE / "data"))
    os.environ.setdefault("FROM_INIT_PY", "true")
    os.environ.setdefault("ENABLE_DB_MIGRATIONS", "false")

    now_ts = int(time.time())
    con = sqlite3.connect(str(DB_PATH))
    try:
        admin_row = con.execute(
            "SELECT id FROM user WHERE role = 'admin' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not admin_row:
            print("ERROR: no admin user found in webui.db", file=sys.stderr)
            return 1
        owner_id = admin_row[0]
        tool_cols = {r[1] for r in con.execute("PRAGMA table_info(tool)").fetchall()}
        fn_cols = {r[1] for r in con.execute("PRAGMA table_info(function)").fetchall()}

        for cfg in TOOLS_CONFIG:
            _upsert_tool(con, owner_id, tool_cols, cfg, now_ts)
        for fcfg in FUNCTIONS_CONFIG:
            _upsert_function(con, owner_id, fn_cols, fcfg, now_ts)
        con.commit()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
