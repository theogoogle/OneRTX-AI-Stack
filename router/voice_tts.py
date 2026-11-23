"""Voice-cloning podcast narration client for the router (talks to 127.0.0.1:8945)."""
import http.client
import json
import re
import time

VOICE_HOST, VOICE_PORT = "127.0.0.1", 8945
VOICE_VRAM_MB = 9000   # unload LM Studio GPU models if free VRAM is below 9 GB

_VOICE_EXCL_RE = re.compile(
    r"^\s*(?:what|how|why|which|where|when|explain|describe|compare(?!.*:)|list|recommend|"
    r"comment|pourquoi|quel(?:le)?s?|explique|d[ée]cris)\b"
    r"|```|\b(?:python|javascript|typescript|html|css|script|code|coding|app|website|api|server|function|program)\b",
    re.I,
)
_VOICE_DIRECT_RE = re.compile(
    r"(?:"
    r"\b(?:read|narrate|speak|tell|voice|say|recite|record|convert|turn|generate|make|"
    r"lis|raconte|narre|dis|r[ée]cite|enregistre|convertis|transforme|fais|g[ée]n[èe]re|cr[ée]e)\b"
    r"[^.!?\n]{0,110}?"
    r"\b(?:(?:in|with|using)\s+my\s+(?:cloned?\s+)?voice|(?:with\s+)?my\s+(?:cloned?\s+)?voice|"
    r"(?:avec|dans)\s+ma\s+voix(?:\s+clon[ée]e)?|voix\s+clon[ée]e)\b"
    r"|^\s*(?:can|could|would|will|peux[- ]tu|pourrais[- ]tu|pouvez[- ]vous)?[^\n]{0,140}?"
    r"\b(?:read|narrate|lis|raconte|narre|podcast|audiobook|qwen3?-tts|chatterbox)\b[^\n]{0,140}:"
    r"|^\s*(?:compare|test|try|essaie|teste)\b[^\n]{0,120}\b(?:qwen3?|chatterbox)\b[^\n]{0,120}:"
    r")",
    re.I,
)


def is_voice_narration_request(text: str) -> bool:
    """True when the prompt explicitly asks to narrate/read pasted story text in the user's cloned voice."""
    if not text:
        return False
    head = text.strip()[:300]
    if not head or _VOICE_EXCL_RE.search(head):
        return False
    return bool(_VOICE_DIRECT_RE.search(head))


def health(timeout: float = 2.0):
    conn = http.client.HTTPConnection(VOICE_HOST, VOICE_PORT, timeout=timeout)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        raw = resp.read()
        return json.loads(raw) if resp.status == 200 else None
    except Exception:
        return None
    finally:
        conn.close()


def available() -> bool:
    return health() is not None


def status():
    """Return (active_engine_or_None, busy_bool)."""
    h = health()
    if not h:
        return None, False
    eng = h.get("active_engine")
    return eng, bool(eng)


def unload(timeout: float = 15.0) -> bool:
    conn = http.client.HTTPConnection(VOICE_HOST, VOICE_PORT, timeout=timeout)
    try:
        conn.request("POST", "/unload", b"{}", {"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        return resp.status == 200
    except Exception:
        return False
    finally:
        conn.close()


def narrate_stream(body: dict, on_progress=None, should_stop=None, timeout: int = 3600) -> dict:
    """Call POST /narrate and stream NDJSON progress until result."""
    conn = http.client.HTTPConnection(VOICE_HOST, VOICE_PORT, timeout=timeout)
    try:
        conn.request("POST", "/narrate", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"voice-tts returned HTTP {resp.status}: {resp.read().decode('utf-8', 'replace')[:300]}")
        while True:
            if should_stop and should_stop():
                unload()
                raise RuntimeError("cancelled by client")
            chunk = resp.readline()
            if not chunk:
                break
            line = chunk.decode("utf-8", "replace").strip()
            if not line:
                continue
            ev = json.loads(line)
            kind = ev.get("type")
            if kind == "progress":
                if on_progress:
                    on_progress(ev.get("message", ""))
            elif kind == "result":
                return ev
            elif kind == "error":
                raise RuntimeError(ev.get("error") or "voice narration failed")
        raise RuntimeError("voice-tts stream ended without a result")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def speech_bytes(body: dict, timeout: int = 300):
    """Proxy OpenAI-format POST /v1/audio/speech -> (status, content_type, raw_bytes)."""
    conn = http.client.HTTPConnection(VOICE_HOST, VOICE_PORT, timeout=timeout)
    try:
        conn.request("POST", "/v1/audio/speech", json.dumps(body, ensure_ascii=False).encode("utf-8"),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        ctype = resp.getheader("Content-Type") or "audio/mpeg"
        return resp.status, ctype, data
    finally:
        conn.close()


def format_narration_markdown(res: dict) -> str:
    """Format an inline Open WebUI audio player + MP3 download reply (plus A/B player link when comparing)."""
    lang_label = "French (FR)" if res.get("language") == "fr" else "English (EN)"
    tracks = res.get("tracks") or []
    lines = [
        f"### 🎙️ {res.get('title', 'Podcast Story')}",
        f"**Language:** {lang_label} · **Voice:** `{res.get('voice', 'default')}` · "
        f"**Length:** {res.get('word_count', 0)} words ({res.get('chunk_count', 0)} segments)",
        "",
    ]
    if len(tracks) > 1 and res.get("player_url"):
        lines.append(f"🎧 **[Open Podcast Player & A/B Compare]({res['player_url']})** *(works on PC & phone)*")
        lines.append("")
    for tr in tracks:
        url = tr.get("url", "")
        dl_sep = "&" if "?" in url else "?"
        dl_url = f"{url}{dl_sep}download=1" if url else ""
        lines.append(
            f"**{tr['label']}** ({tr['duration_str']} audio, generated in {tr['elapsed_str']}) · "
            f"[Download {tr['filename']}]({dl_url})"
        )
        if url:
            lines.append("")
            lines.append(f"<div><audio>{url}</audio></div>")
            lines.append("")
    if not res.get("has_transcript"):
        lines.append(
            "> ℹ️ *Tip: adding a `.txt` transcript next to your reference voice clip in "
            "`%USERPROFILE%\\AI\\voice-tts\\voices` (or in http://127.0.0.1:8945) improves Qwen3-TTS voice similarity.*"
        )
    return "\n".join(lines).rstrip()
