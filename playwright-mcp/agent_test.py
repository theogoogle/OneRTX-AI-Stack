"""Browser-agent loop through the router (model 'auto'), the way Open WebUI runs it: Playwright MCP tools,
screenshots fed back as images. python -I agent_test.py "task" [max_steps]"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from mcp_client import MCP  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROUTER = "http://127.0.0.1:4000/v1/chat/completions"
BLOCK = {"browser_run_code_unsafe", "browser_file_upload", "browser_close"}
task = sys.argv[1] if len(sys.argv) > 1 else "Go to https://www.theo2.co.uk/events and tell me which events are on Sunday 8 November 2026."
max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 12

m = MCP()
specs = [t for t in m.tools() if t["name"] not in BLOCK]
tools = [{"type": "function", "function": {"name": "playwright_" + t["name"], "description": t.get("description", ""),
                                           "parameters": t.get("inputSchema", {"type": "object", "properties": {}})}}
         for t in specs]
msgs = [{"role": "user", "content": task}]
for step in range(1, max_steps + 1):
    t = time.time()
    body = {"model": __import__("os").environ.get("AGENT_MODEL", "auto"), "stream": False, "tools": tools, "messages": msgs, "max_tokens": 8192}
    d = json.load(urllib.request.urlopen(urllib.request.Request(ROUTER, json.dumps(body).encode(),
                                                                {"Content-Type": "application/json"}), timeout=900))
    msg = d["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls and not (msg.get("content") or "").strip():
        rbody = {**body, "reasoning_effort": "none", "messages": msgs + [{"role": "user", "content": "You ran out of tokens while thinking without calling a tool or writing an answer. Based on the page snapshots above, either call the next browser tool needed or write the final answer concisely now."}]}
        d = json.load(urllib.request.urlopen(urllib.request.Request(ROUTER, json.dumps(rbody).encode(), {"Content-Type": "application/json"}), timeout=900))
        msg = d["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
    print(f"\nstep {step} ({time.time() - t:.1f}s): " + (", ".join(f"{c['function']['name']}({c['function']['arguments'][:120]})" for c in calls)
                                                  or "ANSWER: " + (msg.get("content") or "")[:1500]))
    if not calls:
        open(Path(__file__).with_name('last_answer.md'), 'w', encoding='utf-8').write(msg.get('content') or '')
        break
    msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
    images = []
    for c in calls:
        name = c["function"]["name"].removeprefix("playwright_")
        try:
            args = json.loads(c["function"]["arguments"] or "{}")
            res = m.tool(name, **args)
            text = "\n".join(p.get("text", "") for p in res["content"] if p.get("type") == "text")
            images += [p for p in res["content"] if p.get("type") == "image"]
        except Exception as e:
            text = f"error: {e}"
        msgs.append({"role": "tool", "tool_call_id": c.get("id", "x"), "content": text})
        print(f"   -> {name}: {text[:160].replace(chr(10), ' | ')}{' [+image]' if images else ''}")
    if images:   # like Open WebUI: tool images go back to the model as image parts
        msgs.append({"role": "user", "content": [{"type": "text", "text": "Screenshot from the tool:"}] +
                     [{"type": "image_url", "image_url": {"url": f"data:{i['mimeType']};base64,{i['data']}"}} for i in images]})
h = json.load(urllib.request.urlopen("http://127.0.0.1:4000/health"))
print("\nrouter:", {k: h["recent_decisions"][0].get(k) for k in ("tier", "model", "reason", "tools_dropped")})
