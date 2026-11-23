"""
Fair head-to-head CPU semantic tool & tier router benchmark (`lfm3-1.7b` vs `qwen3.5-4b`)
and regression test suite for dynamic custom tool routing in router.py.

Usage:
  python -I -X utf8 %USERPROFILE%\\.lmstudio\\benchmark\\semantic_tool_router_bench.py --compare
  python -I -X utf8 %USERPROFILE%\\.lmstudio\\benchmark\\semantic_tool_router_bench.py
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
OWUI_DB = Path.home() / "AI" / "open-webui" / "data" / "webui.db"
RESULTS_PATH = Path(__file__).resolve().parent / "results" / "semantic_tool_router_bench.json"

LMSTUDIO_HOST = "127.0.0.1"
LMSTUDIO_PORT = 1234

# Generic fallback tool catalog (used if webui.db is absent); matches Open WebUI active default tool docstrings.
FALLBACK_TOOLS: list[tuple[str, str]] = [
    ("compare_tickers", "Compare 2 to 5 tickers/indices/currencies side-by-side with normalized % performance and a 300-DPI multi-line chart."),
    ("create_email_draft", "Save a ready-to-send Gmail draft (NEVER sends the email automatically)."),
    ("create_infographic_or_mindmap", "Generate a publication-grade high-DPI infographic, mind map (PNG + vector SVG), or timeline diagram from a topic or pasted notes."),
    ("edit_presentation", "Apply deterministic edits to an existing generated .pptx (post-download)."),
    ("export_markdown_to_docx", "Export Markdown content into a cleanly formatted Microsoft Word (.docx) document with download link."),
    ("export_markdown_to_pdf", "Export Markdown content into a cleanly styled A4 PDF document with download link."),
    ("generate_slides", "Create a high-quality NATIVE PowerPoint (.pptx) from a JSON spec and return a download link."),
    ("get_market_quote", "Fetch real-time market price, period return, 52-week range, and a 300-DPI price chart for any stock, ETF, index, crypto, or currency pair."),
    ("get_youtube_transcript", "Fetch the full transcript and title/channel metadata for a YouTube video URL or 11-char ID."),
    ("inspect_slides", "MANDATORY FIRST STEP whenever a .pptx is attached or the user wants to inspect or edit slides."),
    ("list_upcoming_events", "List the user's OWN personal/family Google Calendar appointments (NEVER public city/neighborhood events, concerts, or weather)."),
    ("read_email_thread", "Read the full plain-text content (including replies) of a specific Gmail thread returned by `search_emails`."),
    ("run_deep_research", "Run multi-step autonomous web research (4 complementary sub-queries, parallel full-page reading, cited synthesis on Pro, plus downloadable PDF)."),
    ("search_emails", "Search Gmail threads using Gmail search syntax or plain keywords. Returns compact metadata and `thread_id`."),
]

UNSEEN_SMART_HOME_TOOL = (
    "control_smart_home",
    "Control smart home devices (turn lights, thermostat, heating, or smart plugs on/off or set temperature).",
)

FAMILIES: dict[str, set[str]] = {
    "google_workspace": {"list_upcoming_events", "search_emails", "read_email_thread", "create_email_draft"},
    "market_finance": {"get_market_quote", "compare_tickers"},
    "youtube": {"get_youtube_transcript"},
    "export_doc": {"export_markdown_to_docx", "export_markdown_to_pdf"},
    "deep_research": {"run_deep_research"},
    "smart_visuals": {"create_infographic_or_mindmap"},
    "slides": {"generate_slides", "inspect_slides", "edit_presentation"},
    "smart_home_unseen": {"control_smart_home"},
}

SEMANTIC_ROUTER_PROMPT_TEMPLATE = """You route user requests by returning JSON with "tools" first, then "tier". Never answer the request.

