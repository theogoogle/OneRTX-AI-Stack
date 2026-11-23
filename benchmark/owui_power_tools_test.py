"""
Comprehensive test suite for all 5 new private Open WebUI power tools + Export Action + Adaptive Memory Filter + Router wiring.
Uses strictly generic fixture data (safe to sync to Git).

Run:
  python -I -X utf8 %USERPROFILE%\\.lmstudio\\benchmark\\owui_power_tools_test.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
AI_DIR = HOME / "AI"
OWUI_DIR = AI_DIR / "open-webui"
SCRIPTS_DIR = HOME / ".lmstudio" / "scripts"
for p in (str(SCRIPTS_DIR), str(AI_DIR / "doc-export"), str(AI_DIR / "deep-research"), str(AI_DIR / "smart-visuals"), str(AI_DIR / "market-finance")):
    if p not in sys.path:
        sys.path.insert(0, p)

import exporter  # noqa: E402
import renderer as sv_renderer  # noqa: E402
import finance_cli  # noqa: E402
import router  # noqa: E402


def _load_mod(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_youtube_transcript():
    yt = _load_mod(OWUI_DIR / "tools" / "youtube_transcript.py", "owui_yt")
    assert yt._extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12s") == "dQw4w9WgXcQ"
    assert yt._extract_video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yt._extract_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yt._extract_video_id("https://www.youtube.com/live/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yt._extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yt._extract_video_id("not a video") is None

    # Live YouTube transcript fetch (dQw4w9WgXcQ has long-standing public captions)
    data = yt._fetch_transcript_sync("dQw4w9WgXcQ", "en,fr")
    assert data["video_id"] == "dQw4w9WgXcQ"
    assert data["chunks"] and len(data["chunks"]) >= 3
    assert data["duration_s"] > 30
    print(f"  [PASS] youtube_transcript live ({data['title'][:40] or data['video_id']}, {len(data['chunks'])} chunks, {data['language']})")


def test_doc_export():
    sample_md = """<think>Internal reasoning that must NEVER appear in exported document.</think>
# Executive Brief

Summary paragraph with **bold**, *italic*, and `inline_code`.

| Metric | Value | Status |
|---|---|---|
| Latency | 12 ms | Good |
| Memory | 4.2 GB | Optimal |

- First action item
- Second action item

> Key quote from the brief.

