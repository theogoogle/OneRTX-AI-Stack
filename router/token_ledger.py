"""Token ledger: one CSV row per model request handled by the router, kept forever (router.log is trimmed to 7 days).

Written by router.py (every request it forwards, plus its own internal LM Studio calls) and by
northcua_harness.py (the built-in Playwright browser loop, which talks to LM Studio directly).
No chat text is stored: only date, time, who asked, which model, token counts and timing.

Report:
    python -I token_ledger.py              totals + last 14 days + per model / client / source (last 30 days)
    python -I token_ledger.py --days 7     breakdowns over the last 7 days
    python -I token_ledger.py --import-router-log
                                           one-off: add the token counts router.log already holds from before the
                                           ledger existed (safe to re-run: skipped once imported)
"""
import csv
import json
import re
import sys
import threading

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "token-usage.csv"
ROUTER_LOG = HERE / "router.log"
FIELDS = ["date", "time", "source", "client", "door", "tier", "model", "input_tokens", "output_tokens",
          "estimated", "tok_s", "seconds", "status", "user_agent"]
_lock = threading.Lock()


def client_name(user_agent, asked=""):
    """Readable client name from the User-Agent header (raw value is kept in the ledger too)."""
    ua = (user_agent or "").lower()
    if "claude-cli" in ua or "claude-code" in ua:
        return "Claude Code"
    if "opencode" in ua:
        return "OpenCode"
    if "aiohttp" in ua:
        return "Open WebUI"
    if "openai/python" in ua or "asyncopenai" in ua:
        return "OpenAI Python SDK (NorthCUA harness)"
    if "python-urllib" in ua:
        return "Python script"
    if not ua:
        return "unknown"
    return user_agent.split()[0][:40]


def record(source, client, tier, model, input_tokens, output_tokens, *, door="", estimated=False, tok_s="",
           seconds="", status="", user_agent=""):
    """Append one row. Never raises: counting must not break a request."""
    try:
        if not (int(input_tokens or 0) or int(output_tokens or 0)):
            return
        now = datetime.now()
        row = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), "source": source,
               "client": client, "door": door, "tier": tier or "", "model": model or "",
               "input_tokens": int(input_tokens or 0), "output_tokens": int(output_tokens or 0),
               "estimated": 1 if estimated else 0, "tok_s": tok_s, "seconds": seconds,
               "status": str(status)[:60], "user_agent": (user_agent or "")[:80]}
        with _lock:
            new = not LEDGER.exists() or LEDGER.stat().st_size == 0
            with LEDGER.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
    except Exception:
        pass


def usage_counts(usage):
    """(input, output) from an OpenAI or Anthropic usage object; Anthropic cache reads count as input."""
    u = usage or {}
    inp = u.get("prompt_tokens")
    if inp is None:
        inp = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
            + (u.get("cache_creation_input_tokens") or 0)
    out = u.get("completion_tokens")
    if out is None:
        out = u.get("output_tokens") or 0
    return int(inp or 0), int(out or 0)


# ---------------------------------------------------------------- report
def _rows():
    if not LEDGER.exists():
        return []
    with LEDGER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _n(x):
    return f"{x:,}".replace(",", " ")


def _table(title, groups, with_speed=False):
    print(f"\n{title}")
    head = f"  {'':<38}{'requests':>9}{'input':>14}{'output':>12}{'total':>14}"
    print(head + (f"{'tok/s':>8}" if with_speed else ""))
    for key, g in groups:
        line = (f"  {key[:38]:<38}{_n(g['n']):>9}{_n(g['in']):>14}{_n(g['out']):>12}{_n(g['in'] + g['out']):>14}")
        if with_speed:
            line += f"{g['speed_w'] / g['speed_out']:>8.1f}" if g["speed_out"] else f"{'-':>8}"
        print(line)


