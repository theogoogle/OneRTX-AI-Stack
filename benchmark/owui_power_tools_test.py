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
    import re as _re

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
    assert data["paragraphs"] and len(data["paragraphs"]) >= 3
    assert data["word_count"] > 50 and data["read_min"] >= 1
    assert data["duration_s"] > 30

    card_html = yt.render_transcript_html_card(data)
    assert "iframe:height" in card_html
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in card_html
    assert "watchUrl" in card_html and "&t=" in card_html
    for elem_id in ("yt-search", "btn-copy", "btn-dl", "btn-ts", "btn-expand"):
        assert elem_id in card_html, f"Missing {elem_id} in transcript card HTML"
    assert "<script src=" not in card_html.lower() and "<link " not in card_html.lower()

    # Verify async Tools emits native Open WebUI `embeds` + TRANSCRIPT_CARD_DISPLAYED: yes
    yt_tools = yt.Tools()

    async def _check_yt_emit():
        events: list[dict] = []

        async def _emit(ev: dict) -> None:
            events.append(ev)

        res = await yt_tools.get_youtube_transcript("https://www.youtube.com/watch?v=dQw4w9WgXcQ", __event_emitter__=_emit)
        embeds = [e for e in events if e.get("type") == "embeds"]
        assert len(embeds) == 1 and embeds[0]["data"]["replace"] is False, embeds
        assert "yt-search" in embeds[0]["data"]["embeds"][0]
        assert "TRANSCRIPT_CARD_DISPLAYED: yes" in res, res
        return res

    asyncio.run(_check_yt_emit())

    # Headless Chromium Playwright interactive check (search, highlight, timestamps toggle, expand, overflow)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None
    if sync_playwright is not None:
        words = [w for p in data["paragraphs"] for w in _re.findall(r"[A-Za-z]{4,}", p["text"])]
        probe_word = words[0] if words else "never"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for vw in (760, 360):
                    page = browser.new_page(viewport={"width": vw, "height": 700})
                    errs: list[str] = []
                    page.on("pageerror", lambda e: errs.append(str(e)))
                    page.on("console", lambda m: errs.append(m.text) if m.type == "error" and "i.ytimg.com" not in m.text else None)
                    page.set_content(card_html, wait_until="domcontentloaded")
                    assert page.locator(".para").count() == len(data["paragraphs"])
                    assert page.locator(".para a.ts-chip").first.get_attribute("href").startswith("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=")
                    ov = page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                    assert ov, f"Horizontal overflow on {vw}px viewport"
                    if vw == 760:
                        # Search & highlight
                        page.fill("#yt-search", probe_word)
                        assert page.locator("#yt-body mark").count() >= 1
                        assert "/" in page.locator("#yt-match-count").inner_text()
                        page.fill("#yt-search", "")
                        # Timestamps toggle
                        assert page.locator("#btn-ts").get_attribute("aria-pressed") == "true"
                        page.click("#btn-ts")
                        assert page.locator("#btn-ts").get_attribute("aria-pressed") == "false"
                        assert "hide-ts" in (page.locator("#body-scroll").get_attribute("class") or "")
                        page.click("#btn-ts")
                        assert page.locator("#btn-ts").get_attribute("aria-pressed") == "true"
                        # Expand / collapse
                        h_before = page.evaluate("document.getElementById('card').getBoundingClientRect().height")
                        page.click("#btn-expand")
                        assert page.locator("#btn-expand").get_attribute("aria-expanded") == "true"
                        assert page.locator("#btn-expand").inner_text().strip() == "Collapse"
                        h_after = page.evaluate("document.getElementById('card').getBoundingClientRect().height")
                        assert h_after >= h_before, (h_before, h_after)
                        page.click("#btn-expand")
                        assert page.locator("#btn-expand").inner_text().strip() == "Expand"
                    assert not errs, f"YouTube card Playwright errors @ {vw}px: {errs}"
                    page.close()
            finally:
                browser.close()

    print(f"  [PASS] youtube_transcript live + interactive card ({data['title'][:40] or data['video_id']}, {len(data['paragraphs'])} structured paragraphs, {data['word_count']} words, {data['language']})")


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
        q_html = tdp / "nvda.html"
        c_png = tdp / "cmp.png"
        c_html = tdp / "cmp.html"
        q = finance_cli.cmd_quote("NVDA", "1mo", str(q_png), html_out=str(q_html))
        assert q["ok"] and q["price"] > 0 and q_png.is_file() and q_png.stat().st_size > 15_000
        assert q_html.is_file() and q_html.stat().st_size > 6_000
        q_html_txt = q_html.read_text(encoding="utf-8")
        for lbl in ("24H", "7D", "1M", "1Y", "5Y"):
            assert f">{lbl}</button>" in q_html_txt, f"Missing {lbl} tab in quote HTML"
            assert lbl in (q.get("range_returns") or {}), f"Missing {lbl} in quote range_returns: {q.get('range_returns')}"
        assert "iframe:height" in q_html_txt
        assert "<script src=" not in q_html_txt.lower() and "<link " not in q_html_txt.lower()

        fx = finance_cli.cmd_quote("GBP/EUR", "7d", None)
        assert fx["ok"] and 0.5 < fx["price"] < 2.0

        cmp_res = finance_cli.cmd_compare("NVDA,MSFT", "1y", str(c_png), html_out=str(c_html))
        assert cmp_res["ok"] and len(cmp_res["items"]) == 2 and c_png.is_file() and c_png.stat().st_size > 15_000
        assert c_html.is_file() and c_html.stat().st_size > 6_000
        c_html_txt = c_html.read_text(encoding="utf-8")
        for lbl in ("24H", "7D", "1M", "1Y", "5Y"):
            assert f">{lbl}</button>" in c_html_txt, f"Missing {lbl} tab in compare HTML"
            assert lbl in (cmp_res["items"][0].get("range_returns") or {}), f"Missing {lbl} in compare range_returns"
        assert "iframe:height" in c_html_txt
        assert "<script src=" not in c_html_txt.lower() and "<link " not in c_html_txt.lower()

        # Verify market_finance.py async Tools emits native Open WebUI `embeds` without redundant PNG markdown
        mf_mod = _load_mod(OWUI_DIR / "tools" / "market_finance.py", "owui_mf_embed")
        mf_tools = mf_mod.Tools()

        async def _check_emitters():
            events_q: list[dict] = []

            async def _emit_q(ev: dict) -> None:
                events_q.append(ev)

            out_q = await mf_tools.get_market_quote("NVDA", period="1mo", __event_emitter__=_emit_q)
            embeds_q = [e for e in events_q if e.get("type") == "embeds"]
            assert len(embeds_q) == 1 and embeds_q[0]["data"]["replace"] is False, embeds_q
            assert "NVDA" in embeds_q[0]["data"]["embeds"][0]
            q_json = json.loads(out_q)
            assert q_json.get("interactive_chart_displayed") is True
            assert q_json.get("available_ranges") == ["24H", "7D", "1M", "1Y", "5Y"]
            assert "![" not in out_q, f"Unexpected markdown image when embed emitted: {out_q}"

            events_c: list[dict] = []

            async def _emit_c(ev: dict) -> None:
                events_c.append(ev)

            out_c = await mf_tools.compare_tickers("NVDA,MSFT", period="1y", __event_emitter__=_emit_c)
            embeds_c = [e for e in events_c if e.get("type") == "embeds"]
            assert len(embeds_c) == 1 and embeds_c[0]["data"]["replace"] is False, embeds_c
            c_json = json.loads(out_c)
            assert c_json.get("interactive_chart_displayed") is True
            assert c_json.get("available_ranges") == ["24H", "7D", "1M", "1Y", "5Y"]
            assert "![" not in out_c, f"Unexpected markdown image when embed emitted: {out_c}"

        asyncio.run(_check_emitters())

        # Interactive Playwright verification across all 5 tabs + hover crosshair (no external CDN / console errors)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sync_playwright = None
        if sync_playwright is not None:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    for kind, html_path in (("quote", q_html), ("compare", c_html)):
                        page = browser.new_page(viewport={"width": 760, "height": 540})
                        errs: list[str] = []
                        page.on("pageerror", lambda e: errs.append(str(e)))
                        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
                        page.set_content(html_path.read_text(encoding="utf-8"), wait_until="load")
                        seen_paths: dict[str, str] = {}
                        seen_pills: dict[str, str] = {}
                        for rk in ("24h", "7d", "1mo", "1y", "5y"):
                            btn = page.locator(f'button[data-range="{rk}"]')
                            assert btn.is_visible() and not btn.is_disabled(), f"{kind} tab {rk} disabled or hidden"
                            btn.click()
                            assert btn.get_attribute("aria-selected") == "true", f"{kind} tab {rk} not selected"
                            pill_txt = page.locator("#ret-pill").inner_text().strip()
                            assert pill_txt and pill_txt != "—", f"{kind} empty ret-pill on {rk}"
                            if kind == "quote":
                                d_attr = page.locator("#line-path").get_attribute("d") or ""
                            else:
                                d_attr = "|".join(page.locator("#lines-layer path.line-path").nth(i).get_attribute("d") or "" for i in range(2))
                            assert d_attr.startswith("M") and "L" in d_attr, f"{kind} bad SVG path on {rk}: {d_attr[:60]}"
                            seen_paths[rk] = d_attr
                            seen_pills[rk] = pill_txt
                        assert len(set(seen_paths.values())) >= 4, f"{kind} tabs did not update SVG paths: {list(seen_paths.keys())}"
                        box = page.locator("#chart-svg").bounding_box()
                        assert box and box["width"] > 200 and box["height"] > 100
                        page.mouse.move(box["x"] + box["width"] * 0.45, box["y"] + box["height"] * 0.5)
                        page.wait_for_timeout(80)
                        tip_cls = page.locator("#hover-tip").get_attribute("class") or ""
                        tip_txt = page.locator("#hover-tip").inner_text().strip()
                        assert "show" in tip_cls and len(tip_txt) > 5, f"{kind} hover tooltip failed: cls={tip_cls!r} txt={tip_txt!r}"
                        assert not errs, f"{kind} Playwright console/page errors: {errs}"
                        page.close()
                finally:
                    browser.close()

        print(f"  [PASS] market-finance live + interactive 5-tab Playwright cards (NVDA={q['price']} {q['currency']}, GBP/EUR={fx['price']}, compare PNG={c_png.stat().st_size} B)")


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
