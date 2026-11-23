"""One-off token backfill from the logs, for the time before the token ledger existed (2026-11-10).

Sources, both kept 7 days on this PC:
  - router.log: one line per request the router handled (model, seconds, request size, stream or not)
  - LM Studio server logs (%USERPROFILE%\\.lmstudio\\server-logs): "Generated prediction" blocks hold EXACT token
    counts for non-streaming calls; streamed calls only log start/finish times, so their output is estimated as
    generation time x the model's measured speed, and their input as request size / 3.7 bytes per token
    (calibrated on router.log lines that had real counts: 3.5-4.0).

Each router request is matched to the LM Studio call that ended within 3 s for the same model. LM Studio calls with no
router line (the router's own labeller calls, browser-loop steps, anything before router.log starts) are added on
their own; streamed ones of those have no request size, so their input is left at 0 (undercount, said in the report).
Orchestration lines (NorthCUA harness / desktop / browser loop wrappers) are skipped: their model calls are counted
individually. Requests already in the ledger are never added twice.

    python -I token_backfill.py           add the rows (once; re-running does nothing)
    python -I token_backfill.py --dry     show what would be added, change nothing
"""
import csv
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import token_ledger  # noqa: E402

LMS_LOGS = Path(os.path.expandvars(r"%USERPROFILE%\.lmstudio\server-logs"))
SOURCE = "log backfill"
BYTES_PER_TOKEN = 3.7
MATCH_S = 3
ORCHESTRATION = ("harness", "desktop", "browser_mcp")
# measured speeds (router.py docstring, CHANGES.md) for models with no tok/s in router.log
DEFAULT_TOK_S = {"gemma-4-12b-it-qat": 53, "unsloth/gemma-4-12b-it-qat": 53, "google/gemma-4-12b-qat": 49,
                 "qwen3.5-4b": 25, "qwen3.8-27b-gsq-rco": 12.3, "qwen3.6-35b-a3b": 46,
                 "qwen3.5-4b-3d-coder": 50, "northcua-nano": 73, "qwen/qwen3.8-27b": 7.6,
                 "qwen3.8-27b-gsq-rco@iq3_xxs": 23, "lfm3-1.7b": 60}
LMS_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\[\w+\]\[([^\]]+)\] (.*)$")
ROUTER_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\{.*\})\s*$")
CALIBRATION = []   # (model, real in, estimated in, real out, estimated out) for requests that had real counts


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def lmstudio_events():
    """[{t, model, kind: exact|stream, in, out, gen_s}] from all LM Studio server logs, in time order."""
    events = []
    for f in sorted(LMS_LOGS.rglob("*.log")):
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        started, pp_done = {}, {}
        i = 0
        while i < len(lines):
            m = LMS_LINE.match(lines[i])
            i += 1
            if not m:
                continue
            t, model, msg = ts(m.group(1)), m.group(2), m.group(3)
            if msg.startswith("Running chat completion") or msg.startswith("Running Anthropic messages API"):
                started[model], pp_done[model] = t, None
            elif msg.startswith("Prompt processing progress: 100"):
                pp_done[model] = t
            elif msg.startswith("Generated prediction: {"):
                block = ["{"]
                while i < len(lines) and lines[i] != "}":
                    block.append(lines[i])
                    i += 1
                block.append("}")
                i += 1
                try:
                    inp, out = token_ledger.usage_counts(json.loads("\n".join(block)).get("usage"))
                except ValueError:
                    continue
                events.append({"t": t, "model": model, "kind": "exact", "in": inp, "out": out})
            elif msg.startswith("Finished streaming"):
                gen_from = pp_done.get(model) or started.get(model) or t
                events.append({"t": t, "model": model, "kind": "stream", "in": 0, "out": 0,
                               "gen_s": max(1.0, (t - gen_from).total_seconds())})
    return sorted(events, key=lambda e: e["t"])


def router_entries():
    out = []
    for line in token_ledger.ROUTER_LOG.open(encoding="utf-8", errors="replace"):
        m = ROUTER_LINE.match(line)
        if not m:
            continue
        try:
            e = json.loads(m.group(2))
        except ValueError:
            continue
        if "tier" in e and e.get("model") and e.get("tier") not in ("image", "web-search"):
            e["_t"] = ts(m.group(1))
            out.append(e)
    return out


def speeds(entries):
    by = defaultdict(list)
    for e in entries:
        if e.get("tok_s"):
            by[e["model"]].append(float(e["tok_s"]))
    sp = dict(DEFAULT_TOK_S)
    sp.update({m: statistics.median(v) for m, v in by.items()})
    return sp


