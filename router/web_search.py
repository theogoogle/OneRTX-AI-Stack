"""
Live web information for the router. Standard library only.

  - needs_web(): the CPU labeller (LFM3) decides whether a message needs live information and writes the search
    query (or names the place for a weather forecast).
  - search(): Tavily (https://tavily.com, free plan: 1,000 credits a month; "advanced" depth = 2 credits).
    Key: %USERPROFILE%\\AI\\secrets\\tavily_api_key.txt
  - weather(): Open-Meteo (free, no key): exact daily forecast instead of search snippets.
  - context_block(): the text the router adds to the system prompt, with numbered sources.
"""
import concurrent.futures
import copy
import html
import ipaddress
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import rich_ui  # noqa: E402

KEY_FILE = Path.home() / "AI" / "secrets" / "tavily_api_key.txt"
USAGE_FILE = Path(__file__).resolve().parent / "web_usage.json"
DEFAULT_PLACE = "London"          # used when a weather question names no place
COUNTRY = "united kingdom"         # Tavily boosts (does not restrict) results from this country
SEARCH_DEPTH = "advanced"          # quality first: best chunks per page, 2 credits
MAX_RESULTS = 6
TIMEOUT_S = 25
UA = "local-router/1.0"

WEB_SYSTEM = """You decide whether a chat message needs a live web lookup. You never answer the message.
Today is {today}. The assistant that will answer has no internet and its knowledge stops in 2025.
web = true when a good answer depends on recent, changing, or post-2025 facts about any topic, person, company,
organisation, market, event, place, product, sport, or ongoing situation:
- weather, news, events, concerts, shows, exhibitions, things to do on specific dates, sports results or fixtures,
- prices, exchange rates, opening times, tickets or availability, travel disruption, who currently holds a role,
- specific businesses, venues, or a product's current details, reviews, specs, or availability,
- AI models, open-weight/local LLMs, software versions, hardware/GPUs, cars, or tech products — including comparisons
  ("what does X equate to", "how does X compare to Y"), benchmarks, rankings, releases/versions, or recommendations
  ("what is the best/top <model/product> for <use case/hardware>"), even without a year or "latest", because ecosystems change rapidly after 2025,
- anything "today", "last 24h", "past 24 hours", "past few days", "this week", "this weekend", "latest", "recent",
  "current", "now" or after 2025, or when the user asks (even politely) for recent updates or to check online.
web = false for timeless general knowledge, history, science or ML/CS theory concepts (e.g. how transformers,
quantization, gradient descent, overfitting, or network protocols work), timeless personal advice or ideas, recipes,
writing or rewording, maths, debugging/writing code, translation, or casual chat.
query: when web is true, a short search-engine query in the user's language with places and dates written out
(e.g. "concerts The O2 London 7-8 November 2026"); otherwise "". Write it for what the user (a member of the
public, in the UK unless they say otherwise) wants to know, using the conversation for context: aim at official
public guidance and consumer pages, not professional, clinical or trade notices (e.g. "are there train strikes
I should know about?" -> "rail strike dates passengers UK November 2026").
weather: true only when the message asks for a weather forecast or current weather.
place: for weather, the place named in the message, or "" if none."""
WEB_SCHEMA = {"type": "json_schema", "json_schema": {"name": "web", "strict": True, "schema": {
    "type": "object", "additionalProperties": False, "required": ["web", "query", "weather", "place"],
    "properties": {"web": {"type": "boolean"}, "query": {"type": "string"}, "weather": {"type": "boolean"},
                   "place": {"type": "string"}}}}}

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}

_lock = threading.Lock()
_cache = {}            # (conversation key, message hash) -> (time, result) so tool-call rounds do not search again
CACHE_S = 900


def today_text():
    return datetime.now().strftime("%A %d %B %Y")


def has_key():
    return KEY_FILE.exists() and KEY_FILE.read_text(encoding="utf-8").strip() != ""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _http_json(url, body=None, headers=None, timeout=TIMEOUT_S):
    h = {"User-Agent": UA, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data, h)
    with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- usage counter (credits this month)
