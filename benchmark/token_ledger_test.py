"""Offline test for the router's token ledger (2026-11-10). Touches neither LM Studio nor the live router.

Runs router.Handler on 127.0.0.1:4100 against a fake LM Studio on 127.0.0.1:4199, with router.log, last_request_*.json
and token-usage.csv redirected to a temp folder, then checks one ledger row per request shape:
OpenAI stream / non-stream (passthrough), Anthropic stream / non-stream (Claude Code door), the translated thinking
path, a client that disconnects mid-stream (partial, estimated count), the router's own labeller call, and the
User-Agent -> client mapping.

    python -I token_ledger_test.py
"""
import csv
import http.client
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(os.path.expandvars(r"%USERPROFILE%\.lmstudio\scripts"))
sys.path.insert(0, str(SCRIPTS))
import router          # noqa: E402
import token_ledger    # noqa: E402

FAKE_PORT, ROUTER_PORT = 4199, 4100
TMP = Path(tempfile.mkdtemp(prefix="ledger-test-"))
router.LOG_FILE = TMP / "router.log"
token_ledger.LEDGER = TMP / "token-usage.csv"
router.LMSTUDIO_PORT = FAKE_PORT
LOADED = sorted(set(router.MODELS.values()) | {router.ROUTER_MODEL, "test-model"})
SEEN = []


