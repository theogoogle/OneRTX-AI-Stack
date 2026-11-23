"""
Live Market & Finance CLI + 300-DPI chart builder (stdlib urllib + matplotlib).
Zero API keys and zero Tavily credits. Supports equities, ETFs, indices, crypto, and FX.

Usage:
  python -I -X utf8 finance_cli.py quote --symbol "NVDA" --period 1mo --chart-out nvda.png --html-out nvda.html
  python -I -X utf8 finance_cli.py compare --symbols "NVDA,MSFT,AAPL" --period 1y --chart-out cmp.png --html-out cmp.html
"""
from __future__ import annotations

import argparse
import base64
import functools
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

_INTER_WOFF2_PATH = Path.home() / "AI" / "family-table" / "assets" / "fonts" / "inter-ui-latin.woff2"


@functools.lru_cache(maxsize=1)
def _inter_font_face_css() -> str:
    try:
        if _INTER_WOFF2_PATH.is_file():
            b64 = base64.b64encode(_INTER_WOFF2_PATH.read_bytes()).decode("ascii")
            return (
                '@font-face{font-family:"Inter";'
                f'src:url("data:font/woff2;base64,{b64}") format("woff2");'
                'font-weight:400 700;font-style:normal;font-display:swap;}\n'
            )
    except Exception:
        pass
    return ""

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

SYMBOL_ALIASES = {
    "bitcoin": "BTC-USD",
    "btc": "BTC-USD",
    "ethereum": "ETH-USD",
    "eth": "ETH-USD",
    "solana": "SOL-USD",
    "sol": "SOL-USD",
    "ftse": "^FTSE",
    "ftse 100": "^FTSE",
    "ftse100": "^FTSE",
    "s&p 500": "^GSPC",
    "s&p": "^GSPC",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "dow": "^DJI",
    "dow jones": "^DJI",
    "cac 40": "^FCHI",
    "cac40": "^FCHI",
    "dax": "^GDAXI",
    "nikkei": "^N225",
    "gold": "GC=F",
    "silver": "SI=F",
    "oil": "CL=F",
    "brent": "BZ=F",
    "gbp/eur": "GBPEUR=X",
    "gbpeur": "GBPEUR=X",
    "eur/gbp": "EURGBP=X",
    "eurgbp": "EURGBP=X",
    "gbp/usd": "GBPUSD=X",
    "gbpusd": "GBPUSD=X",
    "usd/gbp": "USDGBP=X",
    "eur/usd": "EURUSD=X",
    "eurusd": "EURUSD=X",
    "usd/jpy": "USDJPY=X",
    "usdjpy": "USDJPY=X",
    "nvidia": "NVDA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "amazon": "AMZN",
    "meta": "META",
    "tesla": "TSLA",
    "lvmh": "MC.PA",
}

PERIOD_INTERVAL = {
    "24h": "5m",
    "1d": "5m",
    "5d": "15m",
    "7d": "30m",
    "1mo": "1d",
    "3mo": "1d",
    "6mo": "1d",
    "1y": "1d",
    "2y": "1wk",
    "5y": "1wk",
}

INTERACTIVE_RANGES = ("24h", "7d", "1mo", "1y", "5y")
RANGE_LABELS = {"24h": "24H", "7d": "7D", "1mo": "1M", "1y": "1Y", "5y": "5Y"}
COMPARE_PALETTE = ("#38bdf8", "#10b981", "#f59e0b", "#ec4899", "#a78bfa")


def _normalize_range_key(period: str | None) -> str:
    low = (period or "").strip().lower()
    if low in ("24h", "1d", "today", "intraday"):
        return "24h"
    if low in ("7d", "5d", "1w", "1wk", "week"):
        return "7d"
    if low in ("1mo", "1m", "month", "3mo"):
        return "1mo"
    if low in ("6mo", "1y", "1yr", "year"):
        return "1y"
    if low in ("2y", "5y", "5yr", "max"):
        return "5y"
    return "1mo"


def _http_json(url: str, timeout: int = 12) -> dict[str, Any]:
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429 and attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    assert last_err is not None
    raise last_err


def resolve_symbol(query: str) -> str:
    q = (query or "").strip()
    if not q:
        raise ValueError("Empty ticker or company query.")
    q_clean = re.sub(r"^\$", "", q).strip()
    low = q_clean.lower()
    if low in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[low]
    # Pair like GBP/CHF -> GBPCHF=X
    m_fx = re.match(r"^([A-Za-z]{3})\s*/\s*([A-Za-z]{3})$", q_clean)
    if m_fx:
        return f"{m_fx.group(1).upper()}{m_fx.group(2).upper()}=X"
    # Direct ticker format
    if re.match(r"^[\^A-Za-z0-9.=:-]{1,12}$", q_clean) and (" " not in q_clean):
        if q_clean.upper() == q_clean or "." in q_clean or q_clean.startswith("^") or "=" in q_clean or "-" in q_clean:
            return q_clean.upper()
    # Yahoo Finance search fallback
    url = f"https://query1.finance.yahoo.com/v1/finance/search?{urllib.parse.urlencode({'q': q_clean, 'quotesCount': 5, 'newsCount': 0})}"
    data = _http_json(url)
    quotes = [x for x in (data.get("quotes") or []) if x.get("symbol")]
    if quotes:
        return str(quotes[0]["symbol"])
    return q_clean.upper()


def _parse_chart_response(symbol: str, period: str, data: dict[str, Any]) -> dict[str, Any]:
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise RuntimeError(f"No market data for `{symbol}`: {err.get('description') or 'not found'}")
    r0 = res[0]
    meta = r0.get("meta") or {}
    timestamps = r0.get("timestamp") or []
    closes_raw = (((r0.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    series: list[tuple[datetime, float]] = []
    for ts, val in zip(timestamps, closes_raw):
        if ts is not None and val is not None:
            series.append((datetime.fromtimestamp(int(ts)), float(val)))
    return {"symbol": meta.get("symbol") or symbol, "meta": meta, "series": series, "period": period}


def _fetch_chart(symbol: str, period: str = "1mo") -> dict[str, Any]:
    raw_p = (period or "1mo").strip().lower()
    p = raw_p if raw_p in PERIOD_INTERVAL else "1mo"
    interval = PERIOD_INTERVAL[p]
    sym_enc = urllib.parse.quote(symbol, safe="")

    if p == "7d":
        now_ts = int(time.time())
        url_7d = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}?period1={now_ts - 7 * 86400}&period2={now_ts}&interval={interval}"
        try:
            parsed = _parse_chart_response(symbol, p, _http_json(url_7d))
            if len(parsed["series"]) >= 2:
                return parsed
        except Exception:
            pass
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}?range=5d&interval=15m"
        parsed = _parse_chart_response(symbol, p, _http_json(url))
    else:
        yahoo_range = "1d" if p == "24h" else p
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}?range={yahoo_range}&interval={interval}"
        parsed = _parse_chart_response(symbol, p, _http_json(url))

    if not parsed["series"] and parsed["meta"].get("regularMarketPrice") is None:
        raise RuntimeError(f"Empty price series for `{symbol}`.")
    return parsed


