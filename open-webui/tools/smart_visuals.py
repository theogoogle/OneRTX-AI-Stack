"""
title: Smart Visuals (Infographic & Mind Map)
author: Local AI Stack
description: Turn any topic, outline, or study guide into a crisp high-DPI infographic, mind map (with editable SVG), or visual timeline rendered offline.
version: 1.0.0
license: MIT
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("smart_visuals_tool")

try:
    from fastapi import UploadFile
    from starlette.datastructures import Headers
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler

    _HAS_OWUI_FILES = True
except Exception:
    _HAS_OWUI_FILES = False

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _find_system_python312() -> str:
    for p in (
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
        Path(r"C:\Program Files\Python312\python.exe"),
    ):
        if p.is_file():
            return str(p)
    found = shutil.which("python")
    if found:
        return found
    raise FileNotFoundError("System Python 3.12 not found.")


async def _upload_file(raw_bytes: bytes, filename: str, content_type: str, request: Any, user_dict: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    if _HAS_OWUI_FILES and request is not None and isinstance(user_dict, dict) and user_dict.get("id"):
        try:
            user_obj = Users.get_user_by_id(user_dict["id"])
            if hasattr(user_obj, "__await__"):
                user_obj = await user_obj
            if user_obj is not None:
                import io

                upload = UploadFile(file=io.BytesIO(raw_bytes), filename=filename, headers=Headers({"content-type": content_type}))
                file_obj = upload_file_handler(request=request, file=upload, metadata={"source": "smart_visuals"}, process=False, user=user_obj)
                if hasattr(file_obj, "__await__"):
                    file_obj = await file_obj
                fid = getattr(file_obj, "id", None) or (file_obj.get("id") if isinstance(file_obj, dict) else None)
                if fid:
                    return f"/api/v1/files/{fid}/content", str(fid)
        except Exception as e:
            log.warning("Smart Visuals upload failed: %s", e)
    fallback = Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"
    fallback.mkdir(parents=True, exist_ok=True)
    out_p = fallback / f"{uuid.uuid4().hex[:8]}_{filename}"
    out_p.write_bytes(raw_bytes)
    return str(out_p), None


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(default=True, description="Show progress pills while generating infographics or mind maps.")

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def create_infographic_or_mindmap(
        self,
        topic_or_content: str,
        visual_type: str = "auto",
        language: str = "auto",
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Generate a publication-grade high-DPI infographic, mind map (PNG + vector SVG), or timeline diagram from a topic or pasted notes.

        :param topic_or_content: Subject, key points, or text to visualize clearly.
        :param visual_type: 'auto', 'infographic', 'mindmap', or 'timeline'.
        :param language: Output language ('auto', 'en', or 'fr').
        :return: Inline visual preview image and downloadable PNG/SVG links.
        """
        renderer_py = Path.home() / "AI" / "smart-visuals" / "renderer.py"
        if not renderer_py.is_file():
            return "[TOOL_RESULT]\nSmart Visuals renderer missing."

        vt = visual_type if visual_type in ("auto", "infographic", "mindmap", "timeline") else "auto"
        await self._emit(__event_emitter__, "🎨 Designing Smart Visual layout on Pro…", done=False)
        py312 = _find_system_python312()

        def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                cmd = [py312, "-I", "-X", "utf8", str(renderer_py), "--topic", topic_or_content[:8000], "--type", vt, "--language", language or "auto", "--out-dir", str(tdp)]
                res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=320, creationflags=NO_WINDOW)
                raw = (res.stdout or res.stderr).strip()
                data = json.loads(raw.splitlines()[-1])
                if res.returncode != 0 or not data.get("ok"):
                    raise RuntimeError(data.get("error") or raw[:300])
                png_p = Path(data["png_path"])
                data["png_bytes"] = png_p.read_bytes()
                if data.get("svg_path") and Path(data["svg_path"]).is_file():
                    data["svg_bytes"] = Path(data["svg_path"]).read_bytes()
                return data

        try:
            data = await asyncio.to_thread(_run)
        except Exception as e:
            await self._emit(__event_emitter__, "⚠️ Smart Visual failed", done=True)
            return f"[TOOL_RESULT]\nCould not generate visual: {e}"

        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(data.get("title") or "visual")[:40]).strip("-").lower() or "visual"
        stamp = uuid.uuid4().hex[:6]
        png_name = f"{data['visual_type']}-{slug}_{stamp}.png"
        png_url, png_fid = await _upload_file(data["png_bytes"], png_name, "image/png", __request__, __user__)

        files_emitted = []
        if png_url:
            entry: dict[str, Any] = {"type": "file", "url": png_url, "name": png_name}
            if png_fid:
                entry["id"] = png_fid
            files_emitted.append(entry)

        svg_url = None
        svg_name = f"{data['visual_type']}-{slug}_{stamp}.svg"
        if data.get("svg_bytes"):
            svg_url, svg_fid = await _upload_file(data["svg_bytes"], svg_name, "image/svg+xml", __request__, __user__)
            if svg_url:
                s_entry: dict[str, Any] = {"type": "file", "url": svg_url, "name": svg_name}
                if svg_fid:
                    s_entry["id"] = svg_fid
                files_emitted.append(s_entry)

        if __event_emitter__ and files_emitted:
            try:
                await __event_emitter__({"type": "files", "data": {"files": files_emitted}})
            except Exception:
                pass

        await self._emit(__event_emitter__, f"🎨 {data['visual_type'].title()} ready · {data.get('title', '')[:40]}", done=True)

        lines = [
            "[TOOL_RESULT — display the generated image markdown and download link(s) cleanly below and stop calling tools.]",
            "",
            f"🎨 **{data.get('title')}** ({data['visual_type']})",
            "",
            f"![{data.get('title')}]({png_url})",
            "",
            f"📥 [Download PNG ({png_name})]({png_url})",
        ]
        if svg_url:
            lines.append(f"📐 [Download Editable SVG ({svg_name})]({svg_url})")
        return "\n".join(lines)
