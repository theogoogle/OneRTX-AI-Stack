"""
Live Market & Finance CLI + 300-DPI chart builder (stdlib urllib + matplotlib).
Zero API keys and zero Tavily credits. Supports equities, ETFs, indices, crypto, and FX.

Usage:
  python -I -X utf8 finance_cli.py quote --symbol "NVDA" --period 1mo --chart-out nvda.png
  python -I -X utf8 finance_cli.py compare --symbols "NVDA,MSFT,AAPL" --period 6mo --chart-out cmp.png
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

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
    "1d": "5m",
    "5d": "15m",
    "1mo": "1d",
    "3mo": "1d",
    "6mo": "1d",
    "1y": "1wk",
    "2y": "1wk",
    "5y": "1mo",
}


def _http_json(url: str, timeout: int = 12) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


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


def _fetch_chart(symbol: str, period: str = "1mo") -> dict[str, Any]:
    p = period if period in PERIOD_INTERVAL else "1mo"
    interval = PERIOD_INTERVAL[p]
    sym_enc = urllib.parse.quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym_enc}?range={p}&interval={interval}"
    data = _http_json(url)
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
    if not series and meta.get("regularMarketPrice") is None:
        raise RuntimeError(f"Empty price series for `{symbol}`.")
    return {"symbol": meta.get("symbol") or symbol, "meta": meta, "series": series, "period": p}


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


def cmd_quote(symbol_or_query: str, period: str, chart_out: str | None) -> dict[str, Any]:
    sym = resolve_symbol(symbol_or_query)
    ch = _fetch_chart(sym, period)
    meta = ch["meta"]
    series = ch["series"]
    price = float(meta.get("regularMarketPrice") or (series[-1][1] if series else 0.0))
    prev_close = float(meta.get("chartPreviousClose") or meta.get("previousClose") or (series[0][1] if series else price))
    day_chg = price - prev_close
    day_pct = (day_chg / prev_close * 100.0) if prev_close else 0.0
    p_start = series[0][1] if series else price
    p_ret_pct = ((price - p_start) / p_start * 100.0) if p_start else 0.0
    vals = [v for _, v in series] or [price]

    chart_path = None
    if chart_out and len(series) >= 2:
        out_p = Path(chart_out)
        _render_quote_chart(ch, out_p)
        if out_p.is_file():
            chart_path = str(out_p)

    return {
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
        "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def cmd_compare(symbols_csv: str, period: str, chart_out: str | None) -> dict[str, Any]:
    raw_items = [x.strip() for x in re.split(r"[,;]+", symbols_csv or "") if x.strip()][:5]
    if len(raw_items) < 2:
        raise ValueError("Provide at least 2 comma-separated tickers to compare.")
    charts = []
    rows = []
    for item in raw_items:
        sym = resolve_symbol(item)
        ch = _fetch_chart(sym, period)
        charts.append(ch)
        meta = ch["meta"]
        series = ch["series"]
        price = float(meta.get("regularMarketPrice") or (series[-1][1] if series else 0.0))
        p_start = series[0][1] if series else price
        p_ret = ((price - p_start) / p_start * 100.0) if p_start else 0.0
        rows.append({
            "symbol": ch["symbol"],
            "name": meta.get("shortName") or ch["symbol"],
            "currency": meta.get("currency") or "",
            "price": round(price, 4),
            "period_return_pct": round(p_ret, 2),
        })

    chart_path = None
    if chart_out:
        out_p = Path(chart_out)
        _render_compare_chart(charts, period if period in PERIOD_INTERVAL else "6mo", out_p)
        if out_p.is_file():
            chart_path = str(out_p)

    return {"ok": True, "period": period, "items": rows, "chart_path": chart_path, "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Live Market & Finance CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pq = sub.add_parser("quote")
    pq.add_argument("--symbol", required=True)
    pq.add_argument("--period", default="1mo")
    pq.add_argument("--chart-out", default="")

    pc = sub.add_parser("compare")
    pc.add_argument("--symbols", required=True)
    pc.add_argument("--period", default="6mo")
    pc.add_argument("--chart-out", default="")

    args = ap.parse_args()
    try:
        if args.cmd == "quote":
            res = cmd_quote(args.symbol, args.period, args.chart_out or None)
        else:
            res = cmd_compare(args.symbols, args.period, args.chart_out or None)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