def _group(rows, keyfn):
    acc = defaultdict(lambda: {"n": 0, "in": 0, "out": 0, "speed_w": 0.0, "speed_out": 0, "est": 0})
    for r in rows:
        g = acc[keyfn(r)]
        i, o = int(r["input_tokens"] or 0), int(r["output_tokens"] or 0)
        g["n"] += 1
        g["in"] += i
        g["out"] += o
        g["est"] += int(r.get("estimated") or 0)
        try:
            s = float(r.get("tok_s") or 0)
        except ValueError:
            s = 0
        if s > 0 and o > 0:   # output-weighted average speed
            g["speed_w"] += s * o
            g["speed_out"] += o
    return acc


def report(days=30):
    rows = _rows()
    if not rows:
        print(f"No token data yet ({LEDGER}).")
        return
    today = date.today()
    since = (today - timedelta(days=days - 1)).isoformat()
    first = min(r["date"] for r in rows)
    print(f"Local tokens counted by the router (ledger since {first}; {LEDGER.name})")
    periods = [("Today", today.isoformat()), ("Last 7 days", (today - timedelta(days=6)).isoformat()),
               (f"Last {days} days", since), ("All time", "0000")]
    periods = [p for i, p in enumerate(periods) if p[0] not in [q[0] for q in periods[:i]]]   # --days 7: once
    _table("Totals", [(name, _group([r for r in rows if r["date"] >= start], lambda r: 0)[0])
                      for name, start in periods])
    recent = [r for r in rows if r["date"] >= since]
    by_day = _group([r for r in rows if r["date"] >= (today - timedelta(days=13)).isoformat()], lambda r: r["date"])
    _table("Per day (last 14 days)", sorted(by_day.items(), reverse=True))
    sort = lambda d: sorted(d.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))
    _table(f"Per model (last {days} days)", sort(_group(recent, lambda r: r["model"] or "?")), with_speed=True)
    _table(f"Per client (last {days} days)", sort(_group(recent, lambda r: r["client"] or "?")))
    _table(f"Per source (last {days} days)", sort(_group(recent, lambda r: r["source"] or "?")))
    est = sum(int(r.get("estimated") or 0) for r in recent)
    print(f"\ninput = tokens the model read (prompt, history, files), output = tokens it wrote (thinking + answer).")
    if est:
        print(f"{est} of {len(recent)} requests are estimates (no count from LM Studio: worked out from text length, request size or generation time).")


# ---------------------------------------------------------------- one-off import from router.log
LOG_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\s+(\{.*\})\s*$")


def import_router_log():
    rows = _rows()
    if any(r["source"] == "router.log import" for r in rows):
        print("Already imported: nothing to do.")
        return
    cutoff = min((f"{r['date']} {r['time']}" for r in rows), default="9999")
    found = []
    with ROUTER_LOG.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LOG_LINE.match(line)
            if not m or f"{m.group(1)} {m.group(2)}" >= cutoff:
                continue
            try:
                e = json.loads(m.group(3))
            except ValueError:
                continue
            if not (e.get("input_tokens") or e.get("output_tokens")):
                continue
            found.append({"date": m.group(1), "time": m.group(2), "source": "router.log import",
                          "client": "(before ledger)", "door": "", "tier": e.get("tier", ""),
                          "model": e.get("model", ""), "input_tokens": int(e.get("input_tokens") or 0),
                          "output_tokens": int(e.get("output_tokens") or 0), "estimated": 0,
                          "tok_s": e.get("tok_s", ""), "seconds": e.get("seconds", ""),
                          "status": str(e.get("status", ""))[:60], "user_agent": ""})
    with _lock:
        merged = sorted(found + rows, key=lambda r: (r["date"], r["time"]))
        tmp = LEDGER.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(merged)
        tmp.replace(LEDGER)
    print(f"Imported {len(found)} request(s) from router.log (before {cutoff}).")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--import-router-log" in args:
        import_router_log()
    else:
        d = 30
        if "--days" in args:
            d = max(1, int(args[args.index("--days") + 1]))
        report(d)
