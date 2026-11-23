"""Local Podcast Voice-Cloning Studio & TTS Server (127.0.0.1:8945).

Runs isolated worker subprocesses for Qwen3-TTS (.venv-qwen, Apache 2.0) and
Chatterbox Multilingual (.venv-chatterbox, MIT) so dependencies never collide and
unloading releases 100% of GPU VRAM back to LM Studio / ComfyUI.
"""
import base64
import json
import os
import queue
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import audio_stitch  # noqa: E402
import html_files    # noqa: E402
import local_guard   # noqa: E402
import text_chunks   # noqa: E402

HOST, PORT = "127.0.0.1", 8945
VOICES_DIR = ROOT / "voices"
CONFIG_FILE = VOICES_DIR / "config.json"
STUDIO_HTML = ROOT / "studio.html"
LOG_FILE = ROOT / "voice-tts.log"
NO_WINDOW = 0x08000000
IDLE_UNLOAD_S = 600

QWEN_PY = ROOT / ".venv-qwen" / "Scripts" / "python.exe"
CHATTERBOX_PY = ROOT / ".venv-chatterbox" / "Scripts" / "python.exe"
QWEN_WORKER = ROOT / "worker_qwen.py"
CHATTERBOX_WORKER = ROOT / "worker_chatterbox.py"

VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm", ".aac"}
DEFAULT_CONFIG = {
    "default_engine": "qwen3",
    "default_en": "",
    "default_fr": "",
    "exaggeration": 0.55,
    "cfg_weight": 0.5,
    "seed": 42,
}

_log_lock = threading.Lock()


