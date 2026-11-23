"""Browser agent runner (Playwright MCP loop + NorthCUA Official Harness) and MCP server for the router and Open WebUI.

Default web runner (when `🖥️ Computer use` / `northcua-nano` is picked with no tool toggled, or when the user asks any
model in plain text to open/use the browser):
- Runs the Playwright MCP loop (`run_playwright_task`, `:8932` headless by default, `:8931` headed when the prompt
  asks to see the browser, `HEADED_HINT_RE`) on `northcua-nano` with
  `page_text.py` snapshot compression, streaming live thinking and `🔧 browser_*` steps into `reasoning_content`.

NorthCUA Official Harness (`beipo-ai/NorthCUA` v0.2.0 in `%USERPROFILE%\\AI\\northcua-harness`):
- Kept installed for Phase 3 (Windows desktop control) and used for web tasks when the user explicitly says
  "NorthCUA harness" (`is_explicit_harness_request`) or toggles on the `NorthCUA` MCP tool (`127.0.0.1:8933/mcp`).
"""
import ctypes
from ctypes import wintypes
import http.client
import json
import locale
import os
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_guard  # noqa: E402  (refuses requests sent by web pages / DNS rebinding, security review 2026-11-08)

HARNESS_DIR = Path.home() / "AI" / "northcua-harness"
APP_CONFIG = HARNESS_DIR / "logs" / "my-config" / "app.example.yaml"
MCP_HOST, MCP_PORT = "127.0.0.1", int(os.environ.get("NORTHCUA_MCP_PORT", "8933"))
PLAYWRIGHT_HEADED_URL = "http://127.0.0.1:8931/mcp"
PLAYWRIGHT_HEADLESS_URL = "http://127.0.0.1:8932/mcp"
PLAYWRIGHT_BLOCK_TOOLS = {"browser_run_code_unsafe", "browser_file_upload", "browser_close"}
SCREEN_MIN_MAX_TOKENS = 8192
DEFAULT_MAX_STEPS = 15
TIMEOUT_S = 360
NO_WINDOW = 0x08000000


def _detect_windows_ui_locale() -> str:
    """Return active Windows UI locale (e.g. 'en_US', 'fr_FR') dynamically at runtime."""
    try:
        lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        return str(locale.windows_locale.get(lang_id, "") or "")
    except Exception:
        return ""

HARNESS_TOOLS = {"northcua_browse", "northcua_browse_headless"}
DESKTOP_TOOLS = {"desktop_use", "desktop_use_desktop_use", "northcua_desktop"}

