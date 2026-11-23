"""
100% Local "The Family Table" daily broadsheet pipeline.

Flow:
  1. Wait for local router (127.0.0.1:4000) & network (handles scheduled BIOS RTC wake gracefully).
  2. Gather 100% live data deterministically:
       - Met Office weather + exact dates/ages (fetch_metoffice.js)
       - Google Calendar 7d + 28d, Gmail full threads + replies, Google Sheets Memory Tracker (OAuth 2.0)
       - Live kid-friendly news, local family events & membership heritage outings (Tavily via web_search.py)
       - Private standing family rules (family.md + ideas.md on Mon-Thu)
  3. Author edition JSON locally on Pro (deep = qwen3.6-35b-a3b + thinking, 256K ctx).
  4. Deterministically lock Met Office weather/dates/ages + run strict audit (schema, dates, dedupe, privacy, no [1]).
     If any issue is found -> 1 automatic surgical repair pass on Pro.
  5. Render 1-page broadsheet PDF + 2x PNG preview via build_broadsheet.js (Playwright Chromium).
  6. Run Gemma 4 Vision QA (fast = gemma-4-12b-it-qat) on the rendered PNG + check column balance (<220px spread).
     If imbalanced or Vision QA flags FIX -> 1 surgical layout-polish pass on Pro + re-render (keep only if better).
  7. Save edition JSON, HTML, PDF, PNG, email body MD; append Around Town to Memory Sheet; create/update Gmail Draft.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import google_connector as gc  # noqa: E402
import research  # noqa: E402

ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOG_FILE = _HERE / "family-table.log"

DATE_OR_SCHEDULE_RE = re.compile(
    r"(?i)\b("
    r"runs?\b|running\b|until\b|through\b|thru\b|open\s+(?:daily|sat|sun|mon|tue|wed|thu|fri|weekends?|winter|summer|spring|autumn|year[- ]round|all\s+year)|"
    r"(?:\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)|"
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2})|"
    r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s*[–-]\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)|"
    r"(?:\d{1,2}\s*[–-]\s*\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)|"
    r"(?:winter|spring|summer|autumn)\s+(?:season|hours)"
    r")",
)
CITATION_BRACKET_RE = re.compile(r"\[\d{1,2}\]")

EDITOR_SYSTEM_PROMPT = """You are the chief editor of a warm, crisp, high-signal 1-page family breakfast broadsheet newspaper in British English.
You receive 100% live source data (Met Office weather, Google Calendars, full Gmail threads including replies, Memory Sheet exclusions, live web research, and private standing family rules).

Return ONLY a single valid JSON object (no markdown fences, no commentary before or after) matching this exact schema:
{
  "file_date": "YYYY-MM-DD",
  "date_long": "Weekday D Month YYYY",
  "edition_label": "Breakfast edition" | "Weekend edition",
  "week_title": "The week ahead" | "The weekend ahead",
  "lede": "One vivid, warm 1-2 sentence broadsheet lede summarizing today's weather (with 8 am + high), headline plans, and horizon.",
  "weather": { "high": "...", "summary": "...", "at8": "...", "rain": "...", "sunrise": "...", "sunset": "...", "tomorrow": "..." },
  "week": [
    { "day": "MON", "num": "23", "text": "Concise factual events for that day; use **bold** sparingly for emphasis.", "pickup": "optional weekday pickup string (omit on weekends)" }
  ],
  "school_desk": [
    { "title": "Short crisp headline", "text": "2-3 concrete sentences synthesized from actual school/nursery emails (tests, homework, trips, teacher notes, dates)." }
  ],
  "kids": [
    { "name": "Exact child name", "tag": "Exact tag from metoffice.children", "text": "2-3 high-signal sentences for this child (tests, milestones, teacher notes, parties, countdowns). ZERO filler!" }
  ],
  "watch": [
    { "lead": "Short plain-text lead (NO ** stars):", "text": "Actionable parent checklist item: unpaid/overdue/upcoming bills (ONLY if NOT confirmed paid in thread replies!), missed forms/surveys, RSVP/cancellation deadlines, gifts to buy, schedule squeezes." }
  ],
  "school_run": null | {
    "note": "One short line summarizing morning drop-offs.",
    "rows": [ ["Mon", "3:30 (club)", "Parent A", "4:00", "Parent B"], ... ]
  },
  "coming_up_note": "Birthdays, parties and key dates, next 4 weeks.",
  "coming_up": [
    { "day": "SAT", "num": "28", "when": "In 6 days", "text": "Short high-signal event from the 28-day calendar/emails (use exact weekday and date_short from the calendar data — never guess weekdays!)." }
  ],
  "talk": [
    "Playful, curious table-talk question 1 (tied to today's kid news, school topics, or child interests).",
    "Question 2.",
    "Question 3."
  ],
  "news": [
    { "title": "Crisp upbeat headline", "text": "2 clear, engaging kid-friendly sentences strictly grounded in the live kid_news research.", "source": "Publication / institution name" }
  ],
  "town": [
    { "title": "Venue / Event name", "text": "2-3 lively sentences tailored to the children's ages. MUST state exact run dates or verified seasonal opening schedule from live research (never invent opening days; if exact winter hours weren't in search snippets, note seasonal winter schedule & free entry with membership).", "where": "Venue · Neighbourhood / Postcode (for membership property, write the exact membership name with NO angle brackets, e.g. 'Heritage Pass · Historic District')" }
  ],
  "next_edition": "TomorrowWeekday, 8:00 am"
}

