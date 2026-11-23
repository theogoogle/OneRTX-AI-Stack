"""
Local model router v3 -> LM Studio (127.0.0.1:1234). Listens on 127.0.0.1:4000.

Doors:
  POST /v1/messages           Anthropic format (Claude Code via claude-local)
  POST /v1/chat/completions   OpenAI format (scripts, VS Code extensions, chat apps)
  POST /v1/embeddings         passed straight through

Tiers (measured on a 12 GB RTX GPU + 64 GB RAM):
  fast    Gemma 4 12B              ~53-55 tok/s thinking off  Flash (GPU): DEFAULT for questions, writing, translation, images (192K)
  flash   Qwen3.5-4B (CPU)         ~25 tok/s    thinking off  Flash (CPU): instant answers when GPU is busy (64K)
  code    Qwen3.8-27B (IQ3_S)      ~12.3 tok/s  thinking off  Coding / Planning without thinking (64K)
  think   Qwen3.8-27B (IQ3_S)      ~12.3 tok/s  thinking on   Coding / Planning, Open Code, Claude Code (64K, cap 4000/16384)
  deep    Qwen3.6-35B-A3B (MoE)    ~46 tok/s    thinking on   Pro: complex non-coding queries & >64K long context (256K, cap 4000)
  3d      Qwen3.5-4B-3D-Coder      ~50 tok/s    thinking on   Three.js / voxel / 3D scenes (cap 40000 + stall watchdog)
  screen  NorthCUA-Nano 9B         ~73 tok/s    thinking on   Computer Use / Browser Use / Desktop Use (128K)

Model names you can send: auto | fast | flash | code | think | deep | pro | 3d | screen  (also with a "local-" prefix).
OpenAI door: any other name goes straight to LM Studio. Anthropic door: any other name means auto.
Claude Code background calls (local-background or *haiku*) use LFM3 on CPU, thinking off.

"auto", decided once per message you type (tool-use steps reuse the decision). Quality first:
  OpenAI door                                   Claude Code door
  1. image + UI question      -> screen         (not used: the agent loop needs tools)
     other image              -> fast
  2. 3D keywords              -> 3d             (3D coder on OpenAI door; scene3d MCP tool in coding agents)
  3. "ultrathink"/"think very hard" -> deep     -> think (Coding / Planning on Qwen3.8 IQ3_S)
     "think hard"/"think carefully" -> think    -> think
  4. LFM3 label fast          -> fast (Gemma)   -> fast (Gemma)   everyday questions (Claude Code: only in
                                                   sessions that have not used tools yet; else think)
     LFM3 label code          -> think          -> think          Coding / Planning (Qwen3.8 IQ3_S, thinking on)
     LFM3 label deep          -> deep           -> think          Pro (Qwen3.6 35B) for non-coding; Coding / Planning for code
  A "fast" label on a message with clear software terms (SOFTWARE_WORDS) becomes "code".
Why Qwen3.8 is not the default for hard work in Claude Code: every one of its 27B parameters works on
every token (vs 3B for the MoE) and it does not fit in 12 GB, so it runs ~6x slower; an agent loop
makes many requests. Benchmark: Qwen3.8 thinking 97% of Claude, Qwen3.6 thinking 95%.

Thinking caps: the Anthropic door caps thinking natively. The OpenAI door has no cap in LM Studio, so
capped tiers (think, deep) are translated to the Anthropic format upstream and translated back.

Standard library only. Log: router.log next to this file. Status: GET http://127.0.0.1:4000/
"""

import base64
import copy
import email.parser
import email.policy
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))   # the router runs with python -I (no script dir on path)
import comfy_images   # image generation through ComfyUI (same folder)
import web_search     # live information: Tavily search + Open-Meteo weather (same folder)
import page_text      # shrinks big browser page snapshots for NorthCUA (same folder)
import html_files     # complete HTML answers: saved as files, link + headless browser check (same folder)
import local_guard    # refuses requests sent by web pages / DNS rebinding (same folder, security review 2026-11-08)
import northcua_harness  # official NorthCUA web agent harness + MCP server (same folder)
import token_ledger   # token-usage.csv: one row per model request, kept forever (same folder, 2026-11-10)
import voice_tts      # local voice cloning + podcast narration server (same folder, 127.0.0.1:8945)
import rich_ui        # Open WebUI 0.13+ rich SSE events (status, source/favicons, embeds, files) + HTML cards

# ---------------------------------------------------------------- settings
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", int(os.environ.get("ROUTER_PORT", "4000"))
LMSTUDIO_HOST, LMSTUDIO_PORT = "127.0.0.1", int(os.environ.get("LMSTUDIO_PORT", "1234"))

MODELS = {
    "fast": "gemma-4-12b-it-qat",
    "code": "qwen3.8-27b-gsq-rco",
    "deep": "qwen3.6-35b-a3b",
    "3d": "qwen3.5-4b-3d-coder",
    "screen": "northcua-nano",
    "flash": "qwen3.5-4b",            # unsloth Qwen3.5-4B UD-Q4_K_XL, CPU only, always loaded (2026-11-07)
}
ROUTER_MODEL = "lfm3-1.7b"
FLASH_MODEL = MODELS["flash"]
SEMANTIC_ROUTER_MODEL = FLASH_MODEL   # benchmarked 100.0% (vs 84.0% on lfm3-1.7b) on EN/FR dynamic tool + tier routing
# Raw model names picked in Open WebUI's model list -> the tier that gives them the right protections (see decide())
RAW_MODEL_TIER = {
    MODELS["code"]: "think",
    "qwen3.8-27b-gsq-rco@iq3_s": "think",
    MODELS["deep"]: "deep",
    MODELS["fast"]: "fast",
    "unsloth/gemma-4-12b-it-qat": "fast",
    "gemma-4-12b-it-qat": "fast",
    FLASH_MODEL: "flash",
}
RETIRED_MODEL_REDIRECTS = {
    "google/gemma-4-12b-qat": "fast",
}
CPU_MODELS = {ROUTER_MODEL, FLASH_MODEL}     # never on the GPU: no swap guard, no waiting
# Qwen3.5 model card, instruct (non-thinking) mode for general tasks
FLASH_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5}
FLASH_NOTICE = ("> ⚡ **The GPU is busy** with {busy} (a long task is running), so **Flash (CPU)** (Qwen3.5-4B on the CPU) "
                "is answering instead of interrupting it. For the full model, send your message again when that task "
                "has finished.\n\n")
MODEL_LABELS = {
    "auto": "\U0001f4a1 Smart",
    "fast": "⚡⚡ Flash (GPU)",
    "code": "\U0001f4bb Coding (Quick)",
    "think": "\U0001f4bb Coding / Planning",
    "deep": "\U0001f9e0 Pro",
    "pro": "\U0001f9e0 Pro",
    "3d": "\U0001f9ca 3D Scene Generator",
    "screen": "\U0001f5a5️ Computer use",
    "flash": "⚡ Flash (CPU)",
    "voice": "\U0001f399️ Story Voice (Podcast)",
    "podcast": "\U0001f399️ Story Voice (Podcast)",
    "gemma-4-12b-it-qat": "Gemma 4 12B",
    "unsloth/gemma-4-12b-it-qat": "Gemma 4 12B",
    "google/gemma-4-12b-qat": "Gemma 4 12B",
    "lfm3-1.7b": "LFM3 1.7B (CPU)",
    "qwen3.5-4b": "Qwen 3.5 4B (CPU)",
    "qwen3.6-35b-a3b": "Qwen 3.6 35B",
    "qwen3.5-4b-3d-coder": "\U0001f9ca 3D Scene Generator",
    "qwen3.8-27b-gsq-rco": "Qwen 3.8 27B (IQ3_S)",
    "northcua-nano": "Northcua Nano 9B",
}

THINK_BUDGET = 4000            # Qwen3.6: ~90 s of thinking
# Writing or fixing software from a chat app: 4000 was too little to trace a 1,600-line app (2026-11-07 replay: the
# thinking hit the cap and the "fix" guessed a wrong cause). ~6 more minutes at most, only for coding requests.
CODE_THINK_BUDGET = 16000
AGENT_THINK_BUDGET = 8192              # per-turn thinking cap for external-tool coding agents (Open Code & Claude Code)
AGENT_PRO_OVERFLOW_TOKENS = 38000      # coding agent turns above 38K input+tools tokens overflow to Pro (256K, 45 tok/s)
CHAT_CODE_OVERFLOW_TOKENS = 35000      # single-shot chat coding above 35K input tokens overflows to Pro (256K)
LONG_CODE_SURGICAL_LINES = 400         # existing chat HTML >= 400 lines uses surgical SEARCH/REPLACE patches + auto-merge
HTML_REPAIR_THINK_BUDGET = 3000        # 1-turn headless browser error auto-repair thinking cap
GEMMA_THINK_MIN_MAX_TOKENS = 8192      # native Gemma 4 thinking headroom for <|channel>thought + visible answer
# qwen3.5-4b-3d-coder picked directly in a chat app. It thinks a lot by design (training traces: median ~12K tokens,
# 99% under ~35K, longest ~53K), but on 2026-11-07 21:56 it looped for 38 min / ~100K tokens and never wrote the
# scene (the request was passed through untouched: no cap, no watchdog). Cap above 99% of its training traces.
THREED_CODER_PREFIX = "qwen3.5-4b-3d-coder"
THREED_CODER_BUDGET = 40000
THREED_CODER_ANSWER_TOKENS = 49152      # its training answers reach ~3,300 lines
THREED_CODER_SYSTEM = ("Use only the Three.js core ES module from\n"
                       "https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.module.min.js.\n"
                       "Do not import addons or legacy examples/js paths;\n"
                       "implement any camera controls directly inside the HTML.")   # its model card's system prompt
DEEP_BUDGET = 4000             # Qwen3.8: ~9 min of thinking at ~7.6 tok/s (was 2048 for speed until 2026-11-07)
DEEP_MAX_INPUT_TOKENS = 65536  # auto sends "deep" inputs up to this size to Qwen3.8 (reads ~270-450 tok/s)
THREED_MIN_MAX_TOKENS = 16000  # 4000 thinking + a full HTML scene (~6-10K tokens)
THREED_SYSTEM = ("Use only the Three.js core ES module from\n"
                 "https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.module.min.js.\n"
                 "Do not import addons or legacy examples/js paths;\n"
                 "implement any camera controls directly inside the HTML.\n"
                 "Before you answer, check your code: (1) scene, camera and renderer are declared at the top level, "
                 "before any function uses them; (2) every variable is declared in the scope where it is used; "
                 "(3) the camera starts outside the objects and looks at the centre of the scene, so the whole scene "
                 "is in view; (4) there is enough light to see every object; (5) never declare the same top-level "
                 "`const`/`let` name twice, and ensure any `#loading-screen` or `#welcome-screen` start button hides or "
                 "removes its overlay immediately.")

# Image "quality" (OpenAI parameter) -> sampling steps; anything else uses comfy_images.DEFAULTS
IMAGE_QUALITY_STEPS = {"low": 15, "high": 50, "hd": 50}

CLASSIFY_TIMEOUT_S = 15
CLASSIFY_MAX_CHARS = 1500
ROUTE_CACHE_TTL_S = 60
UPSTREAM_TIMEOUT_S = 3600
STREAM_IDLE_S = 240             # watchdog (capped-thinking path): no output for this long = the model stalled.
                                # LM Studio itself gives up after 300 s; this ends it first with a clear message
TRANSLATED_ANSWER_TOKENS = 32768
BACKGROUND_LFM_MAX_TOKENS = 12000  # housekeeping prompts up to this size go to LFM3 (16K context)
RETRY_WAIT_S = 3                   # LM Studio failing before the first byte (e.g. a model mid-swap): retry once   # answer room after thinking on that path (was 4096: long code was cut off)
MODEL_STATE_TTL_S = 3

LOG_FILE = Path(__file__).with_name("router.log")
LOG_MAX_BYTES = 2_000_000
SAVE_LAST_REQUEST = os.environ.get("ROUTER_DEBUG_SAVE_REQUESTS", "0") == "1"

DEEP_PHRASES = re.compile(r"\b(ultrathink|think (really |very )hard|think harder)\b", re.I)
THINK_PHRASES = re.compile(r"\b(think hard|think deeply|think carefully|think step by step|r[ée]fl[ée]chis|pas [àa] pas)\b", re.I)
VISION_THINK_RE = re.compile(
    r"\b(calculate|compute|solve|equation|formula|prove|derive|analyze|analyse|compare|"
    r"diagram|chart|graph|plot|why|debug|error|sum|total|difference|verify|check|"
    r"explique|calcule|r[ée]sous|analyse|comparer?)\b",
    re.I,
)
MATH_PROBLEM_RE = re.compile(
    r"(\\boxed\s*\{|\\triangle\b|\\frac\s*\{|\b(?:acute|obtuse|isosceles|equilateral)\s+triangle\b|"
    r"\bfind\s+the\s+remainder\s+when\b|\bmodulo\s+\d+|\bprove\s+that\s+for\s+(?:all|every)\b)",
    re.I,
)
THREED_WORDS = re.compile(
    r"\b(three\.?js|webgl2?|webgpu|voxels?|glb|gltf|3d (scene|model|world|render|object|room|map|level|game|board|"
    r"view|viewer|visualizer|simulation|environment|terrain|city|island|space)|in 3d|low[- ]poly|isometric scene)\b",
    re.I,
)
SIM_3D_WORDS = re.compile(
    r"\b(glsl|fragment shader|vertex shader|ray ?march(ing|er)?|ray ?trac(ing|er)|signed distance|sdf|"
    r"n[- ]body|celestial mechanics|gravity sim(ulation|ulator)?|verlet integration|"
    r"particle (physics|system|sim(ulation)?)|fluid (dynamics|sim(ulation)?)|cloth (physics|sim(ulation)?)|"
    r"spring[- ]mass|boids|flocking sim(ulation)?|pendulum physics|rigid[- ]?body physics)\b",
    re.I,
)
EXPLICIT_OPENCODE_RE = re.compile(
    r"\b(use open ?code|with open ?code|in open ?code|via open ?code|use (the )?coding agent|"
    r"avec open ?code|utilise(r|z)? open ?code)\b",
    re.I,
)
MULTIFILE_AGENT_RE = re.compile(
    r"(\bmulti[- ]file\b|\bproject (folder|structure|directory|layout)\b|\bpackage\.json\b|\bpyproject\.toml\b|"
    r"\bcargo\.toml\b|\bgo\.mod\b|\bin (my |the )?(workspace|projects? folder|repo(sitory)?)\b|"
    r"\b(run|execute|verify(ing)? with|pass(es|ing)?)\s+(all\s+)?(the\s+)?(pytest|unittest|jest|vitest|cargo test|"
    r"go test|node --test|unit tests?|test suite)\b|"
    r"\bwith\s+(a\s+)?(pytest|jest|vitest|unittest|cargo test|go test|node --test)\s+(suite|tests?)\b|"
    r"\b(cli|command[- ]line)\s+(tool|app|application|utility)\s+with\s+(subcommands?|flags?|argparse|click|typer)\b|"
    r"\b(rest|graphql|fastapi|express|flask)\s+(api|server|backend)\s+with\b)",
    re.I,
)
SINGLE_FILE_CHAT_RE = re.compile(
    r"\b(single[- ]file|one[- ]file|self[- ]contained|single html|in one html|all in one html|"
    r"un seul fichier|autonome)\b",
    re.I,
)
QUICK_HOWTO_QA_RE = re.compile(
    r"^\s*(how (do|can|to|would)\b|what (is|are|does|command|flag|regex)\b|which (command|flag|option)\b|"
    r"in \w+,\s*what\b|comment\b|quelle (commande|option)\b)",
    re.I,
)
SCREEN_WORDS = re.compile(r"\b(click|tap|button|where (is|do|can|should)|which (icon|menu|tab|field|button)|"
                          r"coordinates?|on (my|the|this) screen|screenshot|ui element|menu item|checkbox|dropdown)\b", re.I)
IMAGE_GEN_TOOLS = {"generate_image", "edit_image"}
SLIDES_TOOLS = {"generate_slides", "inspect_slides", "edit_presentation"}
ADVANCED_DECK_TOOLS = {"build_advanced_deck"}
ADVANCED_DECK_THINK_BUDGET = 6144
ADVANCED_DECK_SYSTEM = (
    "Advanced Deck tool rule: The user explicitly enabled the `Advanced Deck` tool. Call `build_advanced_deck` ONCE "
    "immediately with their `topic`, `slide_count` (exact integer requested by the user, or 0 if they did not specify "
    "a slide count so the tool chooses the natural length automatically), `language` (match the user's language), "
    "`use_web_search` (true for recent/current/seasonal/news/market topics, false for timeless topics), `time_range` "
    "('day'|'week'|'month'|''), `instructions` (any style/data preferences), and `include_cover_art` (true only if "
    "they asked for AI cover art). Do NOT write slide bullets yourself before calling the tool. Once `build_advanced_deck` "
    "returns `[TOOL_RESULT]`, reproduce the download link and `![Slide overview preview](...)` image cleanly and stop calling tools."
)
FAMILY_TABLE_TOOLS = {"generate_family_table"}
FAMILY_TABLE_THINK_BUDGET = 8192
FAMILY_TABLE_SYSTEM = (
    "Family Table tool rule: The user explicitly enabled `The Family Table` tool. Call `generate_family_table` ONCE "
    "immediately with `target_date` ('auto', 'today', 'tomorrow', or 'YYYY-MM-DD' if specified), `create_draft` "
    "(default true unless the user says not to draft), `update_memory` (default true unless the user says not to update "
    "memory), and `instructions` (any special editorial request from the user, or ''). Do NOT write the newsletter yourself "
    "before calling the tool. Once `generate_family_table` returns `[TOOL_RESULT]`, reproduce the PDF download link, "
    "Gmail draft status, and `![The Family Table preview](...)` image cleanly and stop calling tools."
)
GOOGLE_WORKSPACE_TOOLS = {"list_upcoming_events", "search_emails", "read_email_thread", "create_email_draft"}
GOOGLE_WORKSPACE_THINK_BUDGET = 4096
GOOGLE_WORKSPACE_SYSTEM = (
    "Google Workspace assistant rule: You have live access to the user's private Google Calendar and Gmail via "
    "`list_upcoming_events`, `search_emails`, `read_email_thread`, and `create_email_draft`. "
    "ALWAYS call the appropriate tool(s) first — NEVER guess or fabricate calendar events or emails. "
    "For calendar questions (upcoming events, schedule, agenda, meetings, birthdays, parties, what's on this week/month), "
    "call `list_upcoming_events(days=..., query=..., start_date=...)`. "
    "For email/inbox questions, call `search_emails(query=..., max_results=...)` first with a focused query. "
    "Inspect the returned subjects, senders, participants, and snippets first, and ONLY call `read_email_thread(thread_id=...)` "
    "on threads that genuinely have missing details — SKIP duplicate calendar invitations, accept/decline replies, and threads whose "
    "snippet/participants already answer the question. Call `read_email_thread` on AT MOST 4 relevant threads per turn, and NEVER "
    "repeat a `thread_id`. Stop calling tools as soon as you have the facts and present a concise answer in the user's language. "
    "Only call `create_email_draft` if the user explicitly asks to draft/compose an email (it saves a draft only and never sends)."
)
YOUTUBE_TOOLS = {"get_youtube_transcript"}
YOUTUBE_THINK_BUDGET = 4096
YOUTUBE_SYSTEM = (
    "YouTube Transcript rule: Call `get_youtube_transcript` ONCE with the YouTube URL from the user's message. "
    "Ground your summary, timestamps, translation, or answers strictly in the returned transcript. Stop calling tools once returned."
)
YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})\b",
    re.I,
)

EXPORT_DOC_TOOLS = {"export_markdown_to_docx", "export_markdown_to_pdf"}
EXPORT_DOC_THINK_BUDGET = 4096
EXPORT_DOC_SYSTEM = (
    "Document Export rule: The user asked for a Word (.docx) or PDF file. Call `export_markdown_to_docx` (for Word/.docx) "
    "or `export_markdown_to_pdf` (for PDF) ONCE with a crisp `title` and well-structured `markdown_content` (synthesized from "
    "the conversation or the user's request). Once `[TOOL_RESULT]` returns, reproduce the clickable download link cleanly and stop."
)

DEEP_RESEARCH_TOOLS = {"run_deep_research"}
DEEP_RESEARCH_THINK_BUDGET = 8192
DEEP_RESEARCH_SYSTEM = (
    "Deep Research rule: Call `run_deep_research` ONCE immediately with `topic` (the user's full research question) "
    "and `language` ('auto', 'en', or 'fr'). Once `[TOOL_RESULT]` returns the cited report and PDF link, present the "
    "complete report cleanly with all citations and the PDF download link, and stop calling tools."
)

SMART_VISUALS_TOOLS = {"create_infographic_or_mindmap"}
SMART_VISUALS_THINK_BUDGET = 4096
SMART_VISUALS_SYSTEM = (
    "Smart Visuals rule: Call `create_infographic_or_mindmap` ONCE immediately with `topic_or_content` (either the topic "
    "or relevant notes/points from the chat), `visual_type` ('infographic', 'mindmap', 'timeline', or 'auto'), and `language`. "
    "Once `[TOOL_RESULT]` returns the image and download links, reproduce them cleanly and stop calling tools."
)

FINANCE_TOOLS = {"get_market_quote", "compare_tickers"}
FINANCE_THINK_BUDGET = 4096
FINANCE_SYSTEM = (
    "Market & Finance rule: Use `get_market_quote(symbol_or_query, period, include_chart=True)` for single stock/ETF/index/crypto/FX "
    "prices & charts, or `compare_tickers(symbols, period)` when comparing 2–5 assets. Reproduce any returned chart image markdown "
    "`![...](...)` verbatim in your reply and summarize the live metrics cleanly. Never invent prices."
)

AUTO_POWER_TOOL_NAMES = (
    ADVANCED_DECK_TOOLS
    | FAMILY_TABLE_TOOLS
    | GOOGLE_WORKSPACE_TOOLS
    | YOUTUBE_TOOLS
    | EXPORT_DOC_TOOLS
    | DEEP_RESEARCH_TOOLS
    | SMART_VISUALS_TOOLS
    | FINANCE_TOOLS
)
BESPOKE_SLIDES_RE = re.compile(
    r"\b(bespoke|agency[\s-]*(?:grade|quality|level)|visual\s*qa|visual\s*check|libreoffice|"
    r"custom\s+(?:python[\s-]*pptx|charts?|visuals?|artwork)|with\s+matplotlib|"
    r"sur[\s-]*mesure|qualit[ée]\s+(?:agence|claude|gemini)|v[ée]rification\s+visuelle)\b",
    re.I,
)
SLIDES_SCHEMA_PROMPT = (
    "Presentation tool rule (`generate_slides`): pass ONE JSON string in `content` using ONLY these exact keys and "
    "layout names (NEVER invent layout names like `title_slide`/`two_content` or nested keys like `left_content`/"
    "`right_content` — unknown keys are ignored and render empty slides!):\n"
    "Top-level JSON: `{\"title\": str, \"subtitle\": str, \"author\": str, \"theme\": \"midnight\"|\"slate\"|"
    "\"ocean\"|\"charcoal\"|\"forest\"|\"terracotta\"|\"teal\"|\"coral\"|\"berry\"|\"sage\"|\"cherry\"|\"auto\", "
    "\"footer\": str, \"slides\": [...]}`\n"
    "Valid slide `layout` values & EXACT required fields:\n"
    "- `cover`: `title`, `subtitle`, `eyebrow`, `date`, `author`, `icon`, `chips`: [str]\n"
    "- `section`: `number` (e.g. \"01\"), `eyebrow`, `title`, `lead`\n"
    "- `title_bullets`: `title`, `eyebrow`, `bullets`: [str] (3-4 crisp bullets; supports **bold**)\n"
    "- `title_body`: `title`, `eyebrow`, `body`: str (paragraphs separated by \\n)\n"
    "- `two_column_text` or `comparison_two`: `title`, `eyebrow`, `left`: {\"heading\": str, \"icon\": str, \"points\": [str]}, "
    "`right`: {\"heading\": str, \"icon\": str, \"points\": [str]} (or `columns`: [{\"heading\", \"icon\", \"points\": [...], \"highlight\": bool}])\n"
    "- `kpi_row`: `title`, `eyebrow`, `stats`: [{\"value\": str, \"label\": str, \"change\": str}] (3-4 big metrics)\n"
    "- `timeline_horizontal` or `process_flow`: `title`, `steps`: [{\"when\": str, \"title\": str, \"description\": str}] (3-5 steps)\n"
    "- `icon_list_vertical` or `icon_grid_2x2` or `icon_grid_3` or `pillars`: `title`, `items`: [{\"icon\": str, \"title\": str, \"description\": str}]\n"
    "- `chart`: `title`, `chart_type` (bar|line|area|pie|doughnut|radar|stacked_bar), `labels`: [str], `values`: [num] (or `datasets`: [{\"label\": str, \"data\": [num]}]), `insight`: [str]\n"
    "- `table`: `title`, `headers`: [str], `rows`: [[str]]\n"
    "- `funnel` | `pyramid` | `cycle` | `quadrant` | `bullseye`: `title`, `nodes`: [{\"label\": str, \"description\": str}]\n"
    "- `text_image_right` or `image_left_text_right`: `title`, `eyebrow`, `bullets`: [str] (or `body`), `image_url`: exact verified `https://...` URL from `VERIFIED PHOTOS FOR SLIDES` (or `template_media`)\n"
    "- `image_full_caption`: `title`, `subtitle`, `image_url`: exact verified `https://...` URL\n"
    "- `quote`: `quote`, `author`, `role` | `alert`: `title`, `level` (info|tip|warning|danger), `body` or `bullets`: [str]\n"
    "- `closing`: `title`, `eyebrow`, `takeaways`: [str], `contact`: str\n"
    "Executive design rules:\n"
    "1. Match the EXACT slide count requested by the user (e.g., 5 slides requested -> `slides` array has 5 items).\n"
    "2. Never make a monotonous bullet/text deck: AVOID `title_body`, use AT MOST ONE `title_bullets`, and ALWAYS end "
    "with a dark `closing` slide. Showcase rich visual layouts (`cover`, `kpi_row` with short punchy values like `$1.8T`/`+34%`, "
    "`chart`, `table`, `icon_grid_2x2` / `icon_grid_3`, `comparison_two`, `two_column_text`, `timeline_horizontal`, "
    "`icon_list_vertical`, `closing`). Whenever numbers/pricing/benchmarks/counts exist in the source facts, turn them "
    "into a `kpi_row`, `table`, or `chart` slide!\n"
    "3. Photos: ONLY use `text_image_right` / `image_left_text_right` when a `VERIFIED PHOTOS FOR SLIDES` URL is a "
    "clean photograph that specifically matches THAT slide's subject — NEVER use generic news-roundup thumbnails, "
    "infographics with baked-in text, or unrelated images (when in doubt, use native `kpi_row`/`table`/`icon_grid_2x2`/`two_column_text` instead — zero broken placeholders!).\n"
    "4. Write punchy insight headlines (not generic labels like 'Nouveautés'), keep bullets under ~15 words with "
    "`**Bold lead-in**: concrete fact`, put ZERO `[1]`/`[5]` citation brackets inside slide text, and use ONLY verified "
    "built-in icon names (`cpu`, `shield`, `globe`, `zap`, `rocket`, `trending-up`, `bar-chart`, `layers`, `lock`, "
    "`users`, `target`, `clock`, `lightbulb`, `check-circle`, `dollar-sign`, `cloud`, `settings`, `award`, `scale`, "
    "`phone`, `star`, `flag`, `calendar`) — never invent icon names or put emojis in titles.\n"
    "5. Pick a cohesive editorial theme (`midnight`, `slate`, `ocean`, `charcoal`, `forest`, `terracotta`).\n"
    "6. Call `inspect_slides` first ONLY if the user attached a `.pptx` template. Call `generate_slides` ONCE — do NOT "
    "call `edit_presentation` right after creating a new deck. Reproduce the returned markdown download link cleanly."
)
SLIDES_CODE_EXCLUDE_RE = re.compile(
    r"\b(html|css|javascript|react|reveal\.?js|marp|python-pptx|write\s+(?:a\s+)?(?:python\s+)?script|"
    r"code\s+to\s+generate|three\.?js)\b",
    re.I,
)
_PPTX_REF_RE = re.compile(r"\.(?:pptx|potx)\b", re.I)

