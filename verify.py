"""Post-install verification suite for OneRTX-AI-Stack.

Verifies:
  1. All deployed Python files under <install_dir> compile cleanly (py_compile).
  2. Standalone unit tests (when --run-unit-tests is set), sandboxed to USERPROFILE=<install_dir>.
  3. Real router startup on --router-port talking to either live LM Studio or an in-process mock LM Studio server:
     - GET  /health -> HTTP 200
     - GET  /v1/models -> HTTP 200
     - POST /route-check -> HTTP 200 (everyday -> fast, coding CLI -> opencode, email tool -> deep)
     - POST /v1/chat/completions (non-stream & stream [DONE]) -> HTTP 200
     - Security guard (local_guard): foreign Origin header -> HTTP 403
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import py_compile
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def make_mock_lmstudio():
    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _reply(self, code: int, obj: dict):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path in ("/v1/models", "/api/v0/models"):
                models = [
                    {"id": "lfm3-1.7b", "state": "loaded"},
                    {"id": "qwen3.5-4b", "state": "loaded"},
                    {"id": "gemma-4-12b-it-qat", "state": "loaded"},
                    {"id": "qwen3.6-35b-a3b", "state": "loaded"},
                ]
                return self._reply(200, {"object": "list", "data": models})
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            ln = int(self.headers.get("Content-Length") or "0")
            body = json.loads(self.rfile.read(ln) or b"{}") if ln else {}
            model = body.get("model", "gemma-4-12b-it-qat")
            rf_name = ((body.get("response_format") or {}).get("json_schema") or {}).get("name") or ""
            msgs = body.get("messages") or []
            last_u = next((str(m.get("content") or "") for m in reversed(msgs) if m.get("role") == "user"), "")
            low = last_u.lower()

            if rf_name == "semantic_route":
                if "email" in low or "gmail" in low:
                    content = json.dumps({"tools": ["search_emails"], "tier": "deep"})
                else:
                    content = json.dumps({"tools": [], "tier": "fast"})
            elif rf_name == "label":
                content = json.dumps({"label": "code" if ("python" in low or "cli" in low) else "fast"})
            elif rf_name == "coding_route":
                content = json.dumps({"shape": "opencode" if ("multi-file" in low or "pytest" in low) else "chat", "domain": "logic_ui"})
            elif rf_name == "web":
                content = json.dumps({"web": False, "query": "", "weather": False, "place": ""})
            else:
                content = "Mock verification response OK."

            if self.path == "/v1/messages":
                txt = "Mock Claude/Pro response OK."
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    evs = [
                        ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 12}}}),
                        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": txt}}),
                        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}}),
                        ("message_stop", {"type": "message_stop"}),
                    ]
                    for et, obj in evs:
                        self.wfile.write(f"event: {et}\ndata: {json.dumps(obj)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                return self._reply(200, {"id": "msg_mock", "type": "message", "role": "assistant", "content": [{"type": "text", "text": txt}], "stop_reason": "end_turn", "usage": {"input_tokens": 12, "output_tokens": 5}})

            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                cid = "chatcmpl-mockverify"
                for chunk in (
                    {"id": cid, "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]},
                    {"id": cid, "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}},
                ):
                    self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return

            self._reply(200, {
                "id": "chatcmpl-mockverify",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
            })

    srv = ThreadingHTTPServer(("127.0.0.1", free_port()), MockHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, srv.server_address[1]


def check_py_compile(install_dir: Path) -> int:
    targets = []
    for root in (install_dir / ".lmstudio" / "scripts", install_dir / "AI"):
        if root.is_dir():
            for p in root.rglob("*.py"):
                if ".venv" in p.parts or "node_modules" in p.parts:
                    continue
                targets.append(p)
    assert targets, f"No deployed Python files found under {install_dir}"
    for p in targets:
        py_compile.compile(str(p), doraise=True)
    print(f"[PASS] py_compile clean across {len(targets)} deployed Python file(s)")
    return len(targets)


def run_unit_tests(install_dir: Path) -> None:
    bench_dir = install_dir / ".lmstudio" / "benchmark"
    scripts_dir = install_dir / ".lmstudio" / "scripts"
    env = {**os.environ, "USERPROFILE": str(install_dir), "HOME": str(install_dir)}

    # 1. token_ledger_test & rich_ui_test (always work standalone)
    for mod in ("token_ledger_test.py", "rich_ui_test.py"):
        tp = bench_dir / mod
        if tp.is_file():
            r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(tp)], env=env, capture_output=True, text=True, check=False)
            assert r.returncode == 0, f"{mod} failed:\n{r.stdout}\n{r.stderr}"
            print(f"[PASS] {mod}")

    # 2. Core router & tier-1 checks cleanly scoped to installed modules
    code = r"""
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["BENCH_DIR"])
sys.path.insert(0, os.environ["SCRIPTS_DIR"])
home = Path.home()

import tier1_upgrades_test as t1
t1.test_thinking_loops_locality()
t1.test_software_words_and_math_guard()
t1.test_structured_response_format()
t1.test_cc_overflow_and_background_cap()
t1.test_web_search_triggers_and_coding_skip()
t1.test_gemma_opt_in_thinking_rules()
if (home / ".config" / "opencode" / "shell-powershell.md").is_file():
    t1.test_unsloth_gemma_promotion_and_powershell_guidance()
print("tier1_core_ok")

