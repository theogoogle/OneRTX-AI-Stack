"""Complete HTML answers from the coding models: save them as files, serve them, check them in a headless browser.

Why (2026-11-07): the user got 2,000-line apps as code in the chat with no file or link ("where is it?"), and a broken app
gave him nothing to tell the model. Now the router saves the file, adds a link (PC and phone, over the tailnet) and the
result of a quick browser check (page errors with line numbers, a screen still covered after clicking Start), with a
reply he can paste to get it fixed.

Files are served by a small separate server (127.0.0.1:4001; `tailscale serve --https=8443` publishes it to the
tailnet only). A separate port is a separate browser origin: a generated page cannot read Open WebUI's login token or
call the router's API with it.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

GENERATED_DIR = Path.home() / "AI" / "generated"
FILES_HOST, FILES_PORT = "127.0.0.1", int(os.environ.get("HTML_FILES_PORT", "4001"))
TAILNET_HTTPS_PORT = 8443
TAILNET_HOSTNAME = "<pc-name>.<tailnet>.ts.net"     # the name `tailscale serve` forwards for the phone link
PLAYWRIGHT = Path.home() / "AI" / "playwright-mcp" / "node_modules" / "playwright"
CHECK_JS = Path(__file__).with_name("html_check.js")
CHECK_TIMEOUT_S = 75
NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW: the router runs under pythonw, no console flashes

HTML_BLOCK = re.compile(r"```[ \t]*(?:html|htm)?[^\n]*\n(.*?)```", re.S | re.I)
SEARCH_REPLACE_RE = re.compile(
    r"<{5,9}[ \t]*SEARCH[^\n]*\n(.*?)\n={5,9}[ \t]*\n(.*?)\n>{5,9}[ \t]*REPLACE",
    re.S,
)
ERR_LINE_RE = re.compile(r"(?:\bline\s+|:)(\d{1,5})\b", re.I)
ERR_IDENT_RE = re.compile(
    r"(?:Identifier\s+['\"`]([A-Za-z_$][\w$]*)['\"`]"
    r"|([A-Za-z_$][\w$]*)\s+is\s+not\s+(?:defined|a\s+function)"
    r"|reading\s+['\"`]([A-Za-z_$][\w$]*)['\"`])"
)
_base = {"url": None, "at": 0.0}


def extract_html(text):
    """The largest complete HTML document in a model answer (fenced or bare), else None."""
    best = None
    for code in HTML_BLOCK.findall(text or ""):
        low = code.lower()
        if ("<!doctype html" in low or "<html" in low) and "</html>" in low and (best is None or len(code) > len(best)):
            best = code
    if best is None:
        m = re.search(r"(<!DOCTYPE html.*</html>|<html\b.*</html>)", text or "", re.S | re.I)
        best = m.group(1) if m else None
    return best


def _msg_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def find_latest_html(messages, min_lines=30):
    """Latest complete HTML document (>= min_lines) in conversation messages (assistant or pasted by user)."""
    for m in reversed(messages or []):
        if m.get("role") in ("assistant", "user"):
            h = extract_html(_msg_text(m.get("content")))
            if h and h.count("\n") + 1 >= min_lines:
                return h
    return None


def extract_search_replace(text):
    """Return list of (search_str, replace_str) blocks from `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE`."""
    norm = (text or "").replace("\r\n", "\n")
    return SEARCH_REPLACE_RE.findall(norm)


def apply_search_replace(base_html, reply_text):
    """Apply `SEARCH / REPLACE` blocks from `reply_text` onto `base_html`.
    Returns `(merged_html, applied_count, total_blocks)`."""
    blocks = extract_search_replace(reply_text)
    if not blocks or not base_html:
        return None, 0, len(blocks)
    cur = base_html.replace("\r\n", "\n")
    applied = 0
    for s_raw, r_raw in blocks:
        if s_raw and s_raw in cur:
            cur = cur.replace(s_raw, r_raw, 1)
            applied += 1
            continue
        # Fallback: match stripped lines uniquely if indentation shifted slightly
        s_lines = [ln.strip() for ln in s_raw.split("\n")]
        while s_lines and not s_lines[0]:
            s_lines.pop(0)
        while s_lines and not s_lines[-1]:
            s_lines.pop()
        if not s_lines:
            continue
        cur_lines = cur.split("\n")
        cur_stripped = [ln.strip() for ln in cur_lines]
        n = len(s_lines)
        hits = [i for i in range(len(cur_lines) - n + 1) if cur_stripped[i:i + n] == s_lines]
        if len(hits) == 1:
            idx = hits[0]
            cur_lines[idx:idx + n] = r_raw.split("\n")
            cur = "\n".join(cur_lines)
            applied += 1
    if applied == 0:
        return None, 0, len(blocks)
    return cur, applied, len(blocks)


def numbered_error_excerpts(html, errors, covered=None, radius=14, max_windows=5):
    """Build compact numbered line excerpts around line numbers & identifiers named in browser errors."""
    lines = (html or "").replace("\r\n", "\n").split("\n")
    if not lines:
        return ""
    target_lines = set()
    idents = set()
    for err in errors or []:
        for m in ERR_LINE_RE.findall(str(err)):
            ln = int(m)
            if 1 <= ln <= len(lines):
                target_lines.add(ln)
        for g in ERR_IDENT_RE.findall(str(err)):
            ident = next((x for x in g if x), "")
            if len(ident) >= 3:
                idents.add(ident)
    if covered:
        cov_id = str(covered).lstrip("#.").split()[0].strip()
        if len(cov_id) >= 3:
            idents.add(cov_id)
    for ident in sorted(idents):
        pat = re.compile(r"\b" + re.escape(ident) + r"\b")
        matches = [i + 1 for i, ln in enumerate(lines) if pat.search(ln)]
        for ln in matches[:4]:
            target_lines.add(ln)
    if not target_lines:
        return ""
    # Merge overlapping [ln - radius, ln + radius] ranges
    intervals = []
    for ln in sorted(target_lines):
        lo, hi = max(1, ln - radius), min(len(lines), ln + radius)
        if intervals and lo <= intervals[-1][1] + 3:
            intervals[-1][1] = max(intervals[-1][1], hi)
        else:
            intervals.append([lo, hi])
    parts = []
    for lo, hi in intervals[:max_windows]:
        snippet = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(lo, hi + 1))
        parts.append(f"Lines {lo}-{hi} (of {len(lines)}):\n```html\n{snippet}\n```")
    return "\n\n".join(parts)


def _slug(html):
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    s = unicodedata.normalize("NFKD", m.group(1) if m else "app").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "app"


def save(html):
    day = time.strftime("%Y-%m-%d")
    folder = GENERATED_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%H%M%S')}-{_slug(html)}.html"
    path = folder / name
    path.write_text(html, encoding="utf-8")
    return path, f"{day}/{name}"


def public_base():
    """https://<this PC>.ts.net:8443 when tailscale serves the files port, else the local address (cached 10 min)."""
    if _base["url"] and time.time() - _base["at"] < 600:
        return _base["url"]
    url = f"http://{FILES_HOST}:{FILES_PORT}"
    exe = shutil.which("tailscale")
    try:
        if exe:
            st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10,
                                           creationflags=NO_WINDOW).stdout or "{}")
            dns = ((st.get("Self") or {}).get("DNSName") or "").rstrip(".")
            serve = subprocess.run([exe, "serve", "status"], capture_output=True, text=True, timeout=10,
                                   creationflags=NO_WINDOW).stdout
            if dns and f":{TAILNET_HTTPS_PORT}" in serve and f":{FILES_PORT}" in serve:
                url = f"https://{dns}:{TAILNET_HTTPS_PORT}"
    except Exception:
        pass
    _base.update(url=url, at=time.time())
    return url


