"""WAV chunk stitching (fade-in/out + natural pauses), ffmpeg MP3 loudness mastering, and A/B player page."""
import array
import html
import shutil
import subprocess
import wave
from pathlib import Path

NO_WINDOW = 0x08000000


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _apply_edge_fades(pcm_bytes: bytes, sr: int, fade_ms: int = 12) -> bytes:
    """Apply short linear fade-in/out to 16-bit mono PCM so chunk boundaries never click or pop."""
    samples = array.array("h")
    samples.frombytes(pcm_bytes)
    n = len(samples)
    fade_n = min(n // 2, int(sr * fade_ms / 1000))
    if fade_n <= 1:
        return pcm_bytes
    for i in range(fade_n):
        g = i / fade_n
        samples[i] = int(samples[i] * g)
        samples[n - 1 - i] = int(samples[n - 1 - i] * g)
    return samples.tobytes()


def stitch_wavs(chunk_items: list, out_wav: Path) -> float:
    """Concatenate 16-bit mono WAV chunks with edge fades and silence pauses. Returns total duration in seconds."""
    if not chunk_items:
        raise ValueError("no audio chunks to stitch")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    params = None
    total_frames = 0
    with wave.open(str(out_wav), "wb") as wout:
        for item in chunk_items:
            fpath = Path(item["file"])
            pause_ms = max(0, int(item.get("pause_after_ms", 200)))
            with wave.open(str(fpath), "rb") as win:
                cur_params = (win.getnchannels(), win.getsampwidth(), win.getframerate())
                if params is None:
                    params = cur_params
                    wout.setnchannels(cur_params[0])
                    wout.setsampwidth(cur_params[1])
                    wout.setframerate(cur_params[2])
                elif cur_params != params:
                    raise ValueError(f"Mismatched WAV format in {fpath}: {cur_params} != {params}")
                raw = win.readframes(win.getnframes())
            nch, sw, sr = params
            if nch == 1 and sw == 2:
                raw = _apply_edge_fades(raw, sr)
            wout.writeframes(raw)
            total_frames += len(raw) // (nch * sw)
            if pause_ms > 0:
                sil_frames = int(sr * pause_ms / 1000)
                wout.writeframes(b"\x00" * (sil_frames * nch * sw))
                total_frames += sil_frames
    sr = params[2] if params else 24000
    return round(total_frames / max(sr, 1), 2)


def master_to_mp3(in_wav: Path, out_mp3: Path, bitrate: str = "192k") -> Path:
    """Normalize speech loudness (-16 LUFS podcast standard) and encode clean MP3 via ffmpeg."""
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(in_wav),
        "-af",
        "highpass=f=65,loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:a",
        "libmp3lame",
        "-b:a",
        bitrate,
        "-ar",
        "44100",
        "-ac",
        "1",
        str(out_mp3),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
    if r.returncode != 0 or not out_mp3.exists():
        raise RuntimeError(f"ffmpeg MP3 mastering failed: {(r.stderr or r.stdout)[-400:]}")
    return out_mp3


def convert_to_ref_wav(src_path: Path, dst_wav: Path, max_seconds: float = 20.0) -> Path:
    """Convert any uploaded/recorded audio file into clean 24kHz mono 16-bit WAV trimmed to <= max_seconds."""
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-y",
        "-i",
        str(src_path),
        "-t",
        str(max_seconds),
        "-ac",
        "1",
        "-ar",
        "24000",
        "-sample_fmt",
        "s16",
        "-af",
        "highpass=f=70,loudnorm=I=-18:TP=-1.5:LRA=9",
        str(dst_wav),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
    if r.returncode != 0 or not dst_wav.exists():
        raise RuntimeError(f"ffmpeg reference conversion failed: {(r.stderr or r.stdout)[-400:]}")
    return dst_wav


def build_player_html(
    title: str,
    language: str,
    word_count: int,
    chunk_count: int,
    story_preview: str,
    tracks: list,
) -> str:
    """Build a standalone mobile-friendly HTML5 audio player + download page (works on PC and phone)."""
    cards = []
    for tr in tracks:
        label = html.escape(tr["label"])
        subtitle = html.escape(tr.get("subtitle", ""))
        fname = html.escape(tr["filename"])
        dur = html.escape(tr.get("duration_str", ""))
        elapsed = html.escape(tr.get("elapsed_str", ""))
        cards.append(
            f"""<section class="card">
  <div class="head">
    <div>
      <h2>{label}</h2>
      <p class="sub">{subtitle}</p>
    </div>
    <span class="pill">{dur} · {elapsed} gen</span>
  </div>
  <audio controls preload="metadata" src="{fname}"></audio>
  <div class="actions">
    <a class="btn" href="{fname}" download="{fname}">Download MP3</a>
  </div>
</section>"""
        )
    lang_badge = "French (FR)" if language == "fr" else "English (EN)"
    preview_esc = html.escape(story_preview[:700] + ("..." if len(story_preview) > 700 else ""))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: dark; --bg:#0f172a; --card:#1e293b; --border:#334155; --fg:#f8fafc; --muted:#94a3b8; --accent:#38bdf8; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:24px 16px 48px; background:var(--bg); color:var(--fg); font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  main {{ max-width:760px; margin:0 auto; display:grid; gap:16px; }}
  header h1 {{ margin:0 0 6px; font-size:1.5rem; }}
  .meta {{ color:var(--muted); font-size:0.95rem; margin:0; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:18px; display:grid; gap:14px; }}
  .head {{ display:flex; justify-content:space-between; align-items:flex-start; gap:12px; flex-wrap:wrap; }}
  h2 {{ margin:0; font-size:1.15rem; }}
  .sub {{ margin:4px 0 0; color:var(--muted); font-size:0.88rem; }}
  .pill {{ background:#0f172a; border:1px solid var(--border); border-radius:999px; padding:4px 10px; font-size:0.82rem; color:var(--accent); white-space:nowrap; }}
  audio {{ width:100%; height:44px; }}
  .actions {{ display:flex; gap:10px; flex-wrap:wrap; }}
  .btn {{ display:inline-block; background:var(--accent); color:#082f49; font-weight:600; text-decoration:none; padding:9px 16px; border-radius:8px; font-size:0.92rem; }}
  .btn:hover {{ filter:brightness(1.08); }}
  details {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:12px 16px; }}
  summary {{ cursor:pointer; color:var(--muted); font-weight:600; }}
  pre {{ white-space:pre-wrap; font-family:inherit; color:#cbd5e1; line-height:1.55; margin:10px 0 0; }}
</style>
</head>
<body>
<main>
  <header>
    <h1>🎙️ {html.escape(title)}</h1>
    <p class="meta">{lang_badge} · {word_count} words · {chunk_count} narration segments · -16 LUFS mastered MP3</p>
  </header>
  {''.join(cards)}
  <details>
    <summary>Story excerpt</summary>
    <pre>{preview_esc}</pre>
  </details>
</main>
</body>
</html>
"""
