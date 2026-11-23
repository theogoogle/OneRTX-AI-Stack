"""
Image generation through ComfyUI (127.0.0.1:8188) for the router. Standard library only.

Model: Qwen-Image-2.1 (#1 open-weights text-to-image model on the Artificial Analysis v2.1 leaderboard, Nov 2026),
built as the official ComfyUI template "Qwen Image 2.1 Text to Image" (image_qwen_image_2_1_t2i.json), flattened.
Files: %USERPROFILE%\\AI\\models\\ (see extra_model_paths.yaml in %USERPROFILE%\\AI\\ComfyUI).

GPU sharing with LM Studio (12 GB card):
  - before an image: if free VRAM is below IMAGE_VRAM_MB, LM Studio's GPU models are unloaded (LFM3 runs on the CPU
    and stays). The router waits for chat answers in flight to finish first.
  - after an image: ComfyUI keeps its model until the next chat request, which first calls free() so LM Studio can load.
"""
import json
import mimetypes
import random
import shutil
import struct
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

COMFY = "http://127.0.0.1:8188"
LMS = shutil.which("lms") or str(Path.home() / ".lmstudio" / "bin" / "lms.exe")
NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: no console flashes from the pythonw router

# Defaults chosen by the parameter tests on 2026-11-07 (benchmark\image-params\): see CHANGES.md
DEFAULTS = {
    "unet": "qwen_image_2.1_int8_convrot.safetensors",
    "text_encoder": "qwen3vl_8b_int8_convrot.safetensors",
    "vae": "qwen_image_2.1_vae_bf16.safetensors",
    "prompt_enhancer": "qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors",
    "steps": 25,
    "cfg": 1.0,
    "sampler": "euler",
    "scheduler": "simple",
    "width": 1024,
    "height": 1024,
    "enhance": False,
    "negative": "",
}
IMAGE_VRAM_MB = 10500          # measured peak during generation; below this much free VRAM, LM Studio is unloaded
KEEP_IN_LMSTUDIO = {"lfm3-1.7b", "qwen3.5-4b"}   # CPU-only (router labeller, Flash): they use no VRAM, never unload
PE_SYSTEM_FILE = Path.home() / "AI" / "qwen_image_2.1_pe_system_prompt.txt"   # from the official template
# Native aspect ratios from the model card (2K); smaller sizes keep the same shapes, rounded to 32
SIZES_2K = {"1:1": (2048, 2048), "4:3": (2400, 1792), "3:4": (1792, 2400), "3:2": (2528, 1696),
            "2:3": (1696, 2528), "16:9": (2752, 1536), "9:16": (1536, 2752)}


def _post(path, body, timeout=30):
    req = urllib.request.Request(COMFY + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _get(path, timeout=30):
    with urllib.request.urlopen(COMFY + path, timeout=timeout) as r:
        return r.read()


def available():
    try:
        _get("/system_stats", timeout=3)
        return True
    except Exception:
        return False


def status():
    """(jobs running or queued, VRAM held by ComfyUI in MB). (0, 0) when ComfyUI is not reachable."""
    try:
        q = json.loads(_get("/queue", timeout=3))
        stats = json.loads(_get("/system_stats", timeout=3))
        dev = (stats.get("devices") or [{}])[0]
        held = (dev.get("torch_vram_total", 0) - dev.get("torch_vram_free", 0)) // 2**20 if dev else 0
        return len(q.get("queue_running", [])) + len(q.get("queue_pending", [])), max(0, held)
    except Exception:
        return 0, 0


def free():
    """Ask ComfyUI to drop its models from VRAM (they reload from RAM/disk on the next image)."""
    try:
        _post("/free", {"unload_models": True, "free_memory": True}, timeout=30)
        return True
    except Exception:
        return False


def vram_free_mb():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


def unload_lmstudio_gpu_models(log=print):
    """Unload every LM Studio model except the CPU-only ones (KEEP_IN_LMSTUDIO). Returns the identifiers unloaded."""
    try:
        ps = json.loads(subprocess.run([LMS, "ps", "--json"], capture_output=True, text=True, timeout=60,
                                       creationflags=NO_WINDOW).stdout or "[]")
    except Exception as e:
        log(f"images: could not list LM Studio models: {e}")
        return []
    gone = []
    for m in ps:
        ident = m.get("identifier") or m.get("modelKey")
        if ident and ident not in KEEP_IN_LMSTUDIO:
            subprocess.run([LMS, "unload", ident], capture_output=True, timeout=120, creationflags=NO_WINDOW)
            gone.append(ident)
    return gone


def size_for(size=None, aspect=None, megapixels=None):
    """'1024x1024' | aspect '16:9' with megapixels (default from DEFAULTS) -> (w, h), multiples of 32."""
    if size and "x" in str(size):
        w, h = (int(v) for v in str(size).lower().split("x"))
    else:
        base_w, base_h = SIZES_2K.get(aspect or "1:1", SIZES_2K["1:1"])
        mp = megapixels or (DEFAULTS["width"] * DEFAULTS["height"] / 1e6)
        scale = (mp * 1e6 / (base_w * base_h)) ** 0.5
        w, h = base_w * scale, base_h * scale
    return max(256, round(w / 32) * 32), max(256, round(h / 32) * 32)


def build_workflow(prompt, width, height, seed, steps, cfg, sampler, scheduler, negative, enhance, unet,
                   text_encoder, vae, prompt_enhancer, prefix="router/qwen_image"):
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": text_encoder, "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "5": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["3", 0], "prompt": prompt, "negative_prompt": negative, "resolution": 1024}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "seed": seed, "steps": steps, "cfg": cfg,
                                                   "sampler_name": sampler, "scheduler": scheduler,
                                                   "positive": ["5", 0], "negative": ["5", 1],
                                                   "latent_image": ["6", 0], "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["4", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}},
    }
    if enhance:   # the official prompt enhancer (Qwen3.5 9B), same settings as the template
        wf["10"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": prompt_enhancer, "type": "stable_diffusion",
                                                           "device": "default"}}
        wf["11"] = {"class_type": "TextGenerate", "inputs": {
            "clip": ["10", 0], "prompt": prompt, "max_length": 4096, "sampling_mode": "on",
            "sampling_mode.temperature": 1.0, "sampling_mode.top_k": 20, "sampling_mode.top_p": 0.95,
            "sampling_mode.min_p": 0.0, "sampling_mode.repetition_penalty": 1.0, "sampling_mode.seed": seed % 2**63,
            "sampling_mode.presence_penalty": 1.5, "thinking": False, "use_default_template": True, "mtp": "auto",
            "system_prompt": PE_SYSTEM_FILE.read_text(encoding="utf-8")}}
        wf["5"]["inputs"]["prompt"] = ["11", 0]
    return wf


