"""Persistent GPU worker for Chatterbox Multilingual voice cloning (runs inside .venv-chatterbox)."""
import json
import os
import sys
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Keep fd 1 dedicated to structured JSON lines; redirect any library stdout/C-extension prints to stderr.
_ipc_fd = os.dup(1)
os.dup2(2, 1)
_ipc_out = os.fdopen(_ipc_fd, "w", encoding="utf-8", buffering=1)


def send(obj):
    _ipc_out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _ipc_out.flush()


import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: E402


class ChatterboxWorker:
    def __init__(self):
        self.model = None
        self.cond_cache_key = None

    def ensure_loaded(self):
        if self.model is not None:
            return
        send({"event": "status", "message": "Loading Chatterbox Multilingual on CUDA..."})
        self.model = ChatterboxMultilingualTTS.from_pretrained("cuda")
        send({"event": "status", "message": "Chatterbox Multilingual loaded"})

    def prepare_voice(self, ref_audio: str, exaggeration: float):
        if not ref_audio:
            return
        p = Path(ref_audio)
        key = (str(p.resolve()), p.stat().st_mtime, round(float(exaggeration), 3))
        if self.cond_cache_key == key:
            return
        self.model.prepare_conditionals(str(p), exaggeration=float(exaggeration))
        self.cond_cache_key = key

    def synthesize(self, req: dict):
        self.ensure_loaded()
        chunks = req.get("chunks") or []
        ref_audio = req.get("ref_audio") or ""
        out_dir = Path(req["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        exaggeration = float(req.get("exaggeration", 0.55))
        cfg_weight = float(req.get("cfg_weight", 0.5))
        temperature = float(req.get("temperature", 0.8))
        seed = int(req.get("seed", 42))

        if ref_audio:
            self.prepare_voice(ref_audio, exaggeration)
        files = []
        sample_rate = int(self.model.sr)
        total = len(chunks)
        for idx, ch in enumerate(chunks):
            txt = (ch.get("text") or "").strip()
            if not txt:
                continue
            lang = (ch.get("language") or req.get("language") or "en").lower()[:2]
            if lang not in ("en", "fr"):
                lang = "en"
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            wav_t = self.model.generate(
                text=txt,
                language_id=lang,
                audio_prompt_path=None,
                exaggeration=exaggeration,
                cfg_weight=cfg_weight,
                temperature=temperature,
            )
            wav = wav_t.squeeze(0).detach().cpu().numpy().astype(np.float32)
            peak = float(np.max(np.abs(wav))) if wav.size else 0.0
            if peak > 0.99:
                wav = wav * (0.98 / peak)
            out_wav = out_dir / f"chunk_{idx:04d}.wav"
            sf.write(str(out_wav), wav, sample_rate, subtype="PCM_16")
            files.append({"file": str(out_wav), "pause_after_ms": int(ch.get("pause_after_ms", 210))})
            send({"event": "chunk", "done": idx + 1, "total": total})
        send({"event": "done", "sample_rate": sample_rate, "files": files})


def main():
    worker = ChatterboxWorker()
    send({"event": "ready", "engine": "chatterbox"})
    while True:
        raw = sys.stdin.readline()
        if not raw:
            break
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            cmd = req.get("cmd")
            if cmd == "ping":
                send({"event": "pong", "loaded": worker.model is not None})
            elif cmd == "preload":
                worker.ensure_loaded()
                send({"event": "done"})
            elif cmd == "synthesize":
                worker.synthesize(req)
            elif cmd == "shutdown":
                send({"event": "bye"})
                break
            else:
                send({"event": "error", "error": f"unknown cmd: {cmd}"})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            send({"event": "error", "error": f"{e.__class__.__name__}: {e}"})


if __name__ == "__main__":
    main()