NON-NEGOTIABLE EDITORIAL RULES:
1. ZERO HALLUCINATIONS: Every date, time, test topic, teacher note, party, invoice, news story, and outing run-date MUST come strictly from the provided live data or standing family rules. Never fabricate facts, opening hours, or dates.
2. THREAD REPLY CHECK FOR INVOICES: Read EVERY message in each Gmail thread. If a parent already replied confirming payment ("paid", "settled", "well received", etc.), DO NOT flag that bill as unpaid (you may briefly note next month's upcoming due date if on the calendar).
3. PRIVACY: Strictly obey ALL privacy rules in family.md. NEVER output email addresses, phone numbers, home street addresses, IBANs/bank sort codes, job-interview/recruiting/work-party items, secret gifts/surprises, or private medical labels.
4. WEEKDAY vs WEEKEND:
   - Monday–Friday (is_weekend=false): week_title = "The week ahead", `week` has ALL 7 days from metoffice.week_days (with `pickup` on weekdays), `school_run` has the 5 upcoming weekday rows, `town` has 3 picks.
   - Saturday–Sunday (is_weekend=true): week_title = "The weekend ahead", `week` covers ONLY the days in metoffice.week_days (3 days Sat/Sun/Mon on Saturday; 2 days Sun/Mon on Sunday), `school_run` MUST be null, `town` has 4 picks. Include `pickup` on Monday (from `default_pickup`). ONLY add bold `**Suggestion:** ` to open Saturday or Sunday slots matched to that day's Met Office weather — NEVER add `**Suggestion:**` to Monday (Monday is a school & nursery weekday!).
5. AROUND TOWN STRICT RULES:
   - Include at least ONE {membership_name} property (`where` starting literally with `"{membership_name} · "` — NO `<` or `>` angle brackets! — and noting free entry with membership / booking if required).
   - NEVER suggest banned outings (e.g. London blue plaques).
   - NEVER repeat any venue listed in Memory Sheet `excluded_permanent` (Attended/Skip) or `excluded_recent_14d`.
   - EVERY single `town` item MUST state in `text` when it runs or its seasonal opening schedule.
6. CLEAN BROADSHEET PROSE: Zero `[1]` / `[2]` citation brackets anywhere, zero raw `**` inside `watch[].lead`, and keep `coming_up_note` as the short 1-line subtitle `"Birthdays, parties and key dates, next 4 weeks."` (never list events inside `coming_up_note`). Keep all 3 columns well-proportioned so the 1-page broadsheet looks balanced."""

VISION_QA_PROMPT = """You are a newspaper art director inspecting a rendered 1-page broadsheet PNG ("The Family Table").
Check these 4 things strictly:
1. Masthead, top bar, weather block, and footer are clean, centered, and unclipped.
2. All 3 columns look visually balanced at the bottom (no giant empty whitespace hole >25% of column height in one column).
3. Typography & prose look clean: no raw `**` markdown stars, no `[1]` citation brackets, no JSON braces, no overlapping or truncated text.
4. Every section has real readable content (no blank sections).

Reply in this exact short format (under 10 lines):
VERDICT: PASS  (or VERDICT: FIX)
SUMMARY: <1 sentence overall impression>
ISSUES: <"None" if PASS, or 1-3 concrete, minimal fixes telling the editor which specific section to shorten or expand by ~1-2 sentences to balance the columns or clean up formatting>"""


def _log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _emit_progress(cb: Callable[[str, bool], None] | None, msg: str, done: bool = False) -> None:
    _log(msg)
    if cb:
        try:
            cb(msg, done)
        except Exception:
            pass


# ------------------------------------------------------------------ Router & stack readiness
def _router_get_json(path: str, timeout: int = 5) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://{ROUTER_HOST}:{ROUTER_PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _router_post_json(path: str, payload: dict[str, Any], timeout: int = 420) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=timeout)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Router {path} HTTP {resp.status}: {raw.decode('utf-8', 'ignore')[:400]}")
        return json.loads(raw.decode("utf-8"))
    finally:
        conn.close()


def ensure_stack_ready(wait_s: int = 180, wait_idle_s: int = 300, kick_start: bool = True) -> dict[str, Any]:
    """Ensure local router (:4000) is up and idle before starting GPU inference."""
    kicked = False
    deadline = time.time() + max(5, wait_s)
    health: dict[str, Any] | None = None
    while time.time() < deadline:
        try:
            health = _router_get_json("/health", timeout=4)
            if health.get("status") == "ok":
                break
        except Exception:
            if kick_start and not kicked:
                kicked = True
                start_ps1 = Path.home() / ".lmstudio" / "scripts" / "start-llm-stack.ps1"
                if start_ps1.is_file():
                    subprocess.Popen(
                        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(start_ps1)],
                        creationflags=NO_WINDOW,
                    )
            time.sleep(4)
    if not health or health.get("status") != "ok":
        raise RuntimeError("Local LLM router on 127.0.0.1:4000 did not become healthy in time.")

    idle_deadline = time.time() + max(5, wait_idle_s)
    while time.time() < idle_deadline:
        try:
            health = _router_get_json("/health", timeout=4)
            gpu = health.get("gpu") or {}
            if int(gpu.get("llm_inflight") or 0) == 0 and not gpu.get("image_busy") and not gpu.get("voice_busy"):
                return health
        except Exception:
            pass
        time.sleep(5)
    return health


# ------------------------------------------------------------------ Met Office & Broadsheet Node helpers
def _find_node() -> str:
    node = shutil.which("node")
    if node:
        return node
    for cand in (r"C:\Program Files\nodejs\node.exe", r"C:\Program Files (x86)\nodejs\node.exe"):
        if Path(cand).is_file():
            return cand
    raise FileNotFoundError("Node.js not found on PATH.")


def fetch_metoffice(target_arg: str | None, config_path: Path) -> dict[str, Any]:
    cmd = [_find_node(), str(_HERE / "fetch_metoffice.js")]
    if target_arg and target_arg != "auto":
        cmd.append(target_arg)
    cmd.extend(["--config", str(config_path)])
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60, creationflags=NO_WINDOW)
    if res.returncode != 0:
        raise RuntimeError(f"fetch_metoffice.js failed ({res.returncode}): {(res.stderr or res.stdout)[:400]}")
    return json.loads(res.stdout)


def render_broadsheet(edition_path: Path, out_dir: Path, config_path: Path) -> dict[str, Any]:
    cmd = [_find_node(), str(_HERE / "build_broadsheet.js"), str(edition_path), str(out_dir), "--config", str(config_path)]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=90, creationflags=NO_WINDOW)
    if res.returncode != 0:
        raise RuntimeError(f"build_broadsheet.js failed ({res.returncode}): {(res.stderr or res.stdout)[:500]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------ Local LLM calls
def _strip_thinking_and_extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    # Strip markdown code fences if present
    m_fence = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text)
    if m_fence:
        text = m_fence.group(1).strip()
    else:
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]
    return json.loads(text)


def _ask_pro_json(system_prompt: str, user_prompt: str, temperature: float = 0.45, max_tokens: int = 12000) -> dict[str, Any]:
    payload = {
        "model": "deep",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "_internal_family_table": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            resp = _router_post_json("/v1/chat/completions", payload, timeout=480)
            content = (((resp.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
            if not content.strip():
                raise RuntimeError("Local Pro model returned empty response.")
            return _strip_thinking_and_extract_json(content)
        except Exception as e:
            last_err = e
            if attempt == 0 and any(code in str(e) for code in ("HTTP 500", "HTTP 502", "HTTP 503", "Model does not exist")):
                _log(f"Transient router/LM Studio error on Pro call ({e}); retrying in 8s...")
                time.sleep(8)
                continue
            raise
    assert last_err is not None
    raise last_err


def _prepare_vision_image_b64(png_path: Path) -> str:
    """Resize tall broadsheet PNG cleanly for Gemma 4 Vision QA (~1100px wide PNG) so text stays crisp."""
    try:
        from PIL import Image  # type: ignore

        with Image.open(png_path) as im:
            w, h = im.size
            if w > 1100:
                new_h = max(1, int(round(h * (1100 / w))))
                im = im.resize((1100, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return "data:image/png;base64," + base64.b64encode(png_path.read_bytes()).decode("ascii")


def run_vision_qa(png_path: Path, render_info: dict[str, Any]) -> dict[str, Any]:
    data_uri = _prepare_vision_image_b64(png_path)
    cols = render_info.get("columns") or []
    spread = render_info.get("spread")
    meta_note = f"Measured render geometry: total height {render_info.get('height')}px, column heights {cols}, spread {spread}px (target < 220px)."
    payload = {
        "model": "fast",
        "temperature": 0.1,
        "max_tokens": 500,
        "stream": False,
        "_internal_family_table": True,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_QA_PROMPT + "\n\n" + meta_note},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    }
    resp = _router_post_json("/v1/chat/completions", payload, timeout=180)
    critique = ((((resp.get("choices") or [{}])[0]).get("message") or {}).get("content") or "").strip()
    critique = re.sub(r"(?is)<think>.*?</think>", "", critique).strip()
    verdict = "FIX" if re.search(r"(?im)^VERDICT\s*:\s*FIX\b", critique) else "PASS"
    return {"verdict": verdict, "critique": critique}


# ------------------------------------------------------------------ Deterministic locking & strict auditing
def lock_deterministic_fields(ed: dict[str, Any], met: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Force deterministic Met Office weather, dates, labels, and child age tags so they can never drift."""
    ed = dict(ed or {})
    ed["file_date"] = met["file_date"]
    ed["date_long"] = met["date_long"]
    ed["edition_label"] = met["edition_label"]
    ed["week_title"] = met["week_title"]
    ed["next_edition"] = met["next_edition"]
    ed["edition_title"] = cfg.get("edition_title") or "The Family Table"
    ed["location_label"] = cfg.get("location_label") or "London"
    ed["footer_readers"] = cfg.get("footer_readers") or "Family edition"

    wx = met.get("weather") or {}
    ed["weather"] = {
        "high": wx.get("high") or "",
        "summary": wx.get("summary") or "",
        "at8": wx.get("at8") or "",
        "rain": wx.get("rain") or "",
        "sunrise": wx.get("sunrise") or "",
        "sunset": wx.get("sunset") or "",
        "tomorrow": wx.get("tomorrow") or "",
        "source": wx.get("source") or cfg.get("weather_source_label") or "Met Office · London",
    }

    if met.get("is_weekend"):
        ed["school_run"] = None

    # Exact child tags from DOB calculation
    tag_by_name = {c["name"].lower(): c["tag"] for c in (met.get("children") or []) if c.get("name")}
    for k in ed.get("kids") or []:
        if isinstance(k, dict):
            nm = str(k.get("name") or "").strip().lower()
            if nm in tag_by_name:
                k["tag"] = tag_by_name[nm]

    # Exact week day/num labels + weekday default pickup when count matches
    week_days = met.get("week_days") or []
    if isinstance(ed.get("week"), list) and len(ed["week"]) == len(week_days):
        for dst, src in zip(ed["week"], week_days):
            if isinstance(dst, dict):
                dst["day"] = src["day"]
                dst["num"] = src["num"]
                if met.get("is_weekend") and src["day"] in ("SAT", "SUN"):
                    dst.pop("pickup", None)
                elif not dst.get("pickup") and src.get("default_pickup"):
                    dst["pickup"] = src["default_pickup"]

    # Clean coming_up_note and deterministically fix coming_up weekday & relative "when" labels
    ed["coming_up_note"] = "Birthdays, parties and key dates, next 4 weeks."
    base_d = datetime.strptime(met["file_date"], "%Y-%m-%d").date()
    months = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
    for item in ed.get("coming_up") or []:
        if not isinstance(item, dict):
            continue
        num_s = str(item.get("num") or "").strip()
        m_full = re.match(r"^0?(\d{1,2})\s+([A-Za-z]{3,9})$", num_s)
        m_day = re.match(r"^0?(\d{1,2})$", num_s)
        ev_d = None
        if m_full:
            d_n = int(m_full.group(1))
            mon_n = months.get(m_full.group(2).lower()[:3])
            if mon_n:
                yr = base_d.year + (1 if mon_n < base_d.month - 2 else 0)
                try:
                    ev_d = datetime(yr, mon_n, d_n).date()
                except ValueError:
                    ev_d = None
        elif m_day:
            d_n = int(m_day.group(1))
            # If day number < base_d.day, it wraps into next month
            yr, mon_n = base_d.year, base_d.month
            if d_n < base_d.day:
                mon_n += 1
                if mon_n > 12:
                    mon_n, yr = 1, yr + 1
            try:
                ev_d = datetime(yr, mon_n, d_n).date()
            except ValueError:
                ev_d = None
        if ev_d is not None:
            delta = (ev_d - base_d).days
            if 0 <= delta <= 60:
                item["day"] = ev_d.strftime("%a").upper()[:3]
                item["num"] = str(ev_d.day) if ev_d.month == base_d.month else f"{ev_d.day} {ev_d.strftime('%b')}"
                item["when"] = "Today" if delta == 0 else ("Tomorrow" if delta == 1 else f"In {delta} days")

    # Clean accidental literal <Membership> angle brackets & stray ** inside watch[].lead
    for w_item in ed.get("watch") or []:
        if isinstance(w_item, dict) and w_item.get("lead"):
            w_item["lead"] = str(w_item["lead"]).replace("**", "").strip()
    for t_item in ed.get("town") or []:
        if isinstance(t_item, dict) and t_item.get("where"):
            t_item["where"] = re.sub(r"<([^<>]{2,40})>", r"\1", str(t_item["where"])).strip()
    return ed


def _norm_venue(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _venues_clash(candidate: str, excluded: str) -> bool:
    c = _norm_venue(candidate)
    e = _norm_venue(excluded)
    if not c or not e or len(e) < 4:
        return False
    if e in c or c in e:
        return True
    stop = {"the", "and", "at", "of", "in", "for", "london", "museum", "house", "gardens", "park", "palace", "castle", "christmas", "winter"}
    ct = {w for w in c.split() if len(w) >= 4 and w not in stop}
    et = {w for w in e.split() if len(w) >= 4 and w not in stop}
    return len(ct & et) >= 2


def audit_edition(
    ed: dict[str, Any],
    met: dict[str, Any],
    cfg: dict[str, Any],
    memory: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    is_weekend = bool(met.get("is_weekend"))
    expected_days = len(met.get("week_days") or [])

    if not str(ed.get("lede") or "").strip():
        issues.append("Missing `lede`.")

    week = ed.get("week")
    if not isinstance(week, list) or len(week) != expected_days:
        issues.append(f"`week` must have exactly {expected_days} day entries (got {len(week) if isinstance(week, list) else 0}).")

    if is_weekend:
        if ed.get("school_run") is not None:
            issues.append("`school_run` must be null on weekend editions.")
        wknd_texts = " ".join(str(d.get("text") or "") for d in (week or []) if isinstance(d, dict) and d.get("day") in ("SAT", "SUN"))
        if "**Suggestion:**" not in wknd_texts and "**suggestion:**" not in wknd_texts.lower():
            issues.append("Weekend edition must include at least one bold `**Suggestion:**` for a free Saturday/Sunday slot matched to the weather.")
    else:
        sr = ed.get("school_run")
        if not isinstance(sr, dict) or not (sr.get("rows") or []):
            issues.append("Weekday edition must include a non-empty `school_run.rows` table (unless school holidays).")

    for sec in ("school_desk", "kids", "watch", "talk", "news", "town"):
        val = ed.get(sec)
        if not isinstance(val, list) or not val:
            issues.append(f"Section `{sec}` is missing or empty.")

    # Kid desk banned filler
    audit_cfg = cfg.get("audit") or {}
    banned_filler = [t.lower() for t in (audit_cfg.get("banned_kid_filler_terms") or []) if t]
    kids_blob = json.dumps(ed.get("kids") or [], ensure_ascii=False).lower()
    for term in banned_filler:
        if term in kids_blob:
            issues.append(f"Kid desk contains banned routine filler `{term}`.")

    # Around town checks
    town = ed.get("town") or []
    expected_town = 4 if is_weekend else 3
    if isinstance(town, list) and len(town) < expected_town:
        issues.append(f"`town` should have {expected_town} picks (got {len(town)}).")

    res_cfg = cfg.get("research") or {}
    membership = (res_cfg.get("membership_name") or "Heritage Pass").strip()
    banned_outings = [b.lower() for b in (res_cfg.get("banned_outing_terms") or ["blue plaque", "blue plaques"])]
    has_membership = False
    excluded_titles = [
        x.get("title", "")
        for x in (memory.get("excluded_permanent") or []) + (memory.get("excluded_recent_14d") or [])
        if x.get("title")
    ]

    for idx, item in enumerate(town if isinstance(town, list) else [], 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        text = str(item.get("text") or "")
        where = str(item.get("where") or "")
        if membership.lower() in where.lower():
            has_membership = True
        combined_lc = f"{title} {text} {where}".lower()
        for b in banned_outings:
            if b in combined_lc:
                issues.append(f"`town[{idx}]` ({title}) contains banned outing term `{b}`.")
        if not DATE_OR_SCHEDULE_RE.search(text):
            issues.append(f"`town[{idx}]` ({title}) is missing explicit run dates or seasonal opening schedule in `text`.")
        for ex in excluded_titles:
            if _venues_clash(title, ex):
                issues.append(f"`town[{idx}]` ({title}) repeats excluded/recent Memory Sheet venue `{ex}`.")
                break

    if membership and not has_membership:
        issues.append(f"`town` must include at least one `{membership}` property (with `where` starting `{membership} · `).")

    # Citation bracket leak check & privacy scanner across full rendered content
    prose_blob = json.dumps(
        {k: v for k, v in ed.items() if k not in ("file_date", "edition_title", "location_label", "footer_readers")},
        ensure_ascii=False,
    )
    if CITATION_BRACKET_RE.search(prose_blob):
        issues.append("Broadsheet prose contains raw citation brackets like `[1]` — remove all numeric citation brackets.")

    for pat in audit_cfg.get("privacy_forbidden_regex") or []:
        try:
            m = re.search(pat, prose_blob, re.I)
            if m:
                issues.append(f"Privacy violation detected (pattern `{pat}` matched `{m.group(0)[:24]}`) — remove immediately.")
        except re.error:
            pass

    return issues


def build_email_body(ed: dict[str, Any]) -> str:
    lede = str(ed.get("lede") or "").strip()
    town_bits = []
    for item in ed.get("town") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "")
        m_dates = re.search(
            r"(?i)((?:runs?|running|open)\s+[^.;()]{4,45}|\d{1,2}\s*(?:–|-)\s*\d{1,2}\s+[A-Z][a-z]{2,8}|\d{1,2}\s+[A-Z][a-z]{2,8}\s*(?:–|-)\s*\d{1,2}\s+[A-Z][a-z]{2,8})",
            text,
        )
        if title:
            town_bits.append(f"{title} ({m_dates.group(1).strip()})" if m_dates else title)
    lines = [lede, ""]
    if town_bits:
        lines.append("**Around town:** " + " · ".join(town_bits))
        lines.append("")
    lines.append("The full broadsheet edition is attached.")
    return "\n".join(lines).strip() + "\n"


def _build_author_user_prompt(
    met: dict[str, Any],
    family_md: str,
    ideas_md: str,
    calendars: dict[str, Any],
    gmail: dict[str, Any],
    memory: dict[str, Any],
    web_res: dict[str, Any],
    extra_instructions: str = "",
) -> str:
    parts = [
        "=== 1. EXACT DATES, CHILD AGE TAGS & MET OFFICE WEATHER (DO NOT ALTER NUMBERS) ===",
        json.dumps(met, ensure_ascii=False, indent=2),
        "\n=== 2. STANDING FAMILY RULES ===",
        family_md.strip(),
    ]
    if ideas_md.strip():
        parts.extend(["\n=== 2B. EVERGREEN IDEAS BANK (Mon-Thu reference, still verify/state dates or seasonal hours) ===", ideas_md.strip()])
    parts.extend([
        "\n=== 3. LIVE GOOGLE CALENDARS (7-day full detail + 28-day horizon) ===",
        json.dumps(calendars, ensure_ascii=False, indent=1),
        "\n=== 4. LIVE GMAIL THREADS (Full plain-text bodies + replies — check replies before flagging invoices unpaid!) ===",
        json.dumps(gmail, ensure_ascii=False, indent=1),
        "\n=== 5. MEMORY SHEET EXCLUSIONS (NEVER suggest any venue below!) ===",
        json.dumps(memory, ensure_ascii=False, indent=1),
        "\n=== 6. LIVE WEB RESEARCH (Kid news, local family events with dates, membership heritage outings) ===",
        json.dumps(web_res, ensure_ascii=False, indent=1),
    ])
    if extra_instructions.strip():
        parts.extend(["\n=== 7. EXTRA USER REQUEST FOR THIS EDITION ===", extra_instructions.strip()])
    parts.append("\nNow produce the complete, valid JSON object for this edition.")
    return "\n".join(parts)


# ------------------------------------------------------------------ End-to-end runner
def run_pipeline(
    target_date: str = "auto",
    config_path: str | Path | None = None,
    create_draft: bool = True,
    update_memory: bool = True,
    enable_vision_qa: bool = True,
    wait_stack: bool = True,
    extra_instructions: str = "",
    snapshot_in: str | Path | None = None,
    snapshot_out: str | Path | None = None,
    output_dir_override: str | Path | None = None,
    progress_cb: Callable[[str, bool], None] | None = None,
) -> dict[str, Any]:
    t0 = time.time()
    cfg, secrets_dir = gc.load_config(config_path)
    cfg_file = secrets_dir / "config.json" if config_path is None else Path(config_path)

    if wait_stack:
        _emit_progress(progress_cb, "🔌 Checking local LLM router & GPU availability…")
        ensure_stack_ready(wait_s=180, wait_idle_s=300, kick_start=True)

    creds = None
    if snapshot_in:
        _emit_progress(progress_cb, f"📦 Loading snapshot inputs from {Path(snapshot_in).name}…")
        snap = json.loads(Path(snapshot_in).read_text(encoding="utf-8"))
        met = snap["metoffice"]
        calendars = snap["calendars"]
        gmail = snap["gmail"]
        memory = snap["memory"]
        web_res = snap["research"]
        family_md = snap.get("family_md") or ""
        ideas_md = snap.get("ideas_md") or ""
    else:
        _emit_progress(progress_cb, "🌐 Waiting for network & fetching live Met Office forecast…")
        gc.wait_for_network("weather.metoffice.gov.uk", timeout_s=60)
        met = fetch_metoffice(target_date, cfg_file)

        _emit_progress(progress_cb, "🔐 Connecting to Google Calendar, Gmail & Memory Sheet (OAuth 2.0)…")
        gc.wait_for_network("www.googleapis.com", timeout_s=60)
        creds = gc.get_credentials(cfg, secrets_dir, interactive=False)
        cw = met["calendar_window"]
        calendars = gc.fetch_calendars(creds, cfg, cw["start"], cw["week_end"], cw["end"])
        gmail = gc.fetch_gmail_bundles(creds, cfg)
        memory = gc.read_memory_sheet(creds, cfg, met["file_date"])

        _emit_progress(progress_cb, "🔍 Researching live kid news, local events & heritage picks…")
        web_res = research.run_live_research(
            cfg,
            met["file_date"],
            bool(met.get("is_weekend")),
            str((met.get("weather") or {}).get("summary") or ""),
        )

        fam_p = secrets_dir / "family.md"
        family_md = fam_p.read_text(encoding="utf-8") if fam_p.is_file() else ""
        ideas_p = secrets_dir / "ideas.md"
        ideas_md = ideas_p.read_text(encoding="utf-8") if (ideas_p.is_file() and met.get("dow_idx") in (1, 2, 3, 4)) else ""

        if snapshot_out:
            Path(snapshot_out).parent.mkdir(parents=True, exist_ok=True)
            Path(snapshot_out).write_text(
                json.dumps(
                    {
                        "metoffice": met,
                        "calendars": calendars,
                        "gmail": gmail,
                        "memory": memory,
                        "research": web_res,
                        "family_md": family_md,
                        "ideas_md": ideas_md,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    file_date = met["file_date"]
    out_dir = Path(os.path.expandvars(str(output_dir_override or cfg.get("output_dir") or (Path.home() / "Documents" / "The-Family-Table"))))
    editions_dir = out_dir / "editions"
    editions_dir.mkdir(parents=True, exist_ok=True)

    # Author edition locally on Pro
    _emit_progress(progress_cb, f"✍️ Authoring {met['edition_label']} ({met['date_long']}) on local Pro…")
    membership_name = ((cfg.get("research") or {}).get("membership_name") or "Heritage Pass").strip()
    sys_prompt = EDITOR_SYSTEM_PROMPT.replace("{membership_name}", membership_name)
    user_prompt = _build_author_user_prompt(met, family_md, ideas_md, calendars, gmail, memory, web_res, extra_instructions)
    ed = _ask_pro_json(sys_prompt, user_prompt, temperature=0.45, max_tokens=12000)
    ed = lock_deterministic_fields(ed, met, cfg)

    audit_issues = audit_edition(ed, met, cfg, memory)
    repaired_audit = False
    if audit_issues:
        _log("Audit issues found: " + " | ".join(audit_issues))
        _emit_progress(progress_cb, f"🔧 Fixing {len(audit_issues)} audit item(s) on local Pro…")
        fix_prompt = (
            "Your draft edition JSON failed these strict editorial/privacy checks:\n- "
            + "\n- ".join(audit_issues)
            + "\n\nFix EVERY listed issue while preserving all other accurate live facts and returning ONLY the complete corrected JSON object.\n\n"
            + "CURRENT JSON:\n"
            + json.dumps(ed, ensure_ascii=False, indent=2)
            + "\n\nEXCLUDED VENUES (do not use):\n"
            + json.dumps(memory, ensure_ascii=False)
            + "\n\nLIVE RESEARCH (for replacement venues/dates if needed):\n"
            + json.dumps(web_res, ensure_ascii=False)
        )
        try:
            ed2 = _ask_pro_json(sys_prompt, fix_prompt, temperature=0.25, max_tokens=10000)
            ed2 = lock_deterministic_fields(ed2, met, cfg)
            audit_issues_2 = audit_edition(ed2, met, cfg, memory)
            priv_1 = sum(1 for x in audit_issues if "privacy violation" in x.lower())
            priv_2 = sum(1 for x in audit_issues_2 if "privacy violation" in x.lower())
            if (priv_2, len(audit_issues_2)) <= (priv_1, len(audit_issues)):
                ed = ed2
                audit_issues = audit_issues_2
                repaired_audit = True
        except Exception as e:
            _log(f"Audit repair pass failed ({e}); evaluating v1 safety")
    if any("privacy violation" in x.lower() for x in audit_issues):
        raise RuntimeError(f"Aborted delivery due to unresolved privacy violation: {audit_issues}")

    edition_path = editions_dir / f"edition-{file_date}.json"
    edition_path.write_text(json.dumps(ed, ensure_ascii=False, indent=2), encoding="utf-8")

    # Render PDF + PNG
    _emit_progress(progress_cb, "📰 Rendering 1-page broadsheet PDF & preview PNG…")
    render_info = render_broadsheet(edition_path, out_dir, cfg_file)

    # Gemma 4 Vision QA + optional 1-turn balance polish
    vision_qa: dict[str, Any] | None = None
    polished_layout = False
    png_path = Path(render_info["preview"]) if render_info.get("preview") else None
    if enable_vision_qa and png_path and png_path.is_file():
        _emit_progress(progress_cb, "👁️ Running Gemma 4 Vision QA on rendered broadsheet…")
        try:
            vision_qa = run_vision_qa(png_path, render_info)
        except Exception as e:
            vision_qa = {"verdict": "SKIP", "critique": f"Vision QA error: {e}"}

    needs_layout_polish = (not render_info.get("balanced", True)) or (
        bool(vision_qa) and vision_qa.get("verdict") == "FIX"
    )
    if needs_layout_polish and not audit_issues:
        _emit_progress(progress_cb, "✨ Polishing column balance & visual layout on local Pro…")
        cols = render_info.get("columns") or []
        spread = render_info.get("spread")
        polish_prompt = (
            "The rendered 1-page broadsheet needs a small layout polish.\n"
            f"Measured column pixel heights [Col1, Col2, Col3]: {cols} (spread: {spread}px; target < 220px).\n"
            + (f"Gemma 4 Art-Director QA:\n{vision_qa.get('critique')}\n\n" if vision_qa else "")
            + "Make minimal, surgical edits (e.g. tighten 1-2 verbose items in the tallest column, or enrich/clean formatting in the shortest column) "
            "WITHOUT hallucinating any facts, dropping mandatory run dates, or violating privacy rules.\n"
            "Return ONLY the complete updated JSON object.\n\n"
            + json.dumps(ed, ensure_ascii=False, indent=2)
        )
        try:
            ed_pol = _ask_pro_json(sys_prompt, polish_prompt, temperature=0.25, max_tokens=10000)
            ed_pol = lock_deterministic_fields(ed_pol, met, cfg)
            pol_issues = audit_edition(ed_pol, met, cfg, memory)
            if not pol_issues:
                bak_json = editions_dir / f"edition-{file_date}.v1.json"
                shutil.copy2(edition_path, bak_json)
                edition_path.write_text(json.dumps(ed_pol, ensure_ascii=False, indent=2), encoding="utf-8")
                render_v2 = render_broadsheet(edition_path, out_dir, cfg_file)
                old_spread = int(render_info.get("spread") or 9999)
                new_spread = int(render_v2.get("spread") or 9999)
                if new_spread <= old_spread or (vision_qa and vision_qa.get("verdict") == "FIX"):
                    ed = ed_pol
                    render_info = render_v2
                    polished_layout = True
                    bak_json.unlink(missing_ok=True)
                else:
                    shutil.copy2(bak_json, edition_path)
                    render_info = render_broadsheet(edition_path, out_dir, cfg_file)
                    bak_json.unlink(missing_ok=True)
        except Exception as e:
            _log(f"Layout polish skipped (kept v1): {e}")

    # Write email body Markdown
    body_md = build_email_body(ed)
    body_path = editions_dir / f"body-{file_date}.md"
    body_path.write_text(body_md, encoding="utf-8")

    # Append to Memory Sheet & Create/Update Gmail Draft
    memory_result: dict[str, Any] = {"skipped": True}
    draft_result: dict[str, Any] = {"skipped": True}
    if creds is not None:
        if update_memory:
            _emit_progress(progress_cb, "📝 Updating Google Sheets Memory Tracker…")
            try:
                memory_result = gc.append_memory_suggestions(creds, cfg, ed.get("town") or [], file_date)
            except Exception as e:
                memory_result = {"error": str(e)}
        if create_draft:
            _emit_progress(progress_cb, "✉️ Saving Gmail draft with PDF attached…")
            subject = f"{cfg.get('edition_title') or 'The Family Table'} · {met['subject_date']}"
            recipients = list(cfg.get("recipients") or [])
            draft_result = gc.create_or_update_gmail_draft(
                creds, recipients, subject, body_md, Path(render_info["pdf"])
            )

    elapsed_s = round(time.time() - t0, 1)
    summary = {
        "ok": True,
        "file_date": file_date,
        "date_long": met["date_long"],
        "subject_date": met["subject_date"],
        "edition_label": met["edition_label"],
        "lede": ed.get("lede"),
        "edition_json": str(edition_path),
        "body_md": str(body_path),
        "pdf": render_info.get("pdf"),
        "preview_png": render_info.get("preview"),
        "html": render_info.get("html"),
        "render": {
            "engine": render_info.get("engine"),
            "height": render_info.get("height"),
            "columns": render_info.get("columns"),
            "spread": render_info.get("spread"),
            "balanced": render_info.get("balanced"),
        },
        "audit_issues_remaining": audit_issues,
        "repaired_audit": repaired_audit,
        "vision_qa": vision_qa,
        "polished_layout": polished_layout,
        "memory_update": memory_result,
        "gmail_draft": draft_result,
        "elapsed_s": elapsed_s,
    }
    _emit_progress(
        progress_cb,
        f"📰 {cfg.get('edition_title') or 'The Family Table'} ({met['subject_date']}) ready in {elapsed_s}s"
        + (f" · draft {draft_result.get('action')}" if draft_result.get("draft_id") else ""),
        done=True,
    )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="100% Local Family Table daily broadsheet generator")
    ap.add_argument("target", nargs="?", default="auto", help="--today | --tomorrow | YYYY-MM-DD | auto")
    ap.add_argument("--today", action="store_true", help="Build today's edition")
    ap.add_argument("--tomorrow", action="store_true", help="Build tomorrow's edition")
    ap.add_argument("--config", default=None, help="Path to private config.json")
    ap.add_argument("--no-draft", action="store_true", help="Skip creating Gmail draft")
    ap.add_argument("--no-memory", action="store_true", help="Skip appending to Google Sheets Memory Tracker")
    ap.add_argument("--no-vision", action="store_true", help="Skip Gemma 4 Vision QA pass")
    ap.add_argument("--no-wait-stack", action="store_true", help="Do not wait/kick start-llm-stack.ps1")
    ap.add_argument("--instructions", default="", help="Optional editorial note for this run")
    ap.add_argument("--snapshot-in", default=None, help="Load collected inputs from a local snapshot JSON")
    ap.add_argument("--snapshot-out", default=None, help="Save collected inputs to a local snapshot JSON")
    ap.add_argument("--out-dir", default=None, help="Override output directory")
    ap.add_argument("--stream-events", action="store_true", help="Emit JSON progress events to stderr")
    args = ap.parse_args()

    target = "--today" if args.today else ("--tomorrow" if args.tomorrow else args.target)

    def _stderr_cb(msg: str, done: bool) -> None:
        if args.stream_events:
            sys.stderr.write("EVENT:" + json.dumps({"description": msg, "done": done}, ensure_ascii=False) + "\n")
            sys.stderr.flush()

    try:
        res = run_pipeline(
            target_date=target,
            config_path=args.config,
            create_draft=not args.no_draft,
            update_memory=not args.no_memory,
            enable_vision_qa=not args.no_vision,
            wait_stack=not args.no_wait_stack,
            extra_instructions=args.instructions,
            snapshot_in=args.snapshot_in,
            snapshot_out=args.snapshot_out,
            output_dir_override=args.out_dir,
            progress_cb=_stderr_cb if args.stream_events else None,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except gc.GoogleAuthError as e:
        _log(f"AUTH ERROR: {e}")
        print(json.dumps({"ok": False, "auth_error": str(e)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except Exception as e:
        _log(f"ERROR: {e!r}")
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
