"""Offline prompt-capture test for the V6 agent instructions.

Starts a dummy HTTP server on 127.0.0.1:4599 that records every POST body and answers 400 (no model is ever
loaded), points OpenCode and Claude Code at it, and counts which instruction sections each harness really sends.
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 4599
HOME = os.path.expandvars("%USERPROFILE%")
PROJECTS = os.path.join(HOME, "AI", "projects")
OPENCODE = os.path.join(HOME, r"AppData\Local\Programs\ai.opencode.desktop\resources\opencode-cli.exe")
CLAUDE_MD = os.path.join(HOME, r"AI\open-terminal\claude-config\CLAUDE.md")
CLAUDE_SETTINGS = os.path.join(HOME, r"AI\open-terminal\claude-config\settings.json")
MARKERS = {
    "core": "## Verify by running",
    "powershell_section": "## Shell: Windows PowerShell 5.1",
    "gitbash_section": "## Shell: the Bash tool is Git Bash",
    "old_v5_text": "Execute instead of guessing",
}
captured = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._reply(200, {"object": "list", "data": [{"id": "local-think", "object": "model"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        captured.append({"tag": CURRENT[0], "path": self.path, "body": body})
        self._reply(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "capture test: no model"}})


CURRENT = ["none"]


def run(tag, cmd, env, cwd, timeout=150):
    CURRENT[0] = tag
    start = len(captured)
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    t0 = time.time()
    while p.poll() is None and time.time() - t0 < timeout:
        if any('"tools"' in c["body"] for c in captured[start:]):
            time.sleep(2)
            break
        time.sleep(0.5)
    if p.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
    out = (p.stdout.read() or b"").decode("utf-8", "replace") if p.stdout else ""
    return captured[start:], out[-600:]


def report(tag, reqs, out):
    main = [r for r in reqs if '"tools"' in r["body"]]
    print(f"\n=== {tag}: {len(reqs)} request(s), {len(main)} with tools")
    if not main:
        print("  NO tool-bearing request captured. Output tail:\n ", out.replace("\n", "\n  "))
        return False
    body = max(main, key=lambda r: len(r["body"]))["body"]
    j = json.loads(body)
    counts = {k: body.count(json.dumps(v)[1:-1]) for k, v in MARKERS.items()}
    where = []
    if "system" in j and MARKERS["core"] in json.dumps(j["system"]):
        where.append("system")
    for m in j.get("messages", []):
        if MARKERS["core"] in json.dumps(m):
            where.append(m.get("role"))
    tools = [t.get("name") or t.get("function", {}).get("name") for t in j.get("tools", [])]
    max_out = j.get("max_tokens") or j.get("max_completion_tokens")
    print("  path:", max(main, key=lambda r: len(r["body"]))["path"], "| model:", j.get("model"), "| max_out:", max_out)
    print("  marker counts:", counts, "| core found in:", where)
    print("  tools:", len(tools))
    return counts


srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
results = {}
tmp = tempfile.mkdtemp(prefix="v6capture_")
try:
    # --- OpenCode: real global config + real instruction files. A temp project folder holds a project opencode.json
    # that adds a separate "captest" provider pointing at the dummy server; the run is forced onto it with --model, so
    # if the project config were not loaded the provider would not exist and nothing could reach the real router.
    # --standalone = private server (the shared background service already runs with the real config).
    proj = os.path.join(tmp, "oc-project")
    os.makedirs(proj)
    json.dump({"provider": {"local": {"options": {"baseURL": f"http://127.0.0.1:{PORT}/v1"},
                                      "models": {"background": {"name": "capture", "tool_call": True,
                                                                "limit": {"context": 65536, "output": 32768}}}}},
               "small_model": "local/background"}, open(os.path.join(proj, "opencode.json"), "w"))
    env = dict(os.environ)
    reqs, out = run("opencode", [OPENCODE, "run", "--standalone", "--model", "local/background",
                                 "Reply with the single word OK."], env, proj)
    results["opencode"] = report("OpenCode (real global config + instructions)", reqs, out)
    # --- Claude Code, Open WebUI path: CLAUDE_CONFIG_DIR holding CLAUDE.md (copy of the built file).
    cdir = os.path.join(tmp, "claude-config")
    os.makedirs(cdir)
    shutil.copy(CLAUDE_MD, cdir)
    shutil.copy(CLAUDE_SETTINGS, cdir)
    cenv = dict(os.environ, ANTHROPIC_BASE_URL=f"http://127.0.0.1:{PORT}", ANTHROPIC_AUTH_TOKEN="local",
                ANTHROPIC_MODEL="local-auto", ANTHROPIC_DEFAULT_HAIKU_MODEL="local-background",
                CLAUDE_CODE_MAX_CONTEXT_TOKENS="65536", CLAUDE_CODE_AUTO_COMPACT_WINDOW="56000",
                CLAUDE_CODE_MAX_OUTPUT_TOKENS="32768",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", CLAUDE_CONFIG_DIR=cdir)
    claude = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.exe")
    reqs, out = run("claude-openwebui", [claude, "-p", "Reply with the single word OK."], cenv, PROJECTS)
    results["claude_openwebui"] = report("Claude Code via CLAUDE_CONFIG_DIR (Open WebUI path)", reqs, out)

    # --- Claude Code, claude-local path: empty config dir + --append-system-prompt-file (as claude-local.cmd).
    edir = os.path.join(tmp, "empty-config")
    os.makedirs(edir)
    lenv = dict(cenv, CLAUDE_CONFIG_DIR=edir)
    reqs, out = run("claude-local", [claude, "--append-system-prompt-file", CLAUDE_MD, "-p",
                                     "Reply with the single word OK."], lenv, PROJECTS)
    results["claude_local"] = report("Claude Code via --append-system-prompt-file (claude-local path)", reqs, out)
finally:
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

expect = {
    "opencode": {"core": 1, "powershell_section": 1, "gitbash_section": 0, "old_v5_text": 0},
    "claude_openwebui": {"core": 1, "powershell_section": 0, "gitbash_section": 1, "old_v5_text": 0},
    "claude_local": {"core": 1, "powershell_section": 0, "gitbash_section": 1, "old_v5_text": 0},
}
passed = sum(1 for k, v in expect.items() if results.get(k) == v)
print(f"\nRESULT: {passed}/{len(expect)} harness paths send exactly core x1 + their own shell section x1, no old V5 text")
sys.exit(0 if passed == len(expect) else 1)
