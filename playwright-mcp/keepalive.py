"""Keeps the headless Browser tabs open between chat messages.

Playwright MCP closes its shared browser when the LAST client disconnects, and Open WebUI disconnects after every
message: the window (and every tab) vanished after each answer. This process stays connected to the headless server,
so that browser lives on; it checks every 30 s and reconnects if the server restarted.
Since 2026-11-18 the headed server (:8931) is NOT held: holding it kept a visible Chrome window on the desktop all
day. Its window now opens only while a client uses it. To hold it again, add
"headed": "http://127.0.0.1:8931/mcp" back to SERVERS.
Started by start-llm-stack.ps1. python -I keepalive.py
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_client import MCP  # noqa: E402

SERVERS = {"headless": "http://127.0.0.1:8932/mcp"}
LOG = Path(__file__).resolve().parent.parent / "playwright-keepalive.log"
EVERY_S = 30


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + msg + "\n")


sessions = {}
while True:
    for name, url in SERVERS.items():
        try:
            if name not in sessions:
                sessions[name] = MCP(url)
                sessions[name].tool("browser_tabs", action="list")     # opens the shared browser and holds it
                log(f"{name}: connected, browser held open")
            else:
                sessions[name].tool("browser_tabs", action="list")
        except Exception as e:
            if name in sessions:
                log(f"{name}: lost ({e.__class__.__name__}: {str(e)[:120]}), reconnecting")
            sessions.pop(name, None)
    time.sleep(EVERY_S)