EXPLICIT_HARNESS_RE = re.compile(
    r"\b(northcua[\s-]+harness|harness\s+(?:de\s+|d')?northcua)\b",
    re.I,
)
HEADLESS_HINT_RE = re.compile(
    r"\b(headless|sans\s+fen[êe]tre|en\s+arri[èe]re[- ]plan)\b",
    re.I,
)
# Headless is the default since 2026-11-18: the visible Chrome window (:8931) opens only when the prompt asks for it.
HEADED_HINT_RE = re.compile(
    r"\b(?:(?:in|mode|run|go)\s+headed|headed\s+(?:mode|browser|chrome|window)"
    r"|show\s+(?:me\s+)?(?:the\s+|your\s+)?(?:browser|window|chrome)"
    r"|show\s+me\s+what\s+(?:you(?:['’]re|\s+are)|it(?:['’]s|\s+is))\s+doing"
    r"|visible\s+(?:browser|window|chrome)|let\s+me\s+watch|so\s+i\s+can\s+watch"
    r"|avec\s+fen[êe]tre|fen[êe]tre\s+visible|navigateur\s+visible|en\s+mode\s+visible"
    r"|montre[- ]moi\s+(?:le\s+navigateur|la\s+fen[êe]tre|chrome|ce\s+que\s+tu\s+fais))\b",
    re.I,
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
URL_RE = re.compile(r"https?://[^\s,)\"'>\]]+", re.I)
DOMAIN_RE = re.compile(
    r"\b((?:[a-z0-9-]+\.)+(?:com|co\.uk|org|org\.uk|gov\.uk|gov|net|io|ai|edu|ac\.uk|fr|de|eu)(?:/[^\s,)\"'>\]]*)?)\b",
    re.I,
)

KNOWN_SITES = (
    (re.compile(r"\bmet\s*office\b", re.I), "https://www.metoffice.gov.uk"),
    (re.compile(r"\b(the\s+o2|o2\s+arena|o2\s+events?)\b", re.I), "https://www.theo2.co.uk/events"),
    (re.compile(r"\bwikipedia\b", re.I), "https://en.wikipedia.org"),
    (re.compile(r"\bbbc(\s+news|\s+weather)?\b", re.I), "https://www.bbc.co.uk"),
    (re.compile(r"\btime\s*out(?:\s+london)?\b", re.I), "https://www.timeout.com/london"),
    (re.compile(r"\bhacker\s*news\b", re.I), "https://news.ycombinator.com"),
    (re.compile(r"\bgithub\b", re.I), "https://github.com"),
    (re.compile(r"\breddit\b", re.I), "https://www.reddit.com"),
    (re.compile(r"\byoutube\b", re.I), "https://www.youtube.com"),
    (re.compile(r"\bamazon\.co\.uk\b|\bamazon\s+uk\b", re.I), "https://www.amazon.co.uk"),
    (re.compile(r"\bamazon\.com\b|\bamazon\s+us\b", re.I), "https://www.amazon.com"),
    (re.compile(r"\bamazon\b", re.I), "https://www.amazon.co.uk"),     # the user is in the UK
)

# Phrases that explicitly ask the assistant to drive a browser or use NorthCUA even when another model is selected.
# Anchored to imperative or second-person polite request openers so everyday questions mentioning browsers/navigation
# ("How do I navigate to the display settings?", "in the browser and on my phone", "browse to a folder") never match.
_REQ_PREFIX = (
    r"^\s*"
    r"(?:(?:please|pls|hey|hi|hello|bonjour|salut|coucou|s'il\s+(?:te|vous)\s+pla[îi]t|stp|svp)\s*[,!]?\s*)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"|(?:peux[- ]tu|pourrais[- ]tu|pouvez[- ]vous|pourriez[- ]vous|tu\s+peux|vous\s+pouvez|"
    r"est[- ]ce\s+que\s+(?:tu\s+peux|vous\s+pouvez))\s+)?"
)
_WEB_TARGET = (
    r"(?:https?://(?!(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b)\S+"
    r"|(?:[a-z0-9-]+\.)+(?:com|co\.uk|org|org\.uk|gov\.uk|gov|net|io|ai|edu|ac\.uk|fr|de|eu)\b)"
)
# Well-known sites named without an address ("can you go to amazon and ...", 2026-11-08: answered "I can't browse"),
# and "the X website" / "le site de X". Only used after a navigation verb at the start of a request (rule 4).
_SITE_NAME = (
    r"(?:amazon|ebay|argos|john\s+lewis|currys|ikea|etsy|tesco|sainsbury'?s|asda|ocado|boots|zara|uniqlo|asos|"
    r"booking(?:\.com)?|airbnb|expedia|skyscanner|trainline|national\s+rail|tfl|sncf(?:\s+connect)?|ryanair|easyjet|"
    r"eurostar|the\s+o2|o2\s+arena|met\s*office|bbc(?:\s+news|\s+weather|\s+sport)?|the\s+guardian|time\s*out(?:\s+london)?|wikipedia|"
    r"youtube|reddit|github|hacker\s+news|google\s+maps|tripadvisor|rightmove|zoopla|imdb|spotify)\b"
    r"|(?:the\s+)?[\w'&.-]+(?:\s+[\w'&.-]+){0,2}\s+(?:website|web\s*site|site)\b"
    r"|(?:le\s+)?site\s+(?:web\s+)?(?:de\s+la|de\s+l'|du|des|de)\s*\w+"
)
BROWSER_TASK_PHRASES = re.compile(
    _REQ_PREFIX + r"(?:"
    # 1. Explicit NorthCUA / NorthCUA harness invocation: "use NorthCUA to...", "use the NorthCUA harness to..."
    r"(?:use|run|launch|start|with|utilise[rz]?|lance[rz]?|avec)\s+(?:(?:the|le)\s+)?(?:northcua(?:\s+harness)?|harness\s+northcua)\b"
    r"|(?:northcua(?:\s+harness)?|harness\s+northcua)\s*[,:]"
    # 2. Explicit browser launch/use command: "use/open/launch/start the browser to/and ..."
    r"|(?:use|open|launch|start)\s+(?:the\s+|a\s+)?browser\s+(?:to|and)\s+(?!leave\b|join\b|switch\b|have\b|close\b)\w+"
    r"|(?:with|in|using)\s+(?:the\s+|a\s+)?browser\s*(?:,\s*|\s+to\s+|\s+(?:check|open|go|find|look|read|see|search|visit|get)\b)"
    # 3. French browser launch/use command: "ouvre/utilise/lance le navigateur pour/et ..."
    r"|(?:ouvre|ouvrir|utilise[rz]?|lance[rz]?|d[ée]marre[rz]?)\s+(?:le\s+|un\s+)?navigateur\s+(?:pour|et)\s+\w+"
    r"|(?:avec|dans)\s+(?:le\s+|un\s+)?navigateur\s*(?:,\s*|\s+pour\s+|\s+(?:v[ée]rifie|regarde|cherche|va|ouvre|trouve|consulte|lis)\b)"
    # 4. Direct navigation command to an external URL or domain: "go to/on / navigate to / browse to / va sur <url/domain>"
    r"|(?:go\s+(?:to|on)|navigate\s+to|browse\s+to|browse|visit|va\s+sur|aller\s+sur|allez\s+sur)\s+(?:" + _WEB_TARGET + r"|" + _SITE_NAME + r")" +
    # 5. Action command followed by "in/with/using the browser" or "avec/dans le navigateur" or "with NorthCUA (harness)"
    r"|(?:go\s+to|navigate\s+to|browse\s+to|open|visit|check|look\s+up|find|search|"
    r"va\s+sur|aller\s+sur|ouvre|ouvrir|v[ée]rifie[rz]?|regarde[rz]?|cherche[rz]?|trouve[rz]?)\b"
    r"[^.?!\n]{1,80}\b(?:(?:in|with|using)\s+(?:the\s+|a\s+)?browser|(?:avec|dans)\s+(?:le\s+|un\s+)?navigateur|(?:with|via|using|avec)\s+(?:(?:the|le)\s+)?(?:northcua(?:\s+harness)?|harness\s+northcua))\b"
    r")",
    re.I,
)


def is_browser_task(text):
    """True only when `text` explicitly asks the assistant to drive the browser or use NorthCUA for a web task."""
    return bool(text and BROWSER_TASK_PHRASES.search(text))


# Phrases that explicitly ask the assistant to act on or inspect the real Windows desktop (Phase 3: Desktop Use).
# Anchored with `_REQ_PREFIX` so everyday questions mentioning Windows or desktop settings never match.
DESKTOP_TASK_PHRASES = re.compile(
    _REQ_PREFIX + r"(?:"
    # 1. "use/run/launch/start/control (my|the) (windows )?desktop (agent )?(to|and) ..." or "use NorthCUA desktop ..."
    r"(?:use|run|launch|start|control|utilise[rz]?|lance[rz]?|contr[ôo]le[rz]?)\s+(?:(?:my|the|mon|le)\s+)?"
    r"(?:windows\s+)?(?:desktop(?:\s+agent|\s+use)?|northcua\s+desktop|bureau(?:\s+windows)?)\b"
    r"|(?:desktop\s+use|northcua\s+desktop)\s*[,:]"
    # 2. "on (my|the) (windows )?desktop[,] (open|launch|start|click|check|look|find|type|press|take|close|see|read|tell) ..."
    r"|(?:on|from)\s+(?:my\s+|the\s+)?(?:windows\s+)?desktop(?:\s+screen)?\s*(?:,\s*|\s+(?:please\s+)?)"
    r"(?:open|launch|start|click|double[- ]click|check|look|find|type|press|take|close|see|read|tell|what|which)\b"
    # 3. "look at / check / inspect (my|the) desktop( screen)? (and|to) ..."
    r"|(?:look\s+at|check|inspect)\s+(?:my\s+|the\s+)?(?:windows\s+)?desktop(?:\s+screen)?\s+(?:and|to)\s+\w+"
    # 4. French: "sur (mon|le) bureau( windows)?[,] (ouvre|lance|clique|vérifie|regarde|cherche|trouve|ferme|tape|appuie|dis|quel) ..."
    r"|sur\s+(?:mon\s+|le\s+)?bureau(?:\s+windows)?\s*(?:,\s*|\s+(?:s'il\s+te\s+pla[îi]t\s+)?)"
    r"(?:ouvre|ouvrir|lance[rz]?|clique[rz]?|v[ée]rifie[rz]?|regarde[rz]?|cherche[rz]?|trouve[rz]?|ferme[rz]?|tape[rz]?|appuie[rz]?|dis|quel)\b"
    r"|(?:regarde[rz]?|v[ée]rifie[rz]?|inspecte[rz]?)\s+(?:l'[ée]cran\s+de\s+)?(?:mon\s+|le\s+)?bureau(?:\s+windows)?\s+(?:et|pour)\s+\w+"
    # 5. Action command followed by "on my/the (windows )?desktop" or "with Desktop Use" or "sur mon/le bureau"
    r"|(?:open|launch|start|click|check|look\s+at|inspect|close|ouvre|ouvrir|lance[rz]?|clique[rz]?|v[ée]rifie[rz]?|regarde[rz]?|ferme[rz]?)\b"
    r"[^.?!\n]{1,80}\b(?:on\s+(?:my|the)\s+(?:windows\s+)?desktop(?:\s+screen)?|(?:with|using)\s+desktop\s+use|sur\s+(?:mon|le)\s+bureau(?:\s+windows)?)\b"
    # 6. Direct command to open Windows settings / OS system dialogs on this PC
    r"|(?:open|launch|start|ouvre|ouvrir|lance[rz]?)\s+(?:(?:my|the|mes|les|le|la|l')\s*)?"
    r"(?:windows\s+(?:(?:[a-z]+\s+){0,2}(?:settings|menu|panel|page)|update|security|defender|terminal|store)\b"
    r"|(?:control\s+panel|task\s+manager|device\s+manager|file\s+explorer|start\s+menu|"
    r"system\s+settings|display\s+settings|sound\s+settings|bluetooth\s+settings|network\s+settings)\b"
    r"|(?:param[èe]tres\s+(?:windows|de\s+windows|syst[èe]me|d'affichage|r[ée]seau|bluetooth|son)|"
    r"mises?\s+[àa]\s+jour\s+windows|panneau\s+de\s+configuration|gestionnaire\s+des\s+t[âa]ches|"
    r"explorateur\s+de\s+fichiers|menu\s+d[ée]marrer)\b)"
    r")",
    re.I,
)


def is_desktop_task(text):
    """True only when `text` explicitly asks the assistant to control or inspect the Windows desktop."""
    return bool(text and DESKTOP_TASK_PHRASES.search(text))


def is_explicit_harness_request(text):
    """True only when `text` explicitly mentions 'NorthCUA harness' (or 'harness NorthCUA')."""
    return bool(text and EXPLICIT_HARNESS_RE.search(text))

# Short greetings or meta questions that should not launch a multi-step browser session when Computer use is selected.
NON_BROWSE_CHAT = re.compile(
    r"^\s*(hi|hello|hey|bonjour|salut|coucou|yo|good\s+(morning|afternoon|evening)|"
    r"thanks?|thank\s+you|merci|ok|okay|yes|no|oui|non|sure|great|cool|"
    r"who\s+are\s+you\??|what\s+(are\s+you|can\s+you\s+do)\??|tu\s+es\s+qui\s*\??|que\s+peux[- ]tu\s+faire\s*\??)[\s.!?\-]*$",
    re.I,
)

# Cues in a follow-up message (Turn 2+ in Computer use mode) that ask for a new web lookup or page action
# rather than asking about, comparing, formatting, or translating the result already in the chat.
_FOLLOWUP_BROWSE_RE = re.compile(
    r"(\b(browse|navigate|visit|click|tap|scroll|search\s+(?:for|on)|look\s+up|find\s+out|"
    r"open\s+(?:the\s+|a\s+)?(?:page|link|site|website|article|tab|url|first|second|third|event|show|result)|"
    r"go\s+(?:to|back|into)|"
    r"(?:now|also|next)\s+(?:check|find|search|look|go|open|see|get)|"
    r"check\s+(?:on|the\s+(?:site|website|page|link)|for\s+\w+|if\s+there|whether|tomorrow|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"what\s+about\s+(?:on\s+|at\s+|in\s+|for\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"tomorrow|tonight|this\s+weekend|next\s+week|january|february|march|april|may|june|july|august|september|october|november|december))\b|"
    r"\binstead\s*[.?!]*$|"
    r"\bwhat\s+about\s+(?:on\s+|at\s+|in\s+|for\s+)?[A-Z][a-z]{2,}\b|"
    r"\b(ouvre|ouvrir|clique[rz]?|cherche[rz]?|recherche[rz]?|va\s+sur|allez\s+sur|retourne[rz]?\s+sur|"
    r"(?:maintenant|aussi|ensuite)\s+(?:v[ée]rifie|regarde|cherche|trouve|va|ouvre)|"
    r"v[ée]rifie[rz]?\s+(?:aussi|maintenant|sur|pour|les\s+horaires|la\s+m[ée]t[ée]o|les\s+[ée]v[ée]nements|demain|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)|"
    r"regarde[rz]?\s+(?:aussi|maintenant|sur|pour|demain|lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche))\b|"
    r"\b(?:à\s+la\s+place|plut[ôo]t)\s*[.?!]*$|"
    r"\bet\s+(?:pour|sur|[àa])\s+(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|demain|ce\s+week[- ]end|[A-ZÉÈÊ][a-zéèêàù]{2,})\b)",
    re.I,
)


def available():
    return HARNESS_DIR.exists() and APP_CONFIG.exists()


def _uv_exe():
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")


def extract_start_url(query, url=""):
    """Pick the best starting URL from an explicit url argument, a URL/domain in the query, known site names, or DuckDuckGo."""
    if url and str(url).strip():
        u = str(url).strip()
        return u if re.match(r"^https?://", u, re.I) else f"https://{u}"
    text = str(query or "")
    m = URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?")
    for pat, site_url in KNOWN_SITES:
        if pat.search(text):
            return site_url
    m = DOMAIN_RE.search(text)
    if m:
        return "https://" + m.group(1).rstrip(".,;:!?")
    return "https://duckduckgo.com"


def tool_short_names(body):
    return {((t.get("function") or {}).get("name") or "").split("__")[-1]
            for t in (body.get("tools") or []) if isinstance(t, dict)}


def has_only_harness_tools(body):
    """(is_harness_only, headed): True when the request includes NorthCUA Harness MCP tools (`northcua_browse*`)."""
    names = tool_short_names(body)
    harness = names & HARNESS_TOOLS
    if not harness:
        return False, True
    headed = "northcua_browse" in harness or "northcua_browse_headless" not in harness
    return True, headed


def has_only_desktop_tools(body):
    """True when the request includes the Desktop Use MCP tool (`desktop_use` / `desktop_use_desktop_use` / `northcua_desktop`)."""
    for t in (body.get("tools") or []):
        if isinstance(t, dict):
            n = ((t.get("function") or {}).get("name") or t.get("name") or "").lower()
            if "desktop_use" in n or "northcua_desktop" in n:
                return True
    return False


def has_openwebui_playwright_tools(body):
    """(is_owui_pw, headed): True when Open WebUI's `Browser Use` (`browser_browser_*`) or
    `Browser Use (headless)` (`browser_headless_browser_*`) MCP tools are attached."""
    names = [
        ((t.get("function") or {}).get("name") or t.get("name") or "").lower()
        for t in (body.get("tools") or [])
        if isinstance(t, dict)
    ]
    has_headed = any(n.startswith("browser_browser_") for n in names)
    has_headless = any(n.startswith("browser_headless_browser_") for n in names)
    if not (has_headed or has_headless):
        return False, False
    return True, has_headed


def _msg_text(msg):
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text").strip()
    return ""


def has_prior_browsing_turn(messages):
    """True when the chat already has a non-greeting user request and an assistant reply before the latest turn."""
    prior = (messages or [])[:-1]
    had_task_user = any(
        m.get("role") == "user" and _msg_text(m) and not NON_BROWSE_CHAT.match(_msg_text(m))
        for m in prior
    )
    had_assistant = any(m.get("role") == "assistant" and _msg_text(m) for m in prior)
    return had_task_user and had_assistant


def is_followup_browse_task(text, messages=None):
    """In Computer use mode on Turn 2+, True only when the follow-up asks for something new on the web."""
    clean = (text or "").strip()
    if not clean or NON_BROWSE_CHAT.match(clean):
        return False
    if is_browser_task(clean) or URL_RE.search(clean) or DOMAIN_RE.search(clean):
        return True
    if _FOLLOWUP_BROWSE_RE.search(clean):
        return True
    prior_user_text = " ".join(_msg_text(m) for m in (messages or [])[:-1] if m.get("role") == "user")
    for pat, _ in KNOWN_SITES:
        if pat.search(clean) and not pat.search(prior_user_text):
            return True
    return False


_FOLLOWUP_DESKTOP_RE = re.compile(
    r"(\b(click|double[- ]click|press|type|scroll|drag|move|open|launch|start|close|screenshot|"
    r"look\s+at|check|inspect|find|switch\s+to|focus|minimize|maximize|"
    r"clique[rz]?|appuie[rz]?|tape[rz]?|ouvre|ouvrir|lance[rz]?|ferme[rz]?|regarde[rz]?|v[ée]rifie[rz]?)\b)",
    re.I,
)


def is_followup_desktop_task(text):
    """In Desktop Use mode on Turn 2+, True only when the follow-up asks for a new desktop action or observation."""
    clean = (text or "").strip()
    if not clean or NON_BROWSE_CHAT.match(clean):
        return False
    return bool(is_desktop_task(clean) or _FOLLOWUP_DESKTOP_RE.search(clean))


def should_run_browser_loop(body, text, has_img=False):
    """(runner, headed): whether a `screen`-tier OpenAI request should run a router-side browser or desktop loop.
    Returns:
      - ("desktop", True): when the `Desktop Use` MCP tool is toggled on or the user asks in plain text to use the desktop.
      - ("playwright", headed): default for `🖥️ Computer use` with no tool toggled or plain-text browser requests
        (headless unless the prompt asks to see the browser, `HEADED_HINT_RE`).
      - ("harness", headed): when the user explicitly says "NorthCUA harness" or toggles the `NorthCUA` web MCP tool.
      - (None, True): when not a router-side loop (e.g. Playwright MCP tools already attached by Open WebUI,
        screenshot question, greeting, or chat-only follow-up on a previous turn).
    """
    if has_img:
        return None, True
    msgs = body.get("messages") or []
    if not msgs or msgs[-1].get("role") != "user":
        return None, True
    if any(m.get("role") == "tool" for m in msgs):
        return None, True
    clean = (text or "").strip()
    if not clean or NON_BROWSE_CHAT.match(clean):
        return None, True
    desktop_tool = has_only_desktop_tools(body)
    if desktop_tool or is_desktop_task(clean):
        if has_prior_browsing_turn(msgs) and not is_followup_desktop_task(clean):
            return None, True
        return ("desktop", True) if available() else (None, True)
    if has_prior_browsing_turn(msgs) and not is_followup_browse_task(clean, msgs):
        return None, True
    headed = bool(HEADED_HINT_RE.search(clean)) and not HEADLESS_HINT_RE.search(clean)
    if body.get("tools"):
        only_h, h_headed = has_only_harness_tools(body)
        if only_h:
            return ("harness" if available() else "playwright"), h_headed
        is_owui_pw, pw_headed = has_openwebui_playwright_tools(body)
        if is_owui_pw:
            if HEADLESS_HINT_RE.search(clean):
                pw_headed = False
            elif HEADED_HINT_RE.search(clean):
                pw_headed = True
            if is_explicit_harness_request(clean) and available():
                return "harness", pw_headed
            return "playwright", pw_headed
        return None, True
    if is_explicit_harness_request(clean) and available():
        return "harness", headed
    return "playwright", headed


def should_run_harness(body, text, has_img=False):
    """(run_now, headed): True when a `screen`-tier OpenAI request should execute a router-side browser loop."""
    runner, headed = should_run_browser_loop(body, text, has_img)
    return bool(runner), headed


def build_query_from_messages(messages):
    """Combine the latest user request with brief earlier user questions (and last visited URL) if needed for context."""
    user_msgs = []
    last_assistant_url = ""
    for m in messages or []:
        if m.get("role") == "user":
            t = _msg_text(m)
            if t and not t.startswith("### Task:"):
                user_msgs.append(t)
        elif m.get("role") == "assistant":
            um = URL_RE.search(_msg_text(m))
            if um:
                last_assistant_url = um.group(0).rstrip(".,;:!?")
    if not user_msgs:
        return ""
    latest = user_msgs[-1]
    if len(user_msgs) == 1 or len(latest) > 120:
        return latest
    prev = " | ".join(m[:160] for m in user_msgs[:-1][-2:])
    url_hint = f" | Previous page URL: {last_assistant_url}" if last_assistant_url else ""
    return f"{latest}\n(Earlier context in this chat: {prev}{url_hint})"


_kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9


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


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
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


def _attach_kill_on_close_job(proc):
    """Attach `proc` to a Windows Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE."""
    if not _kernel32:
        return None
    try:
        _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            wintypes.HANDLE(job),
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        if not _kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle))):
            _kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except Exception:
        return None