OWUI_BUILTIN_TOOL_NAMES = {
    "add_memory", "ask_user", "calculate_timestamp", "create_automation", "create_calendar_event",
    "create_skill", "create_tasks", "create_tool", "delegate_task", "delete_automation",
    "delete_calendar_event", "delete_memory", "display_file", "edit_image", "execute_code",
    "fetch_url", "generate_image", "generate_video", "get_current_timestamp", "grep_chat_files",
    "grep_knowledge_files", "grep_skill_files", "kb_exec", "list_automations", "list_chat_files",
    "list_knowledge", "list_knowledge_bases", "list_memories", "list_memory_paths", "notify",
    "query_chat_files", "query_knowledge_bases", "query_knowledge_files", "read_memory_path",
    "read_skill_file", "render_visualization", "replace_memory_content", "replace_note_content",
    "search_calendar_events", "search_channel_messages", "search_channels", "search_chats",
    "search_knowledge_bases", "search_knowledge_files", "search_memories", "search_notes",
    "search_tools", "search_users", "search_web", "timer", "toggle_automation", "update_automation",
    "update_calendar_event", "update_memory", "update_skill_files", "update_task",
    "view_channel_message", "view_channel_thread", "view_chat", "view_file", "view_knowledge_file",
    "view_note", "view_skill", "write_note",
    # Open Terminal (:8940) tools when left enabled in Open WebUI composer:
    "browser_action", "browser_screenshot", "browser_snapshot", "browser_tabs",
    "desktop_action", "desktop_screenshot", "get_process_status", "glob_search", "grep_search",
    "kill_process", "list_files", "list_processes", "match_files", "read_file", "read_user_terminal",
    "replace_file_content", "run_command", "search_files", "send_process_input",
    "send_user_terminal_input", "write_file",
}
AGENT_ONLY_TOOL_NAMES = {
    "bash", "shell", "terminal", "exec_command", "run_shell_command", "run_command", "execute",
    "edit", "edit_file", "write", "write_file", "create_file", "replace_file_content",
    "str_replace_editor", "str_replace_based_edit_tool", "apply_patch", "multiedit",
    "glob", "grep", "read", "list", "webfetch", "todowrite", "todoread", "patch", "ls", "find",
}
TOOL_SIBLING_GROUPS = (
    GOOGLE_WORKSPACE_TOOLS,
    SLIDES_TOOLS,
    FINANCE_TOOLS,
    EXPORT_DOC_TOOLS,
)
# Used ONLY when synthetic offline test stubs omit "description"; live Open WebUI always supplies real docstrings.
KNOWN_TOOL_FALLBACK_DESCS = {
    "compare_tickers": "Compare 2 to 5 tickers/indices/currencies side-by-side with normalized % performance and a chart.",
    "create_email_draft": "Save a ready-to-send Gmail draft (NEVER sends the email automatically).",
    "create_infographic_or_mindmap": "Generate a publication-grade high-DPI infographic, mind map (PNG + SVG), or timeline diagram.",
    "edit_presentation": "Apply deterministic edits to an existing generated .pptx presentation.",
    "export_markdown_to_docx": "Export Markdown content into a cleanly formatted Microsoft Word (.docx) document.",
    "export_markdown_to_pdf": "Export Markdown content into a cleanly styled A4 PDF document.",
    "generate_slides": "Create a high-quality NATIVE PowerPoint (.pptx) presentation from a JSON spec.",
    "get_market_quote": "Fetch real-time market price, return, 52-week range, and price chart for any stock, ETF, index, crypto, or FX pair.",
    "get_youtube_transcript": "Fetch the full transcript and metadata for a YouTube video URL or 11-char ID.",
    "inspect_slides": "Inspect or read an attached .pptx presentation or template.",
    "list_upcoming_events": "List upcoming Google Calendar events across the user's calendars.",
    "read_email_thread": "Read the full content (including replies) of a specific Gmail thread.",
    "run_deep_research": "Run multi-step autonomous web research with cited synthesis and downloadable PDF.",
    "search_emails": "Search Gmail threads using keywords or Gmail syntax and return matching metadata + thread_id.",
}
GENERIC_CUSTOM_TOOL_SYSTEM = (
    "Custom tool rule: Call the attached tool(s) as needed to fulfill the user's request directly — NEVER invent results. "
    "Once tool output returns, present the answer clearly in the user's language and stop calling tools."
)
KNOWN_TOOL_BRIEFS = (
    (SLIDES_TOOLS, SLIDES_SCHEMA_PROMPT, None, "PowerPoint / slides request", False),
    (GOOGLE_WORKSPACE_TOOLS, GOOGLE_WORKSPACE_SYSTEM, GOOGLE_WORKSPACE_THINK_BUDGET, "Google Workspace (Calendar/Gmail)", True),
    (YOUTUBE_TOOLS, YOUTUBE_SYSTEM, YOUTUBE_THINK_BUDGET, "YouTube Transcript", True),
    (DEEP_RESEARCH_TOOLS, DEEP_RESEARCH_SYSTEM, DEEP_RESEARCH_THINK_BUDGET, "Deep Research", True),
    (SMART_VISUALS_TOOLS, SMART_VISUALS_SYSTEM, SMART_VISUALS_THINK_BUDGET, "Smart Visuals (Infographic/Mind Map)", True),
    (FINANCE_TOOLS, FINANCE_SYSTEM, FINANCE_THINK_BUDGET, "Market & Finance", True),
    (EXPORT_DOC_TOOLS, EXPORT_DOC_SYSTEM, EXPORT_DOC_THINK_BUDGET, "Export Document (Word/PDF)", True),
)
SEMANTIC_ROUTER_SYSTEM_TEMPLATE = """You route user requests by returning JSON with "tools" first, then "tier". Never answer the request.

Rules for "tools" (array of matching tool names from AVAILABLE CUSTOM TOOLS below; usually [] or 1 name):
- Check ONLY the tools and descriptions actually listed in AVAILABLE CUSTOM TOOLS below. Select a tool whenever the user wants that tool's real capability (e.g. live quotes/tickers/FX, checking Gmail/calendar or drafting an email to a named person, transcribing a video, creating/editing the exact file or visual format described, deep research, or controlling devices).
- If the requested action or output format is NOT provided by any tool listed in AVAILABLE CUSTOM TOOLS (for example, asking for a PowerPoint slide deck when no slide/pptx tool is listed), return tools=[] — NEVER substitute a different tool.
- Return tools=[] whenever the user is:
  * writing/building software code, scripts, scrapers, HTML/React apps, or dashboards,
  * proofreading, rewriting, or translating pasted text, or writing a generic cold outreach/marketing email template,
  * asking how protocols/passwords/settings work, general knowledge, or consumer retail product prices (like a console or TV).
- Follow-ups: if RECENT CHAT TOOLS is shown and the message is a short follow-up about earlier results (e.g. "read the second one", "change slide 3"), pick the appropriate follow-up tool from AVAILABLE CUSTOM TOOLS.

Rules for "tier" ("fast" | "code" | "deep"):
- "code": ONLY software/programming/script/HTML app/shell/git/SQL/regex/config tasks (when tools=[]).
- "deep": whenever "tools" is non-empty, OR for complex architecture, trade-offs, puzzles, or math/geometry proofs.
- "fast": everyday Q&A, proofreading/rewriting, general explanations, consumer prices, or casual chat (when tools=[]).

AVAILABLE CUSTOM TOOLS:
{catalog}"""
# Small fallback regex for bare `SLIDES_TOOLS`-absent checks (e.g. OpenCode `pptx_studio` detection)
SLIDES_FALLBACK_RE = re.compile(
    r"\b(power\s*point|pptx?|slide\s*deck|pitch\s*deck|"
    r"(?:create|make|build|generate|prepare|fais|cr[eé]e|g[eé]n[eè]re|pr[eé]pare)\s+"
    r"(?:me\s+|us\s+|moi\s+)?(?:a\s+|an\s+|un\s+|une\s+)?(?:\d+[\s-]*(?:slide|page)s?\s+)?(?:slides?|presentation|pr[eé]sentation|diaporama|deck))\b",
    re.I,
)
IMAGE_MODE_HINT = "Image Generation mode is enabled for this message"
IMAGE_EDIT_DIRECT_RE = re.compile(
    r"\b(edit|modify|retouch|inpaint|outpaint|recolor|restyle|modifie(r|z)?|retouche(r|z)?)\b"
    r"[^.?!\n]{0,60}?\b(images?|pictures?|photos?|photographs?|illustrations?|portraits?|drawings?|visuels?)\b",
    re.I,
)
IMAGE_EDIT_FOLLOWUP_RE = re.compile(
    r"\b(add|put|place|give|wear(ing)?|dress|remove|erase|delete|replace|swap|change|turn|transform|"
    r"convert|make|keep|recolor|restyle|relight|brighten|darken|zoom|extend|crop|"
    r"ajoute(r|z)?|mets|mettre|enl[èe]ve(r|z)?|supprime(r|z)?|remplace(r|z)?|change(r|z)?|"
    r"transforme(r|z)?|rends?|rendre|garde(r|z)?|habille(r|z)?)\b",
    re.I,
)
PURE_IMAGE_QUESTION_RE = re.compile(
    r"^\s*(what|why|who|where|when|which|how\s+(?:many|much|old|long|big|far|does|do|is|are)|is|are|does|do|"
    r"describe|explain|summarize|summarise|read|extract|transcribe|analyze|analyse|calculate|compute|count|"
    r"quel(le)?s?|pourquoi|combien|d[ée]cris|explique|lis|extrais|calcule|analyse)\b",
    re.I,
)
_FILE_TAG_RE = re.compile(r"<file\b[^>]*>", re.I)
_ATTACHED_FILES_RE = re.compile(r"<attached_files>.*?</attached_files>|<file\b[^>]*>", re.S | re.I)
_OWUI_FILE_URL_RE = re.compile(r"/api/v1/files/[0-9a-fA-F-]+/content")
_UUID_PAT = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_FILE_URL_UUID_RE = re.compile(rf"""\burl\s*=\s*(["'])({_UUID_PAT})\1""", re.I)
_FILE_ID_UUID_RE = re.compile(rf"""\bid\s*=\s*(["'])({_UUID_PAT})\1""", re.I)
IMAGE_GEN_REQUEST_RE = re.compile(
    r"(\b(generate|create|make|draw|paint|sketch|illustrate|render|produce|"
    r"g[ée]n[èe]re(r|z)?|cr[ée]{1,2}(e|r|z)?|fais(-moi)?|faites(-moi)?|dessine(r|z|-moi)?|"
    r"peins(-moi)?|peindre|illustre(r|z)?)\b"
    r"[^.?!\n]{0,80}?\b(images?|pictures?|photos?|photographs?|illustrations?|drawings?|paintings?|"
    r"portraits?|artworks?|wallpapers?|posters?|cartoons?|anime|avatars?|thumbnails?|renders?|"
    r"dessins?|peintures?|visuels?)\b"
    r"|^\s*(an?\s+|the\s+|une?\s+|la\s+|le\s+)?(image|picture|photo|photograph|illustration|drawing|painting|"
    r"portrait|wallpaper|dessin|peinture)\s+(of|showing|depicting|with|de|d['\u2019])\b)",
    re.I,
)
IMAGE_GEN_CODE_EXCLUDE_RE = re.compile(
    r"(\b(html|css|svg|canvas|webgl2?|webgpu|three\.?js|glsl|shader|javascript|typescript|python|react|vue|"
    r"code|coding|script|function|component|app|application|website|web ?page|landing page|game|jeu|"
    r"chart|plot|graph|diagram|mermaid|matplotlib|d3\.?js|pillow|opencv|cli|ascii|screenshot|capture d['\u2019][ée]cran)\b"
    r"|`)",
    re.I,
)
# Backstop for the small labeller: a "fast" label on a message with clear software terms becomes "code".
# A request to build something with code (2026-11-07 incident: a French 4,000-character spec for a 3D app was
# labelled "deep", went to Qwen3.8 at 3-5 tok/s and never finished). Works in English and French.
BUILD_REQUEST = re.compile(
    r"\b(write|build|create|make|generate|implement|develop|code|program|scaffold|prototype|"
    r"cr[ée]{1,2}(e|r|z)?|g[ée]n[èe]re(r|z)?|construi(s|re|sez)|d[ée]veloppe(r|z)?|programme(r|z)?|"
    r"[ée]cri(s|re|vez)|fais|faites|r[ée]alise(r|z)?|code(r|z)?)\b"
    r"[^.?!\n]{0,120}?\b(apps?|applications?|website|site|web ?pages?|pages?|landing|games?|jeux?|jeu|scripts?|"
    r"programs?|programmes?|code|functions?|fonctions?|class(es)?|components?|composants?|api|scenes?|sc[èe]nes?|"
    r"3d|tools?|outils?|prototypes?|demos?|d[ée]mos?|dashboards?|bots?|plugins?|extensions?|html|css|javascript|"
    r"typescript|python|three\.?js|webgl|exp[ée]riences?|simulations?|interfaces?|ui|campus|world|monde|"
    r"cli|package|library|module|server|engine|parser|solver|shader|canvas|visualizer)\b", re.I)
SOFTWARE_WORDS = re.compile(
    r"(`|\b(def|const|let|var|import|async|await|lambda|null|undefined)\b|=>|::|\{\s*\}|\w+\.(py|js|ts|tsx|jsx|java|go|rs|cs|cpp|c|h|rb|php|sh|ps1|sql|json|ya?ml|toml|ini|md|html|css)\b|"
    r"\b(code|coding|program(ming)?|function|variable|bug|debug|compiler?|exception|stack ?trace|traceback|"
    r"typeerror|keyerror|valueerror|syntaxerror|nullpointer|segfault|error code|exit code|"
    r"docker|kubernetes|k8s|git|github|gitlab|commit|branch|rebase|merge conflict|pull request|regex|regexp|sql|"
    r"database|endpoint|api|json|yaml|config|script|bash|powershell|shell\s+(?:script|command|prompt|session)|subshell|terminal|command line|cli|npm|pip|"
    r"python|javascript|typescript|react|vue|angular|node(js)?|java|rust|golang|kotlin|swift|c\+\+|c#|html|css|"
    r"unit tests?|pytest|jest|test suite|refactor|repo(sitory)?|useeffect|webpack|vite|linux|nginx|ci/cd|github actions|"
    r"shader|glsl|raymarch\w*|webgl2?|canvas|three\.?js)\b)",
    re.I)
REMINDER_BLOCKS = re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I)
# Open WebUI housekeeping prompts (title, tags, follow-ups, search queries) all start with "### Task:"
# (except the RAG/web-search answer template, which wraps a real question)
OPENWEBUI_TASK = re.compile(r"^\s*###\s*Task:(?!\s*Respond to the user query)", re.I)
TRIVIAL = re.compile(r"^\s*(y|yes|no|n|ok|okay|go|continue|proceed|sure|thanks?|do it|go ahead)[\s.!]*$", re.I)

CLASSIFIER_SYSTEM = """You are a request classifier. You never answer requests, you only label them.
Categories:
code - ONLY software work: writing, fixing, reviewing, refactoring or explaining code; programming errors, stack traces, scripts, shell or git commands, SQL, regex, config files, a project's files or tests
deep - hard multi-step reasoning: designing system architecture, planning a large project or migration, difficult math or proofs (including olympiad/competition math, geometry, combinatorics, number theory, or LaTeX problems with $...$ / \\boxed{}), puzzles, in-depth trade-off or strategy analysis
fast - everyday questions and tasks on any other topic: health, money, cooking, travel, sport, history, science facts, quick sums, advice, writing or rewording emails and texts, translation, summaries, ideas, casual chat
Rule: a pure math/geometry/olympiad problem or general request that does not ask for software or programming is never code."""
CLASSIFIER_SCHEMA = {"type": "json_schema", "json_schema": {"name": "route", "strict": True, "schema": {
    "type": "object", "properties": {"category": {"type": "string", "enum": ["code", "deep", "fast"]}},
    "required": ["category"], "additionalProperties": False}}}

CODING_ROUTE_SYSTEM = """You classify software & coding requests on two general axes. Never answer the request.

Axis 1 - shape ("opencode" or "chat"):
- "opencode": tasks that need a coding agent with a project folder, multiple files, or terminal test execution:
  * creating or editing a multi-file project, repository, package, backend/API server, full-stack app, or CLI tool with subcommands/flags
  * working inside a folder/workspace, reading or modifying existing project files on disk, or running git/build/shell commands
  * implementing a module/engine AND running automated unit tests (pytest, unittest, jest, vitest, node --test, cargo test, go test) to verify it
- "chat": self-contained single-response tasks:
  * building a single-file web app, interactive HTML/CSS/JS page, 2D/3D browser game, board game, dashboard, calculator, visual scene, or canvas simulation
  * writing one standalone script, function, class, SQL query, regex, or config snippet
  * explaining, reviewing, or fixing code pasted in the chat, or answering a short coding question

Axis 2 - domain ("3d_sim", "logic_ui", or "quick"):
- "3d_sim": 3D graphics (Three.js, WebGL, shaders, GLSL, raymarching, voxels, 3D scenes or 3D games) OR continuous real-time physics/math canvas simulations (gravity, celestial dynamics, particles, fluid/cloth/spring dynamics, boids)
- "logic_ui": discrete rule-based games (2D board/card/grid/turn-based games, puzzles), stateful UI/DOM apps, algorithms, parsers, state machines, async/concurrency, backend/service implementation, or debugging non-trivial code
- "quick": short programming Q&A ("how do I...", "what is...", "difference between..."), single shell/git/CLI command lookup, explaining a concept or short error, writing a regex, SQL query, one-liner, or tiny snippet (<30 lines)"""
CODING_ROUTE_SCHEMA = {"type": "json_schema", "json_schema": {"name": "coding_route", "strict": True, "schema": {
    "type": "object",
    "properties": {
        "shape": {"type": "string", "enum": ["opencode", "chat"]},
        "domain": {"type": "string", "enum": ["3d_sim", "logic_ui", "quick"]},
    },
    "required": ["shape", "domain"],
    "additionalProperties": False}}}

TIERS = ("fast", "code", "think", "deep", "3d", "screen", "flash")

# ---------------------------------------------------------------- state
_lock = threading.Lock()
_conversation_tier = OrderedDict()
_recent = deque(maxlen=50)
_started = time.time()
_model_state = {"at": 0.0, "models": {}}
# One GPU, three users: LM Studio (chat), ComfyUI (images), and Voice TTS (podcast). Any user waits for
# in-flight work, frees the previous GPU user's VRAM, and runs cleanly on the 12 GB card.
_gpu = threading.Condition()
_gpu_state = {"llm_inflight": 0, "image_busy": False, "comfy_dirty": False, "voice_busy": False, "voice_dirty": False}
IMAGE_WAIT_S = 1800


COMFY_HOLD_MB = 600     # ComfyUI holding more VRAM than this (images made via its own page too) gets freed


def llm_enter():
    with _gpu:
        _gpu.wait_for(lambda: not _gpu_state["image_busy"] and not _gpu_state["voice_busy"], timeout=IMAGE_WAIT_S)
        _gpu_state["llm_inflight"] += 1
    # ComfyUI can also be used directly (its own page on :8188), so check it, not only our own flag
    jobs, held = comfy_images.status()
    if _gpu_state["comfy_dirty"] or held > COMFY_HOLD_MB:
        waited = 0
        while jobs and waited < IMAGE_WAIT_S:      # an image in progress: let it finish, then take the GPU
            time.sleep(2)
            waited += 2
            jobs, held = comfy_images.status()
        ok = comfy_images.free()
        _gpu_state["comfy_dirty"] = False
        log(f"images: ComfyUI released {held} MB of VRAM for LM Studio after waiting {waited}s "
            f"({'ok' if ok else 'ComfyUI not reachable'})")
    eng, v_held = voice_tts.status()
    if _gpu_state["voice_dirty"] or v_held:
        ok = voice_tts.unload()
        _gpu_state["voice_dirty"] = False
        log(f"voice: unloaded {eng or 'worker'} VRAM for LM Studio ({'ok' if ok else 'voice-tts not reachable'})")


_model_inflight = {}          # GPU model -> requests in flight (LFM3 runs on the CPU and is not counted)
SWAP_WAIT_S = 1800


_cpu_inflight = {}            # CPU model -> requests in flight (no waiting; the keeper never reloads one in use)
# CPU models kept loaded at all times, as manual loads (no TTL: never auto-evicted). 2026-11-07 23:28: an image
# unloaded Flash, LM Studio then JIT-loaded it (with a TTL) and evicted it again when the 3D coder loaded.
# identifier -> (lms model key, context, expected file). `lms load` takes keys, not paths; the key "qwen3.5-4b" also
# fuzzy-matches the 3D coder, so the loaded file is verified afterwards.
CPU_LOAD = {ROUTER_MODEL: ("lfm3-1.7b", 16384, "lmstudio-community/LFM3-1.7B-GGUF/LFM3-1.7B-Q8_0.gguf"),
            FLASH_MODEL: ("qwen3.5-4b", 32768, "unsloth/Qwen3.5-4B-GGUF/Qwen3.5-4B-UD-Q4_K_XL.gguf")}
KEEPER_EVERY_S = 60
_keeper_lock = threading.Lock()


def ensure_cpu_models(why):
    """Load any missing CPU model; replace a JIT copy (it has a TTL) by a manual one when it is idle."""
    with _keeper_lock:
        try:
            ps = json.loads(subprocess.run([comfy_images.LMS, "ps", "--json"], capture_output=True, text=True,
                                           timeout=60, creationflags=0x08000000).stdout or "[]")
        except Exception as e:
            log(f"keeper ({why}): could not list LM Studio models: {e}")
            return
        loaded = {m.get("identifier"): m for m in ps}
        for ident, (key, ctx, path) in CPU_LOAD.items():
            m = loaded.get(ident)
            if m and not m.get("ttlMs"):
                continue                                # loaded manually: fine
            with _gpu:
                busy = _cpu_inflight.get(ident, 0)
            if m and (busy or m.get("status") != "idle"):
                continue                                # a JIT copy in use: replace it later
            if m:
                subprocess.run([comfy_images.LMS, "unload", ident], capture_output=True, timeout=120,
                               creationflags=0x08000000)
            r = subprocess.run([comfy_images.LMS, "load", key, "--gpu", "off", "--context-length", str(ctx),
                                "--identifier", ident, "-y"], capture_output=True, text=True, timeout=300,
                               creationflags=0x08000000)
            result = "ok" if r.returncode == 0 else "FAILED: " + " ".join((r.stderr or r.stdout).split())[-200:]
            if r.returncode == 0:
                after = json.loads(subprocess.run([comfy_images.LMS, "ps", "--json"], capture_output=True, text=True,
                                                  timeout=60, creationflags=0x08000000).stdout or "[]")
                got = next((x.get("path") for x in after if x.get("identifier") == ident), None)
                if got != path:
                    subprocess.run([comfy_images.LMS, "unload", ident], capture_output=True, timeout=120,
                                   creationflags=0x08000000)
                    result = f"FAILED: key {key} loaded {got}, expected {path}; unloaded it"
            log(f"keeper ({why}): {'replaced the JIT copy of' if m else 'reloaded'} {ident} on the CPU ({result})")
            with _lock:
                _model_state["at"] = 0.0                # model list changed


def _keeper_loop():
    while True:
        time.sleep(KEEPER_EVERY_S)
        try:
            ensure_cpu_models("check")
        except Exception as e:
            log(f"keeper failed: {e.__class__.__name__}: {e}")


def model_enter(model):
    """Wait until no request is running on a different GPU model: loading this one would make LM Studio
    unload that model under a running request ("Model does not exist", 2026-11-07 15:40)."""
    if model in CPU_MODELS:
        with _gpu:
            _cpu_inflight[model] = _cpu_inflight.get(model, 0) + 1
        return 0
    t = time.time()
    with _gpu:
        _gpu.wait_for(lambda: not any(n for m, n in _model_inflight.items() if m != model), timeout=SWAP_WAIT_S)
        _model_inflight[model] = _model_inflight.get(model, 0) + 1
    return round(time.time() - t, 1)


def gpu_busy_with(model):
    """The GPU model other requests are running on when `model` would have to wait for them, else None."""
    if model in CPU_MODELS:
        return None
    with _gpu:
        return next((m for m, n in _model_inflight.items() if n and m != model), None)


def model_exit(model):
    if model in CPU_MODELS:
        with _gpu:
            _cpu_inflight[model] = max(0, _cpu_inflight.get(model, 0) - 1)
        return
    with _gpu:
        _model_inflight[model] = max(0, _model_inflight.get(model, 0) - 1)
        _gpu.notify_all()