def _add_usage(credits):
    with _lock:
        month = datetime.now().strftime("%Y-%m")
        try:
            u = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            u = {}
        m = u.setdefault(month, {"searches": 0, "credits": 0})
        m["searches"] += 1
        m["credits"] += credits
        USAGE_FILE.write_text(json.dumps(u, indent=1), encoding="utf-8")


def usage_this_month():
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8")).get(datetime.now().strftime("%Y-%m"),
                                                                        {"searches": 0, "credits": 0})
    except Exception:
        return {"searches": 0, "credits": 0}


# ---------------------------------------------------------------- decision (LFM3 on the CPU)
# Backstop for the small labeller (web_label_test.py: its misses were all "no search" on live questions):
# a question with a clear live-information cue searches even when the labeller says no.
LIVE_WORDS = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|this (morning|afternoon|evening|week|weekend|month)|next (week|weekend)|"
    r"(?:last|past|derni[èe]res?|derniers?)\s+(?:\d+\s*)?(?:h|hrs?|hours?|heures?|days?|jours?|weeks?|semaines?|months?|mois|24h|48h|72h)|"
    r"24h|48h|72h|right now|at the moment|latest|recent(ly)?|breaking|current(ly)?|news|actualit[ée]s?|updates?\s+on|"
    r"weather|forecast|scores?|fixtures?|"
    r"who won (?:the )?(?:match|game|race|fight|cup|final|election|title|series|grand prix|tonight|yesterday|today)|"
    r"exchange rate|opening (times|hours)|open (on|until|today|now)|close[sd]? (on|at|today)|closing time|"
    r"(line|trains?|tube|buses) (running|delayed)|delays?|strikes?|what'?s on|tickets?|prices?|"
    r"aujourd'?hui|hier|demain|en ce moment)\b", re.I)
STRONG_RECENCY_RE = re.compile(
    r"\b((?:last|past|derni[èe]res?|derniers?)\s+(?:\d+\s*)?(?:h|hrs?|hours?|heures?|days?|jours?|weeks?|semaines?|24h|48h|72h)|"
    r"24h|48h|72h|latest\s+news|breaking\s+news|actualit[ée]s?)\b",
    re.I,
)
QUESTION = re.compile(
    r"\?\s*$|^\s*(?:please\s+)?(who|what|what'?s|when|where|which|is|are|was|were|will|can|could|do|does|did|should|any|"
    r"how (much|many|is|are)|i(?:'d|\s+would)\s+like|i\s+want|give\s+me|tell\s+me|show\s+me|get\s+me|send\s+me|"
    r"find(?:\s+me)?|share|summari[sz]e|update\s+me|je\s+voudrais|j'aimerais|donne[- ]moi|dis[- ]moi|montre[- ]moi)\b",
    re.I,
)
WEATHER_WORDS = re.compile(r"\b(weather|forecast|rain(ing|y)?|snow(ing)?|sunny|cold|hot|warm|windy|temperature|umbrella)\b",
                           re.I)
PLACE_AFTER_IN = re.compile(r"\b(?:in|for|at) ((?:[A-Z][\w'-]+)(?: [A-Z][\w'-]+)*)")
NEWS_INTENT_RE = re.compile(
    r"\b(news|headlines?|breaking|actualit[ée]s?|derni[èe]res?\s+nouvelles?|nouveaut[ée]s?|lancements?|"
    r"latest\s+(?:ai|ia|tech|models?|launches?|releases?|updates?|developments?)|this\s+week\s+in)\b",
    re.I,
)
TIME_DAY_RE = re.compile(
    r"\b(today|tonight|yesterday|last\s+24\s*h(?:ours?)?|past\s+24\s*h(?:ours?)?|24h|breaking|"
    r"aujourd'?hui|hier|derni[èe]res?\s+24\s*h(?:eures?)?)\b",
    re.I,
)
TIME_WEEK_RE = re.compile(
    r"\b(this\s+week|past\s+week|last\s+week|past\s+(?:few|\d+)\s+days?|last\s+(?:few|\d+)\s+days?|"
    r"48h|72h|latest|recent(?:ly)?|cette\s+semaine|actualit[ée]s?|derni[èe]res?\s+nouvelles?|"
    r"ces\s+derniers?\s+jours?|nouveaut[ée]s?)\b",
    re.I,
)
TIME_MONTH_RE = re.compile(r"\b(this\s+month|past\s+month|last\s+month|ce\s+mois|dernier\s+mois)\b", re.I)


