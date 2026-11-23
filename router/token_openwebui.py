"""Show the token ledger in Open WebUI's Admin Panel -> Analytics (2026-11-10).

Open WebUI's Analytics only counts assistant replies stored in its own chats (table chat_message, column usage). Claude
Code, OpenCode in a terminal, the browser agent, the router's own calls and scripts never create such replies. So this
writes one ARCHIVED chat per month, "📊 Token ledger <yyyy-mm> (router)", with one reply per model per day whose usage
holds that day's totals from token-usage.csv. Analytics then shows tokens per model and per day for everything.

Not counted twice: ledger rows from Open WebUI chats themselves (client "Open WebUI", not background tasks) are left out,
because those replies already carry their own usage. Known overlap: agent sessions started from Open WebUI's model picker
(🧑‍💻 Open Code / 🤖 Claude Code) store their own totals AND are in the ledger as OpenCode / Claude Code requests.

Idempotent: fixed chat and message ids, rewritten in place on every run (only today's numbers change). Writes go to
webui.db while Open WebUI runs (WAL mode, short transactions, 15 s busy timeout); chats are dual-written like Open WebUI
does (chat.chat JSON history + chat_message rows). start-llm-stack.ps1 (step 7) runs it every 10 minutes.

    python -I token_openwebui.py          sync now and print what was written
"""
import csv
import json
import os
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import token_ledger  # noqa: E402

WEBUI_DB = Path(os.path.expandvars(r"%USERPROFILE%\AI\open-webui\data\webui.db"))
NS = uuid.UUID("8f0c7a52-3d0e-4c39-9a51-6f7d2b1e0a11")   # fixed namespace -> stable ids


def _skip(row):
    """Open WebUI's own chat replies already store usage: leave them out (its background tasks stay in)."""
    return row["client"] == "Open WebUI" and row["tier"] != "background"


def _totals():
    """{month: {(date, model): {...}}} from the ledger."""
    months = defaultdict(lambda: defaultdict(lambda: {"n": 0, "in": 0, "out": 0, "est": 0,
                                                       "clients": defaultdict(int)}))
    for r in token_ledger._rows():
        if _skip(r) or not r.get("model"):
            continue
        g = months[r["date"][:7]][(r["date"], r["model"])]
        g["n"] += 1
        g["in"] += int(r["input_tokens"] or 0)
        g["out"] += int(r["output_tokens"] or 0)
        g["est"] += int(r.get("estimated") or 0)
        g["clients"][r["client"] or "?"] += 1
    return months


def _fmt(n):
    return f"{n:,}".replace(",", " ")


def _content(day, model, g):
    clients = ", ".join(f"{c} {n}" for c, n in sorted(g["clients"].items(), key=lambda kv: -kv[1]))
    est = f" ({g['est']} estimated)" if g["est"] else ""
    return (f"**{day} · {model}**: {g['n']} model calls{est}, {_fmt(g['in'])} input + {_fmt(g['out'])} output tokens.\n\n"
            f"Callers: {clients}.\n\n_Written by the router from token-usage.csv for Analytics. Not a real conversation._")


def sync(verbose=False):
    if not WEBUI_DB.exists():
        return 0
    months = _totals()
    db = sqlite3.connect(str(WEBUI_DB), timeout=15)
    try:
        db.execute("PRAGMA busy_timeout = 15000")
        users = db.execute("SELECT id FROM user WHERE role = 'admin' ORDER BY created_at LIMIT 1").fetchall()
        if not users:
            return 0
        user_id = users[0][0]
        now = int(time.time())
        written = 0
        for month, days in sorted(months.items()):
            chat_id = str(uuid.uuid5(NS, f"token-ledger-{month}"))
            start = int(datetime.strptime(month + "-01 00:01", "%Y-%m-%d %H:%M").timestamp())
            title = f"📊 Token ledger {month} (router)"
            root_id = str(uuid.uuid5(NS, f"{month}-root"))
            history = {root_id: {"id": root_id, "parentId": None, "childrenIds": [], "role": "user",
                                 "content": f"Token ledger for {month}: one reply per model per day, written by the "
                                            f"router from token-usage.csv so Open WebUI's Analytics shows all local "
                                            f"tokens. Not a real conversation.",
                                 "timestamp": start, "models": [], "user_id": user_id}}
            rows = [(root_id, "user", None, history[root_id]["content"], None, None, start)]
            prev = root_id
            for (day, model), g in sorted(days.items()):
                mid = str(uuid.uuid5(NS, f"{day}-{model}"))
                ts = int(datetime.strptime(day + " 00:01", "%Y-%m-%d %H:%M").timestamp())
                usage = {"input_tokens": g["in"], "output_tokens": g["out"], "total_tokens": g["in"] + g["out"],
                         "prompt_tokens": g["in"], "completion_tokens": g["out"], "model_calls": g["n"],
                         "estimated_calls": g["est"]}
                text = _content(day, model, g)
                history[mid] = {"id": mid, "parentId": prev, "childrenIds": [], "role": "assistant", "content": text,
                                "model": model, "modelIdx": 0, "done": True, "timestamp": ts, "usage": usage,
                                "user_id": user_id}
                history[prev]["childrenIds"] = [mid]
                rows.append((mid, "assistant", prev, text, model, usage, ts))
                prev = mid
            linear = [history[r[0]] for r in rows]
            models = sorted({m for (_, m) in days})
            chat = {"id": chat_id, "title": title, "models": models,
                    "history": {"currentId": prev, "messages": history}, "messages": linear,
                    "files": [], "tags": [], "timestamp": start * 1000}
            with db:   # one short transaction per month
                db.execute(
                    "INSERT INTO chat (id, user_id, title, created_at, updated_at, archived, chat, pinned, meta, "
                    "current_message_id) VALUES (?, ?, ?, ?, ?, 1, ?, 0, '{}', ?) "
                    "ON CONFLICT(id) DO UPDATE SET title = excluded.title, chat = excluded.chat, "
                    "updated_at = excluded.updated_at, current_message_id = excluded.current_message_id",
                    (chat_id, user_id, title, start, now, json.dumps(chat, ensure_ascii=False), prev))
                keep = []
                for mid, role, parent, text, model, usage, ts in rows:
                    cid = f"{chat_id}-{mid}"
                    keep.append(cid)
                    db.execute(
                        "INSERT INTO chat_message (id, chat_id, user_id, role, parent_id, content, model_id, done, "
                        "usage, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET content = excluded.content, usage = excluded.usage, "
                        "parent_id = excluded.parent_id, updated_at = excluded.updated_at",
                        (cid, chat_id, user_id, role, parent, json.dumps(text, ensure_ascii=False), model,
                         json.dumps(usage) if usage else None, ts, now))
                    written += 1
                db.execute(f"DELETE FROM chat_message WHERE chat_id = ? AND id NOT IN ({','.join('?' * len(keep))})",
                           (chat_id, *keep))
            if verbose:
                tin = sum(g["in"] for g in days.values())
                tout = sum(g["out"] for g in days.values())
                print(f"Token ledger {month}: {len(days)} day/model replies, {_fmt(tin)} input + {_fmt(tout)} output tokens")
        return written
    finally:
        db.close()


if __name__ == "__main__":
    n = sync(verbose=True)
    print(f"{n} chat_message rows written to {WEBUI_DB.name}")
