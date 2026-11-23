"""
Live web research for The Family Table using the local stack's Tavily helper (web_search.py).
Collects:
  1. Positive kid-friendly science, space, nature, animal, invention, or sport news from this week.
  2. Live family events & exhibitions in the configured local area with exact start/end dates.
  3. Live events / winter or seasonal opening schedules at nearby membership heritage properties (no blue plaques).
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_LMS_SCRIPTS = str(Path.home() / ".lmstudio" / "scripts")
if _LMS_SCRIPTS not in sys.path:
    sys.path.insert(0, _LMS_SCRIPTS)

LOW_SIGNAL_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "pinterest.",
    "reddit.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "tripadvisor.",
    "eventbrite.com/d/",
)


def _load_web_search():
    import web_search  # type: ignore
    return web_search


def _filter_results(results: list[dict[str, Any]], banned_terms: list[str], limit: int = 6) -> list[dict[str, Any]]:
    seen: set[str] = set()
    clean: list[dict[str, Any]] = []
    low: list[dict[str, Any]] = []
    banned_lc = [b.lower() for b in banned_terms if b]
    for r in results or []:
        url = (r.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        blob = f"{title} {content} {url}".lower()
        if any(b in blob for b in banned_lc):
            continue
        item = {
            "title": title,
            "url": url,
            "published": (r.get("published") or ""),
            "content": content[:1500],
        }
        if any(bad in url.lower() for bad in LOW_SIGNAL_DOMAINS):
            low.append(item)
        else:
            clean.append(item)
    selected = clean[:limit]
    if len(selected) < max(3, limit - 2):
        selected.extend(low[: limit - len(selected)])
    return selected


def run_live_research(
    cfg: dict[str, Any],
    file_date: str,
    is_weekend: bool,
    weather_summary: str = "",
) -> dict[str, Any]:
    ws = _load_web_search()
    if not ws.has_key():
        return {"ok": False, "error": "Tavily API key missing", "kid_news": [], "local_events": [], "heritage_events": []}

    dt = datetime.strptime(file_date, "%Y-%m-%d")
    month_year = dt.strftime("%B %Y")
    res_cfg = cfg.get("research") or {}
    local_area = res_cfg.get("local_area") or "Central London"
    membership = res_cfg.get("membership_name") or "Heritage Pass"
    membership_region = res_cfg.get("membership_region") or "within 1 hour of your city"
    banned = list(res_cfg.get("banned_outing_terms") or ["blue plaque", "blue plaques"])

    queries = [
        (
            "kid_news",
            f"positive science space nature wildlife discovery kids news {month_year}",
            "news",
            "week",
            "advanced",
            6,
        ),
        (
            "local_events",
            f"family kids exhibitions events {local_area} {month_year} runs dates tickets",
            "general",
            None,
            "advanced",
            6,
        ),
        (
            "heritage_events",
            f'"{membership}" properties castles gardens events winter opening times {membership_region} {month_year} -"blue plaque"',
            "general",
            None,
            "advanced" if is_weekend else "basic",
            6,
        ),
    ]

    out: dict[str, Any] = {"ok": True, "queries": [], "kid_news": [], "local_events": [], "heritage_events": []}
    for key, q, topic, time_range, depth, count in queries:
        out["queries"].append({"bucket": key, "query": q, "topic": topic, "time_range": time_range, "depth": depth})
        try:
            results, _imgs = ws.search_rich(q, count=count, depth=depth, topic=topic, time_range=time_range)
            out[key] = _filter_results(results, banned_terms=banned if key != "kid_news" else [], limit=count)
        except Exception as e:
            out[key] = [{"error": f"Search failed: {e}"}]
    return out