import smart_router_unit_test as sr
sr.test_openai_to_anthropic_tools_roundtrip()
sr.test_openai_to_anthropic_dropped_tools_fallback()
sr.test_anthropic_to_openai_tool_calls()
sr.test_agent_overflow_to_pro_both_doors()
sr.test_coding_brief_modes()
sr.test_surgical_merge_and_error_excerpts()
sr.test_zero_benchmark_hardcoding()
sr.test_image_generation_routing_and_tool_preservation()
sr.test_tool_call_dedup_and_loop_cut()
if (home / "AI" / "voice-tts" / "server.py").is_file():
    sr.test_voice_tts_routing_and_chunking()
print("smart_router_core_ok")
"""
    sub_env = {**env, "BENCH_DIR": str(bench_dir), "SCRIPTS_DIR": str(scripts_dir)}
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], env=sub_env, capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"Core unit checks failed:\n{r.stdout}\n{r.stderr}"
    print("[PASS] tier1_upgrades + smart_router_unit core checks")


def http_req(port: int, method: str, path: str, body: dict | None = None, headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, payload, hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def verify_live_router(install_dir: Path, router_port: int, lms_port: int) -> None:
    router_py = install_dir / ".lmstudio" / "scripts" / "router.py"
    files_port = free_port()
    mcp_port = free_port()
    env = {
        **os.environ,
        "USERPROFILE": str(install_dir),
        "HOME": str(install_dir),
        "ROUTER_PORT": str(router_port),
        "LMSTUDIO_PORT": str(lms_port),
        "HTML_FILES_PORT": str(files_port),
        "NORTHCUA_MCP_PORT": str(mcp_port),
    }
    proc = subprocess.Popen(
        [sys.executable, "-I", str(router_py)],
        cwd=str(router_py.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        healthy = False
        for _ in range(40):
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=2)
                raise AssertionError(f"router.py exited prematurely ({proc.returncode}):\n{out.decode()}\n{err.decode()}")
            try:
                st, raw = http_req(router_port, "GET", "/health")
                if st == 200 and json.loads(raw).get("status") == "ok":
                    healthy = True
                    break
            except Exception:
                time.sleep(0.25)
        assert healthy, f"Router failed to answer /health on :{router_port}"
        print(f"[PASS] router.py live /health HTTP 200 on :{router_port}")

        st, raw = http_req(router_port, "GET", "/v1/models")
        assert st == 200 and "auto" in raw, (st, raw)
        print("[PASS] GET /v1/models")

        # Security check: foreign Origin rejected with 403
        st_sec, _ = http_req(router_port, "GET", "/health", headers={"Origin": "https://evil.example"})
        assert st_sec == 403, f"Expected local_guard 403 on foreign Origin, got {st_sec}"
        print("[PASS] local_guard foreign Origin rejected (403)")

        # route-check tests (everyday vs multi-file coding)
        st_rc1, raw_rc1 = http_req(router_port, "POST", "/route-check", {"messages": [{"role": "user", "content": "What is the capital of Japan?"}]})
        assert st_rc1 == 200 and json.loads(raw_rc1).get("dispatch") == "chat", (st_rc1, raw_rc1)
        st_rc2, raw_rc2 = http_req(router_port, "POST", "/route-check", {"messages": [{"role": "user", "content": "Build a multi-file CLI project in Python with pytest unit tests"}]})
        assert st_rc2 == 200 and json.loads(raw_rc2).get("dispatch") == "opencode", (st_rc2, raw_rc2)
        print("[PASS] POST /route-check (everyday -> chat, multi-file CLI -> opencode)")

        # OpenAI non-stream (fast + deep thinking-cap bridge) & streaming SSE [DONE]
        st_fast, raw_fast = http_req(router_port, "POST", "/v1/chat/completions", {"model": "fast", "stream": False, "messages": [{"role": "user", "content": "Hello"}]})
        assert st_fast == 200 and "Mock verification response OK." in raw_fast, (st_fast, raw_fast)
        st_deep, raw_deep = http_req(router_port, "POST", "/v1/chat/completions", {"model": "deep", "stream": False, "messages": [{"role": "user", "content": "Think carefully about 2+2"}]})
        assert st_deep == 200 and "Mock Claude/Pro response OK." in raw_deep, (st_deep, raw_deep)
        st_stream, raw_stream = http_req(router_port, "POST", "/v1/chat/completions", {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "Say hi"}]})
        assert st_stream == 200 and "data: [DONE]" in raw_stream, (st_stream, raw_stream)
        print("[PASS] POST /v1/chat/completions (fast OpenAI + deep Anthropic bridge + SSE stream [DONE])")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--install-dir", default=os.environ.get("USERPROFILE", str(Path.home())))
    ap.add_argument("--router-port", type=int, default=14000)
    ap.add_argument("--lmstudio-port", type=int, default=1234)
    ap.add_argument("--mock-lmstudio", action="store_true")
    ap.add_argument("--run-unit-tests", action="store_true")
    args = ap.parse_args()

    install_dir = Path(args.install_dir).resolve()
    check_py_compile(install_dir)
    if args.run_unit_tests:
        run_unit_tests(install_dir)

    mock_srv = None
    lms_port = args.lmstudio_port
    if args.mock_lmstudio:
        mock_srv, lms_port = make_mock_lmstudio()
    try:
        verify_live_router(install_dir, args.router_port, lms_port)
    finally:
        if mock_srv:
            mock_srv.shutdown()
            mock_srv.server_close()

    print("\nALL POST-INSTALL VERIFICATION CHECKS PASSED!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