def llm_exit():
    with _gpu:
        _gpu_state["llm_inflight"] -= 1
        _gpu.notify_all()


def image_enter():
    with _gpu:
        _gpu.wait_for(lambda: not _gpu_state["image_busy"] and not _gpu_state["voice_busy"] and _gpu_state["llm_inflight"] == 0,
                      timeout=IMAGE_WAIT_S)
        _gpu_state["image_busy"] = True
    eng, v_held = voice_tts.status()
    if _gpu_state["voice_dirty"] or v_held:
        voice_tts.unload()
        _gpu_state["voice_dirty"] = False
        log(f"voice: unloaded {eng or 'worker'} VRAM for ComfyUI")
    free_mb = comfy_images.vram_free_mb()
    if 0 <= free_mb < comfy_images.IMAGE_VRAM_MB:
        gone = comfy_images.unload_lmstudio_gpu_models(log)
        with _lock:
            _model_state["at"] = 0.0     # model list changed
        # CPU models are kept by unload_lmstudio_gpu_models; check anyway, in the background (CPU only, no VRAM)
        threading.Thread(target=ensure_cpu_models, args=("after image unload",), daemon=True).start()
        return f"unloaded {', '.join(gone) or 'nothing'} (only {free_mb} MB VRAM free)"
    return f"{free_mb} MB VRAM free, nothing unloaded"


def image_exit():
    with _gpu:
        _gpu_state["image_busy"] = False
        _gpu_state["comfy_dirty"] = True
        _gpu.notify_all()


def voice_enter():
    with _gpu:
        _gpu.wait_for(lambda: not _gpu_state["image_busy"] and not _gpu_state["voice_busy"] and _gpu_state["llm_inflight"] == 0,
                      timeout=IMAGE_WAIT_S)
        _gpu_state["voice_busy"] = True
    jobs, held = comfy_images.status()
    if _gpu_state["comfy_dirty"] or held > COMFY_HOLD_MB:
        waited = 0
        while jobs and waited < IMAGE_WAIT_S:
            time.sleep(2)
            waited += 2
            jobs, held = comfy_images.status()
        comfy_images.free()
        _gpu_state["comfy_dirty"] = False
        log(f"voice: ComfyUI released {held} MB of VRAM for voice-tts")
    free_mb = comfy_images.vram_free_mb()
    if 0 <= free_mb < voice_tts.VOICE_VRAM_MB:
        gone = comfy_images.unload_lmstudio_gpu_models(log)
        with _lock:
            _model_state["at"] = 0.0
        threading.Thread(target=ensure_cpu_models, args=("after voice unload",), daemon=True).start()
        return f"unloaded {', '.join(gone) or 'nothing'} (only {free_mb} MB VRAM free)"
    return f"{free_mb} MB VRAM free, nothing unloaded"


def voice_exit():
    with _gpu:
        _gpu_state["voice_busy"] = False
        _gpu_state["voice_dirty"] = True
        _gpu.notify_all()


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    with _lock:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


# ---------------------------------------------------------------- LM Studio helpers
def lmstudio_models():
    with _lock:
        if time.time() - _model_state["at"] < MODEL_STATE_TTL_S:
            return dict(_model_state["models"])
    models = {}
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=5)
    try:
        conn.request("GET", "/api/v0/models")
        for m in json.loads(conn.getresponse().read()).get("data", []):
            models[m["id"]] = m.get("state", "not-loaded")
    except Exception as e:
        log(f"could not read LM Studio model list: {e}")
    finally:
        conn.close()
    with _lock:
        _model_state.update(at=time.time(), models=models)
    return models


def installed(models, model_id):
    return not models or model_id in models   # if LM Studio can't be asked, assume yes


def is_loaded(models, model_id):
    return models.get(model_id) == "loaded"


def count_internal(data, model, what):
    """The router's own LM Studio calls (labeller, web decision, search query) also go in the token ledger."""
    usage = data.get("usage") if isinstance(data, dict) else None
    i, o = token_ledger.usage_counts(usage)
    token_ledger.record("router internal", "router", what, model, i, o)


_label_cache = OrderedDict()
_coding_route_cache = OrderedDict()
APP_OR_GAME_NOUNS = re.compile(
    r"\b(apps?|applications?|games?|jeux?|jeu|website|site|web ?pages?|pages?|dashboards?|simulations?|"
    r"scenes?|sc[èe]nes?|parsers?|engines?|solvers?|servers?|visualizers?|calculators?|editors?|clones?|"
    r"boards?|canvas|widgets?|components?)\b",
    re.I,
)


def classify_with_lfm(text):
    ckey = hashlib.sha1(text[:CLASSIFY_MAX_CHARS].encode("utf-8", "ignore")).hexdigest()[:20]
    with _lock:
        hit = _label_cache.get(ckey)
    if hit and time.time() - hit[0] < ROUTE_CACHE_TTL_S:
        return hit[1]
    body = {"model": ROUTER_MODEL, "temperature": 0, "max_tokens": 20, "reasoning_effort": "none",
            "response_format": CLASSIFIER_SCHEMA,
            "messages": [{"role": "system", "content": CLASSIFIER_SYSTEM},
                         {"role": "user", "content": "Label the request below. Do not answer it or carry it out.\n\n"
                                                     "REQUEST:\n<<<\n" + text[:CLASSIFY_MAX_CHARS] + "\n>>>"}]}
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=CLASSIFY_TIMEOUT_S)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        count_internal(data, ROUTER_MODEL, "label")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {str(data)[:200]}")
        label = json.loads(data["choices"][0]["message"]["content"])["category"]
        snippet = text[:CLASSIFY_MAX_CHARS]
        if MATH_PROBLEM_RE.search(snippet) and not (BUILD_REQUEST.search(snippet) or CODE_FENCE.search(text)):
            label = "deep"
        elif label == "fast" and (SOFTWARE_WORDS.search(snippet) or BUILD_REQUEST.search(snippet)):
            label = "code"
        with _lock:
            _label_cache[ckey] = (time.time(), label)
            while len(_label_cache) > 128:
                _label_cache.popitem(last=False)
        return label
    finally:
        conn.close()


def classify_coding_with_lfm(text):
    """2-axis task-agnostic coding classifier: shape ('opencode' | 'chat') and domain ('3d_sim' | 'logic_ui' | 'quick')."""
    snippet = text[:CLASSIFY_MAX_CHARS]
    ckey = hashlib.sha1(snippet.encode("utf-8", "ignore")).hexdigest()[:20]
    with _lock:
        hit = _coding_route_cache.get(ckey)
    if hit and time.time() - hit[0] < ROUTE_CACHE_TTL_S:
        return dict(hit[1])
    t0 = time.time()
    try:
        raw = lfm_json({
            "temperature": 0,
            "max_tokens": 36,
            "reasoning_effort": "none",
            "response_format": CODING_ROUTE_SCHEMA,
            "messages": [
                {"role": "system", "content": CODING_ROUTE_SYSTEM},
                {"role": "user", "content": "Classify the software request below.\n\nREQUEST:\n<<<\n" + snippet + "\n>>>"},
            ],
        })
        shape = raw.get("shape") if raw.get("shape") in ("opencode", "chat") else "chat"
        domain = raw.get("domain") if raw.get("domain") in ("3d_sim", "logic_ui", "quick") else "logic_ui"
        ms = int((time.time() - t0) * 1000)
        note = f"LFM coding ({shape}/{domain}) in {ms} ms"
    except Exception as e:
        shape, domain = "chat", "logic_ui"
        note = f"LFM coding unavailable ({e.__class__.__name__})"

    has_pasted_code = any(b.count("\n") >= 15 for b in CODE_FENCE.findall(text))
    if has_pasted_code and not EXPLICIT_OPENCODE_RE.search(snippet) and not MULTIFILE_AGENT_RE.search(snippet):
        shape = "chat"
        if domain == "quick":
            domain = "logic_ui"
    if SINGLE_FILE_CHAT_RE.search(snippet) and not EXPLICIT_OPENCODE_RE.search(snippet):
        shape = "chat"
    elif EXPLICIT_OPENCODE_RE.search(snippet):
        shape = "opencode"
    elif MULTIFILE_AGENT_RE.search(snippet) and not SINGLE_FILE_CHAT_RE.search(snippet):
        shape = "opencode"

    if THREED_WORDS.search(snippet) or SIM_3D_WORDS.search(snippet):
        domain = "3d_sim"
    if domain == "3d_sim" and not EXPLICIT_OPENCODE_RE.search(snippet) and not MULTIFILE_AGENT_RE.search(snippet):
        shape = "chat"
    elif (
        len(text) < 180
        and text.rstrip().endswith("?")
        and QUICK_HOWTO_QA_RE.search(snippet)
        and not MATH_PROBLEM_RE.search(snippet)
        and not has_pasted_code
        and not BUILD_REQUEST.search(snippet)
        and not MULTIFILE_AGENT_RE.search(snippet)
        and not EXPLICIT_OPENCODE_RE.search(snippet)
    ):
        shape, domain = "chat", "quick"
    elif domain == "quick" and (
        shape == "opencode"
        or has_pasted_code
        or len(text) > 500
        or (BUILD_REQUEST.search(snippet) and APP_OR_GAME_NOUNS.search(snippet))
    ):
        domain = "logic_ui"

    res = {"shape": shape, "domain": domain, "reason": note}
    with _lock:
        _coding_route_cache[ckey] = (time.time(), res)
        while len(_coding_route_cache) > 128:
            _coding_route_cache.popitem(last=False)
    return dict(res)


# ---------------------------------------------------------------- message helpers
def user_text(content):
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text"]
    return REMINDER_BLOCKS.sub("", "\n".join(parts)).strip()


# Computer use (2026-11-07, your rule: GUI/browser agents always use NorthCUA, the computer-use model).
# A request is a computer-use loop when its tools are GUI actions (Anthropic's computer tool, or browser/desktop
# actions such as click, type, scroll, screenshot) and it has no code-editing tools. Coding agents that merely
# have a browser tool (Claude Code + Playwright) stay on the coding model, so they do not swap models on every step.
GUI_TOOL = re.compile(r"(^|[_.-])(northcua|computer|browser|playwright|puppeteer|desktop|mouse|keyboard|click|double_click|"
                      r"left_click|right_click|type_text|press_key|key_press|scroll|drag|hover|navigate|go_to_url|"
                      r"open_url|screenshot|take_screenshot|select_option|fill_form|tap|swipe)([_.-]|$)", re.I)
CODE_EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "str_replace_editor",
                   "str_replace_based_edit_tool", "create_file", "edit_file", "write_file"}
# Open Terminal (`🛠️ Coding Agents`, :8940) attaches its own file/shell/browser/desktop tools and system prompt
# whenever the user leaves the terminal toggle active in Open WebUI's composer. Those tools sit alongside Open WebUI's
# built-in tools (`OPENWEBUI_BUILTIN_MARK`) and must never block explicit `Desktop Use` / `Browser Use` MCP toggles,
# hijack plain-text `screen` routing, or leave a misleading terminal system prompt after tools are dropped.
OPEN_TERMINAL_MARK = {"run_command", "write_file"}
OPEN_TERMINAL_GUI_TOOLS = {
    "browser_action", "browser_screenshot", "browser_snapshot", "browser_tabs",
    "desktop_action", "desktop_screenshot",
}
OPEN_TERMINAL_PROMPT_RE = re.compile(
    r"You have access to a computer running [^\n]+\n+"
    r"Use your tools to directly interact with the system[^\n]*"
    r"(?:\n+When delivering created or updated files[^\n]*)?",
    re.S,
)


def strip_dropped_open_terminal_prompt(body):
    """Remove Open Terminal's injected system prompt once its tools (`run_command`/`write_file`) have been dropped."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return False
    if OPEN_TERMINAL_MARK & tool_names(body):
        return False
    changed = False
    new_msgs = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str) and "Use your tools to directly interact with the system" in c:
                cleaned = OPEN_TERMINAL_PROMPT_RE.sub("", c).strip()
                changed = True
                if cleaned:
                    m2 = dict(m)
                    m2["content"] = cleaned
                    new_msgs.append(m2)
                continue
        new_msgs.append(m)
    if changed:
        body["messages"] = new_msgs
    return changed


BROWSER_AGENT_SYSTEM = """You are a browser agent. You control a real web browser with the tools and report back.
Today is {today}; work out dates such as "tomorrow" or "Monday" from it.
- Go to the website the user names, at its official address (for example "the BBC" is https://www.bbc.co.uk). Do not
  swap it for a similar site. If you do not know the address, search for the site's name first.
- Read pages with browser_snapshot and act on elements by their ref; use screenshots and x,y clicks only when the
  snapshot is not enough.
- If an action did not change the page, do not repeat it: try a direct URL, another element, or the site's search.
- Do not narrate between tool calls; write one clean final answer only once you have the facts.
- Stop as soon as you have the answer. Reply in the user's language with the facts and the URL you read them on."""


PAGE_LIMIT_LAST = 60000    # characters of the newest page snapshot NorthCUA gets (~17K tokens)
PAGE_LIMIT_OLDER = 6000    # older pages in the same task (the model already acted on them)


def shrink_tool_pages(body):
    """Browser tool results (page snapshots) can be 450K characters: more than NorthCUA's 128K-token context.
    Compress them (page_text.py) and cap the size; returns the characters saved."""
    msgs = body.get("messages") or []
    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    saved = 0
    for i in tool_idx:
        limit = PAGE_LIMIT_LAST if i == tool_idx[-1] else PAGE_LIMIT_OLDER
        c = msgs[i].get("content")
        if isinstance(c, str) and len(c) > limit // 2:
            new = page_text.shrink(c, limit)
            saved += len(c) - len(new)
            msgs[i]["content"] = new
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text" and len(part.get("text", "")) > limit // 2:
                    new = page_text.shrink(part["text"], limit)
                    saved += len(part["text"]) - len(new)
                    part["text"] = new
    return saved


def computer_use_tools(body):
    """'' or a short reason when the request's tools make it a computer-use (GUI/browser agent) loop."""
    names, computer_tool = [], False
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("type", "")).startswith("computer_"):     # Anthropic computer-use tool
            computer_tool = True
        names.append(((t.get("function") or {}).get("name") or t.get("name") or "").lower())
    name_set = set(names)
    is_openwebui = OPENWEBUI_BUILTIN_MARK <= name_set
    # Explicit Desktop Use / NorthCUA MCP tools (:8933) always route to NorthCUA, even when Open WebUI's
    # Open Terminal (`🛠️ Coding Agents`) is also active and injects `write_file`.
    if any("desktop_use" in n or "northcua_desktop" in n or n.split("__")[-1] in northcua_harness.DESKTOP_TOOLS for n in names):
        return "Desktop Use tool"
    if any("northcua" in n for n in names):
        return "NorthCUA harness tool"
    # External coding agents (Claude Code / OpenCode + Playwright) stay on the coding model (Rule 3 exception).
    if not is_openwebui and any(n.split("__")[-1] in CODE_EDIT_TOOLS for n in names):
        return ""
    if computer_tool:
        return "computer-use tool"
    # In Open WebUI with Open Terminal attached, ignore Open Terminal's own 6 GUI tools so everyday chats stay off
    # NorthCUA while explicit `Browser Use` / `Browser Use (headless)` MCP tools still trigger NorthCUA.
    if is_openwebui and (OPEN_TERMINAL_MARK <= name_set):
        names = [n for n in names if n not in OPEN_TERMINAL_GUI_TOOLS]
    gui = [n for n in names if GUI_TOOL.search(n.split("__")[-1])]
    return f"{len(gui)} GUI/browser tools" if len(gui) >= 2 else ""


def has_image(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") in ("image", "image_url")
                                             for b in content)


IMAGE_TOKENS_EST = 1024


def _strip_b64_images(obj):
    """Replace base64 image payloads with tiny markers so token estimation counts ~1024 tokens per image."""
    if isinstance(obj, list):
        out, n, changed = [], 0, False
        for item in obj:
            si, c = _strip_b64_images(item)
            out.append(si)
            n += c
            if c:
                changed = True
        return (out if changed else obj), n
    if isinstance(obj, dict):
        t = obj.get("type")
        if t == "image_url":
            iu = obj.get("image_url")
            url = iu.get("url", "") if isinstance(iu, dict) else (iu or "")
            if isinstance(url, str) and url.startswith("data:"):
                return {"type": "image_url", "image_url": {"url": "<img>"}}, 1
        if t == "image" and isinstance(obj.get("source"), dict) and obj["source"].get("type") == "base64":
            return {"type": "image"}, 1
        out, n, changed = {}, 0, False
        for k, v in obj.items():
            sv, c = _strip_b64_images(v)
            out[k] = sv
            n += c
            if c:
                changed = True
        return (out if changed else obj), n
    if isinstance(obj, str) and obj.startswith("data:image/") and ";base64," in obj[:64]:
        return "<img>", 1
    return obj, 0


def estimate_tokens(body):
    msgs, n_imgs = _strip_b64_images(body.get("messages", ""))
    return int(len(json.dumps(msgs)) / 3.5) + n_imgs * IMAGE_TOKENS_EST + int(len(json.dumps(body.get("system", ""))) / 3.5)


CODE_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.S)


def has_code_history(messages, min_lines=15):
    """True when an earlier assistant answer holds a real code block (an app, a script), not a one-line snippet."""
    for m in messages[:-1]:
        if m.get("role") == "assistant":
            text = user_text(m.get("content"))
            if any(block.count("\n") >= min_lines for block in CODE_FENCE.findall(text)):
                return True
    return False


def has_tool_history(messages):
    """True once the assistant has called a tool in this conversation (Anthropic format)."""
    for m in messages:
        if m.get("role") == "assistant" and isinstance(m.get("content"), list):
            if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in m["content"]):
                return True
    return False


def lfm_json(body):
    """One structured call to the CPU labeller; returns the parsed JSON content."""
    body = {"model": ROUTER_MODEL, **body}
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=CLASSIFY_TIMEOUT_S)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        count_internal(data, body["model"], "lfm json")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {str(data)[:200]}")
        return json.loads(data["choices"][0]["message"]["content"])
    finally:
        conn.close()


def web_json(body):
    """One structured call to CPU Qwen3.5-4B for live web-search decisions; falls back to LFM3 if needed."""
    req = {"model": SEMANTIC_ROUTER_MODEL, **body}
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=CLASSIFY_TIMEOUT_S)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(req), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        count_internal(data, req["model"], "web decide")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {str(data)[:200]}")
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception:
        return lfm_json(body)
    finally:
        conn.close()


_WEB_COMPARE_RE = re.compile(
    r"\b(benchmarks?|compar(?:e|ison|ing)|equat(?:e|es)|vs\.?|versus|better\s+than|"
    r"best\s+(?:open|local|ai|llm|model|gpu)|release\s+date|version|pricing|context\s+window|"
    r"par\s+rapport\s+[àa]|que\s+vaut)\b",
    re.I,
)


OPENWEBUI_BUILTIN_MARK = {"get_current_timestamp", "calculate_timestamp"}   # always sent by Open WebUI (native tools)
PERSONAL_OWUI_TOOLS_RE = re.compile(
    r"\b(my notes?|in (?:my )?notes|create a note|write a note|save (?:this )?to (?:my )?(?:notes|memory)|"
    r"remember (?:this|that)|my memories|what do you remember|forget (?:this|that)|previous chats?|earlier chats?|past conversations?|"
    r"mes notes|dans mes notes|m[ée]morise|tu te souviens|oublie)\b",
    re.I,
)
OWUI_PERSONAL_TOOL_NAMES = {
    "get_current_timestamp", "calculate_timestamp",
    "search_notes", "view_note", "write_note", "replace_note_content",
    "search_memories", "add_memory", "replace_memory_content",
    "search_chats", "view_chat",
}
_web_cache = OrderedDict()   # message -> (time, system text or None, info): tool-call rounds reuse the lookup
RAG_TEMPLATE = re.compile(r"^\s*###\s*Task:\s*Respond to the user query", re.I)


def tool_names(body):
    names = set()
    for t in body.get("tools") or []:
        if isinstance(t, dict):
            names.add((t.get("function") or {}).get("name") or t.get("name") or "")
    return names


def has_external_tools(body):
    """True when the request carries client tools other than Open WebUI's built-in set (e.g. OpenCode's coding tools)."""
    names = tool_names(body)
    return bool(names) and not (OPENWEBUI_BUILTIN_MARK <= names)


def has_structured_response_format(body):
    """True when the caller requested native JSON schema / JSON object output via OpenAI response_format."""
    rf = body.get("response_format") if isinstance(body, dict) else None
    return isinstance(rf, dict) and rf.get("type") in ("json_schema", "json_object")


def is_image_gen_request(text):
    if not text:
        return False
    snippet = text[:CLASSIFY_MAX_CHARS]
    if IMAGE_GEN_CODE_EXCLUDE_RE.search(snippet):
        return False
    return bool(IMAGE_GEN_REQUEST_RE.search(snippet))


def _image_kwargs(body):
    size = body.get("size")
    size = None if not size or str(size).lower() == "auto" else str(size)
    quality = str(body.get("quality") or "").lower()
    raw_steps = body.get("steps")
    steps = int(raw_steps) if raw_steps not in (None, "") else (IMAGE_QUALITY_STEPS.get(quality) if quality else None)
    raw_seed = body.get("seed")
    seed = int(raw_seed) if raw_seed not in (None, "") else None
    return dict(
        size=size, aspect=body.get("aspect_ratio"), megapixels=body.get("megapixels"), steps=steps, seed=seed,
        negative=body.get("negative_prompt"), enhance=body.get("enhance"), unet=body.get("unet"),
        text_encoder=body.get("text_encoder"), prefix=body.get("filename_prefix"),
    )


def _parse_images_edit_request(content_type, raw_bytes):
    ctype = (content_type or "").strip()
    if ctype.lower().startswith("multipart/form-data"):
        hdr = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1", "replace")
        msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(hdr + raw_bytes)
        fields, images = {}, []
        for part in msg.walk():
            if part.is_multipart():
                continue
            name = part.get_param("name", header="content-disposition") or ""
            fname = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if fname or name in ("image", "image[]", "images"):
                if payload:
                    images.append((payload, fname or "input.png", part.get_content_type() or "image/png"))
            elif name:
                fields[name] = payload.decode("utf-8", "replace")
        return fields, images
    body = json.loads(raw_bytes or b"{}")
    images = []
    raw_imgs = body.get("images") if body.get("images") is not None else body.get("image")
    if isinstance(raw_imgs, str):
        raw_imgs = [raw_imgs]
    for idx, item in enumerate(raw_imgs or [], 1):
        if isinstance(item, str) and item.strip():
            s = item.strip()
            mime = "image/png"
            if s.startswith("data:") and "," in s:
                head, s = s.split(",", 1)
                mime = head[5:].split(";")[0].strip() or "image/png"
            images.append((base64.b64decode(s), f"input_{idx}.png", mime))
    return body, images


def _chat_image_file_urls(messages):
    """Collect deduplicated Open WebUI /api/v1/files/<id>/content image URLs in chronological order."""
    seen, urls = set(), []

    def _add(url):
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    slide_call_ids = set()
    for m in messages or []:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if fn in (
                    SLIDES_TOOLS | ADVANCED_DECK_TOOLS | FAMILY_TABLE_TOOLS
                    | EXPORT_DOC_TOOLS | DEEP_RESEARCH_TOOLS | SMART_VISUALS_TOOLS | FINANCE_TOOLS
                ) and tc.get("id"):
                    slide_call_ids.add(tc["id"])

    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "tool"):
            continue
        if role == "tool" and m.get("tool_call_id") in slide_call_ids:
            continue
        raw = m.get("content")
        txt = user_text(raw) if isinstance(raw, (str, list)) else json.dumps(raw or "", ensure_ascii=False)
        if not txt:
            continue
        if role == "tool":
            if _PPTX_REF_RE.search(txt):
                continue
            for u in _OWUI_FILE_URL_RE.findall(txt):
                _add(u)
            continue
        consumed = set()
        for tag in _FILE_TAG_RE.findall(txt):
            is_img = bool(
                re.search(r'type="image"', tag, re.I)
                or re.search(r'content_type="image/', tag, re.I)
                or re.search(r'name="[^"]*\.(?:png|jpe?g|webp|gif|bmp)"', tag, re.I)
            )
            has_ctype = bool(re.search(r'content_type="[^"]+"', tag, re.I))
            m_u = _OWUI_FILE_URL_RE.search(tag)
            if m_u:
                u = m_u.group(0)
                consumed.add(u)
                if is_img or not has_ctype:
                    _add(u)
            elif is_img:
                m_uuid = _FILE_URL_UUID_RE.search(tag) or _FILE_ID_UUID_RE.search(tag)
                if m_uuid:
                    _add(m_uuid.group(2))
        for u in _OWUI_FILE_URL_RE.findall(txt):
            if u not in consumed:
                _add(u)
    return urls


def has_prior_or_attached_image(messages):
    if _chat_image_file_urls(messages):
        return True
    for m in messages or []:
        if has_image(m.get("content")):
            return True
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if fn in IMAGE_GEN_TOOLS:
                    return True
    return False


def has_image_gen_intent(body):
    """True when the user explicitly asked to generate/edit an image (EN/FR), toggled Open WebUI's Image switch,
    issued an edit follow-up on a prior/attached image, or is continuing right after generate_image/edit_image ran."""
    msgs = body.get("messages") or []
    for m in msgs:
        if m.get("role") in ("system", "developer") and IMAGE_MODE_HINT in user_text(m.get("content")):
            return True
    last_u_idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
    if last_u_idx is None:
        return False
    for m in msgs[last_u_idx + 1:]:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if fn in IMAGE_GEN_TOOLS:
                    return True
    txt = _ATTACHED_FILES_RE.sub("", user_text(msgs[last_u_idx].get("content"))).strip()
    if is_image_gen_request(txt):
        return True
    snippet = txt[:CLASSIFY_MAX_CHARS]
    if not snippet or IMAGE_GEN_CODE_EXCLUDE_RE.search(snippet) or SCREEN_WORDS.search(snippet):
        return False
    if IMAGE_EDIT_DIRECT_RE.search(snippet):
        return True
    if has_prior_or_attached_image(msgs) and not PURE_IMAGE_QUESTION_RE.search(snippet) and IMAGE_EDIT_FOLLOWUP_RE.search(snippet):
        return True
    return False