def _round_price(val: float) -> float:
    av = abs(float(val))
    if av >= 100:
        return round(float(val), 2)
    if av >= 1:
        return round(float(val), 4)
    return round(float(val), 6)


def _downsample_series(series: list[tuple[datetime, float]], max_pts: int = 200) -> list[tuple[datetime, float]]:
    if len(series) <= max_pts or max_pts < 2:
        return list(series)
    step = (len(series) - 1) / (max_pts - 1)
    idxs = [round(i * step) for i in range(max_pts - 1)] + [len(series) - 1]
    seen: set[int] = set()
    out: list[tuple[datetime, float]] = []
    for idx in idxs:
        if idx not in seen:
            seen.add(idx)
            out.append(series[idx])
    return out


def _fetch_all_ranges(symbol: str, max_workers: int = 5) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {rk: pool.submit(_fetch_chart, symbol, rk) for rk in INTERACTIVE_RANGES}
        for rk, fut in futs.items():
            try:
                results[rk] = fut.result()
            except Exception:
                pass
    return results


def _safe_json_for_script(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


_CARD_BASE_CSS = """
  :root {
    color-scheme: dark light;
    --bg: rgba(29, 29, 29, 0.90);
    --surface: rgba(255, 255, 255, 0.045);
    --border: rgba(255, 255, 255, 0.09);
    --text: #ececec;
    --muted: #9b9b9b;
    --divider: rgba(255, 255, 255, 0.07);
    --grid: rgba(255, 255, 255, 0.07);
    --pill-bg: rgba(255, 255, 255, 0.06);
    --pill-active-bg: rgba(255, 255, 255, 0.14);
    --pill-active-fg: #ffffff;
    --tip-bg: rgba(23, 23, 23, 0.96);
    --tip-border: rgba(255, 255, 255, 0.14);
    --pos: #10b981;
    --pos-bg: rgba(16, 185, 129, 0.14);
    --neg: #f43f5e;
    --neg-bg: rgba(244, 63, 94, 0.14);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #fafafa;
      --surface: rgba(0, 0, 0, 0.04);
      --border: rgba(0, 0, 0, 0.09);
      --text: #171717;
      --muted: #666666;
      --divider: rgba(0, 0, 0, 0.07);
      --grid: rgba(0, 0, 0, 0.07);
      --pill-bg: rgba(0, 0, 0, 0.05);
      --pill-active-bg: rgba(0, 0, 0, 0.10);
      --pill-active-fg: #0d0d0d;
      --tip-bg: rgba(255, 255, 255, 0.97);
      --tip-border: rgba(0, 0, 0, 0.14);
      --pos: #059669;
      --pos-bg: rgba(5, 150, 105, 0.12);
      --neg: #e11d48;
      --neg-bg: rgba(225, 29, 72, 0.12);
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    background: transparent;
    font-family: "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    overflow: hidden;
    -webkit-font-smoothing: antialiased;
  }
  .card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 15px 18px 13px;
    margin: 2px 0 6px;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.16);
    user-select: none;
  }
  .top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    padding-bottom: 11px;
    border-bottom: 1px solid var(--divider);
  }
  .title-wrap { min-width: 0; flex: 1 1 210px; }
  .title-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .asset-name { font-size: 15.5px; font-weight: 700; letter-spacing: -0.01em; }
  .ticker-badge {
    font-size: 11px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 6px;
    background: var(--pill-bg);
    color: var(--muted);
    letter-spacing: 0.02em;
  }
  .sub { font-size: 11.5px; color: var(--muted); margin-top: 3px; }
  .price-wrap { text-align: right; flex-shrink: 0; }
  .price-line {
    font-size: 26px;
    font-weight: 750;
    letter-spacing: -0.025em;
    line-height: 1.05;
    font-variant-numeric: tabular-nums;
  }
  .price-ccy { font-size: 12.5px; font-weight: 600; color: var(--muted); margin-left: 4px; }
  .ret-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin-top: 5px;
    padding: 2.5px 8px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
  }
  .ret-pill.pos { color: var(--pos); background: var(--pos-bg); }
  .ret-pill.neg { color: var(--neg); background: var(--neg-bg); }
  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 10px;
  }
  .tabs {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--divider);
  }
  .tab-btn {
    border: none;
    background: transparent;
    color: var(--muted);
    font: inherit;
    font-size: 11.5px;
    font-weight: 650;
    padding: 5px 11px;
    border-radius: 7px;
    cursor: pointer;
    transition: background-color 0.14s ease, color 0.14s ease, opacity 0.14s ease;
  }
  .tab-btn:hover:not(:disabled) { color: var(--text); }
  .tab-btn[aria-selected="true"] {
    background: var(--pill-active-bg);
    color: var(--pill-active-fg);
  }
  .tab-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .hover-readout {
    font-size: 11.5px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    min-height: 16px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 9px;
  }
  .chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 9px;
    border-radius: 999px;
    background: var(--surface);
    border: 1px solid var(--divider);
    font-size: 11.5px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .chip-dot { width: 8px; height: 8px; border-radius: 999px; flex-shrink: 0; }
  .chip-ret.pos { color: var(--pos); }
  .chip-ret.neg { color: var(--neg); }
  .chart-wrap {
    position: relative;
    margin-top: 10px;
    width: 100%;
  }
  #chart-svg {
    width: 100%;
    height: auto;
    aspect-ratio: 680 / 230;
    display: block;
    touch-action: none;
    cursor: crosshair;
  }
  .axis-txt { fill: var(--muted); font-size: 10.5px; font-variant-numeric: tabular-nums; }
  .tip {
    position: absolute;
    top: 8px;
    pointer-events: none;
    background: var(--tip-bg);
    border: 1px solid var(--tip-border);
    border-radius: 10px;
    padding: 7px 10px;
    font-size: 11.5px;
    line-height: 1.45;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.25);
    backdrop-filter: blur(8px);
    opacity: 0;
    transition: opacity 0.08s ease;
    white-space: nowrap;
    z-index: 4;
    font-variant-numeric: tabular-nums;
  }
  .tip.show { opacity: 1; }
  .tip-time { font-weight: 700; margin-bottom: 2px; color: var(--text); }
  .tip-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .stats {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 14px;
    margin-top: 8px;
    padding-top: 9px;
    border-top: 1px solid var(--divider);
    font-size: 11.5px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .stats strong { color: var(--text); font-weight: 650; }
  @media (max-width: 520px) {
    .card { padding: 13px 13px 11px; }
    .price-line { font-size: 22px; }
    .tab-btn { padding: 5px 8px; font-size: 11px; }
  }
"""

_CHART_COMMON_JS = """
  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmtPrice(v, ccy) {
    if (v === null || v === undefined || isNaN(v)) return '—';
    var av = Math.abs(v);
    var dec = av >= 1000 ? 2 : (av >= 10 ? 2 : (av >= 1 ? 4 : 6));
    var s = Number(v).toLocaleString(undefined, { minimumFractionDigits: dec === 2 ? 2 : 0, maximumFractionDigits: dec });
    return ccy ? (s + ' ' + ccy) : s;
  }
  function fmtPct(v) {
    if (v === null || v === undefined || isNaN(v)) return '0.00%';
    return (v >= 0 ? '+' : '') + Number(v).toFixed(2) + '%';
  }
  function fmtTime(ms, rk, forTooltip) {
    var d = new Date(ms);
    if (rk === '24h') {
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    if (rk === '7d' && forTooltip) {
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
        d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    if (rk === '5y') {
      return forTooltip
        ? d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
        : d.toLocaleDateString([], { month: 'short', year: '2-digit' });
    }
    return forTooltip
      ? d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })
      : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
  }
  function reportHeight() {
    var el = document.getElementById('card');
    var h = el ? Math.ceil(el.getBoundingClientRect().height + 10) : document.documentElement.scrollHeight;
    try { parent.postMessage({ type: 'iframe:height', height: h }, '*'); } catch (e) {}
  }
  window.addEventListener('DOMContentLoaded', reportHeight);
  window.addEventListener('load', reportHeight);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(reportHeight).observe(document.body);
  }
  setTimeout(reportHeight, 60);
  setTimeout(reportHeight, 250);
"""


def _wrap_chart_card_html(title: str, body_html: str, payload: dict[str, Any], script_body: str) -> str:
    tabs_html = "".join(
        f'<button type="button" class="tab-btn" role="tab" data-range="{rk}" aria-selected="false">{RANGE_LABELS[rk]}</button>'
        for rk in INTERACTIVE_RANGES
    )
    doc = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        "<style>__CSS__</style>\n"
        "</head>\n<body>\n"
        '<div class="card" id="card">\n'
        "__BODY__\n"
        "</div>\n"
        "<script>\n"
        "  var DATA = __DATA__;\n"
        "__COMMON_JS__\n"
        "__SCRIPT__\n"
        "</script>\n</body>\n</html>"
    )
    return (
        doc.replace("__CSS__", _inter_font_face_css() + _CARD_BASE_CSS)
        .replace("__BODY__", body_html.replace("__TABS__", tabs_html))
        .replace("__DATA__", _safe_json_for_script(payload))
        .replace("__COMMON_JS__", _CHART_COMMON_JS)
        .replace("__SCRIPT__", script_body)
    )