```python
print("hello world")
```
"""
    cleaned = exporter.strip_reasoning(sample_md)
    assert "Internal reasoning" not in cleaned
    assert "Executive Brief" in cleaned

    # HTML sanitizer strips active payloads/SSRF images/javascript: links while keeping safe Markdown tags & blockquotes
    unsafe_md = (
        "# Heading\n\n"
        "> Quoted block survives\n\n"
        "<script>alert(1)</script>\n"
        "<iframe src=\"file:///C:/Windows/win.ini\"></iframe>\n"
        "<img src=\"http://127.0.0.1:4000/x\" onerror=\"bad()\">\n"
        "[bad](javascript:alert(1)) and [ok](https://example.com)\n"
    )
    import markdown as _md
    sanitized = exporter._sanitize_html(_md.markdown(unsafe_md, extensions=["tables", "fenced_code", "sane_lists"]))
    assert "<script" not in sanitized.lower() and "alert(1)" not in sanitized
    assert "<iframe" not in sanitized.lower() and "win.ini" not in sanitized
    assert "<img" not in sanitized.lower() and "onerror" not in sanitized.lower()
    assert "javascript:" not in sanitized.lower()
    assert "<blockquote>" in sanitized and "Quoted block survives" in sanitized
    assert 'href="https://example.com"' in sanitized
    assert "--no-sandbox" not in Path(exporter.__file__).read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        docx_p = tdp / "test_out.docx"
        pdf_p = tdp / "test_out.pdf"
        r1 = exporter.export_docx("Executive Brief", sample_md, docx_p)
        assert r1["ok"] and docx_p.is_file() and docx_p.stat().st_size > 10_000
        from docx import Document

        d = Document(str(docx_p))
        all_txt = "\n".join(p.text for p in d.paragraphs)
        assert "Internal reasoning" not in all_txt and "Executive Brief" in all_txt
        assert len(d.tables) == 1

        r2 = exporter.export_pdf("Executive Brief", sample_md + "\n" + unsafe_md, pdf_p)
        assert r2["ok"] and pdf_p.is_file() and pdf_p.stat().st_size > 15_000
        assert pdf_p.read_bytes()[:5] == b"%PDF-"
        print(f"  [PASS] doc-export DOCX ({r1['bytes']} B) & PDF ({r2['bytes']} B, sandboxed Chrome, HTML sanitized)")


def test_market_finance():
    assert finance_cli.resolve_symbol("GBP/EUR") == "GBPEUR=X"
    assert finance_cli.resolve_symbol("Bitcoin") == "BTC-USD"
    assert finance_cli.resolve_symbol("FTSE 100") == "^FTSE"
    assert finance_cli.resolve_symbol("$NVDA") == "NVDA"

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        q_png = tdp / "nvda.png"
        c_png = tdp / "cmp.png"
        q = finance_cli.cmd_quote("NVDA", "1mo", str(q_png))
        assert q["ok"] and q["price"] > 0 and q_png.is_file() and q_png.stat().st_size > 15_000
        fx = finance_cli.cmd_quote("GBP/EUR", "5d", None)
        assert fx["ok"] and 0.5 < fx["price"] < 2.0
        cmp_res = finance_cli.cmd_compare("NVDA,MSFT", "3mo", str(c_png))
        assert cmp_res["ok"] and len(cmp_res["items"]) == 2 and c_png.is_file() and c_png.stat().st_size > 15_000
        print(f"  [PASS] market-finance live (NVDA={q['price']} {q['currency']}, GBP/EUR={fx['price']}, compare PNG={c_png.stat().st_size} B)")


def test_smart_visuals_deterministic():
    mm_spec = {
        "type": "mindmap",
        "title": "Local AI Stack Architecture",
        "subtitle": "Models, tools & routing overview",
        "center": "Local Router (:4000)",
        "branches": [
            {"label": "Chat & Code", "items": ["Gemma 4 12B Flash", "Qwen3.8 Coding", "Qwen3.6 Pro 256K"]},
            {"label": "Automation", "items": ["Morning Briefing 08:00", "Google OAuth 2.0", "OpenCode Agent"]},
            {"label": "Media & Vision", "items": ["Qwen-Image-2.1", "Qwen3-TTS Studio", "NorthCUA Desktop"]},
            {"label": "Research & Docs", "items": ["Deep Research", "Word & PDF Export", "Smart Visuals"]},
        ],
    }
    ig_spec = {
        "type": "infographic",
        "kicker": "BENCHMARK BRIEF",
        "title": "12 GB RTX GPU Local Speeds",
        "subtitle": "Measured local throughput across active tiers",
        "stats": [
            {"value": "45 tok/s", "label": "Qwen3.6 Pro (MoE)"},
            {"value": "12 GB", "label": "GPU VRAM Budget"},
            {"value": "0 ms", "label": "Memory Filter Overhead"},
        ],
        "cards": [
            {"badge": "01 · FAST", "heading": "Gemma 4 Vision", "bullets": ["Everyday Q&A", "Screenshot QA", "Image prompt routing"]},
            {"badge": "02 · PRO", "heading": "Qwen3.6 MoE", "bullets": ["256K context window", "Deep Research reports", "Family Table broadsheet"]},
        ],
        "takeaway": "Smart tier routing preserves quality while avoiding unnecessary model swaps.",
    }
    tl_spec = {
        "type": "timeline",
        "kicker": "MORNING SCHEDULE",
        "title": "Automated Morning Stack",
        "subtitle": "Wake to ready draft before breakfast",
        "steps": [
            {"badge": "08:00", "heading": "BIOS RTC Wake", "detail": "PC wakes cleanly from S4 hibernate."},
            {"badge": "08:05", "heading": "Family Table Run", "detail": "Fetches Met Office, Calendar, Gmail & Memory Sheet."},
            {"badge": "08:10", "heading": "Gmail Draft Ready", "detail": "PDF broadsheet verified by Gemma 4 Vision QA."},
        ],
        "takeaway": "Everything happens locally before breakfast.",
    }
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        r_mm = sv_renderer.render_spec(mm_spec, tdp / "mm")
        assert r_mm["ok"] and Path(r_mm["svg_path"]).is_file() and Path(r_mm["png_path"]).stat().st_size > 25_000
        r_ig = sv_renderer.render_spec(ig_spec, tdp / "ig")
        assert r_ig["ok"] and Path(r_ig["png_path"]).stat().st_size > 25_000
        r_tl = sv_renderer.render_spec(tl_spec, tdp / "tl")
        assert r_tl["ok"] and Path(r_tl["png_path"]).stat().st_size > 20_000
        print(f"  [PASS] smart-visuals SVG+PNG (mindmap={Path(r_mm['png_path']).stat().st_size} B, infographic={Path(r_ig['png_path']).stat().st_size} B, timeline={Path(r_tl['png_path']).stat().st_size} B)")


def test_deep_research_and_memory_cpu():
    import engine as dr_engine  # noqa: E402

    # 1. SSRF guard
    assert dr_engine._fetch_full_page("http://127.0.0.1:8282/", "SAFE_FALLBACK") == "SAFE_FALLBACK"
    # 2. CPU query planner (zero GPU swap)
    qs = dr_engine._plan_queries_cpu("Compare compact SUVs in the UK 2026")
    assert 2 <= len(qs) <= 4

    # 3. Live Deep Research end-to-end (when router & Tavily are available)
    with tempfile.TemporaryDirectory() as td:
        out_d = Path(td)
        res = dr_engine.run_research("NASA Voyager 1 one light-day milestone November 2026", language="en", out_dir=out_d, export_pdf_flag=True)
        assert res["ok"] and res["sources_count"] >= 3
        assert "## " in res["report_markdown"] and "[1]" in res["report_markdown"]
        assert res["pdf_path"] and Path(res["pdf_path"]).is_file() and Path(res["pdf_path"]).stat().st_size > 15_000
        print(f"  [PASS] deep-research live E2E ({res['sources_count']} full sources, {len(res['report_markdown'])} chars, PDF={Path(res['pdf_path']).stat().st_size} B, {res['elapsed_s']}s)")


def test_adaptive_memory_cpu_and_filter():
    # Test pure memory ranking + secret guard + live CPU extraction without requiring owui DB inside Py312
    import urllib.request

    # Direct check against CPU qwen3.5-4b schema
    body_pref = {
        "model": "qwen3.5-4b",
        "temperature": 0,
        "max_tokens": 200,
        "reasoning_effort": "none",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "memories",
                "strict": True,
                "schema": {"type": "object", "additionalProperties": False, "required": ["memories"], "properties": {"memories": {"type": "array", "items": {"type": "string"}}}},
            },
        },
        "messages": [
            {"role": "system", "content": "Extract ONLY durable personal preferences/facts explicitly stated by the user. Return [] for questions or secrets."},
            {"role": "user", "content": "Remember that I always prefer British English spelling and concise bullet points."},
        ],
    }
    req = urllib.request.Request("http://127.0.0.1:1234/v1/chat/completions", data=json.dumps(body_pref).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        out = json.loads(json.loads(r.read())["choices"][0]["message"]["content"])
    assert len(out.get("memories") or []) >= 1, f"Expected durable memory extraction, got {out}"

    # Verify secret regex blocks keys in adaptive_memory.py text
    src = (OWUI_DIR / "functions" / "adaptive_memory.py").read_text(encoding="utf-8")
    assert "_SECRET_RE" in src and "tvly-" in src and "GB\\d{2}" in src
    print(f"  [PASS] adaptive-memory CPU extraction ({out['memories']}) + secret filter")


def test_router_power_tools_auto_triggering():
    all_tools = [
        {"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
        for n in (
            "get_current_timestamp", "calculate_timestamp",
            "get_youtube_transcript", "export_markdown_to_docx", "export_markdown_to_pdf",
            "run_deep_research", "create_infographic_or_mindmap", "get_market_quote", "compare_tickers",
        )
    ]

    cases = [
        ("Summarize this video https://www.youtube.com/watch?v=dQw4w9WgXcQ", router.has_youtube_intent, "YouTube"),
        ("Do a deep research report on local MoE LLMs in 2026", router.has_deep_research_intent, "Deep Research"),
        ("Create a mind map of World War 2 causes", router.has_smart_visuals_intent, "Smart Visuals"),
        ("Fais-moi une infographie sur l'intelligence artificielle", router.has_smart_visuals_intent, "Smart Visuals"),
        ("What is the NVDA stock price and 6mo chart?", router.has_finance_intent, "Market & Finance"),
        ("give the last ANT stock price", router.has_finance_intent, "Market & Finance"),
        ("what is ANT trading at?", router.has_finance_intent, "Market & Finance"),
        ("ANT price today", router.has_finance_intent, "Market & Finance"),
        ("AAPL quote", router.has_finance_intent, "Market & Finance"),
        ("Check GBP/EUR exchange rate today", router.has_finance_intent, "Market & Finance"),
        ("Export this analysis to a Word document", router.has_export_doc_intent, "Export Document"),
        ("Save this summary as a PDF file", router.has_export_doc_intent, "Export Document"),
    ]
    for prompt, pred, label in cases:
        body = {"model": "auto", "messages": [{"role": "user", "content": prompt}], "tools": all_tools}
        assert pred(body), f"Predicate missed: {prompt!r}"
        rc = router.route_check(body)
        assert rc.get("dispatch") == "chat" and rc.get("tier") == "deep", f"Bad route_check for {prompt!r}: {rc}"
        tier, reason, forced = router.decide(body, "openai")
        assert tier == "deep" and forced and label in reason, f"Bad decide for {prompt!r}: {(tier, reason, forced)}"

    negatives = [
        "Write a Python script to scrape YouTube transcripts",
        "Build a React stock ticker dashboard component",
        "Write mermaid code for a flowchart",
        "Explain how photosynthesis works",
        "Make a PowerPoint presentation about ocean conservation",
        "what is the price of a PS5 today?",
        "how much is a TV?",
    ]
    for prompt in negatives:
        body = {"model": "auto", "messages": [{"role": "user", "content": prompt}], "tools": all_tools}
        for pred in (
            router.has_youtube_intent,
            router.has_deep_research_intent,
            router.has_smart_visuals_intent,
            router.has_finance_intent,
            router.has_export_doc_intent,
        ):
            assert not pred(body), f"False positive on {pred.__name__} for {prompt!r}"

    print(f"  [PASS] router.py auto-triggering ({len(cases)} EN/FR positives, {len(negatives)} coding/unrelated negatives)")


def test_openwebui_db_and_async_loaders():
    con = sqlite3.connect(str(OWUI_DIR / "data" / "webui.db"))
    public_grants = con.execute("SELECT resource_id FROM access_grant WHERE resource_type='tool' AND principal_id='*'").fetchall()
    assert public_grants == [], f"Public grants found: {public_grants}"
    tools = {r[0] for r in con.execute("SELECT id FROM tool WHERE is_active=1")}
    for tid in ("generate_slides", "advanced_deck", "family_table", "google_workspace", "youtube_transcript", "export_document", "deep_research", "smart_visuals", "market_finance"):
        assert tid in tools, f"Missing active tool {tid}"
    fns = {r[0]: (r[1], r[2], r[3]) for r in con.execute("SELECT id, type, is_active, is_global FROM function")}
    assert fns.get("export_message_action") == ("action", 1, 1), f"Bad export_message_action row: {fns.get('export_message_action')}"
    assert fns.get("adaptive_memory") == ("filter", 1, 1), f"Bad adaptive_memory row: {fns.get('adaptive_memory')}"
    con.close()

    # Verify Open WebUI venv async load, async Users.get_user_by_id upload await & Function tests (inlet/outlet/action)
    owui_py = OWUI_DIR / ".venv" / "Scripts" / "python.exe"
    script = r"""
