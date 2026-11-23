"""make_3d_scene: an MCP tool (stdio) for the coding agents (Open Code, Claude Code) in Open WebUI.

The agent (Qwen3.8 IQ3_S) calls it when a project needs a 3D world. The tool asks the specialised 3D coder
(qwen3.5-4b-3d-coder, Three.js scenes) through the router (thinking cap, loop stop and stall watchdog apply), saves the
HTML inside the projects folder, runs the headless browser check, and returns the result. The agent then builds the
app logic on top. Why (2026-11-08): the 3D coder makes far better-looking scenes than general coding models, but it only
knows how to write one scene in one go; it cannot debug or edit (it looped trying to fix its own file).

Started by the agents themselves (stdio): no port, no key. Python standard library only.
Writes only .html files under SCENE3D_ROOT (default %USERPROFILE%\\AI\\projects). Log: scene3d.log next to this file.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import http.client
from pathlib import Path

ROOT = Path(os.environ.get("SCENE3D_ROOT") or Path.home() / "AI" / "projects").resolve()
ROUTER_HOST, ROUTER_PORT = "127.0.0.1", 4000
MODEL = "qwen3.5-4b-3d-coder"
REPAIR_MODEL = "deep"             # Pro (Qwen3.6 35B-A3B at ~45 tok/s): fast 1-turn surgical JS syntax/runtime repair
TIMEOUT_S = 1500
PLAYWRIGHT = Path.home() / "AI" / "playwright-mcp" / "node_modules" / "playwright"
SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
CHECK_JS = SCRIPTS_DIR / "html_check.js"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import html_files
LOG = Path(__file__).with_name("scene3d.log")
NO_WINDOW = 0x08000000
_lock = threading.Lock()          # one scene at a time (one GPU)

TOOL = {
    "name": "make_3d_scene",
    "description": (
        "Generate a 3D scene (Three.js, one self-contained HTML file) with the local specialised 3D model and save it in "
        "the project. Use it whenever the task needs a 3D world, environment, building, room, landscape or object: it "
        "makes much better-looking scenes than writing them yourself. It takes 3-10 minutes. Then read the file and add "
        "the app logic yourself (controls, UI, game rules), keeping the scene code and the Three.js import as they are. "
        "Write the description in English, at most ~1,000 characters: layout, objects, materials, colours, lighting and "
        "camera (orbit view). Static scene only: no gameplay, menus or quizzes. If you need named objects or positions "
        "for later logic, ask for them in the description (e.g. 'group each room under a THREE.Group named room-<name>')."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "English description of the scene (what to show, layout, style)."},
            "file": {"type": "string", "description": "Where to save it, relative to the project folder, ending in .html (e.g. scene.html)."},
        },
        "required": ["description", "file"],
    },
}


def log(msg):
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def target_path(file):
    p = Path(str(file or "").strip().strip('"'))
    if not p.name:
        raise ValueError("file is required (e.g. scene.html)")
    if not p.is_absolute():
        p = Path.cwd() / p
    p = p.resolve()
    if p.suffix.lower() not in (".html", ".htm"):
        raise ValueError("file must end in .html")
    if ROOT != p and ROOT not in p.parents:
        raise ValueError(f"file must be inside the projects folder {ROOT}")
    return p


def scene_prompt(description):
    d = " ".join(str(description or "").split())
    if not d:
        raise ValueError("description is required")
    if not re.match(r"(?i)^(build|create|make|please create|generate)\b", d):
        d = "Build a self-contained HTML scene with Three.js from cdnjs featuring " + d[0].lower() + d[1:]
    return d


def ask_router_model(model_name, messages, internal_repair=False, internal_scene3d=False):
    """Stream from the router (so its stall watchdog and thinking-loop stop apply). Returns the answer text."""
    body = {"model": model_name, "stream": True, "messages": messages}
    if internal_repair:
        body["_internal_repair"] = True
    if internal_scene3d:
        body["_internal_scene3d"] = True
    conn = http.client.HTTPConnection(ROUTER_HOST, ROUTER_PORT, timeout=TIMEOUT_S)
    conn.request("POST", "/v1/chat/completions", json.dumps(body).encode(), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"router answered HTTP {resp.status}: {resp.read()[:300].decode('utf-8', 'replace')}")
    text, buf, error = [], b"", None
    while True:
        chunk = resp.read1(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.startswith(b"data: {"):
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            if "error" in ev:
                error = (ev["error"] or {}).get("message") or str(ev["error"])
                continue
            text.append(((ev.get("choices") or [{}])[0].get("delta") or {}).get("content") or "")
    conn.close()
    answer = "".join(text)
    if error and "</html>" not in answer.lower() and ">>>>>>> replace" not in answer.lower():
        raise RuntimeError(error)
    return answer


def ask_3d_coder(prompt):
    return ask_router_model(MODEL, [{"role": "user", "content": prompt}], internal_scene3d=True)


def auto_repair_scene(path, html, chk):
    """Targeted surgical fix pass(es) via Pro (45 tok/s) when the 3D coder leaves a JS syntax/runtime error.
    Runs a 2nd pass strictly when pass 1 fixed a parse-time SyntaxError and uncovered a runtime/overlay issue."""
    cur_html, cur_chk, improved = html, chk, False
    for attempt in range(2):
        errs = list((cur_chk or {}).get("errors") or [])
        covered = (cur_chk or {}).get("covered")
        if not errs and not covered:
            break
        excerpts = html_files.numbered_error_excerpts(cur_html, errs, covered=covered, radius=16)
        prob_lines = [f"- {e}" for e in errs[:4]]
        if covered:
            prob_lines.append(f"- Viewport remains covered by {covered} after startup")
        context_part = excerpts if excerpts else f"Full HTML ({len(cur_html.splitlines())} lines):\n```html\n{cur_html}\n```"
        fix_prompt = (
            "Fix the JavaScript/HTML runtime or syntax error(s) in this Three.js scene using minimal exact "
            "`<<<<<<< SEARCH` / `=======` / `>>>>>>> REPLACE` block(s). Do NOT rewrite the whole file — only change "
            "the offending lines (for example, rename a duplicate `const` identifier or hoist a loop variable into scope). "
            "Do NOT include line-number prefixes (`123| `) inside your SEARCH or REPLACE blocks.\n\n"
            "Browser error(s):\n" + "\n".join(prob_lines) + "\n\n" + context_part
        )
        try:
            log(f"auto-repair pass {attempt + 1} start ({REPAIR_MODEL}): {errs[:2]} covered={covered}")
            reply_txt = ask_router_model(
                REPAIR_MODEL,
                [
                    {"role": "system", "content": "Output only minimal exact <<<<<<< SEARCH / ======= / >>>>>>> REPLACE patch blocks that fix the error."},
                    {"role": "user", "content": fix_prompt},
                ],
                internal_repair=True,
            )
            cand_html, applied, _tot = html_files.apply_search_replace(cur_html, reply_txt)
            if not cand_html:
                cand_html = extract_html(reply_txt)
            if not cand_html or cand_html == cur_html:
                log(f"auto-repair pass {attempt + 1} produced no applicable patch; stopping")
                break
            path.write_text(cand_html, encoding="utf-8")
            new_chk = browser_check(path)
            old_bad = (len(errs), 1 if covered else 0)
            new_errs = list(new_chk.get("errors") or [])
            new_bad = (len(new_errs), 1 if new_chk.get("covered") else 0)
            if not new_chk.get("failed") and new_bad < old_bad:
                log(f"auto-repair pass {attempt + 1} succeeded ({applied} patch block(s)): {old_bad} -> {new_bad}")
                had_syntax = any("SyntaxError" in str(e) for e in errs)
                still_syntax = any("SyntaxError" in str(e) for e in new_errs)
                cur_html, cur_chk, improved = cand_html, new_chk, True
                if attempt == 0 and had_syntax and not still_syntax and (new_errs or new_chk.get("covered")):
                    continue
                break
            path.write_text(cur_html, encoding="utf-8")
            log(f"auto-repair pass {attempt + 1} did not improve check ({old_bad} -> {new_bad}); reverted to best")
            break
        except Exception as e:
            path.write_text(cur_html, encoding="utf-8")
            log(f"auto-repair pass {attempt + 1} failed ({e.__class__.__name__}: {e}); kept best")
            break
    return cur_html, cur_chk, improved


def extract_html(text):
    best = None
    for code in re.findall(r"```[ \t]*(?:html|htm)?[^\n]*\n(.*?)```", text or "", re.S | re.I):
        low = code.lower()
        if ("<!doctype html" in low or "<html" in low) and "</html>" in low and (best is None or len(code) > len(best)):
            best = code
    if best is None:
        m = re.search(r"(<!DOCTYPE html.*</html>)", text or "", re.S | re.I)
        best = m.group(1) if m else None
    return best


def browser_check(path):
    try:
        r = subprocess.run(["node", str(CHECK_JS), str(PLAYWRIGHT), str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90, creationflags=NO_WINDOW)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        return json.loads(lines[-1]) if lines else {"failed": (r.stderr or "no output")[-200:]}
    except Exception as e:
        return {"failed": f"{e.__class__.__name__}: {e}"}


def make_3d_scene(args):
    path = target_path(args.get("file"))
    prompt = scene_prompt(args.get("description"))
    if not _lock.acquire(timeout=TIMEOUT_S):
        raise RuntimeError("another scene is still being generated")
    try:
        t0 = time.time()
        log(f"start: {path} <- {prompt[:160]}")
        html = extract_html(ask_3d_coder(prompt))
        if not html:
            raise RuntimeError("the 3D model did not return a complete HTML file; try a shorter, more concrete description")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        chk = browser_check(path)
        repaired = False
        if not chk.get("failed") and (chk.get("errors") or chk.get("covered")):
            html, chk, repaired = auto_repair_scene(path, html, chk)
        secs = round(time.time() - t0)
    finally:
        _lock.release()
    lines = html.count("\n") + 1
    if chk.get("failed"):
        verdict = f"browser check could not run ({chk['failed']})"
    elif chk.get("errors") or chk.get("covered"):
        problems = list(chk.get("errors") or [])
        if chk.get("covered"):
            problems.append(f"the 3D view is still covered by {chk['covered']} after loading (probably stuck)")
        verdict = "PROBLEMS FOUND, fix them before adding logic:\n- " + "\n- ".join(problems)
    else:
        verdict = "it opens in a browser with no errors" + (" and the 3D view is visible" if chk.get("canvas") else "")
        if repaired:
            verdict += " (auto-repaired 1 browser error pass)"
    log(f"done in {secs}s: {path} ({lines} lines): {verdict[:200]}")
    made_by = f"{MODEL} + auto-repair via {REPAIR_MODEL}" if repaired else MODEL
    return (f"Saved {path} ({lines} lines, {secs} s, made by the 3D model {made_by}).\n"
            f"Browser check: {verdict}\n"
            "It uses the Three.js 0.160 ES module from cdnjs: keep that import. Read the file before adding logic; "
            "keep its geometry code and add controls/UI/logic on top.")


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:            # notifications (initialized, cancelled...)
            continue
        if method == "initialize":
            reply(msg_id, {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "scene3d", "version": "1.0.0"}})
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": [TOOL]})
        elif method == "tools/call":
            if params.get("name") != TOOL["name"]:
                reply(msg_id, {"content": [{"type": "text", "text": f"Unknown tool: {params.get('name')}"}], "isError": True})
                continue
            try:
                text, is_error = make_3d_scene(params.get("arguments") or {}), False
            except Exception as e:
                text, is_error = f"make_3d_scene failed: {e}", True
                log(f"error: {e}")
            reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
        else:
            reply(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()