def render_quote_html_card(payload: dict[str, Any]) -> str:
    name = html.escape(str(payload.get("name") or payload.get("symbol") or ""))
    sym = html.escape(str(payload.get("symbol") or ""))
    exch = html.escape(str(payload.get("exchange") or ""))
    ccy = html.escape(str(payload.get("currency") or ""))
    sub_text = f"{exch} · Interactive Market Chart" if exch else "Interactive Market Chart"
    w52_low = payload.get("fiftyTwoWeekLow")
    w52_high = payload.get("fiftyTwoWeekHigh")
    w52_span = ""
    if w52_low is not None and w52_high is not None:
        w52_span = f'<span>52W: <strong>{html.escape(str(round(float(w52_low), 4)))} – {html.escape(str(round(float(w52_high), 4)))}</strong></span>'
    retrieved = html.escape(str(payload.get("retrievedAt") or ""))

    body_html = f"""  <div class="top">
    <div class="title-wrap">
      <div class="title-row">
        <span class="asset-name">{name}</span>
        <span class="ticker-badge">{sym}</span>
      </div>
      <div class="sub">{sub_text}</div>
    </div>
    <div class="price-wrap">
      <div class="price-line"><span id="price-val">—</span><span class="price-ccy">{ccy}</span></div>
      <div><span class="ret-pill pos" id="ret-pill">—</span></div>
    </div>
  </div>
  <div class="toolbar">
    <div class="tabs" role="tablist" aria-label="Timeframe">__TABS__</div>
    <div class="hover-readout" id="hover-readout">Click range or hover chart</div>
  </div>
  <div class="chart-wrap">
    <svg id="chart-svg" viewBox="0 0 680 230" role="img" aria-label="{sym} price chart">
      <defs>
        <linearGradient id="area-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" id="grad-stop-top" stop-color="#10b981" stop-opacity="0.30"/>
          <stop offset="100%" id="grad-stop-bot" stop-color="#10b981" stop-opacity="0.01"/>
        </linearGradient>
      </defs>
      <g id="grid-layer"></g>
      <line id="base-line" stroke="var(--muted)" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>
      <path id="area-path" fill="url(#area-grad)"/>
      <path id="line-path" fill="none" stroke="#10b981" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>
      <line id="crosshair" y1="12" y2="198" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>
      <circle id="focus-dot" r="4.5" stroke="#1d1d1d" stroke-width="2" opacity="0"/>
    </svg>
    <div class="tip" id="hover-tip"></div>
  </div>
  <div class="stats" id="range-stats">
    <span id="stat-range">Range: <strong>—</strong></span>
    {w52_span}
    <span>Updated <strong>{retrieved}</strong></span>
  </div>"""

    script_body = r"""
  var VB_W = 680, VB_H = 230, PAD_L = 10, PAD_R = 64, PAD_T = 14, PAD_B = 28;
  var PLOT_W = VB_W - PAD_L - PAD_R, PLOT_H = VB_H - PAD_T - PAD_B;
  var activeRange = DATA.defaultRange || '1mo';
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab-btn'));
  var svg = document.getElementById('chart-svg');
  var gridLayer = document.getElementById('grid-layer');
  var baseLine = document.getElementById('base-line');
  var areaPath = document.getElementById('area-path');
  var linePath = document.getElementById('line-path');
  var gradTop = document.getElementById('grad-stop-top');
  var gradBot = document.getElementById('grad-stop-bot');
  var crosshair = document.getElementById('crosshair');
  var focusDot = document.getElementById('focus-dot');
  var tip = document.getElementById('hover-tip');
  var priceEl = document.getElementById('price-val');
  var retPill = document.getElementById('ret-pill');
  var readout = document.getElementById('hover-readout');
  var statRange = document.getElementById('stat-range');
  var curCoords = [];
  var curSeries = null;

  tabs.forEach(function (btn) {
    var rk = btn.getAttribute('data-range');
    var rd = (DATA.ranges || {})[rk];
    if (!rd || !rd.pts || rd.pts.length < 2) {
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
    } else {
      btn.addEventListener('click', function () { selectRange(rk); });
    }
  });

  if (!((DATA.ranges || {})[activeRange] && DATA.ranges[activeRange].pts && DATA.ranges[activeRange].pts.length >= 2)) {
    for (var i = 0; i < tabs.length; i++) {
      if (!tabs[i].disabled) { activeRange = tabs[i].getAttribute('data-range'); break; }
    }
  }

  function updateHeader(price, baseline, labelText) {
    priceEl.textContent = fmtPrice(price, '');
    var delta = price - baseline;
    var pct = baseline ? (delta / baseline) * 100 : 0;
    var sign = delta >= 0 ? '+' : '';
    retPill.className = 'ret-pill ' + (pct >= 0 ? 'pos' : 'neg');
    retPill.textContent = fmtPct(pct) + ' (' + sign + fmtPrice(delta, '') + ') · ' + labelText;
  }

  function resetHover() {
    crosshair.setAttribute('opacity', '0');
    focusDot.setAttribute('opacity', '0');
    tip.classList.remove('show');
    if (curSeries) {
      var lastVal = (activeRange === '24h' && DATA.price) ? DATA.price : curSeries.pts[curSeries.pts.length - 1][1];
      updateHeader(lastVal, curSeries.baseline, curSeries.label);
      readout.textContent = 'Hover chart for exact values · ' + curSeries.label;
    }
  }

  function selectRange(rk) {
    var rd = (DATA.ranges || {})[rk];
    if (!rd || !rd.pts || rd.pts.length < 2) return;
    activeRange = rk;
    curSeries = rd;
    tabs.forEach(function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-range') === rk ? 'true' : 'false');
    });
    var pts = rd.pts;
    var minY = Infinity, maxY = -Infinity;
    for (var i = 0; i < pts.length; i++) {
      if (pts[i][1] < minY) minY = pts[i][1];
      if (pts[i][1] > maxY) maxY = pts[i][1];
    }
    if (rk === '24h' && isFinite(rd.baseline)) {
      if (rd.baseline < minY) minY = rd.baseline;
      if (rd.baseline > maxY) maxY = rd.baseline;
    }
    var spanY = maxY - minY;
    var padY = spanY > 0 ? spanY * 0.08 : (Math.abs(maxY) * 0.01 || 1);
    var lo = minY - padY, hi = maxY + padY;
    var t0 = pts[0][0], t1 = pts[pts.length - 1][0], dt = Math.max(1, t1 - t0);

    function px(ts) { return PAD_L + ((ts - t0) / dt) * PLOT_W; }
    function py(val) { return PAD_T + PLOT_H - ((val - lo) / (hi - lo)) * PLOT_H; }

    var isPos = (rd.retPct >= 0);
    var col = isPos ? 'var(--pos)' : 'var(--neg)';
    var hexCol = isPos ? '#10b981' : '#f43f5e';
    linePath.setAttribute('stroke', col);
    gradTop.setAttribute('stop-color', hexCol);
    gradBot.setAttribute('stop-color', hexCol);
    focusDot.setAttribute('fill', hexCol);

    curCoords = [];
    var dLine = '', dArea = '';
    for (var j = 0; j < pts.length; j++) {
      var x = px(pts[j][0]), y = py(pts[j][1]);
      curCoords.push([x, y, pts[j][0], pts[j][1]]);
      dLine += (j === 0 ? 'M' : 'L') + x.toFixed(1) + ' ' + y.toFixed(1);
    }
    var firstX = curCoords[0][0].toFixed(1);
    var lastX = curCoords[curCoords.length - 1][0].toFixed(1);
    var botY = (PAD_T + PLOT_H).toFixed(1);
    dArea = dLine + ' L' + lastX + ' ' + botY + ' L' + firstX + ' ' + botY + ' Z';
    linePath.setAttribute('d', dLine);
    areaPath.setAttribute('d', dArea);

    if (rd.baseline >= lo && rd.baseline <= hi) {
      var by = py(rd.baseline).toFixed(1);
      baseLine.setAttribute('x1', PAD_L);
      baseLine.setAttribute('x2', PAD_L + PLOT_W);
      baseLine.setAttribute('y1', by);
      baseLine.setAttribute('y2', by);
      baseLine.setAttribute('opacity', '0.55');
    } else {
      baseLine.setAttribute('opacity', '0');
    }

    var gHtml = '';
    for (var gi = 0; gi < 4; gi++) {
      var frac = gi / 3;
      var gy = (PAD_T + frac * PLOT_H).toFixed(1);
      var gVal = hi - frac * (hi - lo);
      gHtml += '<line x1="' + PAD_L + '" x2="' + (PAD_L + PLOT_W) + '" y1="' + gy + '" y2="' + gy + '" stroke="var(--grid)" stroke-width="1"/>';
      gHtml += '<text x="' + (PAD_L + PLOT_W + 6) + '" y="' + (Number(gy) + 3.5).toFixed(1) + '" class="axis-txt">' + fmtPrice(gVal, '') + '</text>';
    }
    var xTicks = 5;
    for (var xi = 0; xi < xTicks; xi++) {
      var pIdx = Math.round((xi / (xTicks - 1)) * (pts.length - 1));
      var xPos = px(pts[pIdx][0]).toFixed(1);
      var anc = xi === 0 ? 'start' : (xi === xTicks - 1 ? 'end' : 'middle');
      gHtml += '<text x="' + xPos + '" y="' + (VB_H - 6) + '" text-anchor="' + anc + '" class="axis-txt">' + fmtTime(pts[pIdx][0], rk, false) + '</text>';
    }
    gridLayer.innerHTML = gHtml;

    statRange.innerHTML = escHtml(rd.label) + ' Low–High: <strong>' + escHtml(fmtPrice(rd.low, '') + ' – ' + fmtPrice(rd.high, DATA.currency)) + '</strong>';
    resetHover();
    reportHeight();
  }

  function onPointerMove(ev) {
    if (!curCoords.length) return;
    var rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    var xView = ((ev.clientX - rect.left) / rect.width) * VB_W;
    var bestIdx = 0, bestDist = Infinity;
    for (var i = 0; i < curCoords.length; i++) {
      var d = Math.abs(curCoords[i][0] - xView);
      if (d < bestDist) { bestDist = d; bestIdx = i; }
    }
    var pt = curCoords[bestIdx];
    crosshair.setAttribute('x1', pt[0].toFixed(1));
    crosshair.setAttribute('x2', pt[0].toFixed(1));
    crosshair.setAttribute('opacity', '0.75');
    focusDot.setAttribute('cx', pt[0].toFixed(1));
    focusDot.setAttribute('cy', pt[1].toFixed(1));
    focusDot.setAttribute('opacity', '1');

    var delta = pt[3] - curSeries.baseline;
    var pct = curSeries.baseline ? (delta / curSeries.baseline) * 100 : 0;
    var timeStr = fmtTime(pt[2], activeRange, true);
    updateHeader(pt[3], curSeries.baseline, timeStr);
    readout.textContent = timeStr;

    tip.innerHTML = '<div class="tip-time">' + escHtml(timeStr) + '</div>' +
      '<div class="tip-row"><span>' + escHtml(DATA.symbol) + '</span><strong>' + escHtml(fmtPrice(pt[3], DATA.currency)) + '</strong></div>' +
      '<div class="tip-row"><span>Change</span><span style="color:' + (pct >= 0 ? 'var(--pos)' : 'var(--neg)') + ';font-weight:650">' + escHtml(fmtPct(pct)) + '</span></div>';
    tip.classList.add('show');
    var tipW = tip.offsetWidth || 150;
    var pxLeft = (pt[0] / VB_W) * rect.width;
    var placeLeft = (pxLeft + 14 + tipW > rect.width) ? Math.max(4, pxLeft - tipW - 14) : (pxLeft + 14);
    tip.style.left = placeLeft.toFixed(0) + 'px';
  }

  svg.addEventListener('pointermove', onPointerMove);
  svg.addEventListener('pointerdown', onPointerMove);
  svg.addEventListener('pointerleave', resetHover);
  svg.addEventListener('pointercancel', resetHover);
  selectRange(activeRange);
"""
    return _wrap_chart_card_html(f"{payload.get('symbol', '')} Interactive Chart", body_html, payload, script_body)