def infer_search_options(user_message: str = "", query: str = "") -> dict:
    """Infer Tavily `topic` ('news'|'general') and `time_range` ('day'|'week'|'month'|None) from user intent."""
    combined = f"{user_message or ''} {query or ''}"
    topic = "news" if NEWS_INTENT_RE.search(combined) else "general"
    if TIME_DAY_RE.search(combined):
        time_range = "day"
    elif TIME_WEEK_RE.search(combined):
        time_range = "week"
    elif TIME_MONTH_RE.search(combined):
        time_range = "month"
    else:
        time_range = None
    return {"topic": topic, "time_range": time_range}


def _format_pub_date(raw) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    try:
        return parsedate_to_datetime(s).strftime("%Y-%m-%d")
    except Exception:
        return s[:22]


def decide(classify_call, text, previous_reply="", force=False):
    """LFM decision plus the backstops. force: the user switched on Web Search for this chat."""
    d = needs_web(classify_call, text, previous_reply)
    stripped = text.strip()
    if not d.get("web") and (
        force
        or STRONG_RECENCY_RE.search(stripped)
        or (LIVE_WORDS.search(stripped) and QUESTION.search(stripped))
    ):
        d["web"], d["backstop"] = True, not force
    if d.get("web") and not d.get("weather") and WEATHER_WORDS.search(text):
        place = d.get("place") or (PLACE_AFTER_IN.search(text) or [None, None])[1]
        if place or "weather" in text.lower() or "forecast" in text.lower():
            d["weather"], d["place"] = True, place or ""   # exact forecast instead of search snippets
    if d.get("web") and not d.get("weather") and not d.get("query"):
        d["query"] = text.strip()[:300]
    return d


def needs_web(classify_call, text, previous_reply=""):
    """classify_call(body) -> parsed JSON content. Returns {"web", "query", "weather", "place"}."""
    msg = text[:2000]
    if previous_reply:   # the user's earlier question(s): the topic the new message builds on
        msg = f"EARLIER IN THIS CHAT (context only): {previous_reply[:400]}\n\nMESSAGE:\n{msg}"
    body = {"temperature": 0, "max_tokens": 120, "reasoning_effort": "none", "response_format": WEB_SCHEMA,
            "messages": [{"role": "system", "content": WEB_SYSTEM.format(today=today_text())},
                         {"role": "user", "content": "Decide for the message below. Do not answer it.\n\n<<<\n"
                                                     + msg + "\n>>>"}]}
    return classify_call(body)


# ---------------------------------------------------------------- sources & verified images
IMAGE_PROBE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
JUNK_IMAGE_URL_RE = re.compile(
    r"(\.(?:svg|ico|gif)(?:[?#]|$)|/(?:logo|logos|favicon|avatar|avatars|icon|icons|sprite|sprites|"
    r"pixel|tracker|1x1|placeholder|default[-_]share|default[-_]og|site[-_]logo|badge|badges|banner[-_]ad|ads?)/|"
    r"/(?:api/)?og(?:[/?#]|$)|[?&]type=generic\b|opengraph\.githubassets\.com|"
    r"\b(?:1x1|spacer|blank|favicon|apple-touch-icon|yt_1200|logo[-_](?:dark|light|white|black|sm|lg))\b)",
    re.I,
)
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})\b",
    re.I,
)
BARE_FACT_QUERY_RE = re.compile(
    r"^\s*(?:what(?:'s| is)|combien|quel(?:le)? est|is|are)?\s*(?:the\s+|le\s+|la\s+)?"
    r"(?:current\s+)?(?:[a-z]{3}\s*(?:to|/|-|in)\s*[a-z]{3}|exchange rate|taux de change|"
    r"time in\b|heure [àa]\b|phone number|num[ée]ro de t[ée]l[ée]phone|"
    r"(?:what|quel(?:le)?) time does .+ (?:open|close)|opening (?:hours|times) (?:of|for)|"
    r"is the .+ line (?:running|open|delayed))\b",
    re.I,
)
OG_IMAGE_META_RE = re.compile(
    r'<meta\b[^>]+?(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]*?>',
    re.I | re.S,
)
META_CONTENT_RE = re.compile(r'\bcontent\s*=\s*["\']([^"\']+)["\']', re.I)


