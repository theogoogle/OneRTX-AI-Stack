"""Minimal MCP (streamable HTTP) client for testing the Playwright server. python -I mcp_client.py"""
import json
import urllib.request

URL = "http://127.0.0.1:8931/mcp"


class MCP:
    def __init__(self, url=URL):
        self.url, self.sid, self.n = url, None, 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "router-test", "version": "1"}})
        self.notify("notifications/initialized")

    def _post(self, msg):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(msg).encode(), h), timeout=120) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            raw = r.read().decode("utf-8")
        if not raw.strip():
            return None
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        for line in raw.splitlines():          # SSE: the last data line carries the reply
            if line.startswith("data: "):
                last = json.loads(line[6:])
        return last

    def call(self, method, params=None):
        self.n += 1
        out = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        if "error" in out:
            raise RuntimeError(out["error"])
        return out["result"]

    def notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method})

    def tools(self):
        return self.call("tools/list")["tools"]

    def tool(self, name, **args):
        return self.call("tools/call", {"name": name, "arguments": args})


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    m = MCP()
    names = [t["name"] for t in m.tools()]
    print(len(names), "tools:", ", ".join(names))
    r = m.tool("browser_navigate", url="https://www.theo2.co.uk/events")
    print("navigate:", [c.get("type") for c in r["content"]], r["content"][0].get("text", "")[:300].replace("\n", " | "))
    r = m.tool("browser_take_screenshot")
    print("screenshot:", [(c.get("type"), c.get("mimeType"), len(c.get("data", ""))) for c in r["content"]])