def generate(prompt, size=None, aspect=None, megapixels=None, seed=None, steps=None, cfg=None, sampler=None,
             scheduler=None, negative=None, enhance=None, unet=None, text_encoder=None, timeout=1800, prefix=None):
    """Runs one image. Returns (png_bytes, info dict)."""
    o = {k: v for k, v in dict(steps=steps, cfg=cfg, sampler=sampler, scheduler=scheduler, negative=negative,
                               enhance=enhance, unet=unet, text_encoder=text_encoder).items() if v is not None}
    p = {**DEFAULTS, **o}
    w, h = size_for(size, aspect, megapixels)
    seed = random.randint(0, 2**48) if seed is None else int(seed)
    wf = build_workflow(prompt, w, h, seed, p["steps"], p["cfg"], p["sampler"], p["scheduler"], p["negative"],
                        p["enhance"], p["unet"], p["text_encoder"], p["vae"], p["prompt_enhancer"],
                        prefix or "router/qwen_image")
    t = time.time()
    pid = _post("/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
    while True:
        hist = json.loads(_get(f"/history/{pid}"))
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError(f"ComfyUI error: {json.dumps(msgs)[:600]}")
            imgs = [i for out in entry.get("outputs", {}).values() for i in out.get("images", [])]
            if imgs:
                im = imgs[0]
                q = f"/view?filename={urllib.request.quote(im['filename'])}&subfolder={urllib.request.quote(im['subfolder'])}&type={im['type']}"
                png = _get(q, timeout=60)
                enhanced = None
                for out in entry.get("outputs", {}).values():
                    if "text" in out:
                        enhanced = out["text"]
                return png, {"seconds": round(time.time() - t, 1), "width": w, "height": h, "seed": seed,
                             "steps": p["steps"], "cfg": p["cfg"], "unet": p["unet"], "enhance": p["enhance"],
                             "file": im["filename"], "enhanced_prompt": enhanced}
        if time.time() - t > timeout:
            raise TimeoutError(f"image not finished after {timeout}s")
        time.sleep(1.0)


def _ext_for_image(filename: str, mime_type: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        return ext
    guessed = mimetypes.guess_extension((mime_type or "image/png").split(";")[0].strip().lower())
    return ".jpg" if guessed == ".jpe" else (guessed or ".png")


def upload_image(img_bytes: bytes, filename: str = "input.png", mime_type: str = "image/png") -> str:
    """Upload raw image bytes to ComfyUI's input folder via POST /upload/image and return ComfyUI image ref."""
    ext = _ext_for_image(filename, mime_type)
    unique_name = f"router_edit_{uuid.uuid4().hex[:12]}{ext}"
    ctype = (mime_type or mimetypes.guess_type(unique_name)[0] or "image/png").split(";")[0].strip()
    boundary = f"----RouterComfyBoundary{uuid.uuid4().hex}"
    parts = [
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{unique_name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        + img_bytes
        + b"\r\n",
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
        ).encode("utf-8"),
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        COMFY + "/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())
    name = res["name"]
    sub = res.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def build_edit_workflow(prompt, image_names, size=None, seed=0, steps=None, cfg=None, sampler=None, scheduler=None,
                        negative="", resolution=1024, unet=None, text_encoder=None, vae=None,
                        prefix="router/qwen_image_edit"):
    """Build ComfyUI Qwen-Image-2.1 Image Edit API graph using TextEncodeQwenImage21 + LoadImage."""
    wf = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet or DEFAULTS["unet"], "weight_dtype": "default"}},
        "2": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "3": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": text_encoder or DEFAULTS["text_encoder"], "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": vae or DEFAULTS["vae"]}},
    }
    encode_inputs = {
        "clip": ["3", 0],
        "vae": ["4", 0],
        "prompt": prompt,
        "negative_prompt": negative or "",
        "resolution": int(resolution),
    }
    for idx, img_name in enumerate(image_names[:10], 1):
        nid = str(19 + idx)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": img_name}}
        encode_inputs[f"images.image_{idx}"] = [nid, 0]
    wf["5"] = {"class_type": "TextEncodeQwenImage21", "inputs": encode_inputs}

    if size and str(size).lower() != "auto":
        w, h = size_for(size=size)
        wf["6"] = {"class_type": "EmptyLatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}}
        latent_ref = ["6", 0]
    else:
        latent_ref = ["5", 2]

    wf["7"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["2", 0],
            "seed": int(seed),
            "steps": int(steps if steps is not None else DEFAULTS["steps"]),
            "cfg": float(cfg if cfg is not None else DEFAULTS["cfg"]),
            "sampler_name": sampler or DEFAULTS["sampler"],
            "scheduler": scheduler or DEFAULTS["scheduler"],
            "positive": ["5", 0],
            "negative": ["5", 1],
            "latent_image": latent_ref,
            "denoise": 1.0,
        },
    }
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["4", 0]}}
    wf["9"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}}
    return wf