def build(cutoff):
    events = [e for e in lmstudio_events() if e["t"] < cutoff]
    entries = [e for e in router_entries() if e["_t"] < cutoff]
    sp = speeds(entries)
    by_model = defaultdict(list)
    for ev in events:
        by_model[ev["model"]].append(ev)
    used = set()
    rows = []
    exact_screen = [ev["in"] for ev in by_model.get("northcua-nano", []) if ev["kind"] == "exact" and ev["in"]]
    screen_in = statistics.median(exact_screen) if exact_screen else 0

    def take(model, t):
        best = None
        for ev in by_model.get(model, []):
            d = abs((ev["t"] - t).total_seconds())
            if d <= MATCH_S and id(ev) not in used and (best is None or d < best[0]):
                best = (d, ev)
        if best:
            used.add(id(best[1]))
            return best[1]
        return None

    def row(t, client, tier, model, inp, out, est, seconds="", status=""):
        rows.append({"date": t.strftime("%Y-%m-%d"), "time": t.strftime("%H:%M:%S"), "source": SOURCE,
                     "client": client, "door": "", "tier": tier, "model": model, "input_tokens": int(inp),
                     "output_tokens": int(out), "estimated": 1 if est else 0, "tok_s": "", "seconds": seconds,
                     "status": str(status)[:60], "user_agent": ""})

    for e in entries:
        ev = take(e["model"], e["_t"])            # consume the matching LM Studio call either way
        if any(k in e for k in ORCHESTRATION):
            continue                               # wrapper around many model calls, counted individually
        if e.get("input_tokens") or e.get("output_tokens"):
            if ev and ev["kind"] == "stream":      # calibration: what the estimate would have said
                CALIBRATION.append((e["model"], e.get("input_tokens", 0), round(req_in := (e.get("request_kb") or 0)
                                    * 1024 / BYTES_PER_TOKEN), e.get("output_tokens", 0),
                                    round(ev["gen_s"] * sp.get(e["model"], 20))))
            continue                               # already in the ledger (router.log import)
        req_in = (e.get("request_kb") or 0) * 1024 / BYTES_PER_TOKEN
        if e["tier"] == "screen" and screen_in:
            # screenshots travel as base64 (hundreds of KB, ~1-2K real tokens): use the median of the exact screen calls
            req_in = screen_in
        if ev and ev["kind"] == "exact":
            row(e["_t"], "(before ledger)", e["tier"], e["model"], ev["in"], ev["out"], False, e.get("seconds"),
                e.get("status"))
        elif ev:
            row(e["_t"], "(before ledger)", e["tier"], e["model"], req_in,
                ev["gen_s"] * sp.get(e["model"], 20), True, e.get("seconds"), e.get("status"))
        else:   # no LM Studio line found: whole request time at 70 % generation
            row(e["_t"], "(before ledger)", e["tier"], e["model"], req_in,
                float(e.get("seconds") or 0) * 0.7 * sp.get(e["model"], 20), True, e.get("seconds"), e.get("status"))

    for ev in events:                              # LM Studio calls the router did not log as requests
        if id(ev) in used:
            continue
        client = "router" if ev["model"] == "lfm3-1.7b" else "direct to LM Studio (scripts, benchmarks)"
        if ev["kind"] == "exact":
            row(ev["t"], client, "", ev["model"], ev["in"], ev["out"], False)
        else:
            row(ev["t"], client, "", ev["model"], 0, ev["gen_s"] * sp.get(ev["model"], 20), True)
    return [r for r in rows if r["input_tokens"] or r["output_tokens"]]


def main():
    dry = "--dry" in sys.argv
    existing = token_ledger._rows()
    if any(r["source"] == SOURCE for r in existing):
        print("Backfill already done: nothing to do.")
        return
    live = [f"{r['date']} {r['time']}" for r in existing if r["source"] not in ("router.log import",)]
    cutoff = ts(min(live)) if live else datetime.now()
    rows = build(cutoff)
    exact = [r for r in rows if not r["estimated"]]
    tot = lambda rs, k: sum(int(r[k]) for r in rs)
    print(f"Backfill before {cutoff}: {len(rows)} calls ({len(exact)} exact, {len(rows) - len(exact)} estimated), "
          f"{tot(rows, 'input_tokens'):,} input + {tot(rows, 'output_tokens'):,} output tokens"
          f" (exact part: {tot(exact, 'input_tokens'):,} + {tot(exact, 'output_tokens'):,}).")
    for c in CALIBRATION:
        print(f"  calibration {c[0]}: input real {c[1]:,} vs estimate {c[2]:,}; output real {c[3]:,} vs estimate {c[4]:,}")
    if dry:
        return
    merged = sorted(token_ledger._rows() + rows, key=lambda r: (r["date"], r["time"]))   # re-read: the router appends
    tmp = token_ledger.LEDGER.with_suffix(".csv.tmp")
    with token_ledger._lock, tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=token_ledger.FIELDS)
        w.writeheader()
        w.writerows(merged)
    tmp.replace(token_ledger.LEDGER)
    print(f"Added to {token_ledger.LEDGER.name}.")


if __name__ == "__main__":
    main()