def render_compare_html_card(payload: dict[str, Any]) -> str:
    items = payload.get("items") or []
    chips_html = "".join(
        f'<div class="chip" data-sym="{html.escape(str(it.get("symbol") or ""))}">'
        f'<span class="chip-dot" style="background:{COMPARE_PALETTE[idx % len(COMPARE_PALETTE)]}"></span>'
        f'<span>{html.escape(str(it.get("symbol") or ""))}</span>'
        f'<span class="chip-ret" data-chip-ret="{idx}">—</span>'
        f"</div>"
        for idx, it in enumerate(items)
    )
    retrieved = html.escape(str(payload.get("retrievedAt") or ""))

    body_html = f"""  <div class="top">
    <div class="title-wrap">
      <div class="title-row">
        <span class="asset-name">Normalized Performance Comparison</span>
      </div>
      <div class="sub">Interactive multi-asset % return across 5 timeframes</div>
    </div>
    <div class="price-wrap">
      <div><span class="ret-pill pos" id="ret-pill">—</span></div>
    </div>
  </div>
  <div class="chips" id="chips-row">{chips_html}</div>
  <div class="toolbar">
    <div class="tabs" role="tablist" aria-label="Timeframe">__TABS__</div>
    <div class="hover-readout" id="hover-readout">Click range or hover chart</div>
  </div>
  <div class="chart-wrap">
    <svg id="chart-svg" viewBox="0 0 680 230" role="img" aria-label="Multi-ticker performance comparison chart">
      <g id="grid-layer"></g>
      <line id="zero-line" stroke="var(--muted)" stroke-width="1" stroke-dasharray="4 4" opacity="0.65"/>
      <g id="lines-layer"></g>
      <line id="crosshair" y1="12" y2="198" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity="0"/>
      <g id="dots-layer"></g>
    </svg>
    <div class="tip" id="hover-tip"></div>
  </div>
  <div class="stats" id="range-stats">
    <span id="stat-best">Leader: <strong>—</strong></span>
    <span>Updated <strong>{retrieved}</strong></span>
  </div>"""

    script_body = r"""
  var VB_W = 680, VB_H = 230, PAD_L = 10, PAD_R = 58, PAD_T = 14, PAD_B = 28;
  var PLOT_W = VB_W - PAD_L - PAD_R, PLOT_H = VB_H - PAD_T - PAD_B;
  var PALETTE = DATA.palette || ['#38bdf8', '#10b981', '#f59e0b', '#ec4899', '#a78bfa'];
  var activeRange = DATA.defaultRange || '1y';
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab-btn'));
  var svg = document.getElementById('chart-svg');
  var gridLayer = document.getElementById('grid-layer');
  var zeroLine = document.getElementById('zero-line');
  var linesLayer = document.getElementById('lines-layer');
  var dotsLayer = document.getElementById('dots-layer');
  var crosshair = document.getElementById('crosshair');
  var tip = document.getElementById('hover-tip');
  var retPill = document.getElementById('ret-pill');
  var readout = document.getElementById('hover-readout');
  var statBest = document.getElementById('stat-best');
  var curState = null;

  tabs.forEach(function (btn) {
    var rk = btn.getAttribute('data-range');
    var rd = (DATA.ranges || {})[rk];
    var validCount = 0;
    if (rd && rd.series) {
      rd.series.forEach(function (s) { if (s.pts && s.pts.length >= 2) validCount++; });
    }
    if (!validCount) {
      btn.disabled = true;
      btn.setAttribute('aria-disabled', 'true');
    } else {
      btn.addEventListener('click', function () { selectRange(rk); });
    }
  });

  if (!((DATA.ranges || {})[activeRange])) {
    for (var i = 0; i < tabs.length; i++) {
      if (!tabs[i].disabled) { activeRange = tabs[i].getAttribute('data-range'); break; }
    }
  }

  function updateChips(retsByIdx) {
    (DATA.items || []).forEach(function (it, idx) {
      var el = document.querySelector('[data-chip-ret="' + idx + '"]');
      if (!el) return;
      var val = retsByIdx[idx];
      if (val === null || val === undefined || isNaN(val)) {
        el.className = 'chip-ret';
        el.textContent = 'n/a';
      } else {
        el.className = 'chip-ret ' + (val >= 0 ? 'pos' : 'neg');
        el.textContent = fmtPct(val);
      }
    });
  }

  function resetHover() {
    crosshair.setAttribute('opacity', '0');
    dotsLayer.innerHTML = '';
    tip.classList.remove('show');
    if (curState) {
      updateChips(curState.endRets);
      readout.textContent = 'Hover chart to inspect dates · ' + curState.label;
      if (curState.best) {
        retPill.className = 'ret-pill ' + (curState.best.ret >= 0 ? 'pos' : 'neg');
        retPill.textContent = curState.best.symbol + ' ' + fmtPct(curState.best.ret) + ' · ' + curState.label;
      }
    }
  }

  function selectRange(rk) {
    var rd = (DATA.ranges || {})[rk];
    if (!rd || !rd.series) return;
    activeRange = rk;
    tabs.forEach(function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-range') === rk ? 'true' : 'false');
    });

    var minT = Infinity, maxT = -Infinity, minY = 0, maxY = 0;
    rd.series.forEach(function (s) {
      (s.pts || []).forEach(function (pt) {
        if (pt[0] < minT) minT = pt[0];
        if (pt[0] > maxT) maxT = pt[0];
        if (pt[2] < minY) minY = pt[2];
        if (pt[2] > maxY) maxY = pt[2];
      });
    });
    if (!isFinite(minT)) return;
    var spanY = maxY - minY;
    var padY = spanY > 0 ? spanY * 0.1 : 2.0;
    var lo = minY - padY, hi = maxY + padY;
    var dt = Math.max(1, maxT - minT);

    function px(ts) { return PAD_L + ((ts - minT) / dt) * PLOT_W; }
    function py(val) { return PAD_T + PLOT_H - ((val - lo) / (hi - lo)) * PLOT_H; }

    var linesHtml = '';
    var endRets = {};
    var best = null;
    rd.series.forEach(function (s, idx) {
      var pts = s.pts || [];
      if (pts.length < 2) { endRets[idx] = null; return; }
      var col = PALETTE[idx % PALETTE.length];
      var d = '';
      for (var i = 0; i < pts.length; i++) {
        d += (i === 0 ? 'M' : 'L') + px(pts[i][0]).toFixed(1) + ' ' + py(pts[i][2]).toFixed(1);
      }
      linesHtml += '<path class="line-path" data-sym="' + escHtml(s.symbol) + '" d="' + d + '" fill="none" stroke="' + col + '" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/>';
      endRets[idx] = s.retPct;
      if (!best || s.retPct > best.ret) best = { symbol: s.symbol, ret: s.retPct };
    });
    linesLayer.innerHTML = linesHtml;

    var zy = py(0).toFixed(1);
    zeroLine.setAttribute('x1', PAD_L);
    zeroLine.setAttribute('x2', PAD_L + PLOT_W);
    zeroLine.setAttribute('y1', zy);
    zeroLine.setAttribute('y2', zy);

    var gHtml = '';
    for (var gi = 0; gi < 4; gi++) {
      var frac = gi / 3;
      var gy = (PAD_T + frac * PLOT_H).toFixed(1);
      var gVal = hi - frac * (hi - lo);
      gHtml += '<line x1="' + PAD_L + '" x2="' + (PAD_L + PLOT_W) + '" y1="' + gy + '" y2="' + gy + '" stroke="var(--grid)" stroke-width="1"/>';
      gHtml += '<text x="' + (PAD_L + PLOT_W + 6) + '" y="' + (Number(gy) + 3.5).toFixed(1) + '" class="axis-txt">' + escHtml(fmtPct(gVal)) + '</text>';
    }
    for (var xi = 0; xi < 5; xi++) {
      var ts = minT + (xi / 4) * dt;
      var xPos = px(ts).toFixed(1);
      var anc = xi === 0 ? 'start' : (xi === 4 ? 'end' : 'middle');
      gHtml += '<text x="' + xPos + '" y="' + (VB_H - 6) + '" text-anchor="' + anc + '" class="axis-txt">' + escHtml(fmtTime(ts, rk, false)) + '</text>';
    }
    gridLayer.innerHTML = gHtml;

    curState = { rd: rd, label: rd.label, minT: minT, maxT: maxT, dt: dt, px: px, py: py, endRets: endRets, best: best };
    if (best) {
      statBest.innerHTML = 'Leader (' + escHtml(rd.label) + '): <strong>' + escHtml(best.symbol + ' ' + fmtPct(best.ret)) + '</strong>';
    }
    resetHover();
    reportHeight();
  }

  function onPointerMove(ev) {
    if (!curState) return;
    var rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    var xView = ((ev.clientX - rect.left) / rect.width) * VB_W;
    var frac = Math.max(0, Math.min(1, (xView - PAD_L) / PLOT_W));
    var targetTs = curState.minT + frac * curState.dt;
    var cx = PAD_L + frac * PLOT_W;
    crosshair.setAttribute('x1', cx.toFixed(1));
    crosshair.setAttribute('x2', cx.toFixed(1));
    crosshair.setAttribute('opacity', '0.75');

    var dotsHtml = '';
    var tipRows = '';
    var hoverRets = {};
    var snapTs = targetTs;
    curState.rd.series.forEach(function (s, idx) {
      var pts = s.pts || [];
      if (pts.length < 2) { hoverRets[idx] = null; return; }
      var bestPt = pts[0], bestD = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i][0] - targetTs);
        if (d < bestD) { bestD = d; bestPt = pts[i]; }
      }
      if (idx === 0) snapTs = bestPt[0];
      var col = PALETTE[idx % PALETTE.length];
      var dx = curState.px(bestPt[0]).toFixed(1), dy = curState.py(bestPt[2]).toFixed(1);
      dotsHtml += '<circle cx="' + dx + '" cy="' + dy + '" r="4" fill="' + col + '" stroke="#1d1d1d" stroke-width="1.8"/>';
      hoverRets[idx] = bestPt[2];
      tipRows += '<div class="tip-row"><span><span style="color:' + col + '">●</span> ' + escHtml(s.symbol) + '</span>' +
        '<strong>' + escHtml(fmtPct(bestPt[2])) + ' <span style="font-weight:500;color:var(--muted)">(' + escHtml(fmtPrice(bestPt[1], s.currency)) + ')</span></strong></div>';
    });
    dotsLayer.innerHTML = dotsHtml;
    updateChips(hoverRets);

    var timeStr = fmtTime(snapTs, activeRange, true);
    readout.textContent = timeStr;
    tip.innerHTML = '<div class="tip-time">' + escHtml(timeStr) + '</div>' + tipRows;
    tip.classList.add('show');
    var tipW = tip.offsetWidth || 185;
    var pxLeft = (cx / VB_W) * rect.width;
    var placeLeft = (pxLeft + 14 + tipW > rect.width) ? Math.max(4, pxLeft - tipW - 14) : (pxLeft + 14);
    tip.style.left = placeLeft.toFixed(0) + 'px';
  }

  svg.addEventListener('pointermove', onPointerMove);
  svg.addEventListener('pointerdown', onPointerMove);
  svg.addEventListener('pointerleave', resetHover);
  svg.addEventListener('pointercancel', resetHover);
  selectRange(activeRange);
"""
    symbols_title = " vs ".join(str(it.get("symbol") or "") for it in items)
    return _wrap_chart_card_html(f"{symbols_title} Performance Comparison", body_html, payload, script_body)