def check(path):
    """Headless browser check (html_check.js). Returns its JSON, or None when the check cannot run."""
    node = shutil.which("node")
    if not node or not PLAYWRIGHT.exists() or not CHECK_JS.exists():
        return None
    try:
        r = subprocess.run([node, str(CHECK_JS), str(PLAYWRIGHT), str(path)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT_S, creationflags=NO_WINDOW)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        return json.loads(lines[-1]) if lines else {"failed": (r.stderr or "no output")[-300:]}
    except subprocess.TimeoutExpired:
        return {"failed": f"the check took more than {CHECK_TIMEOUT_S} s"}
    except Exception as e:
        return {"failed": f"{e.__class__.__name__}: {e}"}


def resolve_and_check(answer_text, base_html=None):
    """Extract complete HTML (or merge SEARCH/REPLACE patches onto `base_html`), save & run browser check.
    Returns `(html, path, rel, res, merged_blocks)` or `(None, None, None, None, 0)`."""
    html = extract_html(answer_text)
    merged_blocks = 0
    if not html and base_html:
        merged, applied, _total = apply_search_replace(base_html, answer_text)
        if merged and applied > 0:
            html, merged_blocks = merged, applied
    if not html:
        return None, None, None, None, 0
    path, rel = save(html)
    res = check(path)
    return html, path, rel, res, merged_blocks


def format_footer(path, rel, res, merged_blocks=0, auto_repaired=False):
    url = f"{public_base()}/{quote(rel)}"
    notes = []
    if merged_blocks:
        notes.append(f"merged {merged_blocks} surgical patch{'es' if merged_blocks > 1 else ''}")
    if auto_repaired:
        notes.append("auto-repaired browser error")
    extra = f" · *({', '.join(notes)})*" if notes else ""
    md = [f"\n\n---\n**📄 Saved:** [{path.name}]({url}) (opens in your browser, also on your phone){extra} · on this PC: `{path}`"]
    info = {"html_file": str(path)}
    if merged_blocks:
        info["surgical_merged"] = merged_blocks
    if auto_repaired:
        info["html_repaired"] = True
    if res is None or res.get("failed"):
        md.append(f"**🔎 Browser check:** could not run ({(res or {}).get('failed', 'node or Playwright missing')}).")
        info["html_check"] = "not run"
        return "\n\n".join(md), info
    errors, clicked, covered = res.get("errors") or [], res.get("clicked"), res.get("covered")
    if not errors and not covered:
        done = f', clicked "{clicked}"' if clicked else ""
        shown = " and the 3D view is visible" if res.get("canvas") else ""
        md.append(f"**🔎 Browser check:** ✅ it opens with no errors{done}{shown}. "
                  "(An automatic check: it cannot judge how it looks or whether every feature works.)")
        info["html_check"] = "ok"
        return "\n\n".join(md), info
    md.append("**🔎 Browser check: ⚠️ problem found**")
    md += [f"- `{e}`" for e in errors[:5]]
    if covered:
        md.append(f"- After {'clicking “' + clicked + '”' if clicked else 'loading'}, the 3D view is still hidden "
                  f"behind `{covered}`: the page is probably stuck.")
    if errors:
        say = f"The browser shows this error: {errors[0]}. Find the cause, fix it and send the surgical patch."
    else:
        say = (f"After {'I click “' + clicked + '”' if clicked else 'it loads'}, the page stays on {covered}. "
               "Trace step by step what runs at that moment and fix the cause.")
    md.append(f"To get it fixed, reply: *{say}*")
    info["html_check"] = f"{len(errors)} error(s)" + (", covered" if covered else "")
    return "\n\n".join(md), info


def footer(answer_text, base_html=None):
    """(markdown to append to the answer, log info) or (None, None) when the answer holds no HTML / applicable patch."""
    html, path, rel, res, merged = resolve_and_check(answer_text, base_html=base_html)
    if not html:
        return None, None
    return format_footer(path, rel, res, merged_blocks=merged)


# ---------------------------------------------------------------- file server (127.0.0.1:4001, GET only)
SERVED_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".pdf": "application/pdf",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _parse_byte_range(header_val, total_len):
    """Parse HTTP `Range: bytes=start-end` header; returns `(start, end)` inclusive or `None` if invalid/absent."""
    if not header_val or total_len <= 0:
        return None
    m = re.match(r"^\s*bytes=(\d*)-(\d*)\s*$", str(header_val))
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if not m.group(1):
        suffix = int(m.group(2))
        if suffix <= 0:
            return None
        return max(0, total_len - suffix), total_len - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else total_len - 1
    if start >= total_len or end < start:
        return "invalid"
    return start, min(end, total_len - 1)


class _Files(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        import local_guard            # same folder; refuses foreign host names (DNS rebinding)
        reason = local_guard.refuse_reason(self.headers, extra_hostnames=(TAILNET_HOSTNAME,))
        if reason:
            return self._send(403, f"Forbidden: {reason}".encode(), "text/plain; charset=utf-8")
        path_part, _, query = self.path.partition("?")
        rel = unquote(path_part).lstrip("/")
        if rel in ("", "index.html"):
            return self._index()
        try:
            path = (GENERATED_DIR / rel).resolve()
            ctype = SERVED_MEDIA_TYPES.get(path.suffix.lower())
            ok = bool(ctype) and path.is_file() and GENERATED_DIR.resolve() in path.parents
        except (OSError, ValueError):
            ok, ctype = False, None
        if not ok:
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        if path.suffix.lower() == ".html":
            return self._send(200, path.read_bytes(), ctype)
        size = path.stat().st_size
        qs = parse_qs(query, keep_blank_values=True) if query else {}
        media_headers = {"Accept-Ranges": "bytes"}
        if qs.get("download", [""])[0].lower() in ("1", "true", "yes"):
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name) or "audio.mp3"
            media_headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        rng = _parse_byte_range(self.headers.get("Range"), size)
        if rng == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng is not None:
            start, end = rng
            length = end - start + 1
            with path.open("rb") as f:
                f.seek(start)
                chunk = f.read(length)
            return self._send(
                206,
                chunk,
                ctype,
                extra_headers={
                    **media_headers,
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
            )
        self._send(200, path.read_bytes(), ctype, extra_headers=media_headers)

    def _index(self):
        files = sorted(GENERATED_DIR.rglob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]
        rows = "".join(
            f'<li><a href="{quote(p.relative_to(GENERATED_DIR).as_posix())}">{p.relative_to(GENERATED_DIR).as_posix()}</a></li>'
            for p in files
        )
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                f"<title>Generated apps</title><body style='font-family:system-ui;margin:16px'>"
                f"<h1>Generated apps &amp; podcast audio</h1><ol>{rows or '<li>none yet</li>'}</ol>")
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _send(self, status, data, ctype, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


def start_server():
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((FILES_HOST, FILES_PORT), _Files)
    threading.Thread(target=srv.serve_forever, daemon=True, name="html-files").start()
    return srv