def log(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    with _log_lock:
        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass


def load_config() -> dict:
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:
            pass
    return cfg


def save_config(patch: dict) -> dict:
    cfg = load_config()
    if "default_engine" in patch and patch["default_engine"] in ("both", "qwen3", "chatterbox"):
        cfg["default_engine"] = patch["default_engine"]
    for k in ("default_en", "default_fr"):
        if k in patch:
            v = str(patch[k] or "").strip()
            if not v or VALID_NAME_RE.match(v):
                cfg[k] = v
    if "exaggeration" in patch:
        cfg["exaggeration"] = max(0.1, min(1.2, float(patch["exaggeration"])))
    if "cfg_weight" in patch:
        cfg["cfg_weight"] = max(0.1, min(1.0, float(patch["cfg_weight"])))
    if "seed" in patch:
        cfg["seed"] = int(patch["seed"])
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def _convert_dropped_audio():
    """Auto-convert any non-WAV audio file dropped into voices/ into clean 24kHz mono WAV."""
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    for p in sorted(VOICES_DIR.iterdir()):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS or p.suffix.lower() == ".wav":
            continue
        dst = p.with_suffix(".wav")
        if not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime:
            try:
                audio_stitch.convert_to_ref_wav(p, dst)
                log(f"converted {p.name} -> {dst.name}")
            except Exception as e:
                log(f"convert {p.name} failed: {e}")


def list_voices() -> list:
    _convert_dropped_audio()
    cfg = load_config()
    out = []
    for p in sorted(VOICES_DIR.glob("*.wav")):
        stem = p.stem
        txt_p = p.with_suffix(".txt")
        transcript = txt_p.read_text(encoding="utf-8").strip() if txt_p.exists() else ""
        out.append({
            "name": stem,
            "wav": str(p),
            "transcript": transcript,
            "has_transcript": bool(transcript),
            "is_default_en": cfg.get("default_en") == stem,
            "is_default_fr": cfg.get("default_fr") == stem,
        })
    return out


def resolve_voice(language: str, requested_name: str = None, ref_audio_override: str = None, ref_text_override: str = None):
    """Find best matching (wav_path, transcript, voice_name) for EN or FR."""
    if ref_audio_override:
        voices_root = VOICES_DIR.resolve()
        raw_p = Path(ref_audio_override)
        cand = raw_p if raw_p.is_absolute() else (voices_root / raw_p)
        p = cand.resolve()
        if p == voices_root or not p.is_relative_to(voices_root) or p.suffix.lower() != ".wav" or not p.is_file():
            raise ValueError("ref_audio_override must resolve to an existing .wav file inside VOICES_DIR")
        txt = ref_text_override
        if txt is None:
            txt_p = p.with_suffix(".txt").resolve()
            if txt_p.is_relative_to(voices_root) and txt_p.is_file():
                txt = txt_p.read_text(encoding="utf-8").strip()
        return str(p), (txt or ""), p.stem
    voices = list_voices()
    if not voices:
        return None, "", None
    by_name = {v["name"].lower(): v for v in voices}
    if requested_name:
        hit = by_name.get(requested_name.strip().lower())
        if hit:
            return hit["wav"], hit["transcript"], hit["name"]
    cfg = load_config()
    def_key = cfg.get(f"default_{language}") or ""
    if def_key and def_key.lower() in by_name:
        hit = by_name[def_key.lower()]
        return hit["wav"], hit["transcript"], hit["name"]
    lang_rx = re.compile(rf"(?:^|[-_]){re.escape(language)}(?:$|[-_])", re.I)
    for v in voices:
        if v["name"].lower() == language or lang_rx.search(v["name"]):
            return v["wav"], v["transcript"], v["name"]
    if "default" in by_name:
        hit = by_name["default"]
        return hit["wav"], hit["transcript"], hit["name"]
    if len(voices) == 1:
        return voices[0]["wav"], voices[0]["transcript"], voices[0]["name"]
    return voices[0]["wav"], voices[0]["transcript"], voices[0]["name"]


class WorkerManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._proc = None
        self._engine = None
        self._ev_queue = None
        self._last_used = 0.0
        self._stderr_lines = []

    @property
    def active_engine(self):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._engine
            self._proc = None
            self._engine = None
            self._ev_queue = None
            return None

    def _kill_proc(self, proc):
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=3)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def unload(self) -> str:
        with self._lock:
            old = self._engine
            proc = self._proc
            self._proc = None
            self._engine = None
            self._ev_queue = None
        self._kill_proc(proc)
        if old:
            log(f"unloaded worker ({old})")
        return old or "none"

    def _drain_stderr(self, proc, engine):
        try:
            for line in iter(proc.stderr.readline, ""):
                s = line.rstrip()
                if s:
                    self._stderr_lines.append(f"[{engine}] {s}")
                    if len(self._stderr_lines) > 40:
                        self._stderr_lines.pop(0)
        except Exception:
            pass

    def _pump_stdout(self, proc, q):
        try:
            for raw in iter(proc.stdout.readline, ""):
                s = raw.strip()
                if not s:
                    continue
                try:
                    q.put(json.loads(s))
                except ValueError:
                    continue
        except Exception as exc:
            q.put({"event": "error", "error": f"reader error: {exc}"})
        finally:
            q.put({"event": "_eof"})

    def _ensure_worker(self, engine: str):
        with self._lock:
            if self._proc is not None and self._proc.poll() is None and self._engine == engine and self._ev_queue is not None:
                self._last_used = time.time()
                return self._proc, self._ev_queue
            old_proc = self._proc
            self._proc = None
            self._engine = None
            self._ev_queue = None
        self._kill_proc(old_proc)

        py = QWEN_PY if engine == "qwen3" else CHATTERBOX_PY
        script = QWEN_WORKER if engine == "qwen3" else CHATTERBOX_WORKER
        if not py.exists():
            raise RuntimeError(f"Python venv missing for {engine}: {py}")
        env = dict(os.environ)
        env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.Popen(
            [str(py), "-u", str(script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=NO_WINDOW,
        )
        threading.Thread(target=self._drain_stderr, args=(proc, engine), daemon=True).start()
        ready_line = proc.stdout.readline()
        if not ready_line:
            tail = " | ".join(self._stderr_lines[-5:])
            self._kill_proc(proc)
            raise RuntimeError(f"{engine} worker failed to start: {tail}")
        ev = json.loads(ready_line)
        if ev.get("event") != "ready":
            self._kill_proc(proc)
            raise RuntimeError(f"{engine} worker unexpected handshake: {ev}")
        ev_queue = queue.Queue()
        threading.Thread(target=self._pump_stdout, args=(proc, ev_queue), daemon=True).start()
        with self._lock:
            self._proc = proc
            self._engine = engine
            self._ev_queue = ev_queue
            self._last_used = time.time()
        log(f"started worker ({engine}, pid={proc.pid})")
        return proc, ev_queue

    def synthesize(self, engine: str, req: dict, on_progress=None, cancel_check=None) -> dict:
        with self._job_lock:
            proc, q = self._ensure_worker(engine)
            # Drain any leftover stale events before starting this job
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            payload = json.dumps({"cmd": "synthesize", **req}, ensure_ascii=False) + "\n"
            proc.stdin.write(payload)
            proc.stdin.flush()

            while True:
                if cancel_check and cancel_check():
                    self.unload()
                    raise RuntimeError("cancelled by client")
                try:
                    ev = q.get(timeout=0.35)
                except queue.Empty:
                    if proc.poll() is not None:
                        tail = " | ".join(self._stderr_lines[-5:])
                        self.unload()
                        raise RuntimeError(f"{engine} worker exited unexpectedly ({tail})")
                    continue
                with self._lock:
                    self._last_used = time.time()
                kind = ev.get("event")
                if kind == "status":
                    if on_progress:
                        on_progress(ev.get("message", ""))
                elif kind == "chunk":
                    if on_progress:
                        on_progress(f"Chunk {ev.get('done')}/{ev.get('total')}")
                elif kind == "done":
                    return ev
                elif kind == "error":
                    raise RuntimeError(ev.get("error") or f"{engine} failed")
                elif kind == "_eof":
                    tail = " | ".join(self._stderr_lines[-5:])
                    self.unload()
                    raise RuntimeError(f"{engine} stream ended early ({tail})")

    def maybe_idle_unload(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return
            if time.time() - self._last_used < IDLE_UNLOAD_S:
                return
        if self._job_lock.acquire(blocking=False):
            try:
                log("idle timeout reached, unloading TTS worker")
                self.unload()
            finally:
                self._job_lock.release()


_mgr = WorkerManager()


def _idle_watchdog():
    while True:
        time.sleep(30)
        try:
            _mgr.maybe_idle_unload()
        except Exception:
            pass


def _slugify(text: str) -> str:
    s = unicodedata.normalize("NFKD", (text or "story")[:60]).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "story"


def _fmt_dur(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    m, s = divmod(sec, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _run_story_narration(body: dict, emit_fn, cancel_check):
    cfg = load_config()
    raw_text = str(body.get("text") or "").strip()
    if not raw_text:
        raise ValueError("Story text is required.")

    opts = text_chunks.parse_story_request(raw_text)
    story_text = opts["story_text"]
    req_lang = str(body.get("language") or opts["language"] or "auto").lower()
    language = text_chunks.detect_language(story_text) if req_lang in ("", "auto") else req_lang[:2]
    if language not in ("en", "fr"):
        language = "en"

    engine = (body.get("engine") or opts["engine"] or cfg.get("default_engine") or "qwen3").lower()
    if engine not in ("both", "qwen3", "chatterbox"):
        engine = "qwen3"
    engines = ["qwen3", "chatterbox"] if engine == "both" else [engine]

    voice_name = body.get("voice") or opts["voice"]
    ref_audio_override = body.get("ref_audio")
    ref_text_override = body.get("ref_text")
    allow_builtin = bool(body.get("allow_builtin_fallback"))
    ref_wav, ref_txt, resolved_name = resolve_voice(
        language, voice_name, ref_audio_override=ref_audio_override, ref_text_override=ref_text_override
    )
    if not ref_wav and not (engines == ["chatterbox"] and allow_builtin):
        raise ValueError(
            f"No voice clip found yet in {VOICES_DIR}. Open http://127.0.0.1:{PORT} to record or upload "
            "a ~10s English/French sample of your voice (or drop en.wav + en.txt / fr.wav + fr.txt there)."
        )

    exaggeration = float(body.get("exaggeration") or opts["exaggeration"] or cfg.get("exaggeration") or 0.55)
    cfg_weight = float(body.get("cfg_weight") or cfg.get("cfg_weight") or 0.5)
    seed = int(body.get("seed") or cfg.get("seed") or 42)

    chunks = text_chunks.chunk_story(story_text, language=language)
    if not chunks:
        raise ValueError("No speakable text found after cleaning the story.")

    word_count = len(story_text.split())
    first_line = next((ln.strip() for ln in text_chunks.clean_story_text(story_text).splitlines() if ln.strip()), "Podcast Story")
    title = first_line.replace("__HEADING__", "").strip()[:72] or "Podcast Story"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    folder_name = f"{stamp}-{_slugify(title)}"
    out_dir = html_files.GENERATED_DIR / "podcast" / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    emit_fn(
        f"🎙️ Language: {'French' if language == 'fr' else 'English'} · Voice: {resolved_name or 'built-in'} · "
        f"{word_count} words split into {len(chunks)} segments"
    )
    if resolved_name and not ref_txt and "qwen3" in engines:
        emit_fn("ℹ️ Tip: adding a matching .txt transcript next to your voice clip boosts Qwen3-TTS cloning accuracy.")

    tracks = []
    base_url = f"{html_files.public_base()}/podcast/{quote(folder_name)}"
    for eng in engines:
        if cancel_check():
            raise RuntimeError("cancelled by client")
        t_eng = time.time()
        label = "Qwen3-TTS 1.7B" if eng == "qwen3" else "Chatterbox Multilingual"
        emit_fn(f"🔊 [{label}] Preparing model & reference voice...")
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"tts_{eng}_"))
        try:
            res = _mgr.synthesize(
                eng,
                {
                    "chunks": chunks,
                    "language": language,
                    "ref_audio": ref_wav or "",
                    "ref_text": ref_txt,
                    "out_dir": str(tmp_dir),
                    "exaggeration": exaggeration,
                    "cfg_weight": cfg_weight,
                    "seed": seed,
                },
                on_progress=lambda m, lbl=label: emit_fn(f"🔊 [{lbl}] {m}"),
                cancel_check=cancel_check,
            )
            emit_fn(f"🎛️ [{label}] Stitching {len(res['files'])} segments & mastering -16 LUFS MP3...")
            stitched_wav = tmp_dir / "stitched.wav"
            dur_s = audio_stitch.stitch_wavs(res["files"], stitched_wav)
            mp3_name = f"story-{eng}.mp3"
            audio_stitch.master_to_mp3(stitched_wav, out_dir / mp3_name)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

        gen_s = round(time.time() - t_eng, 1)
        if eng == "qwen3":
            sub = "Apache 2.0 · High speaker similarity, unwatermarked"
        else:
            sub = f"MIT · Expressiveness {exaggeration:.2f} (Perth watermark)"
        tracks.append({
            "engine": eng,
            "label": label,
            "subtitle": sub,
            "filename": mp3_name,
            "url": f"{base_url}/{quote(mp3_name)}",
            "duration_s": dur_s,
            "duration_str": _fmt_dur(dur_s),
            "elapsed_s": gen_s,
            "elapsed_str": f"{gen_s}s",
        })
        if len(engines) > 1 and eng != engines[-1]:
            _mgr.unload()

    player_html = audio_stitch.build_player_html(
        title=title,
        language=language,
        word_count=word_count,
        chunk_count=len(chunks),
        story_preview=story_text,
        tracks=tracks,
    )
    (out_dir / "index.html").write_text(player_html, encoding="utf-8")
    player_url = f"{base_url}/index.html"
    log(f"narrated '{title}' ({language}, {word_count}w, {len(chunks)} chunks) -> {out_dir}")
    return {
        "title": title,
        "language": language,
        "voice": resolved_name or "built-in",
        "has_transcript": bool(ref_txt),
        "word_count": word_count,
        "chunk_count": len(chunks),
        "player_url": player_url,
        "folder": str(out_dir),
        "tracks": tracks,
    }


def _refused_reason(headers) -> str:
    host = local_guard._hostname(headers.get("Host"))
    if host not in local_guard.LOCAL_HOSTNAMES:
        return f"host {headers.get('Host')!r} is not this machine"
    origin = headers.get("Origin")
    if origin is not None:
        norm = origin.strip().lower().rstrip("/")
        allowed = set(local_guard.ALLOWED_ORIGINS) | {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        if norm not in allowed:
            return f"requests from web pages ({origin!r}) are not accepted"
    return ""


def _socket_disconnected(sock) -> bool:
    if sock is None:
        return False
    try:
        r, _, _ = select.select([sock], [], [], 0)
        if not r:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _refused(self) -> bool:
        reason = _refused_reason(self.headers)
        if reason:
            log(f"REFUSED {self.command} {self.path[:80]}: {reason}")
            self._send(403, json.dumps({"error": reason}).encode(), "application/json")
            return True
        return False

    def _send(self, status: int, data: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, obj: dict):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if self._refused():
            return
        path = unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            if STUDIO_HTML.exists():
                return self._send(200, STUDIO_HTML.read_bytes(), "text/html; charset=utf-8")
            return self._send(404, b"studio.html missing", "text/plain")
        if path == "/health":
            return self._json(200, {
                "status": "ok",
                "active_engine": _mgr.active_engine,
                "config": load_config(),
                "voices": list_voices(),
            })
        if path == "/voices":
            return self._json(200, {"voices": list_voices()})
        if path.startswith("/voices/") and path.lower().endswith(".wav"):
            stem = path[len("/voices/"):-4]
            if VALID_NAME_RE.match(stem):
                wp = (VOICES_DIR / f"{stem}.wav").resolve()
                if wp.is_file() and wp.parent == VOICES_DIR.resolve():
                    return self._send(200, wp.read_bytes(), "audio/wav")
            return self._json(404, {"error": "voice not found"})
        self._json(404, {"error": f"not found: {path}"})

    def do_DELETE(self):
        if self._refused():
            return
        path = unquote(self.path.split("?", 1)[0])
        if path.startswith("/voices/"):
            stem = path[len("/voices/"):].strip("/")
            if not VALID_NAME_RE.match(stem):
                return self._json(400, {"error": "invalid profile name"})
            removed = False
            for ext in (".wav", ".txt", ".mp3", ".m4a", ".webm", ".ogg", ".flac"):
                p = VOICES_DIR / f"{stem}{ext}"
                if p.is_file():
                    p.unlink()
                    removed = True
            return self._json(200, {"deleted": removed, "name": stem})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self._refused():
            return
        path = self.path.split("?", 1)[0]
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad JSON: {e}"})

        if path == "/unload":
            unloaded = _mgr.unload()
            return self._json(200, {"ok": True, "unloaded": unloaded})

        if path == "/config":
            return self._json(200, {"ok": True, "config": save_config(body)})

        if path == "/voices":
            name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(body.get("name") or "").strip()).strip("-.")
            if not name or not VALID_NAME_RE.match(name):
                return self._json(400, {"error": "Invalid profile name (letters, numbers, -, _, . only)"})
            b64 = str(body.get("audio_b64") or "").strip()
            if not b64:
                return self._json(400, {"error": "Missing audio data"})
            if "," in b64 and b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            ext = Path(str(body.get("filename") or "clip.webm")).suffix.lower() or ".webm"
            if ext not in AUDIO_EXTS:
                ext = ".webm"
            tmp = Path(tempfile.mktemp(suffix=ext))
            try:
                tmp.write_bytes(base64.b64decode(b64))
                dst_wav = VOICES_DIR / f"{name}.wav"
                audio_stitch.convert_to_ref_wav(tmp, dst_wav)
                transcript = str(body.get("transcript") or "").strip()
                txt_p = VOICES_DIR / f"{name}.txt"
                if transcript:
                    txt_p.write_text(transcript, encoding="utf-8")
                elif txt_p.exists():
                    txt_p.unlink()
                lang = str(body.get("language") or "").lower()
                cfg = load_config()
                if lang in ("en", "fr") and not cfg.get(f"default_{lang}"):
                    save_config({f"default_{lang}": name})
                return self._json(200, {"ok": True, "name": name, "wav": str(dst_wav)})
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

        if path == "/narrate":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def _write_ev(obj):
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
                self.wfile.write(f"{len(line):X}\r\n".encode("ascii") + line + b"\r\n")
                self.wfile.flush()

            try:
                res = _run_story_narration(
                    body,
                    emit_fn=lambda msg: _write_ev({"type": "progress", "message": msg}),
                    cancel_check=lambda: _socket_disconnected(self.connection),
                )
                _write_ev({"type": "result", **res})
            except Exception as e:
                log(f"narrate failed: {e}")
                try:
                    _write_ev({"type": "error", "error": str(e)})
                except OSError:
                    pass
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
            return

        if path == "/v1/audio/speech":
            text = str(body.get("input") or "").strip()
            if not text:
                return self._json(400, {"error": "input is required"})
            fmt = str(body.get("response_format") or "mp3").lower()
            lang = text_chunks.detect_language(text)
            cfg = load_config()
            eng = str(body.get("engine") or cfg.get("default_engine") or "qwen3").lower()
            if eng == "both":
                eng = "qwen3"
            voice_arg = str(body.get("voice") or "").strip()
            if voice_arg in ("alloy", "echo", "fable", "onyx", "nova", "shimmer"):
                voice_arg = ""
            ref_wav, ref_txt, _ = resolve_voice(lang, voice_arg or None)
            if not ref_wav and eng == "qwen3":
                eng = "chatterbox"
            chunks = text_chunks.chunk_story(text, language=lang)
            tmp_dir = Path(tempfile.mkdtemp(prefix="tts_oai_"))
            try:
                res = _mgr.synthesize(
                    eng,
                    {
                        "chunks": chunks,
                        "language": lang,
                        "ref_audio": ref_wav or "",
                        "ref_text": ref_txt,
                        "out_dir": str(tmp_dir),
                        "exaggeration": float(cfg.get("exaggeration") or 0.55),
                        "cfg_weight": float(cfg.get("cfg_weight") or 0.5),
                        "seed": int(cfg.get("seed") or 42),
                    },
                    cancel_check=lambda: _socket_disconnected(self.connection),
                )
                stitched = tmp_dir / "stitched.wav"
                audio_stitch.stitch_wavs(res["files"], stitched)
                if fmt == "wav":
                    return self._send(200, stitched.read_bytes(), "audio/wav")
                out_mp3 = tmp_dir / "speech.mp3"
                audio_stitch.master_to_mp3(stitched, out_mp3)
                return self._send(200, out_mp3.read_bytes(), "audio/mpeg")
            except Exception as e:
                return self._json(500, {"error": str(e)})
            finally:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)

        self._json(404, {"error": f"not found: {path}"})


class ExclusiveServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def already_running() -> bool:
    import http.client
    try:
        c = http.client.HTTPConnection(HOST, PORT, timeout=2)
        c.request("GET", "/health")
        ok = c.getresponse().status == 200
        c.close()
        return ok
    except OSError:
        return False


def main():
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    if already_running():
        log(f"voice-tts: already listening on :{PORT}, exiting")
        return
    try:
        srv = ExclusiveServer((HOST, PORT), Handler)
    except OSError as e:
        log(f"voice-tts: cannot bind :{PORT} ({e}), exiting")
        return
    srv.daemon_threads = True
    threading.Thread(target=_idle_watchdog, daemon=True, name="tts-idle").start()
    log(f"voice-tts listening on http://{HOST}:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