def _render_quote_chart(chart_data: dict[str, Any], out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    series = chart_data["series"]
    if len(series) < 2:
        return
    meta = chart_data["meta"]
    dates = [d for d, _ in series]
    vals = [v for _, v in series]
    up = vals[-1] >= vals[0]
    line_col = "#10B981" if up else "#F43F5E"
    fill_col = "#10B981" if up else "#F43F5E"

    fig, ax = plt.subplots(figsize=(9.5, 4.8), dpi=300)
    fig.patch.set_facecolor("#0F172A")
    ax.set_facecolor("#0F172A")
    ax.plot(dates, vals, color=line_col, linewidth=2.2, label=chart_data["symbol"])
    ax.fill_between(dates, vals, min(vals) * 0.995, color=fill_col, alpha=0.14)

    if len(vals) >= 20:
        ma = [sum(vals[max(0, i - 19) : i + 1]) / min(20, i + 1) for i in range(len(vals))]
        ax.plot(dates, ma, color="#94A3B8", linewidth=1.2, linestyle="--", alpha=0.85, label="20-bar MA")

    cur_price = meta.get("regularMarketPrice") or vals[-1]
    ccy = meta.get("currency") or ""
    pct = ((vals[-1] - vals[0]) / vals[0] * 100.0) if vals[0] else 0.0
    sign = "+" if pct >= 0 else ""
    name = meta.get("shortName") or meta.get("longName") or chart_data["symbol"]
    ax.set_title(
        f"{name} ({chart_data['symbol']})  ·  {cur_price:,.4g} {ccy}  ({sign}{pct:.2f}% over {chart_data['period']})",
        color="#F8FAFC",
        fontsize=12.5,
        fontweight="bold",
        loc="left",
        pad=12,
    )
    ax.tick_params(colors="#94A3B8", labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#334155")
    ax.grid(True, color="#1E293B", linestyle="-", linewidth=0.7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M" if chart_data["period"] in ("1d", "5d") else "%d %b"))
    ax.legend(facecolor="#1E293B", edgecolor="#334155", labelcolor="#E2E8F0", fontsize=8.5, loc="upper left")
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_png), dpi=300, facecolor=fig.get_facecolor())
    plt.close(fig)


def _render_compare_chart(items: list[dict[str, Any]], period: str, out_png: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    palette = ["#38BDF8", "#10B981", "#F59E0B", "#EC4899", "#A78BFA"]
    fig, ax = plt.subplots(figsize=(9.5, 4.8), dpi=300)
    fig.patch.set_facecolor("#0F172A")
    ax.set_facecolor("#0F172A")
    ax.axhline(0, color="#475569", linewidth=1.0, linestyle=":")

    for idx, ch in enumerate(items):
        series = ch["series"]
        if len(series) < 2 or not series[0][1]:
            continue
        base = series[0][1]
        dates = [d for d, _ in series]
        rets = [((v - base) / base) * 100.0 for _, v in series]
        col = palette[idx % len(palette)]
        ax.plot(dates, rets, color=col, linewidth=2.1, label=f"{ch['symbol']} ({rets[-1]:+.1f}%)")

    ax.set_title(f"Normalized Performance Comparison ({period})", color="#F8FAFC", fontsize=12.5, fontweight="bold", loc="left", pad=12)
    ax.set_ylabel("Return (%)", color="#94A3B8", fontsize=9.5)
    ax.tick_params(colors="#94A3B8", labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("bottom", "left"):
        ax.spines[spine].set_color("#334155")
    ax.grid(True, color="#1E293B", linewidth=0.7)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.legend(facecolor="#1E293B", edgecolor="#334155", labelcolor="#E2E8F0", fontsize=9, loc="upper left")
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_png), dpi=300, facecolor=fig.get_facecolor())
    plt.close(fig)


def _build_quote_ranges_payload(
    sym: str,
    ranges_map: dict[str, dict[str, Any]],
    fallback_ch: dict[str, Any],
    price: float,
    prev_close: float,
) -> tuple[dict[str, Any], dict[str, float]]:
    ranges_out: dict[str, Any] = {}
    range_returns: dict[str, float] = {}
    for rk in INTERACTIVE_RANGES:
        rch = ranges_map.get(rk)
        if not rch or len(rch.get("series") or []) < 2:
            continue
        ds = _downsample_series(rch["series"], max_pts=200)
        pts = [[int(dt.timestamp() * 1000), _round_price(val)] for dt, val in ds]
        vals = [val for _, val in ds]
        rmeta = rch.get("meta") or {}
        if rk == "24h":
            baseline = float(rmeta.get("previousClose") or rmeta.get("chartPreviousClose") or prev_close or pts[0][1])
            end_val = float(price or pts[-1][1])
        else:
            baseline = float(pts[0][1])
            end_val = float(pts[-1][1])
        ret_pct = round(((end_val - baseline) / baseline * 100.0) if baseline else 0.0, 2)
        ranges_out[rk] = {
            "label": RANGE_LABELS[rk],
            "baseline": _round_price(baseline),
            "retPct": ret_pct,
            "delta": _round_price(end_val - baseline),
            "low": _round_price(min(vals)),
            "high": _round_price(max(vals)),
            "pts": pts,
        }
        range_returns[RANGE_LABELS[rk]] = ret_pct
    if not ranges_out and len(fallback_ch.get("series") or []) >= 2:
        rk = _normalize_range_key(fallback_ch.get("period"))
        ds = _downsample_series(fallback_ch["series"], max_pts=200)
        pts = [[int(dt.timestamp() * 1000), _round_price(val)] for dt, val in ds]
        vals = [val for _, val in ds]
        baseline = prev_close if rk == "24h" else float(pts[0][1])
        end_val = float(price or pts[-1][1]) if rk == "24h" else float(pts[-1][1])
        ret_pct = round(((end_val - baseline) / baseline * 100.0) if baseline else 0.0, 2)
        ranges_out[rk] = {
            "label": RANGE_LABELS[rk],
            "baseline": _round_price(baseline),
            "retPct": ret_pct,
            "delta": _round_price(end_val - baseline),
            "low": _round_price(min(vals)),
            "high": _round_price(max(vals)),
            "pts": pts,
        }
        range_returns[RANGE_LABELS[rk]] = ret_pct
    return ranges_out, range_returns


def cmd_quote(symbol_or_query: str, period: str, chart_out: str | None, html_out: str | None = None) -> dict[str, Any]:
    sym = resolve_symbol(symbol_or_query)
    raw_p = (period or "1mo").strip().lower()
    p_norm = raw_p if raw_p in PERIOD_INTERVAL else "1mo"
    ranges_map: dict[str, dict[str, Any]] = {}
    if html_out:
        ranges_map = _fetch_all_ranges(sym)
        if p_norm in ranges_map:
            ch = ranges_map[p_norm]
        elif p_norm == "1d" and "24h" in ranges_map:
            ch = ranges_map["24h"]
        else:
            ch = _fetch_chart(sym, p_norm)
    else:
        ch = _fetch_chart(sym, p_norm)

    meta = ch["meta"]
    series = ch["series"]
    price = float(meta.get("regularMarketPrice") or (series[-1][1] if series else 0.0))
    meta_24h = (ranges_map.get("24h") or {}).get("meta") or meta
    prev_close = float(
        meta.get("previousClose")
        or meta_24h.get("previousClose")
        or meta_24h.get("chartPreviousClose")
        or meta.get("chartPreviousClose")
        or (series[0][1] if series else price)
    )
    day_chg = price - prev_close
    day_pct = (day_chg / prev_close * 100.0) if prev_close else 0.0
    p_start = series[0][1] if series else price
    p_ret_pct = ((price - p_start) / p_start * 100.0) if p_start else 0.0
    vals = [v for _, v in series] or [price]
    retrieved_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    chart_path = None
    if chart_out and len(series) >= 2:
        out_p = Path(chart_out)
        _render_quote_chart(ch, out_p)
        if out_p.is_file():
            chart_path = str(out_p)

    html_path = None
    range_returns: dict[str, float] = {}
    if html_out:
        ranges_out, range_returns = _build_quote_ranges_payload(ch["symbol"], ranges_map, ch, price, prev_close)
        if ranges_out:
            card_payload = {
                "symbol": ch["symbol"],
                "name": meta.get("shortName") or meta.get("longName") or ch["symbol"],
                "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
                "currency": meta.get("currency") or "",
                "price": _round_price(price),
                "previousClose": _round_price(prev_close),
                "fiftyTwoWeekLow": meta.get("fiftyTwoWeekLow"),
                "fiftyTwoWeekHigh": meta.get("fiftyTwoWeekHigh"),
                "retrievedAt": retrieved_at,
                "defaultRange": _normalize_range_key(raw_p),
                "ranges": ranges_out,
            }
            out_h = Path(html_out)
            out_h.parent.mkdir(parents=True, exist_ok=True)
            out_h.write_text(render_quote_html_card(card_payload), encoding="utf-8")
            if out_h.is_file():
                html_path = str(out_h)

    res: dict[str, Any] = {
        "ok": True,
        "symbol": ch["symbol"],
        "name": meta.get("shortName") or meta.get("longName") or ch["symbol"],
        "currency": meta.get("currency") or "",
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        "period": ch["period"],
        "price": round(price, 4),
        "previous_close": round(prev_close, 4),
        "day_change": round(day_chg, 4),
        "day_change_pct": round(day_pct, 2),
        "period_return_pct": round(p_ret_pct, 2),
        "period_low": round(min(vals), 4),
        "period_high": round(max(vals), 4),
        "fifty_two_week_low": meta.get("fiftyTwoWeekLow"),
        "fifty_two_week_high": meta.get("fiftyTwoWeekHigh"),
        "chart_path": chart_path,
        "retrieved_at": retrieved_at,
    }
    if html_path:
        res["html_path"] = html_path
        res["range_returns"] = range_returns
    return res


def cmd_compare(symbols_csv: str, period: str, chart_out: str | None, html_out: str | None = None) -> dict[str, Any]:
    raw_items = [x.strip() for x in re.split(r"[,;]+", symbols_csv or "") if x.strip()][:5]
    if len(raw_items) < 2:
        raise ValueError("Provide at least 2 comma-separated tickers to compare.")
    raw_p = (period or "6mo").strip().lower()
    p_norm = raw_p if raw_p in PERIOD_INTERVAL else "6mo"
    syms = [resolve_symbol(item) for item in raw_items]
    retrieved_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    per_sym_ranges: list[dict[str, dict[str, Any]]] = [{} for _ in syms]
    base_charts: list[dict[str, Any] | None] = [None for _ in syms]

    if html_out:
        fetch_periods = list(INTERACTIVE_RANGES)
        if p_norm not in fetch_periods and not (p_norm == "1d"):
            fetch_periods.append(p_norm)
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {(idx, rk): pool.submit(_fetch_chart, sym, rk) for idx, sym in enumerate(syms) for rk in fetch_periods}
            for (idx, rk), fut in futs.items():
                try:
                    ch_r = fut.result()
                    if rk in INTERACTIVE_RANGES:
                        per_sym_ranges[idx][rk] = ch_r
                    if rk == p_norm or (p_norm == "1d" and rk == "24h"):
                        base_charts[idx] = ch_r
                except Exception:
                    pass
    else:
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = {idx: pool.submit(_fetch_chart, sym, p_norm) for idx, sym in enumerate(syms)}
            for idx, fut in futs.items():
                base_charts[idx] = fut.result()

    charts: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for idx, sym in enumerate(syms):
        ch = base_charts[idx] or per_sym_ranges[idx].get(_normalize_range_key(p_norm)) or _fetch_chart(sym, p_norm)
        charts.append(ch)
        meta = ch["meta"]
        series = ch["series"]
        price = float(meta.get("regularMarketPrice") or (series[-1][1] if series else 0.0))
        p_start = series[0][1] if series else price
        p_ret = ((price - p_start) / p_start * 100.0) if p_start else 0.0
        row: dict[str, Any] = {
            "symbol": ch["symbol"],
            "name": meta.get("shortName") or ch["symbol"],
            "currency": meta.get("currency") or "",
            "price": round(price, 4),
            "period_return_pct": round(p_ret, 2),
        }
        if html_out:
            item_rets: dict[str, float] = {}
            for rk in INTERACTIVE_RANGES:
                rch = per_sym_ranges[idx].get(rk)
                if rch and len(rch.get("series") or []) >= 2:
                    s0 = float(rch["series"][0][1])
                    end_v = float(price if rk == "24h" and price else rch["series"][-1][1])
                    base_v = float((rch.get("meta") or {}).get("chartPreviousClose") or (rch.get("meta") or {}).get("previousClose") or s0) if rk == "24h" else s0
                    item_rets[RANGE_LABELS[rk]] = round(((end_v - base_v) / base_v * 100.0) if base_v else 0.0, 2)
            row["range_returns"] = item_rets
        rows.append(row)

    chart_path = None
    if chart_out:
        out_p = Path(chart_out)
        _render_compare_chart(charts, p_norm, out_p)
        if out_p.is_file():
            chart_path = str(out_p)

    html_path = None
    if html_out:
        ranges_out: dict[str, Any] = {}
        for rk in INTERACTIVE_RANGES:
            series_list = []
            for idx, sym in enumerate(syms):
                rch = per_sym_ranges[idx].get(rk)
                canonical_sym = rows[idx]["symbol"]
                ccy = rows[idx]["currency"]
                if not rch or len(rch.get("series") or []) < 2:
                    series_list.append({"symbol": canonical_sym, "currency": ccy, "retPct": None, "pts": []})
                    continue
                ds = _downsample_series(rch["series"], max_pts=200)
                rmeta = rch.get("meta") or {}
                if rk == "24h":
                    base_v = float(rmeta.get("chartPreviousClose") or rmeta.get("previousClose") or ds[0][1])
                else:
                    base_v = float(ds[0][1])
                pts = [
                    [int(dt.timestamp() * 1000), _round_price(val), round(((val - base_v) / base_v * 100.0) if base_v else 0.0, 2)]
                    for dt, val in ds
                ]
                end_v = float(rows[idx]["price"] if rk == "24h" and rows[idx]["price"] else ds[-1][1])
                ret_pct = round(((end_v - base_v) / base_v * 100.0) if base_v else 0.0, 2)
                series_list.append({"symbol": canonical_sym, "currency": ccy, "retPct": ret_pct, "pts": pts})
            if any(len(s["pts"]) >= 2 for s in series_list):
                ranges_out[rk] = {"label": RANGE_LABELS[rk], "series": series_list}
        if ranges_out:
            card_payload = {
                "retrievedAt": retrieved_at,
                "defaultRange": _normalize_range_key(raw_p),
                "palette": list(COMPARE_PALETTE),
                "items": rows,
                "ranges": ranges_out,
            }
            out_h = Path(html_out)
            out_h.parent.mkdir(parents=True, exist_ok=True)
            out_h.write_text(render_compare_html_card(card_payload), encoding="utf-8")
            if out_h.is_file():
                html_path = str(out_h)

    res: dict[str, Any] = {"ok": True, "period": period, "items": rows, "chart_path": chart_path, "retrieved_at": retrieved_at}
    if html_path:
        res["html_path"] = html_path
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Live Market & Finance CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pq = sub.add_parser("quote")
    pq.add_argument("--symbol", required=True)
    pq.add_argument("--period", default="1mo")
    pq.add_argument("--chart-out", default="")
    pq.add_argument("--html-out", default="")

    pc = sub.add_parser("compare")
    pc.add_argument("--symbols", required=True)
    pc.add_argument("--period", default="6mo")
    pc.add_argument("--chart-out", default="")
    pc.add_argument("--html-out", default="")

    args = ap.parse_args()
    try:
        if args.cmd == "quote":
            res = cmd_quote(args.symbol, args.period, args.chart_out or None, html_out=args.html_out or None)
        else:
            res = cmd_compare(args.symbols, args.period, args.chart_out or None, html_out=args.html_out or None)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