class FakeLMStudio(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, ctype="application/json"):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._send({"data": [{"id": m, "type": "llm", "state": "loaded", "max_context_length": 65536,
                              "loaded_context_length": 65536} for m in LOADED]})

    def _sse(self, events, slow=False):
        # chunked + keep-alive like LM Studio (with "Connection: close" http.client drops conn.sock early)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for ev in events:
            data = ev if isinstance(ev, bytes) else b"data: " + json.dumps(ev).encode() + b"\n\n"
            try:
                self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            except OSError:
                return
            if slow:
                time.sleep(0.1)
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append((self.path, body.get("model"), bool(body.get("stream"))))
        text = json.dumps(body.get("messages"))
        slow = "SLOW" in text
        if self.path.startswith("/v1/chat/completions"):
            if not body.get("stream"):
                content = '{"category": "fast", "web": false, "query": "", "weather": false}'
                return self._send({"id": "x", "object": "chat.completion", "model": body["model"],
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                                "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
            n = 40 if slow else 3
            evs = [{"choices": [{"index": 0, "delta": {"content": "word "}, "finish_reason": None}]} for _ in range(n)]
            evs.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            if (body.get("stream_options") or {}).get("include_usage"):
                evs.append({"choices": [], "usage": {"prompt_tokens": 101, "completion_tokens": 33, "total_tokens": 134}})
            evs.append(b"data: [DONE]\n\n")
            return self._sse(evs, slow)
        if self.path.startswith("/v1/messages"):
            if not body.get("stream"):
                return self._send({"id": "m", "type": "message", "role": "assistant", "model": body["model"],
                                   "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                                   "usage": {"input_tokens": 21, "output_tokens": 9}})
            evs = [{"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                                                         "content": [], "usage": {"input_tokens": 202, "output_tokens": 1}}},
                   {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}},
                   {"type": "content_block_stop", "index": 0},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 44}},
                   {"type": "message_stop"}]
            return self._sse([b"event: " + e["type"].encode() + b"\n" + b"data: " + json.dumps(e).encode() + b"\n\n"
                              for e in evs])
        self.send_response(404)
        self.end_headers()


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def post(path, body, ua="test-script/1.0", disconnect_after=None):
    conn = http.client.HTTPConnection("127.0.0.1", ROUTER_PORT, timeout=60)
    hdr = {"Content-Type": "application/json", "User-Agent": ua, "Host": f"127.0.0.1:{ROUTER_PORT}"}
    if path.startswith("/v1/messages"):
        hdr["anthropic-version"] = "2023-06-01"
    conn.request("POST", path, json.dumps(body), hdr)
    resp = conn.getresponse()
    if disconnect_after:
        resp.read(disconnect_after)
        conn.sock.close()
        conn.close()
        return None
    data = resp.read()
    conn.close()
    return data


def rows():
    if not token_ledger.LEDGER.exists():
        return []
    with token_ledger.LEDGER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def wait_rows(n, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if len(rows()) >= n:
            return rows()
        time.sleep(0.1)
    return rows()


def main():
    serve(FakeLMStudio, FAKE_PORT)
    serve(router.Handler, ROUTER_PORT)
    msg = lambda t: [{"role": "user", "content": t}]
    results = []

    def check(name, fn, expect, exact=True):
        before = len(rows())
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"request failed: {e.__class__.__name__}: {e}"))
            return
        wait_rows(before + len(expect))
        time.sleep(0.7)   # a double count would land here
        new = rows()[before:]
        match = lambda r, exp: all(str(r.get(k)) == str(v) for k, v in exp.items())
        if exact:
            ok = len(new) == len(expect) and all(match(r, e) for r, e in zip(new, expect))
        else:
            ok = all(any(match(r, e) for r in new) for e in expect)
        shown = [{k: r[k] for k in ("source", "client", "door", "tier", "model", "input_tokens", "output_tokens",
                                     "estimated")} for r in new]
        results.append((name, ok, json.dumps(shown)))

    check("openai stream (passthrough)",
          lambda: post("/v1/chat/completions", {"model": "test-model", "stream": True, "messages": msg("hi")}),
          [{"source": "request", "door": "openai", "model": "test-model", "input_tokens": 101, "output_tokens": 33,
            "estimated": 0, "client": "test-script/1.0"}])
    check("openai non-stream (passthrough)",
          lambda: post("/v1/chat/completions", {"model": "test-model", "messages": msg("hi")}),
          [{"source": "request", "input_tokens": 11, "output_tokens": 7, "estimated": 0}])
    check("anthropic stream (Claude Code UA)",
          lambda: post("/v1/messages", {"model": "local-fast", "stream": True, "max_tokens": 50, "messages": msg("hi")},
                       ua="claude-cli/2.1.0 (external, cli)"),
          [{"source": "router internal", "tier": "lfm json"},   # the web decision is a real model call too
           {"source": "request", "door": "anthropic", "client": "Claude Code", "input_tokens": 202,
            "output_tokens": 44, "estimated": 0}])
    check("anthropic non-stream",
          lambda: post("/v1/messages", {"model": "local-fast", "max_tokens": 50, "messages": msg("hi")}),
          [{"source": "request", "door": "anthropic", "input_tokens": 21, "output_tokens": 9}])
    check("translated thinking tier (think, OpenAI door, Open WebUI UA)",
          lambda: post("/v1/chat/completions", {"model": "think", "stream": True, "messages": msg("hi")},
                       ua="Python/3.11 aiohttp/3.12.15"),
          [{"source": "request", "client": "Open WebUI", "tier": "think", "input_tokens": 202, "output_tokens": 44,
            "estimated": 0}])
    check("client disconnects mid-stream (partial, estimated)",
          lambda: (post("/v1/chat/completions", {"model": "test-model", "stream": True, "messages": msg("SLOW")},
                        disconnect_after=600), time.sleep(1.5)),
          [{"source": "request", "model": "test-model", "estimated": 1}])
    check("auto: labeller call counted as router internal, then the answer",
          lambda: post("/v1/chat/completions", {"model": "auto", "stream": True,
                                                "messages": msg("what is the capital of france")}),
          [{"source": "router internal", "client": "router", "model": router.ROUTER_MODEL, "input_tokens": 11,
            "output_tokens": 7},
           {"source": "request", "door": "openai", "estimated": 0}], exact=False)

    # the built-in Playwright browser loop talks to LM Studio directly: it records each step itself
    import northcua_harness
    before = len(rows())
    northcua_harness._count_step_tokens("northcua-nano", {"prompt_tokens": 900, "completion_tokens": 120},
                                        [{"role": "user", "content": "x"}], "think", "answer", {}, time.time())
    northcua_harness._count_step_tokens("northcua-nano", None, [{"role": "user", "content": "y" * 380}],
                                        "t" * 190, "a" * 190, {}, time.time())
    new = rows()[before:]
    ok = (len(new) == 2 and new[0]["source"] == "browser loop" and new[0]["input_tokens"] == "900"
          and new[0]["output_tokens"] == "120" and new[0]["estimated"] == "0"
          and new[1]["estimated"] == "1" and new[1]["output_tokens"] == "100")
    results.append(("browser loop steps (usage given / estimated)", ok,
                    json.dumps([{k: r[k] for k in ("source", "model", "input_tokens", "output_tokens", "estimated")}
                                for r in new])))

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    print(f"\nupstream calls seen: {SEEN}")
    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed (temp files in {TMP})")
    print("\n--- report on the test ledger ---")
    token_ledger.report(7)


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