def is_visual_query(text_or_query: str) -> bool:
    """General check (not hardcoded to news): any substantive live web topic benefits from photos except bare
    single-value lookups (exchange rates, time zones, phone numbers, closing hours, tube line status)."""
    s = (text_or_query or "").strip()
    if not s or BARE_FACT_QUERY_RE.search(s):
        return False
    return True


def _fetch_article_og_image(result_item: dict, idx: int, timeout: float = 2.0) -> dict | None:
    page_url = str((result_item or {}).get("url") or "").strip()
    title = re.sub(r"\s+", " ", str((result_item or {}).get("title") or "").strip())[:150]
    desc = f"Source [{idx}] — {title}" if title else f"Article [{idx}] hero photo"
    yt_m = YOUTUBE_ID_RE.search(page_url)
    if yt_m:
        return {"url": f"https://i.ytimg.com/vi/{yt_m.group(1)}/hqdefault.jpg", "description": desc}
    if not _is_public_https_url(page_url):
        return None
    hdrs = {
        "User-Agent": IMAGE_PROBE_UA,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Range": "bytes=0-32767",
    }
    final_url = page_url
    try:
        with safe_open_public_https(page_url, headers=hdrs, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return None
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "xml" not in ctype:
                return None
            if hasattr(resp, "geturl"):
                final_url = resp.geturl() or page_url
            head_html = resp.read(32768).decode("utf-8", "ignore")
    except Exception:
        return None
    for tag_m in OG_IMAGE_META_RE.finditer(head_html):
        cm = META_CONTENT_RE.search(tag_m.group(0))
        if not cm:
            continue
        raw_img = html.unescape(cm.group(1).strip())
        full_img = _normalize_md_image_url(urllib.parse.urljoin(final_url, raw_img))
        if full_img.startswith("https://") and not JUNK_IMAGE_URL_RE.search(full_img):
            return {"url": full_img, "description": desc}
    return None


def extract_article_og_images(results: list[dict], max_pages: int = 5, timeout: float = 2.0) -> list[dict]:
    """Pull `og:image` / `twitter:image` hero photos from the cited articles in parallel (0 Tavily credits)."""
    targets = [(i, r) for i, r in enumerate((results or [])[:max_pages], 1) if (r or {}).get("url")]
    if not targets:
        return []
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(targets))) as pool:
        futs = [pool.submit(_fetch_article_og_image, r, i, timeout) for i, r in targets]
        for fut in futs:
            try:
                item = fut.result(timeout=timeout + 0.6)
            except Exception:
                item = None
            if item:
                found.append(item)
    return found


