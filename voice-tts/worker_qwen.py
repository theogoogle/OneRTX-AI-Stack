"""Persistent GPU worker for Qwen3-TTS-12Hz-1.7B-Base voice cloning (runs inside .venv-qwen)."""
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
from qwen_tts import Qwen3TTSModel  # noqa: E402

MODEL_ID = os.environ.get("QWEN_TTS_MODEL", "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
LANG_MAP = {"en": "English", "fr": "French", "english": "English", "french": "French"}


class QwenWorker:
    def __init__(self):
        self.model = None
        self.prompt_cache = {}
        self.supported_langs = None

    def ensure_loaded(self):
        if self.model is not None:
            return
        send({"event": "status", "message": f"Loading {MODEL_ID} on CUDA (bfloat16 + SDPA)..."})
        self.model = Qwen3TTSModel.from_pretrained(
            MODEL_ID,
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
        )
        langs = getattr(self.model.model, "get_supported_languages", None)
        if callable(langs):
            raw = langs()
            if raw:
                self.supported_langs = {str(x).lower(): str(x) for x in raw}
        send({"event": "status", "message": f"{MODEL_ID} loaded"})

    def get_prompt(self, ref_audio: str, ref_text: str):
        p = Path(ref_audio)
        mtime = p.stat().st_mtime
        clean_ref = (ref_text or "").strip()
        key = (str(p.resolve()), mtime, clean_ref)
        cached = self.prompt_cache.get(key)
        if cached is not None:
            return cached, bool(clean_ref)
        xvec_only = not bool(clean_ref)
        prompt = self.model.create_voice_clone_prompt(
            ref_audio=str(p),
            ref_text=clean_ref if clean_ref else None,
            x_vector_only_mode=xvec_only,
        )
        self.prompt_cache.clear()
        self.prompt_cache[key] = prompt
        return prompt, (not xvec_only)

    def map_lang(self, lang: str) -> str:
        want = LANG_MAP.get((lang or "en").lower(), lang or "English")
        if self.supported_langs:
            return self.supported_langs.get(want.lower(), want)
        return want

    def synthesize(self, req: dict):
        self.ensure_loaded()
        chunks = req.get("chunks") or []
        ref_audio = req["ref_audio"]
        ref_text = req.get("ref_text") or ""
        out_dir = Path(req["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        seed = int(req.get("seed", 42))

        prompt, icl_mode = self.get_prompt(ref_audio, ref_text)
        files = []
        sample_rate = 24000
        total = len(chunks)
        for idx, ch in enumerate(chunks):
            txt = (ch.get("text") or "").strip()
            if not txt:
                continue
            lang = self.map_lang(ch.get("language") or req.get("language") or "en")
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
            wavs, sr = self.model.generate_voice_clone(
                text=txt,
                language=lang,
                voice_clone_prompt=prompt,
                non_streaming_mode=True,
                do_sample=True,
                top_p=0.9,
                temperature=0.75,
                repetition_penalty=1.05,
            )
            sample_rate = int(sr)
            wav = np.asarray(wavs[0], dtype=np.float32)
            # Peak-safe clipping guard per chunk
            peak = float(np.max(np.abs(wav))) if wav.size else 0.0
            if peak > 0.99:
                wav = wav * (0.98 / peak)
            out_wav = out_dir / f"chunk_{idx:04d}.wav"
            sf.write(str(out_wav), wav, sample_rate, subtype="PCM_16")
            files.append({"file": str(out_wav), "pause_after_ms": int(ch.get("pause_after_ms", 210))})
            send({"event": "chunk", "done": idx + 1, "total": total})
        send({"event": "done", "sample_rate": sample_rate, "files": files, "icl_mode": icl_mode})


def main():
    worker = QwenWorker()
    send({"event": "ready", "engine": "qwen3", "model": MODEL_ID})
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