Rules for "tools" (array of matching tool names from AVAILABLE CUSTOM TOOLS below; usually [] or 1 name):
- Check ONLY the tools and descriptions actually listed in AVAILABLE CUSTOM TOOLS below. Select a tool whenever the user wants that tool's real capability (e.g. live quotes/tickers/FX, checking Gmail/calendar or drafting an email to a named person, transcribing a video, creating/editing the exact file or visual format described, deep research, or controlling devices).
- If the requested action or output format is NOT provided by any tool listed in AVAILABLE CUSTOM TOOLS (for example, asking for a PowerPoint slide deck when no slide/pptx tool is listed), return tools=[] — NEVER substitute a different tool.
- Return tools=[] whenever the user is:
  * writing/building software code, scripts, scrapers, HTML/React apps, or dashboards,
  * proofreading, rewriting, or translating pasted text, or writing a generic cold outreach/marketing email template,
  * asking how protocols/passwords/settings work, general knowledge, or consumer retail product prices (like a console or TV),
  * asking about public city/neighborhood events, things to do, concerts, weather, or news (use list_upcoming_events ONLY for the user's own calendar).
- Follow-ups: if RECENT CHAT TOOLS is shown and the message is a short follow-up about earlier results (e.g. "read the second one", "change slide 3"), pick the appropriate follow-up tool from AVAILABLE CUSTOM TOOLS.

Rules for "tier" ("fast" | "code" | "deep"):
- "code": ONLY software/programming/script/HTML app/shell/git/SQL/regex/config tasks (when tools=[]).
- "deep": whenever "tools" is non-empty, OR for complex architecture, trade-offs, puzzles, or math/geometry proofs.
- "fast": everyday Q&A, proofreading/rewriting, general explanations, consumer prices, or casual chat (when tools=[]).

AVAILABLE CUSTOM TOOLS:
{catalog}"""

# 50 diverse EN/FR cases (prompt, expected_family_or_None, expected_tier_for_non_tool_or_deep, recent_tools)
BENCH_CASES: list[tuple[str, str | None, str, tuple[str, ...]]] = [
    # Google Workspace (8)
    ("What's on my calendar this week?", "google_workspace", "deep", ()),
    ("Qu'est-ce que j'ai demain sur mon agenda ?", "google_workspace", "deep", ()),
    ("Do I have any meetings tomorrow?", "google_workspace", "deep", ()),
    ("If you look at my emails, can you list all the persons I interviewed with at ExampleCorp?", "google_workspace", "deep", ()),
    ("Can you identify some emails from school?", "google_workspace", "deep", ()),
    ("Vérifie mes derniers emails de la crèche", "google_workspace", "deep", ()),
    ("Draft an email to Alice about Saturday's lunch", "google_workspace", "deep", ()),
    ("Read the second one please", "google_workspace", "deep", ("search_emails",)),
    # Market & Finance (7)
    ("give the last ANT stock price", "market_finance", "deep", ()),
    ("what is ANT trading at?", "market_finance", "deep", ()),
    ("ANT price today", "market_finance", "deep", ()),
    ("cours de l'action LVMH", "market_finance", "deep", ()),
    ("Check GBP/EUR exchange rate today", "market_finance", "deep", ()),
    ("Compare NVDA and AMD over 6 months", "market_finance", "deep", ()),
    ("How is AAPL doing today?", "market_finance", "deep", ()),
    # YouTube (3)
    ("Summarize this YouTube video https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "deep", ()),
    ("Get the transcript for https://youtu.be/dQw4w9WgXcQ", "youtube", "deep", ()),
    ("Résume cette vidéo YouTube : https://www.youtube.com/shorts/dQw4w9WgXcQ", "youtube", "deep", ()),
    # Export Word & PDF (4)
    ("Export this analysis to a Word document", "export_doc", "deep", ()),
    ("Save this summary as a PDF file", "export_doc", "deep", ()),
    ("Create a clean Word docx report of the meeting notes", "export_doc", "deep", ()),
    ("Exporte ce résumé en PDF", "export_doc", "deep", ()),
    # Deep Research (3)
    ("Do a deep research report on local MoE LLMs in 2026", "deep_research", "deep", ()),
    ("Research thoroughly the pros and cons of solid state batteries", "deep_research", "deep", ()),
    ("Fais une recherche approfondie sur l'énergie géothermique au Royaume-Uni", "deep_research", "deep", ()),
    # Smart Visuals (4)
    ("Create a mind map of World War 2 causes", "smart_visuals", "deep", ()),
    ("Fais-moi une infographie sur l'intelligence artificielle", "smart_visuals", "deep", ()),
    ("Make a timeline diagram of space exploration milestones", "smart_visuals", "deep", ()),
    ("Generate a visual summary infographic of photosynthesis", "smart_visuals", "deep", ()),
    # Simple Deck / Slides (4)
    ("Make me a 6-slide PowerPoint about renewable energy", "slides", "deep", ()),
    ("Create a pitch deck for a local coffee roaster", "slides", "deep", ()),
    ("Fais-moi un PowerPoint sur l'IA locale", "slides", "deep", ()),
    ("Change slide 3 to a darker theme", "slides", "deep", ("generate_slides",)),
    # Unseen future tool (3) — zero router hardcoding!
    ("Turn off the living room lights", "smart_home_unseen", "deep", ()),
    ("Set the bedroom thermostat to 20 degrees", "smart_home_unseen", "deep", ()),
    ("Éteins la lumière du salon", "smart_home_unseen", "deep", ()),
    # Negatives & non-tool tier checks (14)
    ("Write a Python script to parse my emails", None, "code", ()),
    ("Build a React stock ticker dashboard component", None, "code", ()),
    ("Write a Python script to scrape YouTube transcripts", None, "code", ()),
    ("Write mermaid code for a flowchart", None, "code", ()),
    ("Write a Python quicksort function", None, "code", ()),
    ("Build an HTML presentation deck using Reveal.js", None, "code", ()),
    ("Proofread my email before I send it: Hi Alex, thanks for the update.", None, "fast", ()),
    ("Rewrite this email to sound more polite", None, "fast", ()),
    ("How do I change my email password?", None, "fast", ()),
    ("Explain how SMTP email protocols work", None, "fast", ()),
    ("Write a cold outreach email template", None, "fast", ()),
    ("What is the price of a PS5 today?", None, "fast", ()),
    ("what are the events around Kensington and Chelsea this weekend?", None, "fast", ()),
    ("what are the events around Wimbledon and Putney this weekend?", None, "fast", ()),
    ("Quels sont les événements ou expos à Lyon ce week-end ?", None, "fast", ()),
    ("Explain how photosynthesis works", None, "fast", ()),
    ("Solve: prove that for all integers n >= 1, 1^3 + 2^3 + ... + n^3 = (n(n+1)/2)^2", None, "deep", ()),
]


def load_catalog() -> list[tuple[str, str]]:
    """Read auto-attached default custom tools (matching model.meta.toolIds for 'auto') + unseen synthetic tool."""
    tools: dict[str, str] = {}
    if OWUI_DB.is_file():
        con = sqlite3.connect(str(OWUI_DB))
        try:
            row = con.execute("SELECT meta FROM model WHERE id='auto'").fetchone()
            attached = set((json.loads(row[0] or "{}").get("toolIds") or [])) if row else set()
            for tid, specs_s in con.execute("SELECT id, specs FROM tool WHERE is_active=1"):
                if attached and tid not in attached:
                    continue
                for s in json.loads(specs_s or "[]"):
                    n = s.get("name") or ""
                    if n:
                        desc = " ".join((s.get("description") or "").strip().splitlines()[0].split())[:140]
                        tools[n] = desc
        finally:
            con.close()
    if not tools:
        tools = dict(FALLBACK_TOOLS)
    tools[UNSEEN_SMART_HOME_TOOL[0]] = UNSEEN_SMART_HOME_TOOL[1]
    return sorted(tools.items())


def build_schema(tool_names: list[str]) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "semantic_route",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "tools": {"type": "array", "items": {"type": "string", "enum": tool_names}},
                    "tier": {"type": "string", "enum": ["fast", "code", "deep"]},
                },
                "required": ["tools", "tier"],
                "additionalProperties": False,
            },
        },
    }


def call_cpu_classifier(model_id: str, catalog: list[tuple[str, str]], prompt: str, recent_tools: tuple[str, ...]) -> tuple[dict, float]:
    cat_txt = "\n".join(f"- {n}: {d}" for n, d in catalog)
    sys_msg = SEMANTIC_ROUTER_PROMPT_TEMPLATE.format(catalog=cat_txt)
    user_parts = []
    if recent_tools:
        user_parts.append(f"RECENT CHAT TOOLS: {', '.join(recent_tools)}")
    user_parts.append(f"REQUEST:\n<<<\n{prompt[:1500]}\n>>>")
    body = {
        "model": model_id,
        "temperature": 0,
        "max_tokens": 80,
        "reasoning_effort": "none",
        "response_format": build_schema([n for n, _ in catalog]),
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
    }
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=30)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {raw[:200]!r}")
        data = json.loads(raw)
        out = json.loads(data["choices"][0]["message"]["content"])
        ms = (time.perf_counter() - t0) * 1000.0
        return out, ms
    finally:
        conn.close()


def eval_case(expected_family: str | None, expected_tier: str, got_tools: list[str], got_tier: str) -> tuple[bool, bool, bool]:
    """Returns (tool_ok, unseen_fp, tier_ok)."""
    got_set = set(got_tools or [])
    if expected_family is None:
        return (len(got_set) == 0), (len(got_set) > 0), (got_tier == expected_tier)
    fam_set = FAMILIES[expected_family]
    # All returned tools must stay inside the expected family (allow multi-tool within same family)
    tool_ok = bool(got_set & fam_set) and got_set.issubset(fam_set)
    tier_ok = got_tier == "deep"
    return tool_ok, False, tier_ok


def run_compare(catalog: list[tuple[str, str]]) -> int:
    models = ["lfm3-1.7b", "qwen3.5-4b"]
    results = {}
    for m in models:
        # Warm KV-cache prefix once before timing
        call_cpu_classifier(m, catalog, "Hello world", ())
        latencies = []
        tool_hits = 0
        neg_total = 0
        neg_fp = 0
        unseen_hits = 0
        non_tool_tier_hits = 0
        misses = []
        for prompt, exp_fam, exp_tier, recent in BENCH_CASES:
            out, ms = call_cpu_classifier(m, catalog, prompt, recent)
            latencies.append(ms)
            got_tools = out.get("tools") or []
            got_tier = out.get("tier") or ""
            tool_ok, fp, tier_ok = eval_case(exp_fam, exp_tier, got_tools, got_tier)
            if tool_ok:
                tool_hits += 1
            else:
                misses.append({"prompt": prompt, "expected": (exp_fam, exp_tier), "got": (got_tools, got_tier)})
            if exp_fam is None:
                neg_total += 1
                if fp:
                    neg_fp += 1
                if tier_ok:
                    non_tool_tier_hits += 1
            if exp_fam == "smart_home_unseen" and tool_ok:
                unseen_hits += 1

        lat_sorted = sorted(latencies)
        p95 = lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.95))]
        acc_pct = round(100.0 * tool_hits / len(BENCH_CASES), 1)
        fp_pct = round(100.0 * neg_fp / neg_total, 1) if neg_total else 0.0
        results[m] = {
            "total_cases": len(BENCH_CASES),
            "tool_hits": tool_hits,
            "accuracy_pct": acc_pct,
            "negative_fp": neg_fp,
            "negative_total": neg_total,
            "negative_fp_pct": fp_pct,
            "unseen_hits": unseen_hits,
            "unseen_total": 3,
            "non_tool_tier_hits": non_tool_tier_hits,
            "non_tool_tier_total": neg_total,
            "median_ms": round(statistics.median(latencies), 1),
            "p95_ms": round(p95, 1),
            "misses": misses,
        }

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps({"catalog": catalog, "models": results}, indent=2), encoding="utf-8")

    print("\n=== HEAD-TO-HEAD CPU SEMANTIC TOOL ROUTER BENCHMARK (50 CASES) ===")
    print(f"{'Model':<14} {'Acc':>8} {'Hits':>8} {'Neg FP':>9} {'Unseen':>8} {'Tier(Neg)':>10} {'Med ms':>9} {'p95 ms':>9}")
    for m, r in results.items():
        print(
            f"{m:<14} {r['accuracy_pct']:>6.1f}% {r['tool_hits']:>4}/{r['total_cases']:<3} "
            f"{r['negative_fp']:>3}/{r['negative_total']:<3} {r['unseen_hits']:>4}/{r['unseen_total']:<3} "
            f"{r['non_tool_tier_hits']:>5}/{r['non_tool_tier_total']:<4} {r['median_ms']:>8.1f} {r['p95_ms']:>8.1f}"
        )
        if r["misses"]:
            print(f"  Misses for {m}:")
            for miss in r["misses"]:
                print(f"    - {miss['prompt']!r} | exp={miss['expected']} got={miss['got']}")
    return 0


def run_router_regression(catalog: list[tuple[str, str]]) -> int:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import router  # noqa: E402

    # Build OpenAI body containing Open WebUI builtins + all discovered custom tools (incl. unseen smart home tool)
    tools_payload = [
        {"type": "function", "function": {"name": n, "description": "builtin", "parameters": {"type": "object", "properties": {}}}}
        for n in ("get_current_timestamp", "calculate_timestamp", "search_web", "generate_image", "edit_image")
    ] + [
        {"type": "function", "function": {"name": n, "description": d, "parameters": {"type": "object", "properties": {}}}}
        for n, d in catalog
    ]

    misses = []
    hits = 0
    for prompt, exp_fam, exp_tier, recent in BENCH_CASES:
        msgs = []
        for idx, rtool in enumerate(recent):
            cid = f"c_{idx}"
            msgs.append({"role": "user", "content": "previous request"})
            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {"name": rtool, "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": cid, "content": "{}"})
            msgs.append({"role": "assistant", "content": "Done."})
        msgs.append({"role": "user", "content": prompt})

        body = {"model": "auto", "messages": msgs, "tools": tools_payload}
        tier, reason, forced = router.decide(body, "openai")
        sel = router.select_custom_tools(body)
        got_tools = sorted(sel.get("tools") or [])

        if exp_fam is None:
            ok = (len(got_tools) == 0)
            # For coding/negative cases, verify non-deep-tool routing cleanly (quick coding Q&A may run on Flash GPU)
            if exp_tier == "code" and tier not in ("code", "think") and "quick code Q&A" not in reason:
                ok = False
        else:
            fam_set = FAMILIES[exp_fam]
            ok = bool(set(got_tools) & fam_set) and set(got_tools).issubset(fam_set) and tier == "deep" and forced
        if ok:
            hits += 1
        else:
            misses.append((prompt, (exp_fam, exp_tier), (got_tools, tier, reason)))

    print(f"SEMANTIC ROUTER REGRESSION: {hits}/{len(BENCH_CASES)} passed ({100.0 * hits / len(BENCH_CASES):.1f}%)")
    if misses:
        for p, e, g in misses:
            print(f"  MISS: {p!r} | expected={e} got={g}")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true", help="Run fair head-to-head CPU benchmark: lfm3-1.7b vs qwen3.5-4b")
    args = ap.parse_args()
    catalog = load_catalog()
    print(f"Loaded {len(catalog)} custom tools (+1 unseen control_smart_home): {[n for n, _ in catalog]}")
    if args.compare:
        return run_compare(catalog)
    return run_router_regression(catalog)


if __name__ == "__main__":
    raise SystemExit(main())
