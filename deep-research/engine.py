"""
Local Deep Research engine (100% local planning & synthesis + Tavily search + SSRF-safe full-page reader + PDF export).
Emits real-time JSON status pills (`EVENT:{...}`) to stderr and final JSON result to stdout.

Usage:
  python -I -X utf8 engine.py --topic "..." [--language auto|en|fr] [--out-dir <dir>]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

_LMS_SCRIPTS = str(Path.home() / ".lmstudio" / "scripts")
if _LMS_SCRIPTS not in sys.path:
    sys.path.insert(0, _LMS_SCRIPTS)

import web_search  # type: ignore  # noqa: E402

ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4000
LMSTUDIO_HOST, LMSTUDIO_PORT = "127.0.0.1", 1234
LOW_SIGNAL_DOMAINS = (
    "instagram.com",
    "facebook.com",
    "tiktok.com",
    "pinterest.",
    "reddit.com/r/",
    "x.com",
    "twitter.com",
)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _emit(msg: str, done: bool = False) -> None:
    sys.stderr.write("EVENT:" + json.dumps({"description": msg, "done": done}, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def _plan_queries_cpu(topic: str) -> list[str]:
    """Use always-on CPU qwen3.5-4b (0 GPU swaps) to decompose topic into 4 complementary search queries."""
    today = web_search.today_text()
    month_year = datetime.now().strftime("%B %Y")
    body = {
        "model": "qwen3.5-4b",
        "temperature": 0,
        "max_tokens": 180,
        "reasoning_effort": "none",
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are a senior research planner. Today is {today}. Write EXACTLY 4 complementary web search "
                    "queries (one per line, no numbering, no quotes, no filler) to thoroughly investigate the topic "
                    f"across different angles (overview/facts, comparisons/benchmarks/specs, recent {month_year} updates, "
                    "trade-offs/criticisms/pricing). Use English for global tech/science/finance, or the topic's natural "
                    "language for local topics."
                ),
            },
            {"role": "user", "content": topic[:1200]},
        ],
    }
    queries: list[str] = []
    try:
        conn = http.client.HTTPConnection(LMSTUDIO_HOST, LMSTUDIO_PORT, timeout=30)
        try:
            conn.request("POST", "/v1/chat/completions", json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                raw = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
                raw = re.sub(r"(?is)<think>.*?</think>", "", raw).strip()
                for ln in raw.splitlines():
                    cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)]|Query\s*\d+:)\s*", "", ln).strip().strip('"').strip("'")
                    if len(cleaned) >= 6 and cleaned.lower() not in {q.lower() for q in queries}:
                        queries.append(cleaned[:140])
                    if len(queries) >= 4:
                        break
        finally:
            conn.close()
    except Exception:
        pass
    if not queries:
        base = topic.strip()[:100]
        queries = [
            base,
            f"{base} comparison pros cons {month_year}",
            f"{base} latest news updates {month_year}",
        ]
    return queries


def _fetch_full_page(url: str, fallback_snippet: str, timeout: float = 8.0) -> str:
    if not web_search._is_public_https_url(url):
        return fallback_snippet
    hdrs = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
    try:
        with web_search.safe_open_public_https(url, headers=hdrs, timeout=timeout) as resp:
            if resp.status >= 400:
                return fallback_snippet
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "html" not in ctype and "text" not in ctype:
                return fallback_snippet
            raw_bytes = resp.read(524288)
            html_txt = raw_bytes.decode("utf-8", errors="replace")
    except Exception:
        return fallback_snippet

    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html_txt, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        main_node = soup.find("article") or soup.find("main") or soup.body or soup
        text = re.sub(r"\s+", " ", main_node.get_text(" ", strip=True)).strip()
        if len(text) < 200:
            return fallback_snippet
        return text[:3500]
    except Exception:
        return fallback_snippet


def _gather_sources(topic: str, queries: list[str]) -> list[dict[str, Any]]:
    if not web_search.has_key():
        raise RuntimeError("Tavily API key is missing.")
    opts = web_search.infer_search_options(topic, queries[0] if queries else topic)
    s_topic = opts.get("topic", "general")
    t_range = opts.get("time_range")

    seen_urls: set[str] = set()
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []

    for q in queries[:4]:
        try:
            res, _imgs = web_search.search_rich(q, count=5, depth="basic", topic=s_topic, time_range=t_range)
        except Exception:
            continue
        for r in res or []:
            u = (r.get("url") or "").strip()
            if not u or u in seen_urls or not web_search._is_public_https_url(u):
                continue
            seen_urls.add(u)
            item = {
                "title": (r.get("title") or u).strip(),
                "url": u,
                "published": web_search._format_pub_date(r.get("published")),
                "snippet": (r.get("content") or "").strip()[:1200],
            }
            if any(b in u.lower() for b in LOW_SIGNAL_DOMAINS):
                secondary.append(item)
            else:
                primary.append(item)

    selected = primary[:8]
    if len(selected) < 6:
        selected.extend(secondary[: 6 - len(selected)])
    if not selected:
        raise RuntimeError("Web search returned no readable sources.")

    _emit(f"📖 Reading {len(selected)} full web pages in parallel…", done=False)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(selected))) as pool:
        futs = {pool.submit(_fetch_full_page, s["url"], s["snippet"]): s for s in selected}
        for fut in concurrent.futures.as_completed(futs):
            src = futs[fut]
            try:
                src["full_text"] = fut.result(timeout=10)
            except Exception:
                src["full_text"] = src["snippet"]
    return selected


def _synthesize_report(topic: str, language: str, queries: list[str], sources: list[dict[str, Any]]) -> str:
    today = web_search.today_text()
    lang_rule = (
        f"Write the entire report in {language}."
        if language and language.lower() not in ("auto", "")
        else "Match the exact language of the user's topic."
    )
    src_blocks = []
    for idx, s in enumerate(sources, 1):
        pub = f" (published {s['published']})" if s.get("published") else ""
        body = s.get("full_text") or s.get("snippet") or ""
        src_blocks.append(f"[{idx}] {s['title']}{pub}\nURL: {s['url']}\n{body}")

    sys_prompt = (
        f"You are an investigative deep-research analyst. Today is {today}. {lang_rule}\n"
        "Synthesize ONLY from the numbered full-text sources provided below (zero hallucinations or invented stats). "
        "Cite facts inline strictly as [1], [2], etc. Include:\n"
        "1. `# <Clear Executive Report Title>`\n"
        "2. `## Executive Summary` (3-5 crisp key takeaways)\n"
        "3. `## Detailed Findings` (well-organized subheadings with concrete numbers, dates, and cross-checked evidence)\n"
        "4. A clean Markdown comparison/summary table whenever comparing options, specs, prices, or timelines\n"
        "5. `## Caveats & Open Questions` (honestly flag conflicting claims or missing data)\n"
        "6. `## Sources` listing `[n] Title — URL` for every source cited."
    )
    user_prompt = (
        f"RESEARCH TOPIC: {topic}\n"
        f"SUB-QUERIES EXECUTED: {', '.join(queries)}\n\n"
        + "\n\n---\n\n".join(src_blocks)
    )
    payload = {
        "model": "deep",
        "temperature": 0.3,
        "max_tokens": 10000,
        "stream": False,
        "_internal_deep_research": True,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=420)
    try:
        conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"Router HTTP {resp.status}: {raw.decode('utf-8', 'ignore')[:400]}")
        data = json.loads(raw.decode("utf-8"))
    finally:
        conn.close()

    content = (((data.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
    content = re.sub(r"(?is)<think>.*?</think>", "", content).strip()
    if not content:
        raise RuntimeError("Pro returned an empty deep research report.")
    return content


def run_research(topic: str, language: str = "auto", out_dir: Path | None = None, export_pdf_flag: bool = True) -> dict[str, Any]:
    t0 = time.time()
    _emit("🧭 Planning 4 complementary research angles…", done=False)
    queries = _plan_queries_cpu(topic)
    _emit(f"🔍 Searching {len(queries)} research queries via Tavily…", done=False)
    sources = _gather_sources(topic, queries)
    _emit(f"🧠 Synthesizing cited report across {len(sources)} full sources on Pro…", done=False)
    report = _synthesize_report(topic, language, queries, sources)

    pdf_path = None
    if export_pdf_flag and out_dir is not None:
        exporter_dir = str(Path.home() / "AI" / "doc-export")
        if exporter_dir not in sys.path:
            sys.path.insert(0, exporter_dir)
        try:
            import exporter  # type: ignore

            m_h = re.search(r"(?m)^#\s+(.+)$", report)
            title = m_h.group(1).strip() if m_h else topic[:70]
            out_dir.mkdir(parents=True, exist_ok=True)
            pdf_file = out_dir / "deep_research_report.pdf"
            exporter.export_pdf(title, report, pdf_file)
            if pdf_file.is_file():
                pdf_path = str(pdf_file)
        except Exception:
            pdf_path = None

    elapsed_s = round(time.time() - t0, 1)
    _emit(f"🔬 Deep research complete · {len(sources)} sources ({elapsed_s}s)", done=True)
    return {
        "ok": True,
        "topic": topic,
        "queries": queries,
        "sources_count": len(sources),
        "sources": [{"title": s["title"], "url": s["url"], "published": s.get("published")} for s in sources],
        "report_markdown": report,
        "pdf_path": pdf_path,
        "elapsed_s": elapsed_s,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Local Deep Research engine")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--language", default="auto")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    out_d = Path(args.out_dir) if args.out_dir else None
    try:
        res = run_research(args.topic, args.language, out_d, export_pdf_flag=not args.no_pdf)
        print(json.dumps(res, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