ROUTE_CACHE_TTL_S = 60.0
OWUI_DB_PATH = Path.home() / "AI" / "open-webui" / "data" / "webui.db"
_semantic_route_cache = OrderedDict()   # (cat_hash, recent_tools, snippet) -> (ts, picked_tuple, tier)
_last_owui_custom_tools = ()
_owui_db_tools_cache = (0.0, ())
_YOUTUBE_CODE_EXCLUDE_RE = re.compile(r"\b(python|script|code|scrape|scrap(?:er|ing)|javascript|node|curl|yt-dlp)\b", re.I)


def _is_gui_or_agent_tool(name):
    low = (name or "").lower()
    short = name.split("__")[-1] if name else ""
    return (
        not name
        or name in OWUI_BUILTIN_TOOL_NAMES
        or name in ADVANCED_DECK_TOOLS
        or name in FAMILY_TABLE_TOOLS
        or name in IMAGE_GEN_TOOLS
        or low in AGENT_ONLY_TOOL_NAMES
        or "desktop_use" in low
        or "northcua" in low
        or "playwright" in low
        or low.startswith("browser_")
        or short in northcua_harness.DESKTOP_TOOLS
        or short in northcua_harness.HARNESS_TOOLS
    )


def discover_custom_tools(body):
    """Extract custom Open WebUI / external user tools [(name, short_desc), ...] dynamically from body['tools']."""
    global _last_owui_custom_tools
    raw_tools = body.get("tools") or []
    by_name = {}
    for t in raw_tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = (fn.get("name") or "").strip()
        if not name or _is_gui_or_agent_tool(name):
            continue
        raw_desc = (fn.get("description") or "").strip()
        if raw_desc:
            desc = " ".join(raw_desc.splitlines()[0].split())[:140]
        else:
            desc = KNOWN_TOOL_FALLBACK_DESCS.get(name, name)
        by_name[name] = desc
    catalog = tuple(sorted(by_name.items()))
    if OPENWEBUI_BUILTIN_MARK <= tool_names(body):
        with _lock:
            _last_owui_custom_tools = catalog
    return catalog


def is_coding_agent_tools(body):
    """True for coding-agent callers (OpenCode / Claude Code / shell + file tools), False for Open WebUI & custom tools."""
    names = tool_names(body)
    if not names or OPENWEBUI_BUILTIN_MARK <= names:
        return False
    if {n.lower() for n in names} & AGENT_ONLY_TOOL_NAMES:
        return True
    if names & (ADVANCED_DECK_TOOLS | FAMILY_TABLE_TOOLS | AUTO_POWER_TOOL_NAMES | SLIDES_TOOLS):
        return False
    return not bool(discover_custom_tools(body))


def _get_fallback_owui_custom_tools():
    """Cold-start fallback for POST /route-check when Open WebUI has not sent body['tools'] yet."""
    global _owui_db_tools_cache
    now = time.time()
    with _lock:
        ts, cached = _owui_db_tools_cache
    if cached and now - ts < 30.0:
        return cached
    if not OWUI_DB_PATH.is_file():
        return ()
    try:
        import sqlite3
        con = sqlite3.connect(str(OWUI_DB_PATH), timeout=1.0)
        try:
            row = con.execute("SELECT meta FROM model WHERE id='auto'").fetchone()
            attached = set((json.loads(row[0] or "{}").get("toolIds") or [])) if row else set()
            by_name = {}
            for tid, specs_s in con.execute("SELECT id, specs FROM tool WHERE is_active=1"):
                if attached and tid not in attached:
                    continue
                for s in json.loads(specs_s or "[]"):
                    n = (s.get("name") or "").strip()
                    if not n or _is_gui_or_agent_tool(n):
                        continue
                    raw_desc = (s.get("description") or "").strip()
                    desc = " ".join(raw_desc.splitlines()[0].split())[:140] if raw_desc else KNOWN_TOOL_FALLBACK_DESCS.get(n, n)
                    by_name[n] = desc
        finally:
            con.close()
        catalog = tuple(sorted(by_name.items()))
    except Exception:
        catalog = ()
    with _lock:
        _owui_db_tools_cache = (now, catalog)
    return catalog


def classify_route_and_tools(text, custom_tools, recent_tools=()):
    """Unified CPU semantic router on Qwen3.5-4B: returns (selected_tools_tuple, tier). Cached 60s."""
    if not custom_tools or not text:
        return (), ""
    t_names = [n for n, _ in custom_tools]
    cat_txt = "\n".join(f"- {n}: {d}" for n, d in custom_tools)
    cat_hash = hashlib.sha1(cat_txt.encode("utf-8", "ignore")).hexdigest()[:12]
    rec_tup = tuple(recent_tools or ())
    snip = text[:1500]
    ckey = (cat_hash, rec_tup, snip)
    now = time.time()
    with _lock:
        hit = _semantic_route_cache.get(ckey)
        if hit and now - hit[0] < ROUTE_CACHE_TTL_S:
            _semantic_route_cache.move_to_end(ckey)
            return hit[1], hit[2]
    sys_msg = SEMANTIC_ROUTER_SYSTEM_TEMPLATE.format(catalog=cat_txt)
    user_parts = []
    if rec_tup:
        user_parts.append(f"RECENT CHAT TOOLS: {', '.join(rec_tup)}")
    user_parts.append(f"REQUEST:\n<<<\n{snip}\n>>>")
    req_body = {
        "model": SEMANTIC_ROUTER_MODEL,
        "temperature": 0,
        "max_tokens": 80,
        "reasoning_effort": "none",
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "semantic_route",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "tools": {"type": "array", "items": {"type": "string", "enum": t_names}},
                        "tier": {"type": "string", "enum": ["fast", "code", "deep"]},
                    },
                    "required": ["tools", "tier"],
                    "additionalProperties": False,
                },
            },
        },
        "messages": [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ],
    }
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=CLASSIFY_TIMEOUT_S)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(req_body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        count_internal(data, SEMANTIC_ROUTER_MODEL, "semantic route")
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}: {str(data)[:200]}")
        out = json.loads(data["choices"][0]["message"]["content"])
        valid = set(t_names)
        picked = tuple(t for t in (out.get("tools") or []) if t in valid)
        tier = out.get("tier") if out.get("tier") in ("fast", "code", "deep") else ""
    except Exception:
        return (), ""
    finally:
        conn.close()
    with _lock:
        _semantic_route_cache[ckey] = (time.time(), picked, tier)
        _semantic_route_cache.move_to_end(ckey)
        while len(_semantic_route_cache) > 256:
            _semantic_route_cache.popitem(last=False)
    return picked, tier


def _expand_tool_siblings(selected, avail_names):
    out = set(selected) & set(avail_names)
    for grp in TOOL_SIBLING_GROUPS:
        if out & grp:
            out |= (grp & set(avail_names))
    return out


def _brief_for_tools(selected_set):
    for tset, sys_prompt, budget, label, skip_web in KNOWN_TOOL_BRIEFS:
        if selected_set & tset:
            return sys_prompt, budget, label, skip_web
    return GENERIC_CUSTOM_TOOL_SYSTEM, 4096, f"Custom tool ({', '.join(sorted(selected_set))})", True


def select_custom_tools(body, allow_cached_catalog=False, extra_catalog=None):
    """Discover custom tools in body and match relevant tool(s) semantically (cached 60s across pipeline stages)."""
    empty = {"tools": set(), "tier": "", "ran": False, "system": "", "budget": None, "label": "", "skip_web": True}
    catalog = discover_custom_tools(body)
    if not catalog and extra_catalog:
        catalog = extra_catalog
    if not catalog and allow_cached_catalog:
        with _lock:
            catalog = _last_owui_custom_tools
        if not catalog:
            catalog = _get_fallback_owui_custom_tools()
    if not catalog:
        return empty
    avail_names = {n for n, _ in catalog}
    msgs = body.get("messages") or []
    last_u_idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
    if last_u_idx is None:
        return empty

    # Fast path 1 (0 ms): mid-tool-loop after a custom tool already ran for this user turn
    mid_picked = []
    for m in msgs[last_u_idx + 1:]:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if fn in avail_names:
                    mid_picked.append(fn)
    if mid_picked:
        expanded = _expand_tool_siblings(mid_picked, avail_names)
        sys_p, bud, lbl, sw = _brief_for_tools(expanded)
        return {"tools": expanded, "tier": "deep", "ran": False, "system": sys_p, "budget": bud, "label": lbl, "skip_web": sw}

    raw_u = user_text(msgs[last_u_idx].get("content"))
    txt = _ATTACHED_FILES_RE.sub("", raw_u).strip()
    snippet = txt[:CLASSIFY_MAX_CHARS]
    if not raw_u:
        return empty

    # Fast path 2 (0 ms): literal YouTube URL in prompt
    if (YOUTUBE_TOOLS & avail_names) and YOUTUBE_URL_RE.search(snippet) and not _YOUTUBE_CODE_EXCLUDE_RE.search(snippet):
        expanded = YOUTUBE_TOOLS & avail_names
        sys_p, bud, lbl, sw = _brief_for_tools(expanded)
        return {"tools": expanded, "tier": "deep", "ran": False, "system": sys_p, "budget": bud, "label": lbl, "skip_web": sw}

    # Fast path 3 (0 ms): attached .pptx/.potx file reference
    if (SLIDES_TOOLS & avail_names) and _PPTX_REF_RE.search(raw_u) and not SLIDES_CODE_EXCLUDE_RE.search(snippet):
        expanded = _expand_tool_siblings(SLIDES_TOOLS & avail_names, avail_names)
        sys_p, bud, lbl, sw = _brief_for_tools(expanded)
        return {"tools": expanded, "tier": "deep", "ran": False, "system": sys_p, "budget": bud, "label": lbl, "skip_web": sw}

    if not snippet:
        return empty

    recent = []
    for m in msgs[:last_u_idx]:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if fn in avail_names and fn not in recent:
                    recent.append(fn)

    picked, tier = classify_route_and_tools(snippet, catalog, recent[-4:])
    if picked and (set(picked) & SLIDES_TOOLS) and SLIDES_CODE_EXCLUDE_RE.search(snippet):
        picked = tuple(p for p in picked if p not in SLIDES_TOOLS)
    ran = bool(picked or tier)
    if picked:
        expanded = _expand_tool_siblings(picked, avail_names)
        sys_p, bud, lbl, sw = _brief_for_tools(expanded)
        return {"tools": expanded, "tier": "deep", "ran": ran, "system": sys_p, "budget": bud, "label": lbl, "skip_web": sw}
    return {"tools": set(), "tier": tier, "ran": ran, "system": "", "budget": None, "label": "", "skip_web": True}


def has_prior_or_attached_pptx(messages):
    for m in messages or []:
        if _PPTX_REF_RE.search(user_text(m.get("content"))):
            return True
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name") or tc.get("name") or ""
                if fn in SLIDES_TOOLS:
                    return True
    return False


def _has_tool_family_intent(body, family_set):
    extra = None if (family_set & tool_names(body)) else tuple(sorted(KNOWN_TOOL_FALLBACK_DESCS.items()))
    sel = select_custom_tools(body, extra_catalog=extra)
    return bool(sel["tools"] & family_set)


def has_slides_intent(body):
    return _has_tool_family_intent(body, SLIDES_TOOLS)


def has_google_workspace_intent(body):
    return _has_tool_family_intent(body, GOOGLE_WORKSPACE_TOOLS)


def has_youtube_intent(body):
    return _has_tool_family_intent(body, YOUTUBE_TOOLS)


def has_deep_research_intent(body):
    return _has_tool_family_intent(body, DEEP_RESEARCH_TOOLS)


def has_smart_visuals_intent(body):
    return _has_tool_family_intent(body, SMART_VISUALS_TOOLS)


def has_finance_intent(body):
    return _has_tool_family_intent(body, FINANCE_TOOLS)


def has_export_doc_intent(body):
    return _has_tool_family_intent(body, EXPORT_DOC_TOOLS)


QUERY_WRITER_SYSTEM = (
    "You write web search queries. Reply with ONE clean search query only (at most 12 words), nothing else — no quotes, "
    "no markdown, and NEVER include deliverable/format words like 'PowerPoint', 'slides', 'deck', 'presentation', "
    "'diaporama', 'PDF', or 'summary' (e.g. 'Fais-moi un PowerPoint de 5 slides sur l'actualité IA de cette semaine' "
    "-> 'AI news major model launches announcements November 2026').\n"
    "Today is {today}. The user is a member of the public in the UK unless they say otherwise. Target what they "
    "want to know right now: official public guidance, breaking news, and primary releases, not clinical or trade "
    "notices. Write out exact dates/months (e.g. November 2026). For global tech, AI, science, or finance news, "
    "always write the query in English so the very latest global launches surface immediately (keep French/local "
    "language only for France-specific local topics).")
_QUERY_FORMAT_NOISE_RE = re.compile(
    r"\b(power\s*point|pptx?|slide\s*decks?|slides?|presentations?|pr[eé]sentations?|diaporamas?)\b",
    re.I,
)