def socket_disconnected(sock):
    """True when the peer of a non-reading HTTP socket has closed or reset the connection."""
    if sock is None:
        return False
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _kill_process_tree(proc, job=None):
    """Terminate `proc` and all of its descendant processes (uv -> northcua -> python -> node -> chrome)."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                creationflags=NO_WINDOW,
            )
    except Exception:
        pass
    if job and _kernel32:
        try:
            _kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)
        except Exception:
            pass
        try:
            _kernel32.CloseHandle(wintypes.HANDLE(job))
        except Exception:
            pass
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def run_web_task(query, url="", max_steps=DEFAULT_MAX_STEPS, headed=True, on_progress=None,
                 should_stop=None, timeout_s=TIMEOUT_S):
    """Run `northcua local web run` and return a result dict (`success`, `steps`, `answer`, `url`, `session_id`, `seconds`)."""
    start_url = extract_start_url(query, url)
    uv = _uv_exe()
    cmd = [
        uv, "run", "--no-sync", "northcua", "local", "web", "run",
        "--app-config", str(APP_CONFIG),
        "--url", start_url,
        "--query", str(query),
        "--max-steps", str(max_steps),
        *(["--headed"] if headed else []),
    ]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["OPENAI_API_KEY"] = "lm-studio"
    env["NORTHCUA_MODEL"] = "local-screen"

    if on_progress:
        mode = "headed Chrome" if headed else "headless Chrome"
        on_progress(f"🧭 **NorthCUA Harness** starting ({mode}, max {max_steps} steps) → `{start_url}`\n")

    t0 = time.time()
    session_id = None
    trace_dir = None
    in_thought = False
    proc = subprocess.Popen(
        cmd,
        cwd=str(HARNESS_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=NO_WINDOW,
    )
    job = _attach_kill_on_close_job(proc)

    line_q = queue.Queue()

    def _reader():
        try:
            for rline in proc.stdout:
                line_q.put(rline)
        except Exception:
            pass
        finally:
            line_q.put(None)

    threading.Thread(target=_reader, daemon=True, name="northcua-stdout").start()

    timed_out = False
    cancelled = False
    callback_error = None
    raw_lines = []
    try:
        while True:
            if time.time() - t0 >= timeout_s:
                timed_out = True
                break
            if should_stop and should_stop():
                cancelled = True
                break
            try:
                raw_line = line_q.get(timeout=0.3)
            except queue.Empty:
                if proc.poll() is not None and line_q.empty():
                    break
                continue
            if raw_line is None:
                break
            raw_lines.append(raw_line)
            line = ANSI_RE.sub("", raw_line).strip().strip("│").strip()
            if not line:
                continue
            m_sess = re.match(r"^Session:\s*(\S+)", line)
            if m_sess:
                session_id = m_sess.group(1).strip()
                continue
            m_trace = re.match(r"^Trace Directory:\s*(.+)$", line)
            if m_trace:
                trace_dir = m_trace.group(1).strip()
                continue
            m_step = re.match(r"^[›>]\s*Step\s+(\d+)", line)
            if m_step:
                in_thought = False
                if on_progress:
                    on_progress(f"\n**Step {m_step.group(1)}**\n")
                continue
            if line == "Thought:":
                in_thought = True
                continue
            if line.startswith("Tool Summary:"):
                in_thought = False
                if on_progress:
                    on_progress(f"🔧 `{line[len('Tool Summary:'):].strip()}`\n")
                continue
            if line.startswith(("Observation:", "Status:", "Terminal Status:", "Final Answer:", "Success:")):
                in_thought = False
                continue
            if in_thought and on_progress and not line.startswith(("─", "═", "╭", "╰", "│")):
                on_progress(line + "\n")
    except Exception as e:
        callback_error = e
    finally:
        _kill_process_tree(proc, job)

    if callback_error is not None:
        raise callback_error

    elapsed = round(time.time() - t0, 1)
    if cancelled:
        return {
            "success": False,
            "status": "cancelled",
            "terminal_status": "cancelled",
            "steps": 0,
            "answer": "NorthCUA Harness stopped (client disconnected).",
            "url": start_url,
            "session_id": session_id,
            "trace_dir": str(trace_dir or ""),
            "seconds": elapsed,
        }
    trace_path = (HARNESS_DIR / trace_dir) if trace_dir else (HARNESS_DIR / "logs" / session_id if session_id else None)
    if not trace_path or not (trace_path / "result.json").exists():
        web_dirs = sorted(
            (p for p in (HARNESS_DIR / "logs").glob("web-*") if p.stat().st_mtime >= t0 - 2),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if web_dirs:
            trace_path = web_dirs[0]
            session_id = session_id or trace_path.name
    res_file = (trace_path / "result.json") if trace_path else None

    if not timed_out and res_file and res_file.exists():
        try:
            res = json.loads(res_file.read_text(encoding="utf-8", errors="replace"))
            steps_list = res.get("steps") or []
            answer = (res.get("final_answer") or "").strip()
            if not answer and steps_list:
                answer = (steps_list[-1].get("assistant_text") or "").strip()
            return {
                "success": bool(res.get("success")),
                "status": res.get("status", "unknown"),
                "terminal_status": res.get("terminal_status", "unknown"),
                "steps": len(steps_list),
                "answer": answer or "NorthCUA finished without a final text answer.",
                "url": start_url,
                "session_id": session_id,
                "trace_dir": str(trace_path),
                "seconds": elapsed,
            }
        except Exception as e:
            return {
                "success": False,
                "status": "failed",
                "terminal_status": "read_error",
                "steps": 0,
                "answer": f"NorthCUA finished, but its result file could not be read: {e}",
                "url": start_url,
                "session_id": session_id,
                "trace_dir": str(trace_path or ""),
                "seconds": elapsed,
            }

    tail = ANSI_RE.sub("", "".join(raw_lines[-20:])).strip()
    reason = f"timed out after {timeout_s} s" if timed_out else (tail[-400:] or "no result.json produced")
    return {
        "success": False,
        "status": "failed",
        "terminal_status": "timeout" if timed_out else "error",
        "steps": 0,
        "answer": f"NorthCUA Harness could not complete the task ({reason}).",
        "url": start_url,
        "session_id": session_id,
        "trace_dir": str(trace_path or ""),
        "seconds": elapsed,
    }


HUD_SCRIPT = Path(__file__).resolve().parent / "desktop_hud.py"


def _python_console_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _extract_user_task_summary(query: str) -> str:
    """Extract the latest user request from a multi-turn query string for the permission popup & HUD."""
    q = str(query or "").strip()
    marker = "Current request from the user:"
    if marker in q:
        q = q.split(marker, 1)[1].strip()
    return q or "Control the Windows desktop"


def run_desktop_task(
    query,
    max_steps=DEFAULT_MAX_STEPS,
    on_progress=None,
    should_stop=None,
    timeout_s=TIMEOUT_S,
    require_confirm=True,
    show_hud=True,
):
    """Run `northcua local desktop run` on the Windows desktop with permission popup, active screen HUD, and F8/button stop."""
    uv = _uv_exe()
    py_exe = _python_console_exe()
    task_summary = _extract_user_task_summary(query)
    t0 = time.time()

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["OPENAI_API_KEY"] = "lm-studio"
    env["NORTHCUA_MODEL"] = "local-screen"

    # 1. Permission popup on the interactive Windows desktop before touching mouse/keyboard
    if require_confirm and HUD_SCRIPT.exists():
        if on_progress:
            on_progress("⏳ **Waiting for your permission** on the desktop popup (click **Allow** or press **Enter** on your PC)...\n")
        confirm_proc = subprocess.Popen(
            [py_exe, "-I", str(HUD_SCRIPT), "--mode", "confirm", "--task", task_summary, "--timeout", "120"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=env,
            creationflags=NO_WINDOW,
        )
        confirm_cancelled = False
        try:
            while confirm_proc.poll() is None:
                if should_stop and should_stop():
                    confirm_cancelled = True
                    break
                time.sleep(0.2)
        finally:
            if confirm_proc.poll() is None:
                _kill_process_tree(confirm_proc, None)

        if confirm_cancelled:
            return {
                "success": False,
                "status": "cancelled",
                "terminal_status": "cancelled",
                "steps": 0,
                "answer": "Desktop Agent stopped (client disconnected).",
                "session_id": None,
                "trace_dir": "",
                "seconds": round(time.time() - t0, 1),
            }
        if confirm_proc.returncode != 0:
            if on_progress:
                on_progress("🚫 **Desktop control declined** on the PC permission popup.\n")
            return {
                "success": False,
                "status": "declined",
                "terminal_status": "declined",
                "steps": 0,
                "answer": "🚫 Desktop control was declined on the permission popup.",
                "session_id": None,
                "trace_dir": "",
                "seconds": round(time.time() - t0, 1),
            }
        if on_progress:
            on_progress("✓ **Permission granted.** ")

    ui_loc = _detect_windows_ui_locale()
    loc_clause = f" (Windows UI locale: {ui_loc})" if ui_loc else ""
    desktop_query = (
        f"{str(query).strip()}\n\n"
        f"[Environment note: This machine is a real Windows 11 PC{loc_clause}. "
        "It is NEVER Linux/Ubuntu/GNOME — never claim this is Ubuntu or Linux. "
        "System UI labels match the active Windows UI locale, so to open Windows settings, dialogs, or apps "
        "quickly and reliably regardless of OS language, prefer pressing keypress(keys=['win', 'r']) to open the "
        "Windows Run dialog, then in the next step calling type(text=...) and keypress(key='enter') — for example: "
        "'SystemPropertiesPerformance.exe' opens Windows Performance Options (Adjust appearance and performance of Windows), "
        "'sysdm.cpl' opens System Properties, 'ms-settings:' opens Windows Settings, 'taskmgr' opens Task Manager, "
        "'calc' opens Calculator, 'notepad' opens Notepad.]"
    )
    cmd = [
        uv, "run", "--no-sync", "northcua", "local", "desktop", "run",
        "--app-config", str(APP_CONFIG),
        "--query", desktop_query,
        "--max-steps", str(max_steps),
    ]

    if on_progress:
        on_progress(
            f"🖥️ **Desktop Agent (NorthCUA)** active (max {max_steps} steps — click **⏹ STOP** on the top bar or press **F8** anytime to stop)\n"
        )

    # 2. Launch the active screen border + top-center HUD with STOP (F8) button
    hud_proc = None
    if show_hud and HUD_SCRIPT.exists():
        try:
            hud_proc = subprocess.Popen(
                [py_exe, "-I", str(HUD_SCRIPT), "--mode", "hud", "--task", task_summary],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
                creationflags=NO_WINDOW,
            )
        except Exception:
            hud_proc = None

    def _hud_update(status_text: str) -> None:
        if hud_proc is None or hud_proc.poll() is not None or hud_proc.stdin is None:
            return
        try:
            hud_proc.stdin.write(json.dumps({"status": status_text}) + "\n")
            hud_proc.stdin.flush()
        except Exception:
            pass

    t0 = time.time()
    session_id = None
    trace_dir = None
    section = None
    current_step = 0
    proc = subprocess.Popen(
        cmd,
        cwd=str(HARNESS_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=NO_WINDOW,
    )
    job = _attach_kill_on_close_job(proc)

    line_q = queue.Queue()

    def _reader():
        try:
            for rline in proc.stdout:
                line_q.put(rline)
        except Exception:
            pass
        finally:
            line_q.put(None)

    threading.Thread(target=_reader, daemon=True, name="northcua-desktop-stdout").start()

    timed_out = False
    cancelled = False
    stopped_by_hud = False
    callback_error = None
    raw_lines = []
    try:
        while True:
            if time.time() - t0 >= timeout_s:
                timed_out = True
                break
            if should_stop and should_stop():
                cancelled = True
                break
            if hud_proc is not None and hud_proc.poll() == 42:
                stopped_by_hud = True
                break
            try:
                raw_line = line_q.get(timeout=0.25)
            except queue.Empty:
                if hud_proc is not None and hud_proc.poll() == 42:
                    stopped_by_hud = True
                    break
                if proc.poll() is not None and line_q.empty():
                    break
                continue
            if raw_line is None:
                break
            raw_lines.append(raw_line)
            clean_box = ANSI_RE.sub("", raw_line).strip()
            m_step_box = re.match(r"^[┌╭]─\s*Step\s+(\d+)\b", clean_box)
            if m_step_box:
                section = None
                current_step = int(m_step_box.group(1))
                _hud_update(f"Step {current_step}/{max_steps} — Inspecting screen & thinking...")
                if on_progress:
                    on_progress(f"\n**Step {current_step}**\n")
                continue
            if clean_box.startswith(("┌", "╭", "└", "╰", "─", "═")):
                if "NorthCUA Summary" in clean_box:
                    section = "summary"
                continue
            line = clean_box.strip("│").strip()
            if not line:
                continue
            m_sess = re.match(r"^Session:\s*(\S+)", line)
            if m_sess:
                session_id = m_sess.group(1).strip()
                continue
            m_trace = re.match(r"^Trace Dir(?:ectory)?:\s*(.+)$", line)
            if m_trace:
                trace_dir = m_trace.group(1).strip()
                continue
            m_step = re.match(r"^[›>]\s*Step\s+(\d+)", line)
            if m_step:
                section = None
                current_step = int(m_step.group(1))
                _hud_update(f"Step {current_step}/{max_steps} — Inspecting screen & thinking...")
                if on_progress:
                    on_progress(f"\n**Step {current_step}**\n")
                continue
            if line in ("Thought", "Thought:"):
                section = "thought"
                continue
            if line in ("Assistant", "Assistant:"):
                section = "assistant"
                continue
            if line in ("Tools", "Tools:"):
                section = "tools"
                continue
            if line in ("Summary", "Run", "Warning"):
                section = "summary"
                continue
            if section == "thought":
                if line != "(none)" and on_progress:
                    on_progress(line + "\n")
                continue
            if section == "tools":
                if line.startswith("•"):
                    tool_desc = line.lstrip("•").strip()
                    _hud_update(f"Step {current_step or 1}/{max_steps} — 🔧 {tool_desc}")
                    if on_progress:
                        on_progress(f"🔧 `{tool_desc}`\n")
                continue
    except Exception as e:
        callback_error = e
    finally:
        _kill_process_tree(proc, job)
        if hud_proc is not None:
            try:
                if hud_proc.stdin:
                    hud_proc.stdin.close()
            except Exception:
                pass
            if hud_proc.poll() is None:
                _kill_process_tree(hud_proc, None)

    if callback_error is not None:
        raise callback_error

    elapsed = round(time.time() - t0, 1)
    if stopped_by_hud:
        if on_progress:
            on_progress("\n🛑 **Stopped by user from Desktop HUD (STOP button / F8).**\n")
        return {
            "success": False,
            "status": "stopped_by_user",
            "terminal_status": "stopped_by_user",
            "steps": current_step,
            "answer": "⏹ Desktop control was stopped from your screen (STOP button / F8).",
            "session_id": session_id,
            "trace_dir": str(trace_dir or ""),
            "seconds": elapsed,
        }
    if cancelled:
        return {
            "success": False,
            "status": "cancelled",
            "terminal_status": "cancelled",
            "steps": current_step,
            "answer": "Desktop Agent stopped (client disconnected).",
            "session_id": session_id,
            "trace_dir": str(trace_dir or ""),
            "seconds": elapsed,
        }

    trace_path = (HARNESS_DIR / trace_dir) if trace_dir else (HARNESS_DIR / "logs" / session_id if session_id else None)
    if not trace_path or not trace_path.exists():
        task_dirs = sorted(
            (p for p in (HARNESS_DIR / "logs").glob("task-*") if p.stat().st_mtime >= t0 - 2),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if task_dirs:
            trace_path = task_dirs[0]
            session_id = session_id or trace_path.name

    steps_file = (trace_path / "steps.jsonl") if trace_path else None
    eval_file = (trace_path / "evaluation.jsonl") if trace_path else None

    if not timed_out and trace_path and ((steps_file and steps_file.exists()) or (eval_file and eval_file.exists())):
        try:
            steps_rows = []
            if steps_file and steps_file.exists():
                for sline in steps_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if sline.strip():
                        steps_rows.append(json.loads(sline))
            eval_row = {}
            if eval_file and eval_file.exists():
                for eline in eval_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if eline.strip():
                        eval_row = json.loads(eline)
            last_step = steps_rows[-1] if steps_rows else {}
            td = eval_row.get("terminal_decision") or last_step.get("terminal_decision") or {}
            answer = (
                td.get("final_answer")
                or eval_row.get("result")
                or last_step.get("result")
                or last_step.get("assistant_text")
                or ""
            ).strip()
            status = str(eval_row.get("status") or last_step.get("status") or td.get("action") or "completed")
            success = bool(td.get("success")) if "success" in td else (status == "finish" and bool(answer))
            return {
                "success": success,
                "status": "completed" if success else status,
                "terminal_status": str(td.get("reason") or status),
                "steps": len(steps_rows) or int(eval_row.get("session_step") or 0),
                "answer": answer or "Desktop Agent finished without a final text answer.",
                "session_id": session_id,
                "trace_dir": str(trace_path),
                "seconds": elapsed,
            }
        except Exception as e:
            return {
                "success": False,
                "status": "failed",
                "terminal_status": "read_error",
                "steps": 0,
                "answer": f"Desktop Agent finished, but its trace log could not be read: {e}",
                "session_id": session_id,
                "trace_dir": str(trace_path or ""),
                "seconds": elapsed,
            }

    tail = ANSI_RE.sub("", "".join(raw_lines[-25:])).strip()
    reason = f"timed out after {timeout_s} s" if timed_out else (tail[-400:] or "no desktop trace produced")
    return {
        "success": False,
        "status": "failed",
        "terminal_status": "timeout" if timed_out else "error",
        "steps": 0,
        "answer": f"Desktop Agent could not complete the task ({reason}).",
        "session_id": session_id,
        "trace_dir": str(trace_path or ""),
        "seconds": elapsed,
    }


# ---------------------------------------------------------------- Playwright MCP loop (router built-in browser agent)
class _PlaywrightMCP:
    """Streamable HTTP MCP client for the local Playwright MCP servers (:8931 headed, :8932 headless)."""

    def __init__(self, url=PLAYWRIGHT_HEADED_URL, timeout=90):
        self.url = url
        self.timeout = timeout
        self.sid = None
        self.n = 0
        self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "router-playwright-loop", "version": "1.0"},
            },
        )
        self.notify("notifications/initialized")

    def _post(self, msg):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(msg).encode("utf-8"), headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            self.sid = resp.headers.get("Mcp-Session-Id") or self.sid
            raw = resp.read().decode("utf-8", errors="replace")
        if not raw.strip():
            return None
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        last = None
        for line in raw.splitlines():
            if line.startswith("data: "):
                last = json.loads(line[6:])
        return last

    def call(self, method, params=None):
        self.n += 1
        out = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if not out:
            raise RuntimeError(f"Empty MCP reply from {self.url} for {method}")
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})

    def tools(self):
        return self.call("tools/list").get("tools") or []

    def tool(self, name, **args):
        return self.call("tools/call", {"name": name, "arguments": args})


def _close_http_conn(conn):
    if conn is None:
        return
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _count_step_tokens(model, usage, step_msgs, thought, content, tool_calls, t0):
    """One Playwright-loop model call -> token_ledger (this loop talks to LM Studio directly, not via the router).
    Uses LM Studio's usage chunk; falls back to ~3.8 characters per token (marked as estimated). Never raises."""
    try:
        import token_ledger   # same folder (router.py put it on sys.path)
        inp, out = token_ledger.usage_counts(usage)
        estimated = not (inp and out)
        if not out:
            out = int((len(thought) + len(content) + len(json.dumps(tool_calls))) / 3.8) if (thought or content or tool_calls) else 0
        if not inp:
            inp = int(len(json.dumps(step_msgs, ensure_ascii=False)) / 3.8)
        token_ledger.record("browser loop", "router (Playwright loop)", "screen", model, inp, out,
                            door="openai", estimated=estimated, seconds=round(time.time() - t0, 1))
    except Exception:
        pass