def edit(prompt, images, size=None, seed=None, steps=None, cfg=None, sampler=None, scheduler=None,
         negative=None, resolution=1024, unet=None, text_encoder=None, timeout=1800, prefix=None):
    """Run one native Qwen-Image-2.1 edit from 1..10 source images. Returns (png_bytes, info dict)."""
    if not images:
        raise ValueError("at least one source image is required")
    p = {**DEFAULTS}
    if steps is not None: p["steps"] = int(steps)
    if cfg is not None: p["cfg"] = float(cfg)
    if sampler is not None: p["sampler"] = sampler
    if scheduler is not None: p["scheduler"] = scheduler
    if negative is not None: p["negative"] = negative
    if unet is not None: p["unet"] = unet
    if text_encoder is not None: p["text_encoder"] = text_encoder
    seed = random.randint(0, 2**48) if seed is None else int(seed)

    uploaded = []
    for item in images[:10]:
        if isinstance(item, (bytes, bytearray)):
            uploaded.append(upload_image(bytes(item)))
        else:
            raw, fname, mtype = item
            uploaded.append(upload_image(bytes(raw), fname, mtype))

    wf = build_edit_workflow(
        prompt, uploaded, size=size, seed=seed, steps=p["steps"], cfg=p["cfg"], sampler=p["sampler"],
        scheduler=p["scheduler"], negative=p["negative"], resolution=int(resolution),
        unet=p["unet"], text_encoder=p["text_encoder"], vae=p["vae"], prefix=prefix or "router/qwen_image_edit",
    )
    t = time.time()
    pid = _post("/prompt", {"prompt": wf, "client_id": str(uuid.uuid4())})["prompt_id"]
    while True:
        hist = json.loads(_get(f"/history/{pid}"))
        if pid in hist:
            entry = hist[pid]
            st = entry.get("status", {})
            if st.get("status_str") == "error":
                msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError(f"ComfyUI edit error: {json.dumps(msgs)[:600]}")
            imgs = [i for out in entry.get("outputs", {}).values() for i in out.get("images", [])]
            if imgs:
                im = imgs[0]
                q = f"/view?filename={urllib.request.quote(im['filename'])}&subfolder={urllib.request.quote(im['subfolder'])}&type={im['type']}"
                png = _get(q, timeout=60)
                w, h = (struct.unpack(">II", png[16:24]) if len(png) >= 24 and png[:8] == b"\x89PNG\r\n\x1a\n"
                        else (int(resolution), int(resolution)))
                return png, {
                    "seconds": round(time.time() - t, 1),
                    "width": w,
                    "height": h,
                    "seed": seed,
                    "steps": p["steps"],
                    "cfg": p["cfg"],
                    "unet": p["unet"],
                    "source_images": len(uploaded),
                    "file": im["filename"],
                }
        if time.time() - t > timeout:
            raise TimeoutError(f"image edit not finished after {timeout}s")
        time.sleep(1.0)