def _is_public_ip(ip_obj) -> bool:
    """True only for globally routable public IPs (unwrapping IPv4-mapped IPv6 addresses)."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    return bool(
        ip_obj.is_global
        and not (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )
    )


def _is_public_https_url(url: str) -> bool:
    """SSRF guard: only allow standard-port (:443) public HTTPS hosts (no userinfo, loopback, LAN, link-local, or tailnet)."""
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 1500:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".ts.net", ".home.arpa")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return _is_public_ip(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            if not _is_public_ip(ipaddress.ip_address(addr)):
                return False
        except ValueError:
            return False
    return True


def safe_open_public_https(
    url: str,
    headers: dict | None = None,
    timeout: float = 8.0,
    max_redirects: int = 3,
):
    """Open a public HTTPS URL while revalidating EVERY redirect hop against `_is_public_https_url`."""
    cur_url = str(url or "").strip()
    seen: set[str] = set()
    for _ in range(max_redirects + 1):
        if cur_url in seen or not _is_public_https_url(cur_url):
            raise urllib.error.URLError(f"Blocked non-public or cyclic URL: {cur_url[:120]}")
        seen.add(cur_url)
        req = urllib.request.Request(cur_url, headers=dict(headers or {}))
        try:
            return _NO_REDIRECT_OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = (e.headers.get("Location") or "").strip()
                try:
                    e.close()
                except Exception:
                    pass
                if not loc:
                    raise
                next_url = urllib.parse.urljoin(cur_url, loc)
                if not _is_public_https_url(next_url):
                    raise urllib.error.URLError(f"Blocked unsafe redirect target: {next_url[:120]}")
                cur_url = next_url
                continue
            raise
    raise urllib.error.URLError("Too many redirects")


def _normalize_md_image_url(url: str) -> str:
    """Percent-encode literal spaces and parentheses so Markdown `![caption](url)` never splits mid-URL."""
    return (url or "").strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _probe_single_image(item: dict, timeout: float = 2.5) -> dict | None:
    url = _normalize_md_image_url(item["url"])
    item = {**item, "url": url}
    if not _is_public_https_url(url):
        return None
    # Match a real browser cross-origin `<img src="...">` GET (no `Range` header + external `Referer`) so CDNs/WAFs
    # that hotlink-block or return 403 on standard image loads are rejected here instead of failing in the browser.
    hdrs = {
        "User-Agent": IMAGE_PROBE_UA,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "http://127.0.0.1:8282/",
    }
    try:
        with safe_open_public_https(url, headers=hdrs, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return None
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/") or ctype in ("image/svg+xml", "image/gif", "image/x-icon"):
                return None
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) < 4000:
                return None
            head_bytes = resp.read(4096)
            if len(head_bytes) < 2500 and (not cl or not cl.isdigit()):
                return None
            return item
    except Exception:
        return None


def verify_images(raw_images, max_keep: int = 5, timeout: float = 2.5) -> list[dict]:
    """Filter & verify candidate image URLs from Tavily + article og:image tags in parallel."""
    candidates = []
    seen = set()
    for raw in raw_images or []:
        if isinstance(raw, dict):
            url = _normalize_md_image_url(html.unescape(str(raw.get("url") or "").strip()))
            desc = re.sub(r"\s+", " ", str(raw.get("description") or "").strip())[:200]
        elif isinstance(raw, str):
            url, desc = _normalize_md_image_url(html.unescape(raw.strip())), ""
        else:
            continue
        if not url.startswith("https://") or url in seen or JUNK_IMAGE_URL_RE.search(url):
            continue
        seen.add(url)
        candidates.append({"url": url, "description": desc})
        if len(candidates) >= 12:
            break
    if not candidates:
        return []
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
        futs = [pool.submit(_probe_single_image, c, timeout) for c in candidates]
        for fut in futs:
            try:
                ok_item = fut.result(timeout=timeout + 0.8)
            except Exception:
                ok_item = None
            if ok_item:
                verified.append(ok_item)
                if len(verified) >= max_keep:
                    break
    return verified


def search_rich(query, count=MAX_RESULTS, depth=SEARCH_DEPTH, topic="general", time_range=None):
    """Query Tavily with zero-extra-credit `include_images` + article `og:image` enrichment; returns `(results, verified_images)`."""
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    topic = "news" if topic == "news" else "general"
    body = {
        "query": query[:400],
        "topic": topic,
        "search_depth": depth,
        "max_results": count,
        "chunks_per_source": 3,
        "include_images": True,
        "include_image_descriptions": True,
        "include_usage": True,
    }
    if topic == "general":
        body["include_published_date"] = True
        body["country"] = COUNTRY
    if time_range in ("day", "week", "month", "year"):
        body["time_range"] = time_range
    data = _http_json("https://api.tavily.com/search", body, {"Authorization": f"Bearer {key}"})
    _add_usage((data.get("usage") or {}).get("credits", 2 if depth == "advanced" else 1))
    results = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "published": r.get("published_date"),
        }
        for r in data.get("results", [])
    ]
    # Prefer article-matched og:image hero photos first (exact [1]..[5] story match), followed by Tavily's image pool
    og_imgs = extract_article_og_images(results, max_pages=5)
    raw_imgs = list(og_imgs) + list(data.get("images") or [])
    for r in data.get("results", []):
        for r_img in r.get("images") or []:
            raw_imgs.append(r_img if isinstance(r_img, dict) else {"url": str(r_img), "description": r.get("title", "")})
    images = verify_images(raw_imgs, max_keep=5) if raw_imgs else []
    return results, images


def search(query, count=MAX_RESULTS, depth=SEARCH_DEPTH):
    results, _ = search_rich(query, count=count, depth=depth)
    return results


def weather_rich(place):
    place = (place or DEFAULT_PLACE).strip()
    q = urllib.parse.urlencode({"name": place.split(",")[0], "count": 1, "language": "en", "format": "json"})
    geo = _http_json(f"https://geocoding-api.open-meteo.com/v1/search?{q}").get("results") or []
    if not geo:
        raise LookupError(f"place not found: {place}")
    g = geo[0]
    q = urllib.parse.urlencode({
        "latitude": g["latitude"], "longitude": g["longitude"], "timezone": "auto", "forecast_days": 7,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "precipitation_sum,wind_speed_10m_max,sunrise,sunset"})
    f = _http_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    name = ", ".join(x for x in (g.get("name"), g.get("admin1"), g.get("country")) if x)
    c, d = f.get("current", {}), f.get("daily", {})
    now_hm = f"{datetime.now():%H:%M}"
    lines = [f"Weather for {name} (Open-Meteo forecast, retrieved {now_hm}, local time zone "
             f"{f.get('timezone')}).",
             f"Now: {c.get('temperature_2m')}°C (feels {c.get('apparent_temperature')}°C), "
             f"{WMO.get(c.get('weather_code'), '?')}, wind {c.get('wind_speed_10m')} km/h."]
    daily_meta = []
    for i, day in enumerate(d.get("time", [])):
        dt_obj = datetime.strptime(day, "%Y-%m-%d")
        dt = dt_obj.strftime("%a %d %b")
        wcode = d["weather_code"][i]
        desc = WMO.get(wcode, "?")
        tmin = d["temperature_2m_min"][i]
        tmax = d["temperature_2m_max"][i]
        rprob = d["precipitation_probability_max"][i]
        rsum = d["precipitation_sum"][i]
        wmax = d["wind_speed_10m_max"][i]
        srise = d["sunrise"][i][-5:]
        sset = d["sunset"][i][-5:]
        lines.append(f"{dt}: {desc}, {tmin}–{tmax}°C, rain chance {rprob}% ({rsum} mm), wind up to {wmax} km/h, "
                     f"sunrise {srise}, sunset {sset}")
        daily_meta.append({
            "date_label": dt,
            "day_short": dt_obj.strftime("%a"),
            "code": wcode,
            "desc": desc,
            "t_min": tmin,
            "t_max": tmax,
            "rain_prob": rprob,
            "precip_mm": rsum,
            "wind_max": wmax,
            "sunrise": srise,
            "sunset": sset,
        })
    url = f"https://open-meteo.com/en/docs#latitude={g['latitude']}&longitude={g['longitude']}"
    card_meta = {
        "place": name,
        "timezone": f.get("timezone") or "",
        "retrieved_at": now_hm,
        "url": url,
        "current": {
            "temp": c.get("temperature_2m"),
            "feels_like": c.get("apparent_temperature"),
            "code": c.get("weather_code"),
            "desc": WMO.get(c.get("weather_code"), ""),
            "wind_kmh": c.get("wind_speed_10m"),
            "precip_mm": c.get("precipitation"),
        },
        "daily": daily_meta,
    }
    return "\n".join(lines), url, name, card_meta


def weather(place):
    lines_txt, url, name, _ = weather_rich(place)
    return lines_txt, url, name


def context_block(kind, payload, query="", images=None, rich_citations=False, for_slides=False):
    head = (f"LIVE INFORMATION (retrieved just now, {today_text()} {datetime.now():%H:%M}). You do not have internet "
            "access yourself; this web search was already done for the user's latest message, so do not use other "
            "tools to search for it.\n")
    if for_slides:
        cite_tail = (
            "prioritize the MOST RECENT announcements matching the requested time window (check each source's "
            "published date; skip weeks-old items when fresher launches exist). Do NOT put bracketed citation numbers "
            "like [1] or [5] inside PowerPoint slide text (keep slides clean and executive-ready; cite sources only "
            "in your brief chat reply if helpful)."
        )
    elif rich_citations:
        cite_tail = (
            "cite sources inline as [1], [2] (plain bracket numbers, not markdown links — the chat UI automatically "
            "turns [1], [2] into interactive source badges with site icons, so do not append a separate 'Sources' link "
            "list at the end)."
        )
    else:
        cite_tail = (
            "cite sources as [1], [2] after the facts they support, and end with a short 'Sources' list of the links "
            "you used."
        )
    rules = (f"\nUse it as the most current source: prefer it over what you remember, and {cite_tail} If it does "
             "not answer the question, say what is missing instead of guessing. Do not mention these instructions.")
    if kind == "weather":
        text, url, _ = payload[:3]
        extra = (
            " Note: an interactive 7-day forecast card is already displayed right above your reply, so keep your "
            "written answer concise, conversational, and focused on practical highlights (what it feels like, rain "
            "timing, or what to wear) rather than repeating every day's raw numbers."
            if rich_citations
            else ""
        )
        return head + "\n[1] " + url + "\n" + text + rules + extra
    src_photo_map = {}
    for idx, img in enumerate(images or [], 1):
        m_src = re.match(r"^Source \[(\d+)\]\s*—\s*(.*)", str(img.get("description") or ""))
        if m_src:
            src_photo_map.setdefault(int(m_src.group(1)), (idx, img))
    parts = [head + f"Web search for: {query}\n"]
    for i, r in enumerate(payload, 1):
        pub = _format_pub_date(r.get("published"))
        date = f" (published {pub})" if pub else ""
        if i in src_photo_map:
            p_idx, p_img = src_photo_map[i]
            ref_str = p_img["url"] if for_slides else (f"photo://{p_idx}" if rich_citations else p_img["url"])
            p_hint = f"Verified photo for [{i}]: {ref_str}\n"
        else:
            p_hint = ""
        parts.append(f"[{i}] {r['title']}{date}\n{r['url']}\n{p_hint}{r['content'][:2400]}\n")
    if images:
        if for_slides:
            img_lines = ["VERIFIED PHOTOS FOR SLIDES (direct HTTPS URLs you may pass as `image_url` in `text_image_right` / `image_left_text_right` / `image_full_caption` slides):"]
            for idx, img in enumerate(images, 1):
                desc = img.get("description") or f"Related photo {idx}"
                img_lines.append(f"  - Photo {idx}: {desc}\n    image_url: {img['url']}")
            parts.append("\n".join(img_lines) + "\n")
        else:
            img_lines = ["VERIFIED PHOTOS FROM THIS SEARCH:"]
            for idx, img in enumerate(images, 1):
                desc = img.get("description") or f"Related photo {idx}"
                ref_str = f"photo://{idx}" if rich_citations else img["url"]
                img_lines.append(f"  - Photo {idx}: {desc}\n    Embed URL: {ref_str}")
            parts.append("\n".join(img_lines) + "\n")
            target_n = "2 or 3" if len(images) >= 2 else "1"
            url_fmt = "`photo://1`, `photo://2`, etc." if rich_citations else "exact `https://...` URL above"
            rules += (
                f"\nVisual layout rule: Whenever verified photos are listed above and visually illustrate the subject "
                f"(any substantive topic — news, companies, people, events, sports, places, travel, science, culture, "
                f"products, etc.), embed {target_n} distinct photos spread across different sections of your answer "
                + ("(do NOT stop after just 1 photo when 2+ relevant photos are available — include 2 or 3 so the layout "
                   "feels balanced!) " if len(images) >= 2 else "")
                + f"using exact Markdown `![Short descriptive caption]({url_fmt})` on its own line right below the "
                "matching heading. Never invent image URLs, never repeat the same photo twice, and only skip photos for "
                "bare single-value lookups (such as a currency conversion, a phone number, or a closing time)."
            )
    return "\n".join(parts) + rules


def lookup(cache_key, decision, on_status=None, rich_citations=False, user_message="", for_slides=False):
    """Runs the lookup the decision asks for. Returns (system text or None, log info)."""
    full_key = f"{cache_key}|rc={int(bool(rich_citations))}|fs={int(bool(for_slides))}"
    with _lock:
        hit = _cache.get(full_key)
        if hit and time.time() - hit[0] < CACHE_S:
            info_copy = copy.deepcopy(hit[1][1])
            info_copy["cached"] = True
            ui_cached = info_copy.get("_ui") or {}
            if on_status and ui_cached.get("status_done"):
                on_status(ui_cached["status_done"])
            return hit[1][0], info_copy
    t = time.time()
    try:
        if decision.get("weather"):
            place_hint = (decision.get("place") or DEFAULT_PLACE).strip()
            if on_status:
                on_status(rich_ui.status_event(f"🌤️ Fetching 7-day forecast for {place_hint}…", done=False))
            lines_txt, url, name, card_meta = weather_rich(decision.get("place"))
            done_st = rich_ui.status_event(f"🌤️ 7-day forecast · {name}", done=True)
            if on_status:
                on_status(done_st)
            ui_meta = {
                "sources": rich_ui.source_events_for_weather(url, name, lines_txt),
                "embed_html": rich_ui.build_weather_card_html(card_meta),
                "verified_images": [],
                "visual_topic": False,
                "status_done": done_st,
            }
            out = (
                context_block("weather", (lines_txt, url, name), rich_citations=rich_citations),
                {"web": "weather", "place": name, "_ui": ui_meta},
            )
        else:
            if not has_key():
                return None, {"web": "skipped: no Tavily key"}
            q = decision.get("query") or ""
            opts = infer_search_options(user_message, q)
            topic = decision.get("topic") or opts["topic"]
            time_range = decision.get("time_range") or opts["time_range"]
            if on_status:
                on_status(
                    rich_ui.status_event(
                        f'🔍 Searching live web: "{q[:80]}"…' if q else "🔍 Searching the web…",
                        done=False,
                        action="web_search_queries_generated" if q else None,
                        queries=[q] if q else None,
                    )
                )
            results, images = search_rich(q, topic=topic, time_range=time_range)
            items = [{"link": r["url"], "title": r["title"] or r["url"]} for r in results if r.get("url")]
            done_st = rich_ui.status_event(
                f"Searched {len(items)} site{'s' if len(items) != 1 else ''}"
                + (f" · {len(images)} photo{'s' if len(images) != 1 else ''}" if images else ""),
                done=True,
                action="web_search" if items else None,
                query=q,
                items=items or None,
            )
            if on_status:
                on_status(done_st)
            ui_meta = {
                "sources": rich_ui.source_events_for_search(results),
                "embed_html": None,
                "verified_images": [] if for_slides else images,
                "visual_topic": False if for_slides else is_visual_query(f"{user_message} {q}"),
                "status_done": done_st,
            }
            out = (
                context_block("search", results, q, images=images, rich_citations=rich_citations, for_slides=for_slides),
                {
                    "web": "search",
                    "query": q,
                    "topic": topic,
                    **({"time_range": time_range} if time_range else {}),
                    "results": len(results),
                    "images": len(images),
                    "_ui": ui_meta,
                },
            )
    except Exception as e:
        if on_status:
            on_status(rich_ui.status_event("⚠️ Live lookup unavailable, answering directly", done=True))
        return None, {"web": f"failed: {e.__class__.__name__}: {e}"[:200]}
    out[1]["web_seconds"] = round(time.time() - t, 1)
    with _lock:
        _cache[full_key] = (time.time(), copy.deepcopy(out))
        for k in [k for k, v in _cache.items() if time.time() - v[0] > CACHE_S]:
            _cache.pop(k, None)
    return out


def openwebui_results(query, count):
    """For Open WebUI's 'External' search engine: [{link, title, snippet}]."""
    return [{"link": r["url"], "title": r["title"], "snippet": r["content"]}
            for r in search(query, count=max(1, min(int(count or 5), 10)))]