def _step_thought_loops(text):
    """Detect degenerate consecutive repetition in the trailing reasoning window without aborting multi-pass checks."""
    n = len(text)
    if n >= 3000:
        w = text[-250:]
        if len(w.strip()) >= 80 and text[-3000:].count(w) >= 6:
            return True
    if n >= 6000:
        w = text[-400:]
        if len(w.strip()) >= 100 and text[-6000:].count(w) >= 5:
            return True
    if n >= 12000:
        w = text[-600:]
        if len(w.strip()) >= 120 and text[-12000:].count(w) >= 5:
            return True
    return False


def run_playwright_task(
    messages,
    model="northcua-nano",
    system_prompt="",
    max_steps=DEFAULT_MAX_STEPS,
    headed=True,
    on_progress=None,
    should_stop=None,
    timeout_s=TIMEOUT_S,
    lmstudio_host="127.0.0.1",
    lmstudio_port=1234,
    page_shrink_fn=None,
    page_limit_last=60000,
    page_limit_older=6000,
):
    """Run the Playwright MCP browser loop (`agent_test.py` logic + `page_text.py` compression) directly in the router.
    Streams live `<think>` and `🔧 browser_*` tool calls via `on_progress`, gives `northcua-nano` 8192 output tokens
    per step, recovers automatically if thinking hits the token cap without a tool call or answer, and aborts the
    in-flight LM Studio request within 0.25 s on client disconnect or timeout.
    """
    t0 = time.time()
    mcp_url = PLAYWRIGHT_HEADED_URL if headed else PLAYWRIGHT_HEADLESS_URL
    if on_progress:
        mode = "headed Chrome" if headed else "headless Chrome"
        on_progress(f"🌐 **Browser Agent (Playwright MCP)** starting ({mode}, max {max_steps} steps)\n")

    try:
        mcp = _PlaywrightMCP(mcp_url)
        specs = [t for t in mcp.tools() if t.get("name") not in PLAYWRIGHT_BLOCK_TOOLS]
    except Exception as e:
        return {
            "success": False,
            "status": "failed",
            "terminal_status": "mcp_unreachable",
            "steps": 0,
            "model_calls": 0,
            "answer": f"Playwright MCP server ({mcp_url}) could not be reached: {e}",
            "url": "",
            "pages_shrunk_kb": 0,
            "seconds": round(time.time() - t0, 1),
        }

    tools = [
        {
            "type": "function",
            "function": {
                "name": "playwright_" + t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
            },
        }
        for t in specs
    ]

    if isinstance(messages, str):
        msgs = [{"role": "user", "content": messages}]
    else:
        msgs = [
            {"role": m.get("role"), "content": m.get("content")}
            for m in (messages or [])
            if m.get("role") in ("user", "assistant")
        ]
    if system_prompt:
        msgs = [{"role": "system", "content": system_prompt}] + msgs

    pages_shrunk_bytes = 0
    model_calls = 0
    last_url = ""
    last_req_file = Path(__file__).with_name("last_request_screen.json")

    def _shrink_in_place(step_msgs):
        nonlocal pages_shrunk_bytes
        if not page_shrink_fn:
            return
        tool_idx = [i for i, m in enumerate(step_msgs) if m.get("role") == "tool"]
        for i in tool_idx:
            limit = page_limit_last if i == tool_idx[-1] else page_limit_older
            c = step_msgs[i].get("content")
            if isinstance(c, str) and len(c) > limit // 2:
                new = page_shrink_fn(c, limit)
                if len(new) < len(c):
                    pages_shrunk_bytes += len(c) - len(new)
                    step_msgs[i]["content"] = new

    def _stream_llm_step(step_msgs, step_tools=None, reasoning_effort=None):
        """Stream one `/v1/chat/completions` turn from LM Studio, polling `should_stop` and `timeout_s` every 0.25 s."""
        nonlocal model_calls
        _shrink_in_place(step_msgs)
        req_body = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},   # token counts for token_ledger (2026-11-10)
            "messages": step_msgs,
            "max_tokens": SCREEN_MIN_MAX_TOKENS,
        }
        if step_tools:
            req_body["tools"] = step_tools
        if reasoning_effort:
            req_body["reasoning_effort"] = reasoning_effort
        try:
            last_req_file.write_text(json.dumps(req_body, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        model_calls += 1
        conn_holder = {"conn": None}
        q = queue.Queue()

        def _worker():
            conn = http.client.HTTPConnection(lmstudio_host, lmstudio_port, timeout=120)
            conn_holder["conn"] = conn
            try:
                conn.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps(req_body).encode("utf-8"),
                    {"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                if resp.status != 200:
                    body_err = resp.read().decode("utf-8", errors="replace")[:400]
                    q.put(("error", f"LM Studio HTTP {resp.status}: {body_err}"))
                    return
                while True:
                    raw = resp.fp.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        q.put(("chunk", json.loads(payload)))
                    except Exception:
                        pass
            except Exception as e:
                q.put(("error", f"{e.__class__.__name__}: {e}"))
            finally:
                _close_http_conn(conn)
                q.put(("done", None))

        threading.Thread(target=_worker, daemon=True, name="playwright-llm-step").start()

        step_thought = ""
        step_content = ""
        step_usage = None
        step_t0 = time.time()
        tc_by_idx = {}
        finish_reason = None
        looped = False
        cancelled = False
        timed_out = False
        err_msg = None

        try:
            while True:
                if time.time() - t0 >= timeout_s:
                    timed_out = True
                    _close_http_conn(conn_holder["conn"])
                    break
                if should_stop and should_stop():
                    cancelled = True
                    _close_http_conn(conn_holder["conn"])
                    break
                try:
                    kind, val = q.get(timeout=0.25)
                except queue.Empty:
                    continue
                if kind == "done":
                    break
                if kind == "error":
                    err_msg = val
                    break
                if val.get("usage"):
                    step_usage = val["usage"]
                choice = (val.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                rc = delta.get("reasoning_content")
                if rc:
                    step_thought += rc
                    if on_progress:
                        on_progress(rc)
                    if _step_thought_loops(step_thought):
                        looped = True
                        _close_http_conn(conn_holder["conn"])
                        break
                cc = delta.get("content")
                if cc:
                    step_content += cc
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    cur = tc_by_idx.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        cur["function"]["arguments"] += fn["arguments"]
        except Exception:
            _close_http_conn(conn_holder["conn"])
            raise
        finally:
            _count_step_tokens(model, step_usage, step_msgs, step_thought, step_content, tc_by_idx, step_t0)

        tool_calls = [tc_by_idx[k] for k in sorted(tc_by_idx)]
        for idx, c in enumerate(tool_calls):
            if not c.get("id"):
                c["id"] = f"call_{model_calls}_{idx}"
        return {
            "content": step_content,
            "tool_calls": tool_calls,
            "thought": step_thought,
            "finish_reason": finish_reason,
            "looped": looped,
            "cancelled": cancelled,
            "timed_out": timed_out,
            "error": err_msg,
        }

    steps_done = 0
    for step in range(1, max_steps + 1):
        if time.time() - t0 >= timeout_s:
            return {
                "success": False,
                "status": "failed",
                "terminal_status": "timeout",
                "steps": steps_done,
                "model_calls": model_calls,
                "answer": f"Browser agent timed out after {timeout_s} s.",
                "url": last_url,
                "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                "seconds": round(time.time() - t0, 1),
            }
        if should_stop and should_stop():
            return {
                "success": False,
                "status": "cancelled",
                "terminal_status": "cancelled",
                "steps": steps_done,
                "model_calls": model_calls,
                "answer": "Browser agent stopped (client disconnected).",
                "url": last_url,
                "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                "seconds": round(time.time() - t0, 1),
            }

        steps_done = step
        if on_progress:
            on_progress(f"\n**Step {step}**\n")

        sres = _stream_llm_step(msgs, step_tools=tools)
        if sres["cancelled"]:
            return {
                "success": False,
                "status": "cancelled",
                "terminal_status": "cancelled",
                "steps": steps_done,
                "model_calls": model_calls,
                "answer": "Browser agent stopped (client disconnected).",
                "url": last_url,
                "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                "seconds": round(time.time() - t0, 1),
            }
        if sres["timed_out"]:
            return {
                "success": False,
                "status": "failed",
                "terminal_status": "timeout",
                "steps": steps_done,
                "model_calls": model_calls,
                "answer": f"Browser agent timed out after {timeout_s} s.",
                "url": last_url,
                "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                "seconds": round(time.time() - t0, 1),
            }

        calls = sres["tool_calls"]
        content = (sres["content"] or "").strip()

        # Recovery when <think> exhausted max_tokens or looped without emitting a tool call or answer
        if not calls and not content:
            if on_progress:
                on_progress(f"\n⚠️ *Thinking reached limit on step {step}; requesting direct action/answer...*\n")
            recovery_msgs = msgs + [
                {
                    "role": "user",
                    "content": (
                        "You ran out of tokens while thinking without calling a tool or writing an answer. "
                        "Based on the page snapshots above, either call the next browser tool needed or write "
                        "the final answer concisely now."
                    ),
                }
            ]
            sres = _stream_llm_step(recovery_msgs, step_tools=tools, reasoning_effort="none")
            if sres["cancelled"]:
                return {
                    "success": False,
                    "status": "cancelled",
                    "terminal_status": "cancelled",
                    "steps": steps_done,
                    "model_calls": model_calls,
                    "answer": "Browser agent stopped (client disconnected).",
                    "url": last_url,
                    "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                    "seconds": round(time.time() - t0, 1),
                }
            if sres["timed_out"]:
                return {
                    "success": False,
                    "status": "failed",
                    "terminal_status": "timeout",
                    "steps": steps_done,
                    "model_calls": model_calls,
                    "answer": f"Browser agent timed out after {timeout_s} s.",
                    "url": last_url,
                    "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                    "seconds": round(time.time() - t0, 1),
                }
            calls = sres["tool_calls"]
            content = (sres["content"] or "").strip()

        if not calls:
            return {
                "success": bool(content),
                "status": "completed" if content else "incomplete",
                "terminal_status": "completed" if content else "empty_answer",
                "steps": steps_done,
                "model_calls": model_calls,
                "answer": content or "Browser agent finished without a text answer.",
                "url": last_url,
                "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                "seconds": round(time.time() - t0, 1),
            }

        if content and on_progress:
            on_progress(f"\n{content}\n")
        msgs.append({"role": "assistant", "content": content, "tool_calls": calls})

        images = []
        for c in calls:
            if should_stop and should_stop():
                return {
                    "success": False,
                    "status": "cancelled",
                    "terminal_status": "cancelled",
                    "steps": steps_done,
                    "model_calls": model_calls,
                    "answer": "Browser agent stopped (client disconnected).",
                    "url": last_url,
                    "pages_shrunk_kb": pages_shrunk_bytes // 1024,
                    "seconds": round(time.time() - t0, 1),
                }
            raw_name = (c.get("function") or {}).get("name") or ""
            name = raw_name.removeprefix("playwright_")
            raw_args = (c.get("function") or {}).get("arguments") or "{}"
            if on_progress:
                on_progress(f"🔧 `{name}({raw_args[:140]})`\n")
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                res = mcp.tool(name, **args)
                text = "\n".join(p.get("text", "") for p in (res.get("content") or []) if p.get("type") == "text")
                images += [p for p in (res.get("content") or []) if p.get("type") == "image"]
                m_url = re.search(r"-\s*Page URL:\s*(\S+)", text)
                if m_url:
                    last_url = m_url.group(1).strip()
                elif name == "browser_navigate" and args.get("url"):
                    last_url = str(args["url"])
            except Exception as e:
                text = f"error: {e}"
            msgs.append({"role": "tool", "tool_call_id": c.get("id", "x"), "content": text})

        if images:
            msgs.append(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Screenshot from the tool:"}]
                    + [
                        {"type": "image_url", "image_url": {"url": f"data:{i['mimeType']};base64,{i['data']}"}}
                        for i in images
                    ],
                }
            )

    # Reached max_steps without stopping: ask for a final summary from the pages already read
    if on_progress:
        on_progress(f"\n*Reached max steps ({max_steps}); summarizing findings...*\n")
    final_msgs = msgs + [
        {
            "role": "user",
            "content": (
                "You have reached the maximum number of browsing steps. Write the best complete answer you can "
                "from the pages you have already read, including the URL."
            ),
        }
    ]
    sres = _stream_llm_step(final_msgs, step_tools=None, reasoning_effort="none")
    content = (sres["content"] or "").strip()
    return {
        "success": bool(content),
        "status": "max_steps_reached" if content else "incomplete",
        "terminal_status": "max_steps_reached",
        "steps": steps_done,
        "model_calls": model_calls,
        "answer": content or f"Browser agent reached {max_steps} steps without finishing.",
        "url": last_url,
        "pages_shrunk_kb": pages_shrunk_bytes // 1024,
        "seconds": round(time.time() - t0, 1),
    }


# ---------------------------------------------------------------- MCP server (127.0.0.1:8933/mcp)
MCP_TOOLS = [
    {
        "name": "desktop_use",
        "description": (
            "Run the NorthCUA desktop agent on the Windows desktop (seeing the screen and using mouse "
            "clicks, keyboard shortcuts, typing, and scrolling) to complete a task on the PC."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full instructions for what to do or check on the Windows desktop.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "northcua_browse",
        "description": (
            "Run the official NorthCUA web agent harness in a visible Chrome browser window to complete a "
            "multi-step browsing task (navigating, typing, clicking, reading pages) and return the final answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full instructions for what to find or do on the web.",
                },
                "url": {
                    "type": "string",
                    "description": "Optional starting URL (e.g. https://www.metoffice.gov.uk). Leave empty to auto-select.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "northcua_browse_headless",
        "description": (
            "Run the official NorthCUA web agent harness in a headless Chrome browser (no visible window) "
            "to complete a multi-step browsing task and return the final answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Full instructions for what to find or do on the web.",
                },
                "url": {
                    "type": "string",
                    "description": "Optional starting URL (e.g. https://en.wikipedia.org). Leave empty to auto-select.",
                },
            },
            "required": ["task"],
        },
    },
]


class _MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Mcp-Session-Id", "northcua-mcp")
        self.end_headers()
        self.wfile.write(data)

    def _refused(self):
        # a web page could otherwise start desktop_use / northcua_browse here (security review 2026-11-08)
        reason = local_guard.refuse_reason(self.headers)
        if reason:
            self.close_connection = True
            self._send_json(403, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": f"forbidden: {reason}"}})
        return bool(reason)

    def do_GET(self):
        if self._refused():
            return
        if self.path.rstrip("/") in ("", "/health", "/mcp"):
            return self._send_json(200, {"status": "ok", "service": "northcua-harness-mcp", "available": available()})
        self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        if self._refused():
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self._refused():
            return
        if not self.path.startswith("/mcp"):
            return self._send_json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})

        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method.startswith("notifications/") or req_id is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Mcp-Session-Id", "northcua-mcp")
            self.end_headers()
            return

        if method == "initialize":
            return self._send_json(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "northcua-harness", "version": "0.2.0"},
                },
            })

        if method == "ping":
            return self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})

        if method == "tools/list":
            return self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}})

        if method == "tools/call":
            name = (params.get("name") or "").split("__")[-1]
            args = params.get("arguments") or {}
            if name not in (HARNESS_TOOLS | DESKTOP_TOOLS):
                return self._send_json(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True},
                })
            task = str(args.get("task") or args.get("query") or "").strip()
            if not task:
                return self._send_json(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": "Parameter 'task' is required."}], "isError": True},
                })
            if name in DESKTOP_TOOLS:
                res = run_desktop_task(
                    task,
                    should_stop=lambda: socket_disconnected(self.connection),
                )
                summary = (
                    f"{res['answer']}\n\n"
                    f"[NorthCUA Desktop: {res['status']} in {res['steps']} steps, {res['seconds']}s]"
                )
                return self._send_json(200, {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": summary}],
                        "isError": not res["success"],
                    },
                })
            url = str(args.get("url") or "").strip()
            headed = name != "northcua_browse_headless"
            res = run_web_task(
                task,
                url=url,
                headed=headed,
                should_stop=lambda: socket_disconnected(self.connection),
            )
            summary = (
                f"{res['answer']}\n\n"
                f"[NorthCUA Harness: {res['status']} in {res['steps']} steps, {res['seconds']}s, start URL: {res['url']}]"
            )
            return self._send_json(200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": summary}],
                    "isError": not res["success"],
                },
            })

        return self._send_json(200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        })


def start_server():
    srv = ThreadingHTTPServer((MCP_HOST, MCP_PORT), _MCPHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="northcua-mcp").start()
    return srv


if __name__ == "__main__":
    print(f"NorthCUA MCP server listening on http://{MCP_HOST}:{MCP_PORT}/mcp")
    ThreadingHTTPServer((MCP_HOST, MCP_PORT), _MCPHandler).serve_forever()