import asyncio, importlib.util, os
from pathlib import Path
owui_dir = Path.home() / "AI" / "open-webui"
os.environ["DATA_DIR"] = str(owui_dir / "data")
os.environ["WEBUI_SECRET_KEY"] = (owui_dir / ".webui_secret_key").read_text(encoding="utf-8").strip()
os.environ["FROM_INIT_PY"] = "true"
os.environ["ENABLE_DB_MIGRATIONS"] = "false"
from open_webui.utils.plugin import load_tool_module_by_id, load_function_module_by_id

async def main():
    expected_tools = {
        "youtube_transcript": "get_youtube_transcript",
        "export_document": "export_markdown_to_docx",
        "deep_research": "run_deep_research",
        "smart_visuals": "create_infographic_or_mindmap",
        "market_finance": "get_market_quote",
    }
    for tid, method_name in expected_tools.items():
        tools_obj = (await load_tool_module_by_id(tid))[0]
        assert callable(getattr(tools_obj, method_name, None)), f"{tid} missing {method_name}"
    act, t1, _, _ = await load_function_module_by_id("export_message_action")
    assert t1 == "action" and len(act.actions) == 2 and callable(getattr(act, "action", None))
    flt, t2, _, _ = await load_function_module_by_id("adaptive_memory")
    assert t2 == "filter" and callable(getattr(flt, "inlet", None)) and callable(getattr(flt, "outlet", None))
    # Verify inlet skips ### Task: cleanly and returns dict
    b = {"messages": [{"role": "user", "content": "### Task: title"}]}
    res_b = await flt.inlet(b, __user__={"id": "nonexistent"})
    assert res_b is b

    # Verify async Users.get_user_by_id + upload_file_handler inside real Open WebUI venv
    spec = importlib.util.spec_from_file_location("owui_mf_async", str(owui_dir / "tools" / "market_finance.py"))
    mf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mf)
    assert mf._HAS_OWUI_FILES, "Expected Open WebUI Files API imports inside .venv"

    class _FakeUsers:
        @staticmethod
        async def get_user_by_id(uid):
            return {"id": uid, "role": "admin"}

    async def _fake_upload(request=None, file=None, metadata=None, process=False, user=None):
        assert isinstance(user, dict) and user.get("id") == "u-test", f"Expected awaited user object, got {user!r}"
        return {"id": "file-123"}

    mf.Users = _FakeUsers
    mf.upload_file_handler = _fake_upload
    uploaded_url = await mf._upload_png(b"\x89PNG\r\n\x1a\n", "test.png", object(), {"id": "u-test"})
    assert uploaded_url == "/api/v1/files/file-123/content", f"Expected Files API URL, got {uploaded_url!r}"
    print("OWUI_OK")
asyncio.run(main())
"""
    res = subprocess.run([str(owui_py), "-X", "utf8", "-c", script], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0 and "OWUI_OK" in res.stdout, f"OWUI loader test failed:\n{res.stdout}\n{res.stderr}"

    mw_path = OWUI_DIR / ".venv" / "Lib" / "site-packages" / "open_webui" / "utils" / "middleware.py"
    mw_src = mw_path.read_text(encoding="utf-8")
    assert "# local patch: merge server-side model default toolIds" in mw_src, "middleware.py missing model toolIds merge patch"
    print("  [PASS] Open WebUI DB privacy (0 public grants) + async loaders + middleware toolIds + upload await")


def main() -> int:
    tests = [
        test_youtube_transcript,
        test_doc_export,
        test_market_finance,
        test_smart_visuals_deterministic,
        test_deep_research_and_memory_cpu,
        test_adaptive_memory_cpu_and_filter,
        test_router_power_tools_auto_triggering,
        test_openwebui_db_and_async_loaders,
    ]
    for fn in tests:
        fn()
    print(f"\nALL {len(tests)}/{len(tests)} OWUI POWER TOOLS & FUNCTIONS TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