def write_query(model, text, prev, for_slides=False):
    """The answering model (already loaded) writes the search query: much better than the 1.7B labeller
    (2026-11-07: 'NHS alerts' -> clinical safety notices with the labeller's query). None on any problem."""
    msg = (f"Earlier questions: {prev}\n" if prev else "") + f"New message: {text[:1500]}"
    body = {"model": model, "temperature": 0, "max_tokens": 40, "reasoning_effort": "none",
            "messages": [{"role": "system", "content": QUERY_WRITER_SYSTEM.format(today=web_search.today_text())},
                         {"role": "user", "content": msg}]}
    conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=30)
    try:
        conn.request("POST", "/v1/chat/completions", json.dumps(body), {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        count_internal(data, model, "search query")
        q = (data["choices"][0]["message"].get("content") or "").strip().strip('"').splitlines()[0].strip()
        if for_slides and q:
            q = re.sub(r"\s{2,}", " ", _QUERY_FORMAT_NOISE_RE.sub("", q)).strip(" -:,_")
        return q if 3 <= len(q) <= 200 else None
    except Exception:
        return None
    finally:
        conn.close()


EXPLICIT_WEB_IN_IMAGE_RE = re.compile(
    r"\b(search (?:the )?(?:web|internet|online)|look (?:it )?up online|check online|"
    r"cherch\w+ sur (?:internet|le web))\b",
    re.I,
)


def add_live_info(body, door, tier, reason, model=None, models=None, on_status=None, rich_citations=False):
    """Web search / weather for messages that need live facts. Returns log info (or None when not considered)."""
    force = door == "openai" and "search_web" in tool_names(body)     # Open WebUI's Web Search switch is on
    if tier not in ("fast", "think", "deep", "flash") or "uses tools" in reason:
        return None
    if body.get("_internal_advanced_deck") or body.get("_internal_family_table") or body.get("_internal_deep_research") or body.get("_internal_smart_visuals"):
        return None
    if door == "openai" and ((ADVANCED_DECK_TOOLS | FAMILY_TABLE_TOOLS) & tool_names(body)):
        return None   # tool runs its own live search/data pipeline internally
    sel = select_custom_tools(body) if door == "openai" else {"tools": set(), "skip_web": True}
    if sel["tools"] and sel["skip_web"]:
        return None   # dedicated local/custom tool answers directly without duplicate Tavily search
    if not force and (
        "image generation" in reason
        or (door == "openai" and (IMAGE_GEN_TOOLS & tool_names(body)) and has_image_gen_intent(body))
    ):
        return None
    if door == "anthropic" and tier != "fast":          # Claude Code: only its everyday questions (Gemma)
        return None
    if door == "openai" and is_coding_agent_tools(body):    # OpenCode / external coding agent with its own tools
        return None
    messages = body.get("messages") or []
    idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
    if idx is None:
        return None
    text = user_text(messages[idx].get("content"))
    if not text or TRIVIAL.match(text) or RAG_TEMPLATE.match(text):
        return None
    if not force and has_image(messages[idx].get("content")) and not EXPLICIT_WEB_IN_IMAGE_RE.search(text):
        return None
    snip = text[:CLASSIFY_MAX_CHARS]
    web_compare = bool(
        _WEB_COMPARE_RE.search(snip)
        and not BUILD_REQUEST.search(snip)
        and not CODE_FENCE.search(text)
        and not has_code_history(messages)
    )
    if not force and is_coding_request(tier, reason) and not web_compare:
        return None
    if not force and not web_compare and (
        BUILD_REQUEST.search(snip)
        or SOFTWARE_WORDS.search(snip)
        or has_code_history(messages)
    ):
        return None
    for_slides = bool(sel["tools"] & SLIDES_TOOLS)
    key = (
        conversation_key(messages)
        + hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
        + f"|f={int(bool(force))}|rc={int(bool(rich_citations))}|fs={int(for_slides)}"
    )
    with _lock:
        hit = _web_cache.get(key)
    if hit and time.time() - hit[0] < web_search.CACHE_S:
        block, info = hit[1], {**copy.deepcopy(hit[2]), "cached": True}
        ui_cached = info.get("_ui") or {}
        if on_status and ui_cached.get("status_done"):
            on_status(ui_cached["status_done"])
    else:
        # context for the query: the user's previous questions (short, on-topic), not the long previous answer
        prev = " / ".join(t for t in (user_text(m.get("content"))[:200] for m in messages[:idx]
                                      if m.get("role") == "user") if t and not OPENWEBUI_TASK.match(t))[-400:]
        t = time.time()
        try:
            d = web_search.decide(web_json, text, prev, force=force)
            if d.get("web") and not d.get("weather"):
                if model and models and is_loaded(models, model):
                    if on_status:
                        on_status(rich_ui.status_event("🔍 Preparing search query…", done=False))
                    q = write_query(model, text, prev, for_slides=for_slides)
                    if q:
                        d["query"], d["query_by"] = q, model
                elif for_slides or d.get("backstop") or not d.get("query"):
                    q_writer = FLASH_MODEL if (models and is_loaded(models, FLASH_MODEL)) else None
                    if q_writer:
                        if on_status:
                            on_status(rich_ui.status_event("🔍 Preparing search query…", done=False))
                        q = write_query(q_writer, text, prev, for_slides=for_slides)
                        if q:
                            d["query"], d["query_by"] = q, q_writer
                else:
                    d["query_by"] = SEMANTIC_ROUTER_MODEL   # Qwen3.5-4B already generated a clean search query
        except Exception as e:
            return {"web": f"decision failed: {e.__class__.__name__}"}
        info = {"web": "no", "web_decision_ms": int((time.time() - t) * 1000)}
        block = None
        if d.get("web"):
            block, more = web_search.lookup(
                key, d, on_status=on_status, rich_citations=rich_citations,
                user_message=text, for_slides=for_slides,
            )
            info.update(more)
            if d.get("query_by"):
                info["query_by"] = d["query_by"]
            if d.get("backstop"):
                info["web"] += " (backstop)"
            if force:
                info["web"] += " (switch on)"
        with _lock:
            _web_cache[key] = (time.time(), block, copy.deepcopy(info))
            while len(_web_cache) > 200:
                _web_cache.popitem(last=False)
    if block:
        inject_system(body, door, block)
        if door == "openai" and OPENWEBUI_BUILTIN_MARK <= tool_names(body) and not force:
            if sel["tools"]:
                pass   # _dispatch keeps only sel["tools"] so live web results can feed directly into a deck/visual
            else:
                # Open WebUI's built-in tools: with web results attached, Gemma otherwise searched its empty knowledge
                # bases 7 times before answering (2026-11-07 16:18, "NHS alerts")
                info["tools_dropped"] = len(body.pop("tools", []) or [])
                body.pop("tool_choice", None)
    return info


def inject_system(body, door, text):
    if door == "anthropic":
        sysp = body.get("system")
        if isinstance(sysp, list):
            sysp.append({"type": "text", "text": text})
        else:
            body["system"] = (str(sysp) + "\n\n" if sysp else "") + text
        return
    msgs = body.setdefault("messages", [])
    if msgs and msgs[0].get("role") in ("system", "developer"):
        c = msgs[0].get("content")
        if isinstance(c, list):
            c.append({"type": "text", "text": text})
        else:
            msgs[0]["content"] = (str(c) + "\n\n" if c else "") + text
    else:
        msgs.insert(0, {"role": "system", "content": text})


def conversation_key(messages):
    for m in messages:
        if m.get("role") == "user":
            raw = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"])[:4000]
            return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    return "none"


# ---------------------------------------------------------------- routing
def route_check(body):
    """Fast CPU-only preflight for Open WebUI 'Auto': decides whether to hand the turn to Open Code ('opencode')
    or keep it as a normal router chat ('chat'). Never acquires the GPU lock or swaps a GPU model."""
    messages = body.get("messages") or []
    last = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if not last:
        return {"dispatch": "chat", "reason": "no user message"}
    content = last.get("content")
    text = user_text(content)
    if not text or TRIVIAL.match(text) or OPENWEBUI_TASK.match(text) or RAG_TEMPLATE.match(text):
        return {"dispatch": "chat", "reason": "trivial or housekeeping"}
    if has_image(content):
        return {"dispatch": "chat", "reason": "image input"}
    snippet = text[:CLASSIFY_MAX_CHARS]
    if EXPLICIT_OPENCODE_RE.search(snippet):
        return {"dispatch": "opencode", "shape": "opencode", "domain": "logic_ui", "reason": "explicit OpenCode request"}
    if has_image_gen_intent(body):
        return {"dispatch": "chat", "tier": "fast", "reason": "image generation"}
    if ADVANCED_DECK_TOOLS & tool_names(body):
        return {"dispatch": "chat", "tier": "deep", "reason": "Advanced Deck tool selected"}
    if FAMILY_TABLE_TOOLS & tool_names(body):
        return {"dispatch": "chat", "tier": "deep", "reason": "Family Table tool selected"}
    if voice_tts.is_voice_narration_request(text):
        return {"dispatch": "chat", "tier": "voice", "reason": "voice narration request"}
    if not BUILD_REQUEST.search(snippet) and (
        northcua_harness.is_desktop_task(text) or northcua_harness.is_browser_task(text)
    ):
        return {"dispatch": "chat", "reason": "desktop/browser task"}

    sel = select_custom_tools(body, allow_cached_catalog=True)
    if sel["tools"]:
        return {"dispatch": "chat", "tier": "deep", "reason": sel["label"]}
    if has_code_history(messages):
        return {"dispatch": "chat", "reason": "existing chat with code"}

    if not (
        THREED_WORDS.search(snippet)
        or SIM_3D_WORDS.search(snippet)
        or MULTIFILE_AGENT_RE.search(snippet)
        or BUILD_REQUEST.search(snippet)
    ):
        label = sel["tier"] if sel["ran"] and sel["tier"] else None
        if label is None:
            try:
                label = classify_with_lfm(text)
            except Exception as e:
                return {"dispatch": "chat", "reason": f"labeller unavailable ({e.__class__.__name__})"}
        if MATH_PROBLEM_RE.search(snippet) and not CODE_FENCE.search(text):
            label = "deep"
        if label == "fast":
            return {"dispatch": "chat", "tier": "fast", "reason": "everyday question"}
        if label == "deep" and not (BUILD_REQUEST.search(snippet) or SOFTWARE_WORDS.search(snippet)):
            return {"dispatch": "chat", "tier": "deep", "reason": "non-coding complex reasoning"}

    cinfo = classify_coding_with_lfm(text)
    dispatch = "opencode" if cinfo["shape"] == "opencode" else "chat"
    return {
        "dispatch": dispatch,
        "shape": cinfo["shape"],
        "domain": cinfo["domain"],
        "reason": cinfo["reason"],
    }


def is_voice_chat_request(body, door="openai"):
    """(True, reason) when a chat completion should be narrated by Podcast Voice Studio (never triggers on housekeeping)."""
    if door != "openai" or not isinstance(body, dict):
        return False, ""
    model = str(body.get("model") or "").lower()
    bare = model[6:] if model.startswith("local-") else model
    msgs = body.get("messages") or []
    last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
    last_text = user_text(last.get("content")) if last else ""
    if last and OPENWEBUI_TASK.match(last_text):
        return False, ""
    if bare in ("voice", "podcast"):
        return True, "forced by model name"
    if bare == "auto" and last_text and not is_coding_agent_tools(body) and not RAG_TEMPLATE.match(last_text):
        if voice_tts.is_voice_narration_request(last_text):
            return True, "voice narration request -> Podcast Voice Studio"
    return False, ""


def decide(body, door):
    """(tier, reason, forced). tier None means pass through untouched; 'background' is Claude Code housekeeping."""
    model = str(body.get("model", "")).lower()
    bare = model[6:] if model.startswith("local-") else model
    if bare == "pro":
        bare = "deep"
    if (door == "anthropic" and "haiku" in model) or bare == "background":
        return "background", "background call (titles, summaries)", False
    last = next((m for m in reversed(body.get("messages") or []) if m.get("role") == "user"), None)
    if door == "openai" and last and OPENWEBUI_TASK.match(user_text(last.get("content"))):
        return "background", "Open WebUI task (title, tags, follow-ups)", False
    first_sys = next((m for m in (body.get("messages") or []) if m.get("role") == "system"), None)
    if door == "openai" and first_sys and "You name conversations so the user can find them later" in user_text(first_sys.get("content")):
        return "background", "OpenCode title call", False
    if bare in ("voice", "podcast"):
        return "voice", "forced by model name", True
    cua = computer_use_tools(body)
    if cua and bare != "screen":
        return "screen", f"computer-use loop ({cua}): NorthCUA", True
    if bare == MODELS["screen"]:
        return "screen", "NorthCUA picked directly", True
    last_text = user_text(last.get("content")) if last else ""
    if door == "openai" and last_text and not is_coding_agent_tools(body) \
            and not BUILD_REQUEST.search(last_text[:CLASSIFY_MAX_CHARS]) \
            and not has_code_history(body.get("messages") or []):
        if northcua_harness.is_desktop_task(last_text):
            return "screen", "explicit desktop request: Desktop Use (NorthCUA)", True
        if northcua_harness.is_browser_task(last_text):
            if northcua_harness.is_explicit_harness_request(last_text):
                return "screen", "explicit NorthCUA harness request: NorthCUA Harness", True
            return "screen", "explicit browser/NorthCUA request: Browser MCP (NorthCUA)", True
    if door == "openai" and bare in ("think", "code", "auto") and is_coding_agent_tools(body):
        toks = _input_and_tools_tokens(body)
        if toks > AGENT_PRO_OVERFLOW_TOKENS:
            return "deep", f"coding agent ({bare}) ~{toks} tok > {AGENT_PRO_OVERFLOW_TOKENS // 1000}K -> Pro (256K)", True
        msgs_a = body.get("messages") or []
        is_pptx_session = bool(
            _PPTX_REF_RE.search(last_text)
            or SLIDES_FALLBACK_RE.search(last_text[:CLASSIFY_MAX_CHARS])
            or any(
                any(k in user_text(m.get("content")) for k in ("pptx_studio", "inspect_pptx_visually", "search_slide_facts", "publish_pptx", ".pptx"))
                for m in msgs_a
            )
        )
        if is_pptx_session:
            return "deep", f"coding agent ({bare}) bespoke PPTX -> Pro (45 tok/s, {AGENT_THINK_BUDGET} think cap)", True
        return "think", f"coding agent ({bare}) -> Qwen3.8 + {AGENT_THINK_BUDGET} think cap", True
    if door == "anthropic" and bare in ("think", "code", "auto") and _input_and_tools_tokens(body) > AGENT_PRO_OVERFLOW_TOKENS:
        return "deep", f"Claude Code ({bare}) > {AGENT_PRO_OVERFLOW_TOKENS // 1000}K -> Pro (256K)", True
    if bare in RETIRED_MODEL_REDIRECTS:
        return RETIRED_MODEL_REDIRECTS[bare], f"redirected retired {bare} -> {RETIRED_MODEL_REDIRECTS[bare]}", True
    if bare in TIERS:
        return bare, "forced by model name", True
    raw_tier = RAW_MODEL_TIER.get(bare)
    if raw_tier and door == "openai" and OPENWEBUI_BUILTIN_MARK <= tool_names(body):
        # a raw model picked in Open WebUI got its 42 built-in tools and no thinking cap (Qwen3.8 ran 15+ min,
        # 2026-11-08 21:01): give it its tier's protections. Benchmarks/scripts (no Open WebUI tools) stay raw.
        return raw_tier, f"{bare} picked in Open WebUI: same protections as the {raw_tier} tier", True
    if door == "openai" and bare != "auto":
        return None, "passthrough", False
    if cua:
        return "screen", f"computer-use loop ({cua}): NorthCUA", True
    if door == "openai" and bare == "auto" and last_text and not is_coding_agent_tools(body) \
            and not RAG_TEMPLATE.match(last_text) and voice_tts.is_voice_narration_request(last_text):
        return "voice", "voice narration request -> Podcast Voice Studio", True

    messages = body.get("messages") or []
    key = conversation_key(messages)
    last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    content = last_user.get("content") if last_user else ""
    text = user_text(content)
    snippet = text[:CLASSIFY_MAX_CHARS]
    with _lock:
        cached = _conversation_tier.get(key)
    if (not text or TRIVIAL.match(text)) and not has_image(content):
        if cached:
            if door == "anthropic" and cached in ("think", "code", "auto") and _input_and_tools_tokens(body) > AGENT_PRO_OVERFLOW_TOKENS:
                return "deep", f"reused decision; agent context >{AGENT_PRO_OVERFLOW_TOKENS // 1000}K -> Pro (256K)", False
            return cached, "reused from this message's decision", False
        return ("think" if door == "anthropic" else "fast"), "default (no new text)", False

    tier, reason = None, ""
    if door == "openai" and (IMAGE_GEN_TOOLS & tool_names(body)) and has_image_gen_intent(body):
        with _lock:
            _conversation_tier[key] = "fast"
            _conversation_tier.move_to_end(key)
        return "fast", "image generation request -> Flash GPU (generate_image -> ComfyUI)", True
    if door == "openai" and (ADVANCED_DECK_TOOLS & tool_names(body)):
        # User explicitly toggled Advanced Deck in Open WebUI's + -> Tools menu.
        # If build_advanced_deck just finished and Gemma is already warm (no polish pass ran), answer on fast (zero swap).
        last_u_idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
        post_adv = last_u_idx is not None and any(
            m.get("role") == "assistant" and any(
                ((tc.get("function") or {}).get("name") or tc.get("name") or "") in ADVANCED_DECK_TOOLS
                for tc in (m.get("tool_calls") or [])
            )
            for m in messages[last_u_idx + 1:]
        )
        if post_adv:
            loaded_m = lmstudio_models()
            if is_loaded(loaded_m, MODELS["fast"]) and not is_loaded(loaded_m, MODELS["deep"]):
                return "fast", "Advanced Deck post-tool reply (Flash GPU already warm)", True
        with _lock:
            _conversation_tier[key] = "deep"
            _conversation_tier.move_to_end(key)
        return "deep", "Advanced Deck tool selected -> Pro (build_advanced_deck)", True
    if door == "openai" and (FAMILY_TABLE_TOOLS & tool_names(body)):
        last_u_idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
        post_ft = last_u_idx is not None and any(
            m.get("role") == "assistant" and any(
                ((tc.get("function") or {}).get("name") or tc.get("name") or "") in FAMILY_TABLE_TOOLS
                for tc in (m.get("tool_calls") or [])
            )
            for m in messages[last_u_idx + 1:]
        )
        if post_ft:
            loaded_m = lmstudio_models()
            if is_loaded(loaded_m, MODELS["fast"]) and not is_loaded(loaded_m, MODELS["deep"]):
                return "fast", "Family Table post-tool reply (Flash GPU already warm)", True
        with _lock:
            _conversation_tier[key] = "deep"
            _conversation_tier.move_to_end(key)
        return "deep", "Family Table tool selected -> Pro (generate_family_table)", True
    sel = select_custom_tools(body) if (door == "openai" and not has_image(content)) else {"tools": set(), "tier": "", "ran": False}
    if sel["tools"]:
        with _lock:
            _conversation_tier[key] = "deep"
            _conversation_tier.move_to_end(key)
        return "deep", f"{sel['label']} -> Pro", True
    if door == "openai" and has_image(content):
        tier, reason = ("screen", "image + UI question") if SCREEN_WORDS.search(text) else ("fast", "image")
    elif door == "anthropic" and has_image(content) and SCREEN_WORDS.search(text) and not has_tool_history(messages):
        tier, reason = "screen", "screenshot + UI question"
    elif door == "openai" and has_code_history(messages):
        if _input_and_tools_tokens(body) > CHAT_CODE_OVERFLOW_TOKENS:
            tier, reason = "deep", "follow-up in a chat that holds code (>35K context) -> Pro (256K)"
        elif THREED_WORDS.search(snippet) or SIM_3D_WORDS.search(snippet):
            tier, reason = "deep", "3D/simulation follow-up in a chat that holds code -> Pro (Qwen3.6)"
        else:
            tier = cached if cached in ("think", "deep") else "think"
            reason = f"follow-up in a chat that already holds code, stays on {tier}"
    elif door == "openai" and (THREED_WORDS.search(snippet) or SIM_3D_WORDS.search(snippet)):
        cinfo = classify_coding_with_lfm(text)
        tier = "deep"
        reason = f"smart coding ({cinfo['domain']}, 3D/simulation) -> Pro (Qwen3.6); {cinfo['reason']}"
    elif DEEP_PHRASES.search(text):
        if door == "anthropic" or has_external_tools(body):
            tier, reason = "think", "explicit phrase (coding agent -> Qwen3.8)"
        elif BUILD_REQUEST.search(snippet) or SOFTWARE_WORDS.search(text):
            cinfo = classify_coding_with_lfm(text)
            tier = "deep" if cinfo["domain"] == "3d_sim" else "think"
            reason = f"explicit phrase + smart coding ({cinfo['domain']}) -> {tier}"
        else:
            tier, reason = "deep", "explicit phrase (complex query -> Pro)"
    elif THINK_PHRASES.search(text):
        tier, reason = "think", "explicit phrase"
    else:
        if sel["ran"] and sel["tier"]:
            label = sel["tier"]
            if MATH_PROBLEM_RE.search(snippet) and not (BUILD_REQUEST.search(snippet) or CODE_FENCE.search(text)):
                label = "deep"
            reason = f"Semantic router said '{label}'"
        else:
            try:
                t = time.time()
                label = classify_with_lfm(text)
                reason = f"LFM said '{label}' in {int((time.time() - t) * 1000)} ms"
            except Exception as e:
                label, reason = None, f"LFM unavailable ({e.__class__.__name__})"
        if label == "fast" and door == "anthropic" and has_tool_history(messages):
            tier = "think"
            reason += "; session already uses tools, stays on Qwen3.8"
        elif label == "fast":
            tier = "fast"
        elif door == "anthropic":
            tier = "think" if label != "deep" or BUILD_REQUEST.search(snippet) or SOFTWARE_WORDS.search(text) else "deep"
        elif label == "code" or (label == "deep" and (BUILD_REQUEST.search(snippet) or SOFTWARE_WORDS.search(text))):
            cinfo = classify_coding_with_lfm(text)
            dom = cinfo["domain"]
            if dom == "3d_sim":
                tier = "deep"
                reason += f"; smart coding (3d_sim) -> Pro (Qwen3.6) [{cinfo['reason']}]"
            elif dom == "quick":
                if is_loaded(lmstudio_models(), MODELS["code"]):
                    tier = "code"
                    reason += f"; quick code Q&A -> Qwen3.8 (thinking off, already loaded) [{cinfo['reason']}]"
                else:
                    tier = "fast"
                    reason += f"; quick code Q&A -> Flash GPU [{cinfo['reason']}]"
            elif _input_and_tools_tokens(body) > CHAT_CODE_OVERFLOW_TOKENS:
                tier = "deep"
                reason += f"; smart coding (logic_ui >35K) -> Pro (256K) [{cinfo['reason']}]"
            else:
                tier = "think"
                reason += f"; smart coding (logic_ui) -> Coding / Planning (Qwen3.8) [{cinfo['reason']}]"
        elif label == "deep":
            tier = "deep"
            reason += "; complex query -> Pro (Qwen3.6)"
        else:   # labeller unavailable: the strong default
            tier = "think"

    if door == "anthropic" and tier in ("think", "code", "fast") and _input_and_tools_tokens(body) > AGENT_PRO_OVERFLOW_TOKENS:
        tier = "deep"
        reason += f"; Claude Code context >{AGENT_PRO_OVERFLOW_TOKENS // 1000}K -> Pro (256K)"

    with _lock:
        _conversation_tier[key] = tier
        _conversation_tier.move_to_end(key)
        while len(_conversation_tier) > 500:
            _conversation_tier.popitem(last=False)
    return tier, reason, False


# Context windows set in each model's LM Studio config. An input that does not fit Qwen3.8 (64K) or Gemma (192K)
# falls back to Pro (Qwen3.6, 256K) instead of failing with "exceeds the available context size".
SCREEN_MIN_MAX_TOKENS = 8192   # NorthCUA reasons in <think> before each tool call / answer (2048 cut off on O2 Try 3)
CONTEXT_WINDOW = {"fast": 196608, "code": 65536, "think": 65536, "deep": 262144, "flash": 32768}
REPLY_ROOM = {"fast": 2048, "code": 4096, "think": THINK_BUDGET + 4096, "deep": DEEP_BUDGET + 4096,
              "screen": SCREEN_MIN_MAX_TOKENS, "flash": 2048}
TOO_LONG_FALLBACK = {"fast": "deep", "code": "deep", "think": "deep", "screen": "deep", "flash": "fast"}
def _input_and_tools_tokens(body):
    return estimate_tokens(body) + int(len(json.dumps(body.get("tools", ""))) / 3.0)


def fit_context(body, tier, reason, forced=False):
    """Only the hard window limit: requests exceeding 64K on Coding / Planning fall back to Pro (256K)."""
    if tier not in CONTEXT_WINDOW:
        return tier, reason
    n = _input_and_tools_tokens(body)
    if n + REPLY_ROOM[tier] <= CONTEXT_WINDOW[tier]:
        return tier, reason
    new = TOO_LONG_FALLBACK[tier]
    return new, f"{reason}; input ~{n} tokens does not fit {tier} ({CONTEXT_WINDOW[tier] // 1024}K), using {new}"


def plan(tier, models, door="anthropic"):
    """tier -> (model_id, thinking, budget or None, note). Falls back when a model is not installed."""
    if tier == "background":   # titles/summaries: whichever chat model is already in memory, no swap
        for m in (MODELS["code"], MODELS["fast"], MODELS["deep"]):
            if is_loaded(models, m):
                return m, False, None, "uses the model already loaded"
        if door == "openai":   # Open WebUI: its chats mostly go to Gemma, so load that one
            return MODELS["fast"], False, None, "nothing loaded, using Gemma"
        return MODELS["code"], False, None, "nothing loaded, using Qwen3.8"
    if tier in ("code", "think") and not installed(models, MODELS["code"]):
        return MODELS["deep"], True, THINK_BUDGET, f"{MODELS['code']} not installed, using Pro (Qwen3.6)"
    if tier == "deep" and not installed(models, MODELS["deep"]):
        return MODELS["code"], True, THINK_BUDGET, "Pro (Qwen3.6) not installed, using Coding / Planning (Qwen3.8)"
    if tier in ("3d", "screen", "fast") and not installed(models, MODELS[tier]):
        return MODELS["code"], tier == "3d", None, f"{MODELS[tier]} not installed, using Qwen3.8"
    if tier == "flash" and not installed(models, FLASH_MODEL):
        return MODELS["fast"], False, None, f"{FLASH_MODEL} not installed, using Flash (GPU)"
    return {
        "flash": (FLASH_MODEL, False, None, ""),       # CPU: answers even while the GPU is busy
        "fast": (MODELS["fast"], False, None, ""),
        "code": (MODELS["code"], False, None, ""),
        "think": (MODELS["code"], True, THINK_BUDGET, ""),
        "deep": (MODELS["deep"], True, DEEP_BUDGET, ""),
        "3d": (MODELS["3d"], True, THREED_CODER_BUDGET, ""),
        "screen": (MODELS["screen"], True, None, ""),     # NorthCUA reasons before each action (model card)
    }.get(tier, (MODELS["code"], False, None, "unknown tier"))


def strip_client_effort(body):
    body.pop("reasoning_effort", None)
    oc = body.get("output_config")
    if isinstance(oc, dict):                 # Claude Code's effort setting would override "thinking off"
        oc.pop("effort", None)
        if not oc:
            body.pop("output_config", None)


def prepare_anthropic(body, tier, model, thinking, budget):
    body["model"] = model
    strip_client_effort(body)
    # Some chat templates (Qwen3.8's) accept exactly one system message, at the very start. Claude Code sends
    # `system` as several blocks AND puts extra role:"system" messages inside `messages` (e.g. environment info).
    # Merge all of it into one top-level system string, in order.
    def _text(c):
        return c if isinstance(c, str) else "\n\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
    parts = [_text(body.get("system"))] if body.get("system") else []
    msgs = body.get("messages") or []
    parts += [_text(m.get("content")) for m in msgs if m.get("role") == "system"]
    body["messages"] = [m for m in msgs if m.get("role") != "system"]
    if parts:
        body["system"] = "\n\n".join(p for p in parts if p)
    if thinking:
        b = budget or 8000
        if int(body.get("max_tokens") or 0) <= b:
            body["max_tokens"] = b + 4096
        body["thinking"] = {"type": "enabled", "budget_tokens": b}
    else:
        body["thinking"] = {"type": "disabled"}
    if tier == "3d":
        body["max_tokens"] = max(int(body.get("max_tokens") or 0), THREED_MIN_MAX_TOKENS)
    if model == MODELS["code"] and body.get("max_tokens"):
        floor = (b + 4096) if thinking else 4096
        room = max(floor, CONTEXT_WINDOW["think"] - _input_and_tools_tokens(body) - 1024)
        body["max_tokens"] = min(int(body["max_tokens"]), room)


def prepare_openai(body, tier, model, thinking):
    body["model"] = model
    msgs = body.get("messages") or []
    sys_parts = [user_text(m.get("content")) for m in msgs if m.get("role") in ("system", "developer")]
    if len(sys_parts) > 1 or (sys_parts and msgs[0].get("role") not in ("system", "developer")):
        other = [m for m in msgs if m.get("role") not in ("system", "developer")]
        body["messages"] = [{"role": "system", "content": "\n\n".join(p for p in sys_parts if p)}] + other
    if body.get("stream"):
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
    if has_structured_response_format(body):
        if "reasoning_effort" not in body:
            mt = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
            if (0 < mt <= 2048) or not thinking:
                body["reasoning_effort"] = "none"
    elif thinking:
        body.pop("reasoning_effort", None)    # LM Studio default for these models is thinking on
    else:
        body["reasoning_effort"] = "none"
    if tier == "3d":
        apply_3d_openai(body)
    elif tier == "screen":
        for k in ("max_tokens", "max_completion_tokens"):
            if k in body:
                body[k] = max(int(body[k] or 0), SCREEN_MIN_MAX_TOKENS)
        body.setdefault("max_tokens", SCREEN_MIN_MAX_TOKENS)
    elif tier == "fast" and thinking and not has_structured_response_format(body):
        for k in ("max_tokens", "max_completion_tokens"):
            if k in body:
                body[k] = max(int(body[k] or 0), GEMMA_THINK_MIN_MAX_TOKENS)
        if "max_tokens" not in body and "max_completion_tokens" not in body:
            body["max_tokens"] = GEMMA_THINK_MIN_MAX_TOKENS
    elif model == MODELS["code"]:
        room = max(4096, CONTEXT_WINDOW["think"] - _input_and_tools_tokens(body) - 1024)
        for k in ("max_tokens", "max_completion_tokens"):
            if body.get(k):
                body[k] = min(int(body[k]), room)
    if model == FLASH_MODEL:
        for k, v in FLASH_SAMPLING.items():
            body.setdefault(k, v)


def _build_usage_stats(in_tokens, out_tokens, started, first_token_at, entry):
    gen_s = max(0.05, time.time() - (first_token_at or started))
    tok_s = round(out_tokens / gen_s, 1) if out_tokens > 0 else 0.0
    if out_tokens > 0:
        entry["output_tokens"] = out_tokens
        entry["tok_s"] = tok_s
    if in_tokens > 0:
        entry["input_tokens"] = in_tokens
    return {
        "prompt_tokens": in_tokens,
        "completion_tokens": out_tokens,
        "total_tokens": in_tokens + out_tokens,
        "response_token/s": tok_s,
        "generation_time_s": round(gen_s, 1),
    }


# Chat apps (Open WebUI): the user runs the code, they do not edit it. 2026-11-07: a bug fix came back as a
# JavaScript fragment with "replace the <script> section" and "keep as is from previous code"; the user was lost.
CODING_CHAT_SYSTEM = """You write code for someone who will save it and run it, not edit it by hand.
- Always reply with COMPLETE files, ready to save and run. Never a fragment, never "replace the X section", never placeholders such as "... rest unchanged" or "keep as before". When you fix or change a program from earlier in this chat, send the whole updated file again.
- One code block per file, with the file name in bold just above it.
- Before the code, in one or two sentences: what you made, or for a fix, what the actual cause was and what you changed.
- After the code, how to run it in plain steps (for example: save it as vibe-academy.html, then double-click it).
- To fix a bug, trace what really runs, in order (startup, event handlers, what each step needs), until you find the cause. Do not guess.
- Before answering, check the code would run: every function and element you use exists, handlers are attached before they are needed, nothing is left unfinished.
- Answer in the user's language."""

CODING_SURGICAL_SYSTEM = """You are fixing or updating an existing long single-file HTML/JS program in this chat.
- Do NOT rewrite the entire file from scratch. The router automatically merges your patch blocks onto the latest HTML file in this chat, saves the complete updated .html file, checks it in a headless browser, and gives the user a ready-to-open link.
- Output one or more exact `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` blocks:
<<<<<<< SEARCH
exact existing lines copied verbatim from the program
=======
updated lines
>>>>>>> REPLACE
- Every `SEARCH` block must match the existing code verbatim (same indentation, enough surrounding context lines to be unique). Never use `...` placeholders inside `SEARCH` or `REPLACE`.
- Before the patch blocks, state in 1-2 sentences the exact root cause (trace startup and event handlers in order; do not guess) and what you changed.
- Answer in the user's language."""


# Thinking loop detector: require repeated blocks inside the trailing reasoning window (never kill multi-step checks
# whose 400-char verification template spans a full trace).
LOOP_WINDOW, LOOP_REPEATS, LOOP_CHECK_EVERY = 400, 5, 2000
MAX_PARALLEL_TOOL_CALLS_OWUI = 6
MAX_PARALLEL_TOOL_CALLS_AGENT = 16


def _canonical_tool_sig(name, raw_args):
    """Return a canonical (name, sorted-json-or-stripped-str) key for deduplicating identical tool calls."""
    s = (raw_args or "").strip() or "{}"
    try:
        canon = json.dumps(json.loads(s), sort_keys=True, ensure_ascii=False)
    except Exception:
        canon = s
    return (name or "", canon)


def thinking_loops(text):
    n = len(text)
    if n >= 3000:
        w250 = text[-250:]
        if len(w250.strip()) >= 80 and text[-3000:].count(w250) >= 6:
            return True
    if n >= 6000:
        w400 = text[-400:]
        if len(w400.strip()) >= 100 and text[-6000:].count(w400) >= 5:
            return True
    if n >= 12000:
        w600 = text[-600:]
        if len(w600.strip()) >= 120 and text[-12000:].count(w600) >= 5:
            return True
    return False


LINE_REF = re.compile(r"\blines?\s+(\d{1,5})\b", re.I)
EXCERPT_RADIUS = 10


def add_code_excerpts(body):
    """'... (line 601, called from line 999)' in the user's message: attach those lines of the app from this chat,
    numbered. 2026-11-07: the 3D coder tried to count 1,300 lines from memory to find line 601 and looped."""
    messages = body.get("messages") or []
    idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
    if idx is None:
        return None
    nums = sorted({int(n) for n in LINE_REF.findall(user_text(messages[idx].get("content")))})[:4]
    if not nums:
        return None
    html = next((h for h in (html_files.extract_html(user_text(m.get("content")))
                             for m in reversed(messages[:idx]) if m.get("role") == "assistant") if h), None)
    if not html:
        return None
    lines = html.split("\n")
    parts = []
    for n in nums:
        if 1 <= n <= len(lines):
            lo, hi = max(1, n - EXCERPT_RADIUS), min(len(lines), n + EXCERPT_RADIUS)
            parts.append(f"Lines {lo}-{hi}:\n```\n" + "\n".join(f"{i:5}| {lines[i - 1]}" for i in range(lo, hi + 1))
                         + "\n```")
    if not parts:
        return None
    note = ("\n\n[Added by the router: the lines mentioned above, from the latest version of the file in this chat "
            f"({len(lines)} lines), with their real line numbers. Do not count lines yourself.]\n\n"
            + "\n\n".join(parts))
    c = messages[idx].get("content")
    if isinstance(c, list):
        c.append({"type": "text", "text": note})
    else:
        messages[idx]["content"] = str(c or "") + note
    return nums


CODING_REASON_MARKS = (
    "'code'",
    "smart coding",
    "quick code Q&A",
    "build software",
    "holds code",
    "software/planning",
    "3D/simulation",
    "coding agent",
    "Coding / Planning",
)


def is_coding_request(tier, reason):
    """Writing, fixing, or answering about software."""
    if tier in ("3d", "code"):
        return True
    if tier in ("think", "deep", "fast") and any(s in (reason or "") for s in CODING_REASON_MARKS):
        return True
    if tier == "think" and reason == "forced by model name":
        return True
    return False


def needs_coding_brief(tier, reason):
    """True when a chat coding response should get complete-file / surgical-patch instructions and 16K thinking."""
    if tier not in ("think", "deep", "3d"):
        return False
    if "quick code Q&A" in (reason or ""):
        return False
    return is_coding_request(tier, reason)


def apply_3d_openai(body):
    """Room for a whole scene + the Three.js system prompt. Used on both OpenAI-door paths."""
    for k in ("max_tokens", "max_completion_tokens"):
        if k in body:
            body[k] = max(int(body[k] or 0), THREED_MIN_MAX_TOKENS)
    body.setdefault("max_tokens", THREED_MIN_MAX_TOKENS)
    msgs = body.get("messages") or []
    if not any(m.get("role") == "system" for m in msgs):
        body["messages"] = [{"role": "system", "content": THREED_SYSTEM}] + msgs


# ---------------------------------------------------------------- OpenAI <-> Anthropic (for capped thinking + tools)
def _to_anthropic_content(content):
    if isinstance(content, str):
        return content
    out = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            out.append({"type": "text", "text": p.get("text", "")})
        elif p.get("type") == "image_url":
            url = (p.get("image_url") or {}).get("url", "")
            m = re.match(r"data:([^;]+);base64,(.*)", url, re.S)
            out.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
                       if m else {"type": "image", "source": {"type": "url", "url": url}})
    return out


def _as_blocks(c):
    if isinstance(c, list):
        return c
    return [{"type": "text", "text": c}] if c else []


def _parse_tool_args(raw_args):
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        s = raw_args.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            return {"_raw": raw_args}
    return {}


def _openai_tools_to_anthropic(tools):
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        schema = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        if "type" not in schema:
            schema = {"type": "object", **schema}
        spec = {"name": name, "input_schema": schema}
        if fn.get("description"):
            spec["description"] = str(fn["description"])
        out.append(spec)
    return out


def openai_to_anthropic(body, model, budget):
    keep_tools = bool(body.get("tools")) and body.get("tool_choice") != "none"
    atools = _openai_tools_to_anthropic(body.get("tools")) if keep_tools else []
    keep_tools = bool(atools)
    seen_tool_ids = set()
    system, messages = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            system.append(m["content"] if isinstance(m.get("content"), str) else user_text(m.get("content")))
            continue
        if role == "tool":
            tc_id = str(m.get("tool_call_id") or "")
            res_text = user_text(m.get("content")) if isinstance(m.get("content"), list) else str(m.get("content") or "")
            if keep_tools and tc_id and tc_id in seen_tool_ids:
                role = "user"
                content = [{"type": "tool_result", "tool_use_id": tc_id, "content": res_text}]
            else:
                role, content = "user", "[tool result]\n" + res_text
        elif role == "assistant" and m.get("tool_calls"):
            if keep_tools:
                blocks = []
                txt_part = _to_anthropic_content(m.get("content"))
                if txt_part:
                    blocks.extend(_as_blocks(txt_part))
                for tc in m.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    tc_id = str(tc.get("id") or ("call_" + uuid.uuid4().hex[:12]))
                    seen_tool_ids.add(tc_id)
                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": str(fn.get("name") or "tool"),
                        "input": _parse_tool_args(fn.get("arguments")),
                    })
                content = blocks
            elif not user_text(m.get("content")):
                calls = ", ".join((c.get("function") or {}).get("name", "?") for c in m["tool_calls"])
                content = f"[called tools: {calls}]"
            else:
                content = _to_anthropic_content(m.get("content"))
        elif role in ("user", "assistant"):
            content = _to_anthropic_content(m.get("content"))
        else:
            continue
        if not content:
            continue
        if messages and messages[-1]["role"] == role:     # the Anthropic format alternates user/assistant
            messages[-1]["content"] = _as_blocks(messages[-1]["content"]) + _as_blocks(content)
        else:
            messages.append({"role": role, "content": content})
    max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
    want_max = max(max_tokens, budget + TRANSLATED_ANSWER_TOKENS)
    if model == MODELS["code"]:
        room = max(budget + 4096, CONTEXT_WINDOW["think"] - _input_and_tools_tokens(body) - 1024)
        want_max = min(want_max, room)
    out = {"model": model, "messages": messages, "stream": bool(body.get("stream")),
           "max_tokens": want_max,
           "thinking": {"type": "enabled", "budget_tokens": budget}}
    if atools:
        out["tools"] = atools
    if system:
        out["system"] = "\n\n".join(system)
    for k in ("temperature", "top_p", "top_k", "stop"):
        if k in body:
            out["stop_sequences" if k == "stop" else k] = body[k] if k != "stop" or isinstance(body[k], list) else [body[k]]
    return out


FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}


def anthropic_to_openai(d, model, max_tool_calls=None):
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    thinking = "".join(b.get("thinking", "") for b in d.get("content", []) if b.get("type") == "thinking")
    tool_calls, seen_sigs = [], set()
    for b in d.get("content", []):
        if b.get("type") != "tool_use":
            continue
        name = b.get("name") or ""
        raw_args = json.dumps(b.get("input") or {}, ensure_ascii=False)
        sig = _canonical_tool_sig(name, raw_args)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        tool_calls.append({
            "id": b.get("id") or ("call_" + uuid.uuid4().hex[:12]),
            "type": "function",
            "function": {"name": name, "arguments": raw_args},
        })
        if max_tool_calls and len(tool_calls) >= max_tool_calls:
            break
    if not text and not tool_calls and thinking:
        text = thinking.strip()
    msg = {"role": "assistant", "content": text or (None if tool_calls else ""), "reasoning_content": thinking}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else FINISH.get(d.get("stop_reason"), "stop")
    u = d.get("usage", {})
    return {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
            "usage": {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                      "total_tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}}


def readable_upstream_error(raw, model):
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        j = json.loads(text)
        text = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else str(j.get("error") or j)
    except ValueError:
        if "<html" in text.lower():
            text = "LM Studio returned an internal error"
    return (f"The local model ({model}) could not answer: {text}. The router already retried once; "
            "please send the message again.")[:1500]


# ---------------------------------------------------------------- HTTP server
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "local-router/3.0"

    def log_message(self, *args):
        pass

    def _json(self, status, obj, extra_headers=None):
        if getattr(self, "_sse_started", False):
            err = obj.get("error") if isinstance(obj, dict) else {"message": str(obj)}
            if not isinstance(err, dict):
                err = {"message": str(err)}
            self._chunk(
                b"data: " + json.dumps({"error": err}, ensure_ascii=False).encode("utf-8") + b"\n\ndata: [DONE]\n\n"
            )
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        data = json.dumps(obj, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, door, message):
        if door == "anthropic":
            return self._json(status, {"type": "error", "error": {"type": "api_error", "message": message}})
        return self._json(status, {"error": {"message": message, "type": "router_error"}})

    def _chunk(self, data):
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _is_openwebui_stream(self, door, body):
        if door != "openai" or not body.get("stream"):
            return False
        if (
            body.get("_internal_repair")
            or body.get("_internal_scene3d")
            or body.get("_internal_family_table")
            or body.get("_internal_deep_research")
            or body.get("_internal_smart_visuals")
            or has_external_tools(body)
            or has_structured_response_format(body)
        ):
            return False
        ua = (self.headers.get("User-Agent") or "").lower() if self.headers else ""
        return bool(OPENWEBUI_BUILTIN_MARK <= tool_names(body) or "aiohttp" in ua)

    def _ensure_sse_headers(self, tier="", model="", extra_headers=None):
        if getattr(self, "_sse_started", False):
            return
        self._sse_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        if tier:
            self.send_header("X-Router-Tier", str(tier))
        if model:
            self.send_header("X-Router-Model", str(model))
        for k, v in (extra_headers or {}).items():
            if k not in ("X-Router-Tier", "X-Router-Model"):
                self.send_header(k, str(v))
        self.end_headers()

    def _emit_sse_json(self, obj, tier="", model=""):
        self._ensure_sse_headers(tier=tier, model=model)
        self._chunk(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")

    def _emit_owui_selected_once(self, entry):
        sel = entry.pop("_owui_selected_model", None)
        if sel:
            self._emit_sse_json(
                {"selected_model_id": sel},
                tier=entry.get("tier", ""),
                model=entry.get("model", ""),
            )

    def _emit_owui_preamble(self, entry):
        if not entry.get("_owui_stream"):
            entry.pop("_ui", None)
            entry.pop("_owui_selected_model", None)
            return {}
        self._emit_owui_selected_once(entry)
        ui_meta = entry.pop("_ui", None) or {}
        tier = entry.get("tier", "")
        model = entry.get("model", "")
        for src_ev in ui_meta.get("sources") or []:
            self._emit_sse_json(src_ev, tier=tier, model=model)
        if ui_meta.get("embed_html"):
            self._emit_sse_json(rich_ui.embeds_event([ui_meta["embed_html"]]), tier=tier, model=model)
        return ui_meta

    def _clear_owui_loading_status(self, entry, ui_meta):
        if not entry.pop("_owui_loading_status", False):
            return
        tier = entry.get("tier", "")
        model = entry.get("model", "")
        if ui_meta and ui_meta.get("status_done"):
            self._emit_sse_json(ui_meta["status_done"], tier=tier, model=model)
        else:
            self._emit_sse_json(rich_ui.status_event("Ready", done=True, hidden=True), tier=tier, model=model)

    def _emit_owui_fallback_photos(self, entry, ui_meta, answer_text):
        if not entry.get("_owui_stream") or not ui_meta or not ui_meta.get("visual_topic"):
            return
        imgs = ui_meta.get("verified_images") or []
        if len(imgs) < 2:
            return
        tier = entry.get("tier", "")
        model = str(entry.get("model") or "")
        resolved_text = rich_ui.resolve_photo_refs(answer_text or "", imgs)
        used = set(rich_ui.extract_markdown_image_urls(resolved_text))
        # Never emit `files_event` with external HTTPS URLs: Open WebUI's `message.files` gallery renders `<Image>`
        # with `allowExternal=false` (`CbtsD9oz.js`), replacing external URLs with `/favicon.png` (default logo).
        # Instead, append 1–2 unused verified photos as side-by-side Markdown (`allowExternal=true` in `DqJZGdnw.js`).
        if len(used) < 2 and len(resolved_text) >= 180:
            unused = [im for im in imgs if im.get("url") and im["url"] not in used]
            want_extra = min(len(unused), 2 if (len(used) == 0 or len(imgs) >= 3) else 1)
            if want_extra > 0:
                extra_lines = []
                for im in unused[:want_extra]:
                    cap = re.sub(r"^Source \[\d+\]\s*—\s*", "", str(im.get("description") or "Related photo")).replace("]", "")[:120]
                    extra_lines.append(f"![{cap}]({im['url']})")
                # Single space between images keeps them in the same <p> so custom.css renders them side-by-side
                md_extra = "\n\n" + " ".join(extra_lines) + "\n"
                self._emit_sse_json(
                    {
                        "id": "chatcmpl-photo-supp",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": md_extra}, "finish_reason": None}],
                    },
                    tier=tier,
                    model=model,
                )

    def _refused(self):
        reason = local_guard.refuse_reason(self.headers)
        if reason:
            log(f"REFUSED {self.command} {self.path[:80]} from {self.client_address[0]}: {reason}")
            self.close_connection = True
            self._json(403, {"error": {"message": f"forbidden: {reason}", "type": "forbidden"}})
        return bool(reason)

    def do_GET(self):
        if self._refused():
            return
        path = self.path.split("?")[0].rstrip("/")
        models = lmstudio_models()
        if path in ("", "/health"):
            tiers = {t: plan(t, models)[0] + (" + thinking" if plan(t, models)[1] else "") for t in TIERS}
            tiers["image"] = "qwen-image-2.1 via ComfyUI" + ("" if comfy_images.available() else " (ComfyUI not running)")
            tiers["voice"] = "Qwen3-TTS 1.7B" + ("" if voice_tts.available() else " (voice-tts not running)")
            self._json(200, {"status": "ok", "uptime_s": int(time.time() - _started), "tiers": tiers,
                             "gpu": dict(_gpu_state),
                             "web": {"tavily_key": web_search.has_key(), "this_month": web_search.usage_this_month()},
                             "loaded": [m for m, s in models.items() if s == "loaded"],
                             "recent_decisions": list(_recent)[::-1]})
        elif path == "/v1/models":
            now = int(_started)
            labels = MODEL_LABELS
            names = ["auto", *TIERS, "voice", "background"] + [f"local-{t}" for t in ("auto", *TIERS, "voice", "background")] + list(models) + ["qwen-image-2.1"]
            self._json(200, {"object": "list", "has_more": False, "data": [
                {"id": n, "name": labels.get(n, n), "object": "model", "type": "model",
                 "display_name": labels.get(n, n), "created": now, "created_at": now,
                 "owned_by": "lmstudio" if n in models else "router"} for n in names]})
        else:
            self._error(404, "openai", f"not found: {self.path}")

    def do_POST(self):
        self._sse_started = False
        if self._refused():
            return
        path = self.path.split("?")[0]
        door = "anthropic" if path.startswith("/v1/messages") else "openai"
        raw_bytes = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if path.startswith("/v1/images/edits"):
            try:
                fields, images = _parse_images_edit_request(self.headers.get("Content-Type"), raw_bytes)
            except Exception as e:
                return self._error(400, door, f"bad image edit request: {e}")
            return self._image_edits(fields, images)
        try:
            body = json.loads(raw_bytes or b"{}")
        except Exception as e:
            return self._error(400, door, f"bad JSON: {e}")

        if path == "/route-check":
            return self._json(200, route_check(body))
        if path.startswith("/v1/messages/count_tokens"):
            return self._json(200, {"input_tokens": max(1, estimate_tokens(body))})
        if path.startswith("/v1/images/generations"):
            return self._images(body)
        if path.startswith("/v1/audio/speech"):
            return self._audio_speech(body)
        if path.startswith("/v1/web/search"):          # Open WebUI's "External" web search engine
            try:
                results = web_search.openwebui_results(str(body.get("query") or ""), body.get("count"))
            except Exception as e:
                log(f"web search for Open WebUI failed: {e}")
                return self._json(502, {"error": str(e)[:300]})
            log(json.dumps({"tier": "web-search", "query": str(body.get("query"))[:120], "results": len(results),
                            "time": time.strftime("%H:%M:%S")}))
            return self._json(200, results)
        if not (path.startswith("/v1/messages") or path.startswith("/v1/chat/completions")
                or path.startswith("/v1/embeddings")):
            return self._error(404, door, f"not found: {path}")
        if path.startswith("/v1/chat/completions"):
            is_voice, voice_reason = is_voice_chat_request(body, door)
            if is_voice:
                return self._forward_voice_chat(body, voice_reason)
        llm_enter()
        try:
            return self._route(path, door, body)
        finally:
            llm_exit()

    def _route(self, path, door, body):
        if path.startswith("/v1/embeddings"):
            return self._forward(path, body, {"tier": "embeddings", "reason": "passthrough"})

        asked = body.get("model")
        tier, reason, forced = decide(body, door)
        excerpts = add_code_excerpts(body) if door == "openai" and not has_external_tools(body) else None
        notice = None
        is_direct_3d = tier is None and str(body.get("model") or "").startswith(THREED_CODER_PREFIX)
        if door == "openai" and (is_direct_3d or (tier == "3d" and installed(lmstudio_models(), THREED_CODER_PREFIX))) \
                and (not body.get("tools") or OPENWEBUI_BUILTIN_MARK <= tool_names(body)):
            if not has_code_history(body.get("messages") or []):
                return self._route_3d_coder(body, asked, excerpts)
            # a fix or change to the scene it wrote: the 3D coder only learned to build a scene in one go (it looped
            # trying to count lines, 2026-11-07 23:45); Coding / Planning (Qwen3.8 + thinking) does edits and debugging
            tier, forced = "think", True
            reason = "3D coder / 3D scene follow-up in a chat that already holds code: Coding / Planning (Qwen3.8)"
            notice = ("> 🔧 **Fixes and changes go to Coding / Planning (Qwen 3.8)**: the 3D coder is trained to build "
                      "a new scene in one go, not to debug or edit one. For a brand-new scene, start a new chat.\n\n")
        if tier is None:
            target = str(body.get("model") or "")
            waited = model_enter(target)
            try:
                return self._forward(path, body, {"asked": asked, "tier": "passthrough", "reason": reason,
                                                  **({"waited_s": waited} if waited >= 1 else {})})
            finally:
                model_exit(target)
        tier, reason = fit_context(body, tier, reason, forced)
        models = lmstudio_models()
        model, thinking, budget, note = plan(tier, models, door)
        if (
            door == "openai"
            and tier == "fast"
            and model == MODELS["fast"]
            and not has_structured_response_format(body)
            and not has_image_gen_intent(body)
        ):
            msgs_u = body.get("messages") or []
            last_u = next((m for m in reversed(msgs_u) if m.get("role") == "user"), None)
            u_content = last_u.get("content") if last_u else ""
            u_txt = user_text(u_content)
            if (
                THINK_PHRASES.search(u_txt)
                or DEEP_PHRASES.search(u_txt)
                or (has_image(u_content) and VISION_THINK_RE.search(u_txt))
            ):
                thinking = True
                budget = None
                note = (note + "; " if note else "") + "Gemma native thinking (vision/explicit)"
        if door == "anthropic" and thinking and budget and body.get("tools"):
            budget = max(budget, AGENT_THINK_BUDGET)
            note = (note + "; " if note else "") + f"agent thinking budget {budget}"
        elif door == "openai" and thinking and budget and is_coding_agent_tools(body):
            budget = AGENT_THINK_BUDGET
            note = (note + "; " if note else "") + f"agent thinking budget {budget}"
        elif door == "openai" and thinking and budget and body.get("_internal_repair"):
            budget = HTML_REPAIR_THINK_BUDGET
            note = (note + "; " if note else "") + f"auto-repair thinking budget {budget}"
        elif door == "openai" and thinking and budget and body.get("_internal_advanced_deck"):
            budget = ADVANCED_DECK_THINK_BUDGET
            note = (note + "; " if note else "") + f"advanced deck thinking budget {budget}"
        elif door == "openai" and thinking and budget and body.get("_internal_family_table"):
            budget = FAMILY_TABLE_THINK_BUDGET
            note = (note + "; " if note else "") + f"family table thinking budget {budget}"
        elif door == "openai" and thinking and budget and body.get("_internal_deep_research"):
            budget = DEEP_RESEARCH_THINK_BUDGET
            note = (note + "; " if note else "") + f"deep research thinking budget {budget}"
        elif door == "openai" and thinking and budget and body.get("_internal_smart_visuals"):
            budget = SMART_VISUALS_THINK_BUDGET
            note = (note + "; " if note else "") + f"smart visuals thinking budget {budget}"
        elif door == "openai" and thinking and budget and (sel := select_custom_tools(body))["tools"] and sel["budget"]:
            budget = sel["budget"]
            note = (note + "; " if note else "") + f"{sel['label'].lower()} thinking budget {budget}"
        elif door == "openai" and thinking and budget and model in (MODELS["code"], MODELS["deep"]) and needs_coding_brief(tier, reason):
            budget = max(budget, CODE_THINK_BUDGET)
            note = (note + "; " if note else "") + f"coding: thinking budget {budget}"
        entry = {"asked": asked, "tier": tier, "model": model, "thinking": thinking,
                 "reason": reason + (f"; {note}" if note else ""),
                 "swap": bool(models) and not is_loaded(models, model)}
        if excerpts:
            entry["excerpts"] = excerpts
        if notice:
            entry["_notice"] = notice
        busy = gpu_busy_with(model) if door == "openai" and not forced else None
        if busy and tier not in ("screen", "3d", "flash", "background") and not is_coding_request(tier, reason) \
                and installed(models, FLASH_MODEL) \
                and estimate_tokens(body) + REPLY_ROOM["flash"] <= CONTEXT_WINDOW["flash"]:
            # another model is mid-task on the GPU: loading this one would wait for it (or, without the swap guard,
            # eject it). Everyday questions go to Flash on the CPU now instead; coding, 3D, browser tasks still wait.
            entry["_notice"] = FLASH_NOTICE.format(busy=MODEL_LABELS.get(busy, busy))
            tier, model, thinking, budget = "flash", FLASH_MODEL, False, None
            entry.update(tier=tier, model=model, thinking=False, swap=False,
                         reason=entry["reason"] + f"; GPU busy with {busy}: redirected to Flash (CPU)")
        if model in CPU_LOAD and models and not is_loaded(models, model):
            ensure_cpu_models(f"{model} needed now")    # load it manually first: a JIT load would get a TTL
            entry["reloaded"] = model
        owui_stream = tier != "background" and self._is_openwebui_stream(door, body)
        if owui_stream:
            entry["_owui_stream"] = True
            if str(asked or "").lower() in ("auto", "local-auto"):
                entry["_owui_selected_model"] = tier

        def _live_status(ev):
            if owui_stream:
                self._emit_owui_selected_once(entry)
                self._emit_sse_json(ev, tier=tier, model=model)

        live = add_live_info(
            body, door, tier, reason, model, models,
            on_status=_live_status if owui_stream else None,
            rich_citations=owui_stream,
        )
        if live:
            entry.update(live)
        if tier == "background" and estimate_tokens(body) + len(json.dumps(body.get("tools", ""))) / 3.5 \
                <= BACKGROUND_LFM_MAX_TOKENS and installed(models, ROUTER_MODEL):
            # titles, tags, follow-ups: the CPU labeller does them, so they never load or unload a GPU model
            model = entry["model"] = ROUTER_MODEL
            entry["reason"] = "background call (titles, tags, follow-ups) on LFM3 (CPU, never swaps a GPU model)"
            entry["swap"] = False
            for k in ("max_tokens", "max_completion_tokens"):
                if body.get(k):
                    body[k] = min(int(body[k]), 2048)
            if not body.get("max_tokens") and not body.get("max_completion_tokens"):
                body["max_tokens"] = 2048
        entered = model
        if owui_stream and entered not in CPU_MODELS:
            tier_label = MODEL_LABELS.get(tier) or MODEL_LABELS.get(entered, entered)
            if gpu_busy_with(entered):
                self._emit_owui_selected_once(entry)
                self._emit_sse_json(
                    rich_ui.status_event(f"⏳ Waiting for GPU, then loading {tier_label}…", done=False),
                    tier=tier, model=entered,
                )
                entry["_owui_loading_status"] = True
            elif entry.get("swap"):
                self._emit_owui_selected_once(entry)
                self._emit_sse_json(
                    rich_ui.status_event(f"🔄 Loading {tier_label} into VRAM…", done=False),
                    tier=tier, model=entered,
                )
                entry["_owui_loading_status"] = True
        waited = model_enter(entered)
        if waited >= 1:
            entry["waited_s"] = waited          # waited for another model's request to finish before swapping
        try:
            return self._dispatch(path, door, body, tier, model, thinking, budget, models, entry)
        finally:
            model_exit(entered)                 # exactly what was reserved, even if _dispatch changed the model

    def _route_3d_coder(self, body, asked, excerpts=None):
        """The 3D coder (picked by name or routed via 3d tier): its own system prompt, a thinking cap, stall watchdog."""
        raw_m = str(body.get("model") or "")
        model = raw_m if raw_m.startswith(THREED_CODER_PREFIX) else THREED_CODER_PREFIX
        owui_stream = self._is_openwebui_stream("openai", body)
        entry = {"asked": asked, "tier": "3d-coder", "model": model, "thinking": True,
                 "reason": f"3D coder ({model}): thinking cap {THREED_CODER_BUDGET} tokens, stall watchdog"}
        if owui_stream:
            entry["_owui_stream"] = True
            if str(asked or "").lower() in ("auto", "local-auto"):
                entry["_owui_selected_model"] = "3d"
        if excerpts:
            entry["excerpts"] = excerpts
        names = tool_names(body)
        if names:                                   # Open WebUI's built-in tools only (checked by the caller)
            body.pop("tools", None)
            body.pop("tool_choice", None)
            entry["tools_dropped"] = len(names)
            if OPEN_TERMINAL_MARK <= names:
                strip_dropped_open_terminal_prompt(body)
        if not any(m.get("role") in ("system", "developer") for m in body.get("messages") or []):
            inject_system(body, "openai", THREED_CODER_SYSTEM)
        body["max_tokens"] = max(int(body.get("max_tokens") or 0), THREED_CODER_BUDGET + THREED_CODER_ANSWER_TOKENS)
        body.pop("max_completion_tokens", None)
        waited = model_enter(model)
        if waited >= 1:
            entry["waited_s"] = waited
        try:
            entry["translated"] = "openai->anthropic for thinking cap"
            return self._forward_translated(body, model, THREED_CODER_BUDGET, entry)
        finally:
            model_exit(model)

    def _dispatch(self, path, door, body, tier, model, thinking, budget, models, entry):
        orig_owui_terminal = (
            door == "openai"
            and OPENWEBUI_BUILTIN_MARK <= tool_names(body)
            and OPEN_TERMINAL_MARK <= tool_names(body)
        )
        if (
            door == "openai"
            and tier != "screen"
            and OPENWEBUI_BUILTIN_MARK <= tool_names(body)
            and (IMAGE_GEN_TOOLS & tool_names(body))
            and has_image_gen_intent(body)
        ):
            # Keep only generate_image / edit_image (drops ~9K tokens of Open WebUI + terminal tools and preserves
            # image generation on thinking tiers like Pro / Coding via the translated tool bridge).
            img_tools = [
                t for t in (body.get("tools") or [])
                if isinstance(t, dict)
                and ((t.get("function") or {}).get("name") or t.get("name") or "") in IMAGE_GEN_TOOLS
            ]
            entry["tools_dropped"] = len(body.get("tools") or []) - len(img_tools)
            body["tools"] = img_tools
            names_kept = {((t.get("function") or {}).get("name") or t.get("name") or "") for t in img_tools}
            urls = _chat_image_file_urls(body.get("messages") or [])
            rule_parts = []
            if names_kept:
                rule_parts.append(
                    "Image tool rule: Call `edit_image` or `generate_image` ONCE for the user's request (unless they "
                    "explicitly asked for multiple distinct images). Once a tool returns `\"status\": \"success\"`, "
                    "the image is already visible in chat—stop calling tools, ignore any instructions about "
                    "`display_file` or `terminal_path`, and give a brief final reply."
                )
            if "edit_image" in names_kept and urls:
                rule_parts.append(
                    "When modifying, iterating on, or adding/removing/changing elements of an existing image in this "
                    "chat, call `edit_image` (NOT `generate_image`) so the original subject and composition are "
                    f"preserved, passing `image_urls` as a JSON array of strings (not a quoted string), e.g. "
                    f"`image_urls={json.dumps([urls[-1]])}` (or multiple URLs if combining images), and a clear "
                    "description of the edit in `prompt`. Call `generate_image` only for a brand-new unrelated subject."
                )
            if rule_parts:
                inject_system(body, door, " ".join(rule_parts))

        if (
            door == "openai"
            and tier != "screen"
            and (ADVANCED_DECK_TOOLS & tool_names(body))
        ):
            # Advanced Deck explicitly enabled in + -> Tools: keep ONLY build_advanced_deck (drops Simple Deck + 60 built-ins)
            adv_tools = [
                t for t in (body.get("tools") or [])
                if isinstance(t, dict)
                and ((t.get("function") or {}).get("name") or t.get("name") or "") in ADVANCED_DECK_TOOLS
            ]
            entry["tools_dropped"] = len(body.get("tools") or []) - len(adv_tools)
            body["tools"] = adv_tools
            inject_system(body, door, ADVANCED_DECK_SYSTEM)

        if (
            door == "openai"
            and tier != "screen"
            and (FAMILY_TABLE_TOOLS & tool_names(body))
        ):
            ft_tools = [
                t for t in (body.get("tools") or [])
                if isinstance(t, dict)
                and ((t.get("function") or {}).get("name") or t.get("name") or "") in FAMILY_TABLE_TOOLS
            ]
            entry["tools_dropped"] = len(body.get("tools") or []) - len(ft_tools)
            body["tools"] = ft_tools
            inject_system(body, door, FAMILY_TABLE_SYSTEM)

        if (
            door == "openai"
            and tier != "screen"
            and not ((ADVANCED_DECK_TOOLS | FAMILY_TABLE_TOOLS) & tool_names(body))
            and not ((IMAGE_GEN_TOOLS & tool_names(body)) and has_image_gen_intent(body))
        ):
            sel = select_custom_tools(body)
            if sel["tools"]:
                kept_tools = [
                    t for t in (body.get("tools") or [])
                    if isinstance(t, dict)
                    and ((t.get("function") or {}).get("name") or t.get("name") or "") in sel["tools"]
                ]
                entry["tools_dropped"] = len(body.get("tools") or []) - len(kept_tools)
                body["tools"] = kept_tools
                if sel["system"]:
                    inject_system(body, door, sel["system"])

        if tier in ("fast", "flash") and door == "openai" and OPENWEBUI_BUILTIN_MARK <= tool_names(body):
            # Drop Open WebUI's 38 built-in tools + any stray Open Terminal tools (`browser_tabs`, `browser_action`,
            # `run_command` when `🛠️ Coding Agents` is left switched on) so Gemma/Flash saves ~8K-10K input tokens and
            # never loops on empty local knowledge bases or unopened terminal browser tabs.
            last_u_msg = next((m for m in reversed(body.get("messages") or []) if m.get("role") == "user"), None)
            last_u_txt = user_text(last_u_msg.get("content")) if last_u_msg else ""
            if tier == "fast" and PERSONAL_OWUI_TOOLS_RE.search(last_u_txt):
                kept = [
                    t for t in (body.get("tools") or [])
                    if isinstance(t, dict)
                    and ((t.get("function") or {}).get("name") or t.get("name") or "") in OWUI_PERSONAL_TOOL_NAMES
                ]
                entry["tools_dropped"] = len(body.get("tools") or []) - len(kept)
                body["tools"] = kept
            else:
                entry["tools_dropped"] = entry.get("tools_dropped", 0) + len(body.pop("tools", []) or [])
                body.pop("tool_choice", None)

        if tier == "screen" and door == "openai" and OPENWEBUI_BUILTIN_MARK <= tool_names(body):
            # Open WebUI: keep only explicit GUI/browser MCP tools (Playwright/NorthCUA/Desktop Use); drop Open WebUI's
            # built-in tools and any stray Open Terminal tools (`browser_action`, `desktop_action`, etc.).
            gui = [
                t for t in body.get("tools") or []
                if isinstance(t, dict)
                and GUI_TOOL.search(((t.get("function") or {}).get("name") or "").split("__")[-1])
                and not (orig_owui_terminal and (((t.get("function") or {}).get("name") or "").lower() in OPEN_TERMINAL_GUI_TOOLS))
            ]
            entry["tools_dropped"] = len(body.get("tools") or []) - len(gui)
            if gui:
                body["tools"] = gui
            else:
                body.pop("tools", None)      # a screenshot question or direct Computer use pick: no tools left
                body.pop("tool_choice", None)
        if orig_owui_terminal:
            strip_dropped_open_terminal_prompt(body)
        if tier == "screen" and door == "openai" and str(entry.get("asked") or "").lower() != "local-screen":
            last_u = next((m for m in reversed(body.get("messages") or []) if m.get("role") == "user"), None)
            u_content = last_u.get("content") if last_u else ""
            runner_mode, headed_h = northcua_harness.should_run_browser_loop(
                body, user_text(u_content), has_image(u_content)
            )
            if runner_mode == "desktop":
                return self._forward_desktop(body, model, entry)
            if runner_mode == "harness":
                return self._forward_harness(body, model, headed_h, entry)
            if runner_mode == "playwright":
                return self._forward_browser_mcp(body, model, headed_h, entry)
            only_h_tools = (
                not body.get("tools")
                or northcua_harness.has_only_harness_tools(body)[0]
                or northcua_harness.has_only_desktop_tools(body)
                or northcua_harness.has_openwebui_playwright_tools(body)[0]
            )
            if only_h_tools and northcua_harness.has_prior_browsing_turn(body.get("messages") or []):
                entry["harness"] = "skipped (follow-up answered from chat)"
            if body.get("tools") and only_h_tools:
                body.pop("tools", None)
                body.pop("tool_choice", None)
        if tier == "screen" and door == "openai" and body.get("tools"):
            if not any(m.get("role") in ("system", "developer") for m in body.get("messages") or []):
                # chat apps (Open WebUI) send no agent instructions: give NorthCUA a browser-agent brief
                inject_system(body, door, BROWSER_AGENT_SYSTEM.format(today=web_search.today_text()))
                entry["brief"] = "browser agent"
            else:
                today = web_search.today_text()
                sys_msgs = [user_text(m.get("content")) for m in body.get("messages") or []
                            if m.get("role") in ("system", "developer")]
                if not any(today in s for s in sys_msgs):
                    inject_system(body, door, f"Today is {today}; work out relative dates from it.")
        if tier == "screen" and door == "openai":
            saved = shrink_tool_pages(body)
            if saved:
                entry["pages_shrunk_kb"] = saved // 1024
        if door == "anthropic":
            prepare_anthropic(body, tier, model, thinking, budget)
            return self._forward(path, body, entry)
        if thinking and budget and body.get("tools"):
            names = tool_names(body)
            if OPENWEBUI_BUILTIN_MARK <= names:
                # Open WebUI's built-in tools (calendar, notes, memory...) only. Without them the request can
                # take the capped-thinking path; live web results are already in the prompt (add_live_info).
                body.pop("tools", None)
                body.pop("tool_choice", None)
                entry["tools_dropped"] = len(names)
                if orig_owui_terminal:
                    strip_dropped_open_terminal_prompt(body)
        if has_structured_response_format(body):
            prepare_openai(body, tier, model, thinking)
            return self._forward(path, body, entry)
        if thinking and budget and (not body.get("tools") or has_external_tools(body)):
            if tier == "3d" and not has_external_tools(body):
                apply_3d_openai(body)
            self._coding_brief(body, tier, entry)
            entry["translated"] = "openai->anthropic for thinking cap" + (" + tools" if body.get("tools") else "")
            return self._forward_translated(body, model, budget, entry)
        self._coding_brief(body, tier, entry)
        prepare_openai(body, tier, model, thinking)
        return self._forward(path, body, entry)

    @staticmethod
    def _coding_brief(body, tier, entry):
        if body.get("_internal_repair") or body.get("_internal_advanced_deck") or body.get("_internal_family_table") or body.get("_internal_deep_research") or body.get("_internal_smart_visuals") or has_external_tools(body):
            return
        reason = entry.get("reason", "")
        if not needs_coding_brief(tier, reason):
            return
        msgs = body.get("messages") or []
        last_u = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        last_txt = user_text(last_u.get("content")) if last_u else ""
        if tier in ("deep", "think") and THREED_WORDS.search(last_txt[:CLASSIFY_MAX_CHARS]) \
                and not any(THREED_SYSTEM[:60] in user_text(m.get("content")) for m in msgs if m.get("role") in ("system", "developer")):
            inject_system(body, "openai", THREED_SYSTEM)
        long_html = html_files.find_latest_html(msgs, min_lines=LONG_CODE_SURGICAL_LINES)
        if long_html:
            inject_system(body, "openai", CODING_SURGICAL_SYSTEM)
            entry["brief"] = f"surgical patch ({long_html.count(chr(10)) + 1}L base)"
        else:
            inject_system(body, "openai", CODING_CHAT_SYSTEM)
            entry["brief"] = "complete files"

    # -- images (OpenAI format) through ComfyUI + Qwen-Image-2.1
    def _run_comfy_image_job(self, entry, n, run_one):
        started = time.time()
        if not comfy_images.available():
            return self._error(503, "openai", "ComfyUI is not running on 127.0.0.1:8188 (start-llm-stack.ps1 starts it)")
        data, status = [], 200
        entry["gpu"] = image_enter()
        try:
            for _ in range(n):
                png, info = run_one()
                item = {"b64_json": base64.b64encode(png).decode()}
                if info.get("enhanced_prompt"):
                    item["revised_prompt"] = str(info["enhanced_prompt"])[:4000]
                data.append(item)
                for k in ("width", "height", "steps", "seed", "source_images"):
                    if k in info:
                        entry[k] = info[k]
        except Exception as e:
            status = 500
            entry["error"] = f"{e.__class__.__name__}: {e}"[:400]
        finally:
            image_exit()
        entry.update(time=time.strftime("%H:%M:%S"), status=status, images=len(data),
                     seconds=round(time.time() - started, 1))
        _recent.append(entry)
        log(json.dumps(entry))
        if status != 200:
            return self._error(status, "openai", entry["error"])
        return self._json(200, {"created": int(time.time()), "data": data})

    def _images(self, body):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return self._error(400, "openai", "prompt is required")
        n = max(1, min(int(body.get("n") or 1), 4))
        kw = _image_kwargs(body)
        entry = {"asked": body.get("model"), "tier": "image", "model": "qwen-image-2.1", "prompt": prompt[:120]}
        return self._run_comfy_image_job(entry, n, lambda: comfy_images.generate(prompt, **kw))

    def _image_edits(self, body, images):
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return self._error(400, "openai", "prompt is required")
        if not images:
            return self._error(400, "openai", "at least one source image is required")
        n = max(1, min(int(body.get("n") or 1), 4))
        kw = _image_kwargs(body)
        edit_kw = {k: kw[k] for k in ("size", "steps", "seed", "negative", "unet", "text_encoder", "prefix") if kw[k] is not None}
        if body.get("resolution") not in (None, ""):
            edit_kw["resolution"] = int(body["resolution"])
        entry = {"asked": body.get("model"), "tier": "image-edit", "model": "qwen-image-2.1",
                 "prompt": prompt[:120], "source_images": len(images)}
        return self._run_comfy_image_job(entry, n, lambda: comfy_images.edit(prompt, images, **edit_kw))

    # -- voice cloning & podcast narration through AI\voice-tts (127.0.0.1:8945)
    def _audio_speech(self, body):
        started = time.time()
        if not voice_tts.available():
            return self._error(503, "openai", "Voice TTS server is not running on 127.0.0.1:8945 (start-llm-stack.ps1 starts it)")
        entry = {"asked": body.get("model"), "tier": "audio-speech", "model": "voice-tts"}
        entry["gpu"] = voice_enter()
        try:
            st, ctype, raw = voice_tts.speech_bytes(body)
            entry.update(time=time.strftime("%H:%M:%S"), status=st, bytes=len(raw), seconds=round(time.time() - started, 1))
            _recent.append(entry)
            log(json.dumps(entry))
            if st != 200:
                return self._error(st, "openai", raw.decode("utf-8", "replace")[:300])
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except Exception as e:
            entry.update(time=time.strftime("%H:%M:%S"), status=500, error=str(e)[:200], seconds=round(time.time() - started, 1))
            _recent.append(entry)
            log(json.dumps(entry))
            return self._error(500, "openai", f"Voice TTS failed: {e}")
        finally:
            voice_exit()

    @staticmethod
    def _resolve_story_input(messages):
        """Use pasted story text directly, or pull the prior assistant story when the user gives a short command."""
        last_idx = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), None)
        if last_idx is None:
            return ""
        txt = user_text(messages[last_idx].get("content"))
        # If the user only gave a short directive ("Read that story in my voice") without pasting the story,
        # pull the most recent prose assistant reply in this conversation.
        has_colon_body = ":" in txt[:200] and len(txt.split(":", 1)[1].split()) >= 6
        if not has_colon_body and len(txt.split()) <= 18:
            for m in reversed(messages[:last_idx]):
                if m.get("role") != "assistant":
                    continue
                prev = user_text(m.get("content"))
                if prev and not prev.lstrip().startswith("### 🎙️") and not CODE_FENCE.search(prev) and len(prev.split()) >= 15:
                    return txt.rstrip(":") + ":\n\n" + prev
        return txt

    def _forward_voice_chat(self, body, reason):
        started = time.time()
        msgs = body.get("messages") or []
        stream = bool(body.get("stream"))
        story_input = self._resolve_story_input(msgs)
        entry = {"asked": body.get("model"), "tier": "voice", "model": "Qwen3-TTS 1.7B", "reason": reason}
        if not voice_tts.available():
            msg = ("⚠️ **Voice TTS server is not running** on `127.0.0.1:8945`. "
                   "Run the `LM Studio - start LLM stack` task or `%USERPROFILE%\\AI\\voice-tts\\run.ps1`.")
            if not stream:
                return self._json(200, {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion",
                    "created": int(time.time()), "model": "voice",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": msg}}],
                })
        cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

        def emit(delta, finish=None):
            obj = {
                "id": cid, "object": "chat.completion.chunk", "created": created, "model": "voice",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self._chunk(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n")

        status = 200
        try:
            if stream:
                self._ensure_sse_headers(tier="voice", model="voice")
                if self._is_openwebui_stream("openai", body) and str(entry.get("asked") or "").lower() in ("auto", "local-auto"):
                    self._emit_sse_json({"selected_model_id": "voice"}, tier="voice", model="voice")
                emit({"role": "assistant", "content": ""})
                if not voice_tts.available():
                    emit({"content": "⚠️ **Voice TTS server is not running** on `127.0.0.1:8945`."}, None)
                    emit({}, "stop")
                    self._chunk(b"data: [DONE]\n\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return
                emit({"reasoning_content": "Freeing GPU VRAM for voice cloning...\n"})

            entry["gpu"] = voice_enter()
            try:
                res = voice_tts.narrate_stream(
                    {"text": story_input},
                    on_progress=(lambda m: emit({"reasoning_content": m + "\n"})) if stream else None,
                    should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
                )
            finally:
                voice_exit()

            answer = voice_tts.format_narration_markdown(res)
            entry.update(language=res.get("language"), words=res.get("word_count"),
                         chunks=res.get("chunk_count"), tracks=len(res.get("tracks") or []))
            if not stream:
                out = {
                    "id": cid, "object": "chat.completion", "created": created, "model": "voice",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": answer}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                self._json(200, out, {"X-Router-Tier": "voice"})
            else:
                emit({"content": answer})
                emit({}, "stop")
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = "200 (client disconnected)"
        except Exception as e:
            if "cancelled by client" in str(e).lower():
                status = "200 (client disconnected)"
            else:
                status = f"failed: {e.__class__.__name__}: {e}"
                err_md = f"⚠️ **Voice narration error:** {e}"
                try:
                    if stream:
                        emit({"content": err_md})
                        emit({}, "stop")
                        self._chunk(b"data: [DONE]\n\n")
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    else:
                        self._error(500, "openai", str(e))
                except Exception:
                    pass
        entry.update(time=time.strftime("%H:%M:%S"), stream=stream, status=status, seconds=round(time.time() - started, 1))
        _recent.append(entry)
        log(json.dumps(entry))

    # -- plain proxy (streams whatever LM Studio sends)
    def _forward(self, path, body, entry):
        started = time.time()
        if body.get("stream") and path.startswith("/v1/chat/completions"):
            # exact token counts for every OpenAI stream, passthrough model names included (token ledger, 2026-11-10)
            body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
        payload = json.dumps(body).encode()
        conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=UPSTREAM_TIMEOUT_S)
        status = "?"
        wait_done = threading.Event()
        client_aborted = [False]
        conn_ref = [conn]
        client_sock = self.connection
        in_tokens, out_tokens, out_chars = 0, 0, 0   # read by the finally block (partial counts on a stop)

        def _watch_client():
            while not wait_done.wait(0.25):
                if northcua_harness.socket_disconnected(client_sock):
                    client_aborted[0] = True
                    northcua_harness._close_http_conn(conn_ref[0])
                    break

        threading.Thread(target=_watch_client, daemon=True, name="upstream-disconnect-watch").start()
        try:
            headers = {"Content-Type": "application/json"}
            if path.startswith("/v1/messages"):
                headers["anthropic-version"] = self.headers.get("anthropic-version", "2023-06-01")
            conn.request("POST", path, payload, headers)
            resp = conn.getresponse()
            status = resp.status
            if resp.status >= 500:
                raw = resp.read()
                log(f"UPSTREAM {resp.status} before any output (model {body.get('model')}), retrying in "
                    f"{RETRY_WAIT_S} s: {raw[:300].decode('utf-8', 'replace')}")
                conn.close()
                time.sleep(RETRY_WAIT_S)
                conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=UPSTREAM_TIMEOUT_S)
                conn_ref[0] = conn
                conn.request("POST", path, payload, headers)
                resp = conn.getresponse()
                status = f"{resp.status} (after retry)"
                if resp.status >= 500:
                    raw = resp.read()
                    wait_done.set()
                    log(f"UPSTREAM ERROR {resp.status} after retry: {raw[:600].decode('utf-8', 'replace')}")
                    self._error(502, "anthropic" if path.startswith("/v1/messages") else "openai",
                                readable_upstream_error(raw, body.get("model")))
                    conn.close()
                    self._record(entry, body, payload, started, status)
                    return
            if body.get("stream"):
                wait_done.set()
            if not getattr(self, "_sse_started", False):
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("X-Router-Tier", str(entry.get("tier")))
                self.send_header("X-Router-Model", str(entry.get("model", body.get("model"))))
                self.end_headers()
                if body.get("stream") and resp.status < 400 and not path.startswith("/v1/messages"):
                    self._sse_started = True
            is_oai_stream = bool(body.get("stream")) and resp.status < 400 and not path.startswith("/v1/messages")
            ui_meta = self._emit_owui_preamble(entry) if is_oai_stream else {}
            notice = entry.pop("_notice", None)
            if notice and resp.status < 400 and body.get("stream"):
                # "GPU busy, Flash is answering": a first content chunk before the model's own stream
                self._chunk(b"data: " + json.dumps({
                    "id": "chatcmpl-flash-notice", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": str(body.get("model")),
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": notice},
                                 "finish_reason": None}]}).encode() + b"\n\n")
            elif notice and resp.status < 400:
                raw = resp.read()
                wait_done.set()
                try:
                    obj = json.loads(raw)
                    ni, no = token_ledger.usage_counts(obj.get("usage"))
                    if ni:
                        entry["input_tokens"] = ni
                    if no:
                        entry["output_tokens"] = no
                    msg = obj["choices"][0]["message"]
                    msg["content"] = notice + (msg.get("content") or "")
                    raw = json.dumps(obj).encode()
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                self._chunk(raw)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                conn.close()
                self._record(entry, body, payload, started, status)
                return
            error_sample = b""
            # token counting for the other shapes (2026-11-10): Anthropic streams (Claude Code) are read, not changed;
            # non-streaming bodies (Open WebUI titles/tags, scripts, embeddings) are copied to read their "usage"
            is_anth_stream = bool(body.get("stream")) and resp.status < 400 and path.startswith("/v1/messages")
            body_copy = bytearray() if not body.get("stream") and resp.status < 400 else None
            sse_buf = b""
            first_token_at = None
            in_tokens, out_tokens, out_chars = 0, 0, 0
            out_text_parts = []
            photo_rw = (
                rich_ui.PhotoStreamRewriter(ui_meta.get("verified_images"))
                if is_oai_stream and entry.get("_owui_stream")
                else None
            )
            saw_done = False
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if resp.status >= 400 and len(error_sample) < 600:
                    error_sample += chunk
                if is_oai_stream:
                    sse_buf += chunk
                    rewritten = bytearray() if photo_rw is not None else None
                    while b"\n" in sse_buf:
                        sline, sse_buf = sse_buf.split(b"\n", 1)
                        sline = sline.strip()
                        if not sline:
                            continue
                        if sline == b"data: [DONE]":
                            saw_done = True
                            continue
                        if not sline.startswith(b"data: "):
                            if rewritten is not None:
                                rewritten.extend(sline + b"\n")
                            continue
                        try:
                            sev = json.loads(sline[6:])
                        except ValueError:
                            if rewritten is not None:
                                rewritten.extend(sline + b"\n\n")
                            continue
                        drop_empty_delta = False
                        for ch in sev.get("choices") or []:
                            dl = ch.get("delta") or {}
                            c_piece = dl.get("content") or ""
                            if c_piece:
                                out_chars += len(c_piece)
                                if photo_rw is not None:
                                    new_piece = photo_rw.feed(c_piece)
                                    dl["content"] = new_piece
                                    if new_piece:
                                        out_text_parts.append(new_piece)
                                    elif not dl.get("role") and not dl.get("reasoning_content") and not dl.get("tool_calls") and not ch.get("finish_reason"):
                                        drop_empty_delta = True
                                else:
                                    out_text_parts.append(c_piece)
                            r_piece = dl.get("reasoning_content") or ""
                            if r_piece:
                                out_chars += len(r_piece)
                            if (c_piece or r_piece or dl.get("tool_calls")) and first_token_at is None:
                                first_token_at = time.time()
                                self._clear_owui_loading_status(entry, ui_meta)
                            if photo_rw is not None and ch.get("finish_reason"):
                                tail_piece = photo_rw.flush()
                                if tail_piece:
                                    out_text_parts.append(tail_piece)
                                    rewritten.extend(
                                        b"data: "
                                        + json.dumps({
                                            "id": sev.get("id") or "chatcmpl-tail",
                                            "object": "chat.completion.chunk",
                                            "created": sev.get("created") or int(time.time()),
                                            "model": sev.get("model") or str(entry.get("model", "")),
                                            "choices": [{"index": 0, "delta": {"content": tail_piece}, "finish_reason": None}],
                                        }, ensure_ascii=False).encode("utf-8")
                                        + b"\n\n"
                                    )
                        su = sev.get("usage") or {}
                        if su:
                            in_tokens = int(su.get("prompt_tokens") or su.get("input_tokens") or in_tokens)
                            out_tokens = int(su.get("completion_tokens") or su.get("output_tokens") or out_tokens)
                        if rewritten is not None and not drop_empty_delta:
                            rewritten.extend(b"data: " + json.dumps(sev, ensure_ascii=False).encode("utf-8") + b"\n\n")
                    if rewritten is not None:
                        chunk = bytes(rewritten)
                    elif b"data: [DONE]" in chunk:
                        saw_done = True
                        chunk = chunk.replace(b"data: [DONE]\r\n\r\n", b"").replace(b"data: [DONE]\n\n", b"").replace(b"data: [DONE]", b"")
                elif is_anth_stream:
                    sse_buf += chunk
                    while b"\n" in sse_buf:
                        sline, sse_buf = sse_buf.split(b"\n", 1)
                        sline = sline.strip()
                        if not sline.startswith(b"data: "):
                            continue
                        try:
                            sev = json.loads(sline[6:])
                        except ValueError:
                            continue
                        etype = sev.get("type")
                        if etype == "message_start":
                            in_tokens = token_ledger.usage_counts((sev.get("message") or {}).get("usage"))[0] or in_tokens
                        elif etype == "content_block_delta" and first_token_at is None:
                            first_token_at = time.time()
                        elif etype == "message_delta":
                            mi, mo = token_ledger.usage_counts(sev.get("usage"))
                            in_tokens, out_tokens = mi or in_tokens, mo or out_tokens
                elif body_copy is not None and len(body_copy) < 16_000_000:
                    body_copy += chunk
                if chunk:
                    self._chunk(chunk)
            if is_oai_stream:
                if photo_rw is not None:
                    rem = photo_rw.flush()
                    if rem:
                        out_text_parts.append(rem)
                        self._emit_sse_json({
                            "id": "chatcmpl-tail",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": str(entry.get("model", body.get("model"))),
                            "choices": [{"index": 0, "delta": {"content": rem}, "finish_reason": None}],
                        }, tier=entry.get("tier", ""), model=str(entry.get("model", "")))
                self._clear_owui_loading_status(entry, ui_meta)
                self._emit_owui_fallback_photos(entry, ui_meta, "".join(out_text_parts))
                if (out_tokens <= 0 and out_chars > 0) or in_tokens <= 0:
                    entry["tokens_estimated"] = True   # LM Studio sent no usage: counted from text length
                if out_tokens <= 0 and out_chars > 0:
                    out_tokens = max(1, int(out_chars / 3.8))
                if in_tokens <= 0:
                    in_tokens = estimate_tokens(body)
                if out_tokens > 0:
                    usage_stats = _build_usage_stats(in_tokens, out_tokens, started, first_token_at, entry)
                    self._chunk(b"data: " + json.dumps({
                        "id": "chatcmpl-usage",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": str(entry.get("model", body.get("model"))),
                        "choices": [],
                        "usage": usage_stats,
                    }).encode() + b"\n\n")
                if saw_done:
                    self._chunk(b"data: [DONE]\n\n")
            elif is_anth_stream and (in_tokens or out_tokens):
                _build_usage_stats(in_tokens, out_tokens, started, first_token_at, entry)
            elif body_copy:
                try:   # no stream timing here, so no tok/s: just the counts
                    bi, bo = token_ledger.usage_counts(json.loads(bytes(body_copy)).get("usage"))
                    if bi:
                        entry["input_tokens"] = bi
                    if bo:
                        entry["output_tokens"] = bo
                except (ValueError, AttributeError):
                    pass
            wait_done.set()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            if error_sample:
                log(f"UPSTREAM ERROR {status}: {error_sample[:600].decode('utf-8', 'replace')}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = f"{status} (client disconnected)"
        except Exception as e:
            if client_aborted[0]:
                status = f"{status} (client disconnected)"
            else:
                status = f"failed: {e.__class__.__name__}: {e}"
                try:
                    self._error(502, "anthropic" if path.startswith("/v1/messages") else "openai",
                                f"LM Studio unreachable or failed: {e}")
                except Exception:
                    pass
        finally:
            wait_done.set()
            conn.close()
            if not (entry.get("input_tokens") or entry.get("output_tokens")) and (in_tokens or out_tokens or out_chars):
                # stopped part-way (Stop button, client gone, error): still count what the model already read/wrote
                entry["input_tokens"] = in_tokens or estimate_tokens(body)
                if out_tokens or out_chars:
                    entry["output_tokens"] = out_tokens or max(1, int(out_chars / 3.8))
                entry["tokens_estimated"] = True
        self._record(entry, body, payload, started, status)

    # -- OpenAI request answered via LM Studio's Anthropic endpoint, so the thinking cap (+ tools) applies
    def _forward_translated(self, body, model, budget, entry):
        started = time.time()
        abody = openai_to_anthropic(body, model, budget)
        payload = json.dumps(abody).encode()
        stream = abody["stream"]
        max_parallel_tools = MAX_PARALLEL_TOOL_CALLS_OWUI if entry.get("_owui_stream") else MAX_PARALLEL_TOOL_CALLS_AGENT
        conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=UPSTREAM_TIMEOUT_S)
        status = "?"
        wait_done = threading.Event()
        client_aborted = [False]
        conn_ref = [conn]
        client_sock = self.connection
        in_tokens, thought_len, answer, tool_arg_chars = 0, 0, [], 0

        def _watch_client():
            while not wait_done.wait(0.25):
                if northcua_harness.socket_disconnected(client_sock):
                    client_aborted[0] = True
                    northcua_harness._close_http_conn(conn_ref[0])
                    break

        threading.Thread(target=_watch_client, daemon=True, name="translated-disconnect-watch").start()
        try:
            hdr = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
            conn.request("POST", "/v1/messages", payload, hdr)
            resp = conn.getresponse()
            status = resp.status
            if resp.status >= 500:
                raw = resp.read()
                log(f"UPSTREAM {resp.status} before any output (model {model}), retrying in {RETRY_WAIT_S} s: "
                    f"{raw[:300].decode('utf-8', 'replace')}")
                conn.close()
                time.sleep(RETRY_WAIT_S)
                conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=UPSTREAM_TIMEOUT_S)
                conn_ref[0] = conn
                conn.request("POST", "/v1/messages", payload, hdr)
                resp = conn.getresponse()
                status = resp.status
            hdrs = {"X-Router-Tier": entry["tier"], "X-Router-Model": model}
            if resp.status >= 400 or not stream:
                raw = resp.read()
                wait_done.set()
                if resp.status >= 400:
                    log(f"UPSTREAM ERROR {status} (model {model}): {raw[:600].decode('utf-8', 'replace')}")
                    self._json(502 if resp.status >= 500 else resp.status,
                               {"error": {"message": readable_upstream_error(raw, model), "type": "upstream_error"}},
                               hdrs)
                else:
                    tdata = json.loads(raw)
                    ti, to = token_ledger.usage_counts(tdata.get("usage"))
                    if ti:
                        entry["input_tokens"] = ti
                    if to:
                        entry["output_tokens"] = to
                    oai_obj = anthropic_to_openai(tdata, model, max_tool_calls=max_parallel_tools)
                    msg_obj = oai_obj["choices"][0]["message"]
                    if not msg_obj.get("tool_calls") and msg_obj.get("content"):
                        foot = self._html_footer_with_repair(body, msg_obj["content"], model, entry)
                        if foot:
                            msg_obj["content"] += foot
                    self._json(200, oai_obj, hdrs)
                self._record(entry, abody, payload, started, status)
                return

            self._ensure_sse_headers(tier=entry["tier"], model=model, extra_headers=hdrs)
            ui_meta = self._emit_owui_preamble(entry)
            cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

            def emit(delta, finish=None, usage=None):
                obj = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage:
                    obj["usage"] = usage
                self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")

            emit({"role": "assistant", "content": ""})
            notice = entry.pop("_notice", None)
            if notice:
                emit({"content": notice})
            buf = b""
            got_output = False
            first_token_at = None
            in_tokens, out_tokens = 0, 0
            answer, footer_done = [], False
            photo_rw_t = rich_ui.PhotoStreamRewriter(ui_meta.get("verified_images")) if entry.get("_owui_stream") else None
            thought, thought_len, checked_at, looped = [], 0, 0, False
            cur_tool_idx = -1
            pending_tools = OrderedDict()
            seen_tool_sigs = set()
            dup_tool_calls = 0
            tool_looped = False
            saw_tool_use = False

            def _finalize_tool_block(blk):
                nonlocal cur_tool_idx, dup_tool_calls, tool_looped, saw_tool_use, tool_arg_chars
                raw_parts = "".join(blk["parts"]).strip()
                if raw_parts:
                    raw_args = raw_parts
                elif isinstance(blk.get("init"), dict) and blk["init"]:
                    raw_args = json.dumps(blk["init"], ensure_ascii=False)
                else:
                    raw_args = "{}"
                tool_arg_chars += len(raw_args)
                sig = _canonical_tool_sig(blk["name"], raw_args)
                if sig in seen_tool_sigs:
                    dup_tool_calls += 1
                    if dup_tool_calls >= 2 or (saw_tool_use and len(seen_tool_sigs) >= 3):
                        tool_looped = True
                    return
                if len(seen_tool_sigs) >= max_parallel_tools:
                    tool_looped = True
                    return
                seen_tool_sigs.add(sig)
                cur_tool_idx += 1
                saw_tool_use = True
                emit({
                    "tool_calls": [{
                        "index": cur_tool_idx,
                        "id": blk["id"],
                        "type": "function",
                        "function": {"name": blk["name"], "arguments": raw_args},
                    }]
                })

            while True:
                try:
                    chunk = resp.read1(65536)
                except TimeoutError:
                    msg = (f"The model stopped responding (no output for {STREAM_IDLE_S // 60} minutes), "
                           "probably because the PC ran short of memory. Please send the message again.")
                    log(f"WATCHDOG: {model} gave no output for {STREAM_IDLE_S} s, request ended")
                    status = "stalled (watchdog)"
                    self._chunk(b"data: " + json.dumps({"error": {"message": msg, "type": "model_stalled"}}).encode()
                                + b"\n\n")
                    break
                if not chunk:
                    break
                if not got_output:   # after the first bytes (model loaded, prompt read): watch for stalls
                    got_output = True
                    conn.sock.settimeout(STREAM_IDLE_S)
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.startswith(b"data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    etype = ev.get("type")
                    if etype == "message_start":
                        u = ((ev.get("message") or {}).get("usage") or {})
                        in_tokens = int(u.get("input_tokens") or in_tokens)
                    elif etype == "content_block_start":
                        cb = ev.get("content_block") or {}
                        if cb.get("type") == "tool_use":
                            if first_token_at is None:
                                first_token_at = time.time()
                                self._clear_owui_loading_status(entry, ui_meta)
                            pending_tools[ev.get("index")] = {
                                "id": cb.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                                "name": cb.get("name") or "",
                                "init": cb.get("input"),
                                "parts": [],
                            }
                    elif etype == "content_block_delta":
                        if first_token_at is None:
                            first_token_at = time.time()
                            self._clear_owui_loading_status(entry, ui_meta)
                        dl = ev.get("delta", {})
                        dtype = dl.get("type")
                        if dtype == "text_delta":
                            raw_t = dl.get("text", "")
                            out_t = photo_rw_t.feed(raw_t) if photo_rw_t is not None else raw_t
                            if out_t:
                                answer.append(out_t)
                                emit({"content": out_t})
                        elif dtype == "thinking_delta":
                            emit({"reasoning_content": dl.get("thinking", "")})
                            thought.append(dl.get("thinking", ""))
                            thought_len += len(thought[-1])
                            if thought_len - checked_at >= LOOP_CHECK_EVERY:
                                checked_at = thought_len
                                if thinking_loops("".join(thought)):
                                    looped = True
                                    break
                        elif dtype == "input_json_delta":
                            pjson = dl.get("partial_json") or ""
                            if pjson:
                                bidx = ev.get("index")
                                if bidx in pending_tools:
                                    pending_tools[bidx]["parts"].append(pjson)
                                elif pending_tools:
                                    pending_tools[next(reversed(pending_tools))]["parts"].append(pjson)
                    elif etype == "content_block_stop":
                        blk = pending_tools.pop(ev.get("index"), None)
                        if blk is not None:
                            _finalize_tool_block(blk)
                            if tool_looped:
                                break
                    elif etype == "message_delta":
                        if pending_tools:
                            for blk in list(pending_tools.values()):
                                _finalize_tool_block(blk)
                                if tool_looped:
                                    break
                            pending_tools.clear()
                            if tool_looped:
                                break
                        u = ev.get("usage") or {}
                        out_tokens = int(u.get("output_tokens") or out_tokens)
                        stop_reason = (ev.get("delta") or {}).get("stop_reason")
                        if photo_rw_t is not None:
                            rem_t = photo_rw_t.flush()
                            if rem_t:
                                answer.append(rem_t)
                                emit({"content": rem_t})
                        self._clear_owui_loading_status(entry, ui_meta)
                        if not answer and not saw_tool_use and not looped and thought:
                            recovered = "".join(thought).strip()
                            if recovered:
                                answer.append(recovered)
                                emit({"content": recovered})
                                entry["recovered_from_thinking"] = True
                        full_ans = "".join(answer)
                        if not footer_done and not saw_tool_use:
                            footer_done = True
                            foot = self._html_footer_with_repair(
                                body,
                                full_ans,
                                model,
                                entry,
                                emit_fn=lambda s: emit({"content": s}),
                                status_fn=(
                                    (lambda ev_obj: self._emit_sse_json(ev_obj, tier=entry.get("tier", ""), model=model))
                                    if entry.get("_owui_stream")
                                    else None
                                ),
                            )
                            if foot:
                                emit({"content": foot})
                        self._emit_owui_fallback_photos(entry, ui_meta, full_ans)
                        eff_in = in_tokens or estimate_tokens(body)
                        written_chars = thought_len + sum(len(a) for a in answer) + tool_arg_chars
                        eff_out = out_tokens or max(1, int(written_chars / 3.8))
                        if not (in_tokens and out_tokens):
                            entry["tokens_estimated"] = True
                        usage_stats = _build_usage_stats(eff_in, eff_out, started, first_token_at, entry)
                        fin = "tool_calls" if (saw_tool_use or stop_reason == "tool_use") else FINISH.get(stop_reason, "stop")
                        emit({}, fin, usage=usage_stats)
                if tool_looped:
                    northcua_harness._close_http_conn(conn)
                    log(f"TOOL_LOOP: {model} hit duplicate/excessive parallel tool calls ({len(seen_tool_sigs)} kept, "
                        f"{dup_tool_calls} duplicates dropped), cutting stream cleanly")
                    status = 200
                    entry["tool_loop_cut"] = f"{len(seen_tool_sigs)} kept, {dup_tool_calls} dup dropped"
                    self._clear_owui_loading_status(entry, ui_meta)
                    eff_in = in_tokens or estimate_tokens(body)
                    written_chars = thought_len + sum(len(a) for a in answer) + tool_arg_chars
                    eff_out = out_tokens or max(1, int(written_chars / 3.8))
                    if not (in_tokens and out_tokens):
                        entry["tokens_estimated"] = True
                    usage_stats = _build_usage_stats(eff_in, eff_out, started, first_token_at, entry)
                    emit({}, "tool_calls" if saw_tool_use else "stop", usage=usage_stats)
                    break
                if looped:
                    log(f"LOOP: {model} repeated the same reasoning {LOOP_REPEATS} times after "
                        f"{thought_len} characters of thinking, request stopped")
                    status = "stopped (thinking loop)"
                    eff_in = in_tokens or estimate_tokens(body)
                    eff_out = out_tokens or max(1, int((thought_len + sum(len(a) for a in answer) + tool_arg_chars) / 3.8))
                    if not (in_tokens and out_tokens):
                        entry["tokens_estimated"] = True
                    usage_stats = _build_usage_stats(eff_in, eff_out, started, first_token_at, entry)
                    emit({"content": "⚠️ **Stopped by the router:** the model was going round in circles (it repeated "
                                     "the same reasoning several times), so it was stopped instead of running on for "
                                     "minutes. Send the message again, or make it more specific (for example: paste "
                                     "the error and the lines it points to)."}, "stop", usage=usage_stats)
                    break
            wait_done.set()
            self._chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = f"{status} (client disconnected)"
        except Exception as e:
            if client_aborted[0]:
                status = f"{status} (client disconnected)"
            else:
                status = f"failed: {e.__class__.__name__}: {e}"
                try:
                    self._error(502, "openai", f"LM Studio unreachable or failed: {e}")
                except Exception:
                    pass
        finally:
            wait_done.set()
            conn.close()
            if not (entry.get("input_tokens") or entry.get("output_tokens")) and (in_tokens or thought_len or answer or tool_arg_chars):
                # stopped part-way (Stop button, client gone, watchdog): still count what was read/written
                entry["input_tokens"] = in_tokens or estimate_tokens(body)
                written = thought_len + sum(len(a) for a in answer) + tool_arg_chars
                if written:
                    entry["output_tokens"] = max(1, int(written / 3.8))
                entry["tokens_estimated"] = True
        self._record(entry, abody, payload, started, status)

    # -- NorthCUA Official Harness (`northcua local web run`) streamed directly to Open WebUI
    def _forward_harness(self, body, model, headed, entry):
        started = time.time()
        payload = json.dumps(body).encode()
        query = northcua_harness.build_query_from_messages(body.get("messages") or [])
        stream = bool(body.get("stream"))
        entry["harness"] = "headed" if headed else "headless"
        hdrs = {"X-Router-Tier": entry["tier"], "X-Router-Model": model}
        status = 200
        try:
            if not stream:
                res = northcua_harness.run_web_task(
                    query,
                    headed=headed,
                    should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
                )
                entry.update(harness_steps=res["steps"], harness_status=res["status"], start_url=res["url"])
                if res.get("terminal_status") == "cancelled":
                    status = "200 (client disconnected)"
                    self._record(entry, body, payload, started, status)
                    return
                out = {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": res["answer"]},
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                self._json(200, out, hdrs)
                self._record(entry, body, payload, started, status)
                return

            self._ensure_sse_headers(tier=entry["tier"], model=model, extra_headers=hdrs)
            self._emit_owui_preamble(entry)
            cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

            def emit(delta, finish=None):
                obj = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")

            emit({"role": "assistant", "content": ""})
            res = northcua_harness.run_web_task(
                query,
                headed=headed,
                on_progress=lambda txt: emit({"reasoning_content": txt}),
                should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
            )
            entry.update(harness_steps=res["steps"], harness_status=res["status"], start_url=res["url"])
            if res.get("terminal_status") == "cancelled":
                status = "200 (client disconnected)"
            else:
                emit({"content": res["answer"]})
                emit({}, "stop")
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = "200 (client disconnected)"
        except Exception as e:
            status = f"failed: {e.__class__.__name__}: {e}"
            try:
                self._error(502, "openai", f"NorthCUA Harness failed: {e}")
            except Exception:
                pass
        self._record(entry, body, payload, started, status)

    # -- NorthCUA Desktop Agent (`northcua local desktop run`) streamed directly to Open WebUI
    def _forward_desktop(self, body, model, entry):
        started = time.time()
        payload = json.dumps(body).encode()
        query = northcua_harness.build_query_from_messages(body.get("messages") or [])
        stream = bool(body.get("stream"))
        entry["desktop"] = "native"
        hdrs = {"X-Router-Tier": entry["tier"], "X-Router-Model": model}
        status = 200
        try:
            if not stream:
                res = northcua_harness.run_desktop_task(
                    query,
                    should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
                )
                entry.update(desktop_steps=res["steps"], desktop_status=res["status"])
                if res.get("terminal_status") == "cancelled":
                    status = "200 (client disconnected)"
                    self._record(entry, body, payload, started, status)
                    return
                out = {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": res["answer"]},
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                self._json(200, out, hdrs)
                self._record(entry, body, payload, started, status)
                return

            self._ensure_sse_headers(tier=entry["tier"], model=model, extra_headers=hdrs)
            self._emit_owui_preamble(entry)
            cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

            def emit(delta, finish=None):
                obj = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")

            emit({"role": "assistant", "content": ""})
            res = northcua_harness.run_desktop_task(
                query,
                on_progress=lambda txt: emit({"reasoning_content": txt}),
                should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
            )
            entry.update(desktop_steps=res["steps"], desktop_status=res["status"])
            if res.get("terminal_status") == "cancelled":
                status = "200 (client disconnected)"
            else:
                emit({"content": res["answer"]})
                emit({}, "stop")
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = "200 (client disconnected)"
        except Exception as e:
            status = f"failed: {e.__class__.__name__}: {e}"
            try:
                self._error(502, "openai", f"NorthCUA Desktop failed: {e}")
            except Exception:
                pass
        self._record(entry, body, payload, started, status)

    # -- Built-in Playwright MCP browser loop (`run_playwright_task`) streamed directly to Open WebUI
    def _forward_browser_mcp(self, body, model, headed, entry):
        started = time.time()
        payload = json.dumps(body).encode()
        msgs = body.get("messages") or []
        stream = bool(body.get("stream"))
        entry["browser_mcp"] = "headed" if headed else "headless"
        entry["brief"] = "browser agent"
        sys_prompt = BROWSER_AGENT_SYSTEM.format(today=web_search.today_text())
        hdrs = {"X-Router-Tier": entry["tier"], "X-Router-Model": model}
        status = 200
        try:
            if not stream:
                res = northcua_harness.run_playwright_task(
                    msgs,
                    model=model,
                    system_prompt=sys_prompt,
                    headed=headed,
                    should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
                    lmstudio_host=LMSTUDIO_HOST,
                    lmstudio_port=LMSTUDIO_PORT,
                    page_shrink_fn=page_text.shrink,
                    page_limit_last=PAGE_LIMIT_LAST,
                    page_limit_older=PAGE_LIMIT_OLDER,
                )
                entry.update(
                    browser_steps=res["steps"],
                    model_calls=res["model_calls"],
                    browser_status=res["status"],
                )
                if res.get("pages_shrunk_kb"):
                    entry["pages_shrunk_kb"] = res["pages_shrunk_kb"]
                if res.get("url"):
                    entry["last_url"] = res["url"]
                if res.get("terminal_status") == "cancelled":
                    status = "200 (client disconnected)"
                    self._record(entry, body, payload, started, status)
                    return
                out = {
                    "id": "chatcmpl-" + uuid.uuid4().hex[:24],
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": res["answer"]},
                    }],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
                self._json(200, out, hdrs)
                self._record(entry, body, payload, started, status)
                return

            self._ensure_sse_headers(tier=entry["tier"], model=model, extra_headers=hdrs)
            self._emit_owui_preamble(entry)
            cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())

            def emit(delta, finish=None):
                obj = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")

            emit({"role": "assistant", "content": ""})
            res = northcua_harness.run_playwright_task(
                msgs,
                model=model,
                system_prompt=sys_prompt,
                headed=headed,
                on_progress=lambda txt: emit({"reasoning_content": txt}),
                should_stop=lambda: northcua_harness.socket_disconnected(self.connection),
                lmstudio_host=LMSTUDIO_HOST,
                lmstudio_port=LMSTUDIO_PORT,
                page_shrink_fn=page_text.shrink,
                page_limit_last=PAGE_LIMIT_LAST,
                page_limit_older=PAGE_LIMIT_OLDER,
            )
            entry.update(
                browser_steps=res["steps"],
                model_calls=res["model_calls"],
                browser_status=res["status"],
            )
            if res.get("pages_shrunk_kb"):
                entry["pages_shrunk_kb"] = res["pages_shrunk_kb"]
            if res.get("url"):
                entry["last_url"] = res["url"]
            if res.get("terminal_status") == "cancelled":
                status = "200 (client disconnected)"
            else:
                emit({"content": res["answer"]})
                emit({}, "stop")
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            status = "200 (client disconnected)"
        except Exception as e:
            status = f"failed: {e.__class__.__name__}: {e}"
            try:
                self._error(502, "openai", f"Browser MCP loop failed: {e}")
            except Exception:
                pass
        self._record(entry, body, payload, started, status)

    @staticmethod
    def _auto_repair_html_once(model, path, html, res):
        """Targeted surgical fix pass(es) on the already-loaded coding model when html_check.js catches an error.
        Runs a 2nd pass strictly when pass 1 fixed a parse-time SyntaxError and uncovered a runtime/overlay issue."""
        cur_html, cur_res, improved = html, res, False
        for attempt in range(2):
            errs = list((cur_res or {}).get("errors") or [])
            covered = (cur_res or {}).get("covered")
            if not errs and not covered:
                break
            excerpts = html_files.numbered_error_excerpts(cur_html, errs, covered=covered, radius=16)
            prob_lines = [f"- {e}" for e in errs[:4]]
            if covered:
                prob_lines.append(f"- After startup, the page remains covered behind `{covered}` (likely stuck overlay/handler).")
            ctx = excerpts if excerpts else f"Full HTML ({len(cur_html.splitlines())} lines):\n```html\n{cur_html}\n```"
            prompt = (
                "Fix the JavaScript/HTML runtime or syntax error(s) in this single-file browser app using minimal exact "
                "`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` patch block(s). Do NOT rewrite the whole file and do NOT "
                "include line-number prefixes (`  123| `) inside SEARCH or REPLACE.\n\n"
                "Browser check findings:\n" + "\n".join(prob_lines) + "\n\n" + ctx
            )
            rbody = {
                "model": model,
                "stream": False,
                "max_tokens": HTML_REPAIR_THINK_BUDGET + 6144,
                "thinking": {"type": "enabled", "budget_tokens": HTML_REPAIR_THINK_BUDGET},
                "system": "Output only exact `<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` patch blocks that fix the error.",
                "messages": [{"role": "user", "content": prompt}],
            }
            conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=240)
            try:
                conn.request(
                    "POST",
                    "/v1/messages",
                    json.dumps(rbody).encode(),
                    {"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
                )
                resp = conn.getresponse()
                raw = resp.read()
                if resp.status != 200:
                    break
                rdata = json.loads(raw)
                count_internal(rdata, model, "html auto-repair")
                reply_txt = "".join(b.get("text", "") for b in rdata.get("content", []) if b.get("type") == "text")
                cand_html, applied, _tot = html_files.apply_search_replace(cur_html, reply_txt)
                if not cand_html:
                    cand_html = html_files.extract_html(reply_txt)
                if not cand_html or cand_html == cur_html:
                    break
                path.write_text(cand_html, encoding="utf-8")
                new_res = html_files.check(path)
                old_bad = (len(errs), 1 if covered else 0)
                new_errs = list((new_res or {}).get("errors") or [])
                new_bad = (len(new_errs), 1 if (new_res or {}).get("covered") else 0)
                if new_res and not new_res.get("failed") and new_bad < old_bad:
                    log(f"html auto-repair pass {attempt + 1} succeeded ({model}, {applied} patch block(s)): {old_bad} -> {new_bad}")
                    had_syntax = any("SyntaxError" in str(e) for e in errs)
                    still_syntax = any("SyntaxError" in str(e) for e in new_errs)
                    cur_html, cur_res, improved = cand_html, new_res, True
                    if attempt == 0 and had_syntax and not still_syntax and (new_errs or new_res.get("covered")):
                        continue
                    break
                path.write_text(cur_html, encoding="utf-8")
                log(f"html auto-repair pass {attempt + 1} did not improve ({old_bad} -> {new_bad}); reverted")
                break
            except Exception as e:
                try:
                    path.write_text(cur_html, encoding="utf-8")
                except OSError:
                    pass
                log(f"html auto-repair pass {attempt + 1} failed ({e.__class__.__name__}: {e})")
                break
            finally:
                conn.close()
        return cur_html, cur_res, improved

    @classmethod
    def _html_footer_with_repair(cls, body, text, model, entry, emit_fn=None, status_fn=None):
        if body.get("_internal_repair") or body.get("_internal_scene3d") or body.get("_internal_family_table") or body.get("_internal_deep_research") or body.get("_internal_smart_visuals") or has_external_tools(body) or has_structured_response_format(body):
            return None
        low = (text or "").lower()
        if "</html>" not in low and ">>>>>>> replace" not in low:
            return None
        try:
            if status_fn:
                status_fn(rich_ui.status_event("🧪 Verifying generated app in headless Chromium…", done=False))
            base_html = html_files.find_latest_html(body.get("messages") or [], min_lines=30)
            html, path, rel, res, merged = html_files.resolve_and_check(text, base_html=base_html)
            if not html:
                if status_fn:
                    status_fn(rich_ui.status_event("Ready", done=True, hidden=True))
                return None
            auto_repaired = False
            errs = list((res or {}).get("errors") or [])
            covered = (res or {}).get("covered")
            if (
                res
                and not res.get("failed")
                and (errs or covered)
                and model in (MODELS["code"], MODELS["deep"])
            ):
                issue = errs[0][:110] if errs else f"stuck behind {covered}"
                if status_fn:
                    status_fn(rich_ui.status_event(f"🔧 Auto-repairing browser error ({issue[:60]})…", done=False))
                if emit_fn:
                    emit_fn(f"\n\n> 🔧 **Browser self-check caught an issue** (`{issue}`) — running 1 automatic fix pass...\n")
                html, res, auto_repaired = cls._auto_repair_html_once(model, path, html, res)
            if status_fn:
                final_errs = list((res or {}).get("errors") or [])
                ok_check = bool(res and not res.get("failed") and not final_errs and not (res or {}).get("covered"))
                status_fn(
                    rich_ui.status_event(
                        "✅ Verified in headless Chromium" if ok_check else "🧪 Browser check completed",
                        done=True,
                    )
                )
            md, info = html_files.format_footer(path, rel, res, merged_blocks=merged, auto_repaired=auto_repaired)
            if info:
                entry.update(info)
            return md
        except Exception as e:
            log(f"html_files failed: {e.__class__.__name__}: {e}")
            return None

    @staticmethod
    def _html_footer(text, entry):
        if "</html>" not in text.lower():
            return None
        try:
            md, info = html_files.footer(text)
            if info:
                entry.update(info)
            return md
        except Exception as e:
            log(f"html_files failed: {e.__class__.__name__}: {e}")
            return None

    def _record(self, entry, body, payload, started, status):
        entry.pop("_notice", None)
        entry.pop("_ui", None)
        entry.pop("_owui_stream", None)
        entry.pop("_owui_selected_model", None)
        entry.pop("_owui_loading_status", None)
        if SAVE_LAST_REQUEST:
            try:   # keep the last forwarded request per tier, for debugging (overwritten each time)
                LOG_FILE.with_name(f"last_request_{entry.get('tier', 'x')}.json").write_bytes(payload)
            except OSError:
                pass
        entry.update(time=time.strftime("%H:%M:%S"), stream=bool(body.get("stream")), status=status,
                     seconds=round(time.time() - started, 1), request_kb=len(payload) // 1024)
        _recent.append(entry)
        log(json.dumps(entry))
        if entry.get("input_tokens") or entry.get("output_tokens"):   # token ledger (token-usage.csv), kept forever
            ua = self.headers.get("User-Agent", "") if self.headers else ""
            token_ledger.record(
                "request", token_ledger.client_name(ua, entry.get("asked")), entry.get("tier"),
                entry.get("model", body.get("model")), entry.get("input_tokens", 0), entry.get("output_tokens", 0),
                door="anthropic" if str(self.path).startswith("/v1/messages") else "openai",
                estimated=entry.get("tokens_estimated", False), tok_s=entry.get("tok_s", ""),
                seconds=entry.get("seconds", ""), status=status, user_agent=ua)


class ExclusiveServer(ThreadingHTTPServer):
    """Python's HTTP server sets SO_REUSEADDR, which on Windows lets a second process bind the same port: 31 routers
    shared :4000 on 2026-11-08 (each with its own in-flight counts, so the swap guard and Flash redirect broke).
    Exclusive use makes a second copy fail to start instead."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def router_already_running():
    try:
        conn = http.client.HTTPConnection(LISTEN_HOST, LISTEN_PORT, timeout=3)
        conn.request("GET", "/health")
        ok = conn.getresponse().status == 200
        conn.close()
        return ok
    except OSError:
        return False


def main():
    if router_already_running():
        log("router: another router already answers on :4000, this copy exits")
        return
    try:
        server = ExclusiveServer((LISTEN_HOST, LISTEN_PORT), Handler)
    except OSError as e:
        log(f"router: cannot take :4000 ({e}), this copy exits")
        return
    server.daemon_threads = True
    log(f"router v3 listening on http://{LISTEN_HOST}:{LISTEN_PORT} -> LM Studio {LMSTUDIO_HOST}:{LMSTUDIO_PORT}")
    try:
        html_files.start_server()
        log(f"generated apps served on http://{html_files.FILES_HOST}:{html_files.FILES_PORT} "
            f"(folder {html_files.GENERATED_DIR})")
    except OSError as e:
        log(f"generated-apps file server not started: {e}")
    try:
        northcua_harness.start_server()
        log(f"NorthCUA Harness MCP server listening on http://{northcua_harness.MCP_HOST}:{northcua_harness.MCP_PORT}/mcp")
    except OSError as e:
        log(f"NorthCUA Harness MCP server not started: {e}")
    threading.Thread(target=ensure_cpu_models, args=("startup",), daemon=True).start()
    threading.Thread(target=_keeper_loop, daemon=True, name="cpu-keeper").start()
    server.serve_forever()


if __name__ == "__main__":
    main()
