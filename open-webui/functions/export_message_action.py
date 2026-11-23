"""
title: Export Word / PDF
author: Local AI Stack
description: 1-click message action buttons under every assistant reply to export clean Word (.docx) or A4 PDF documents (automatically stripping thinking blocks).
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

log = logging.getLogger("export_message_action")

try:
    from fastapi import UploadFile
    from starlette.datastructures import Headers
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler

    _HAS_OWUI_FILES = True
except Exception:
    _HAS_OWUI_FILES = False

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_ICON_SVG = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='currentColor' "
    "stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z'/>"
    "<polyline points='14 2 14 8 20 8'/><line x1='16' y1='13' x2='8' y2='13'/>"
    "<line x1='16' y1='17' x2='8' y2='17'/></svg>"
)


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


def _extract_target_message(body: dict) -> tuple[str, str]:
    msgs = body.get("messages") or []
    target_id = body.get("id")
    content = ""
    user_prompt = ""
    if target_id:
        for i, m in enumerate(msgs):
            if m.get("id") == target_id and m.get("role") == "assistant":
                content = str(m.get("content") or "")
                for j in range(i - 1, -1, -1):
                    if msgs[j].get("role") == "user":
                        user_prompt = str(msgs[j].get("content") or "")
                        break
                break
    if not content:
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                content = str(msgs[i].get("content") or "")
                for j in range(i - 1, -1, -1):
                    if msgs[j].get("role") == "user":
                        user_prompt = str(msgs[j].get("content") or "")
                        break
                break
    m_h = re.search(r"(?m)^#{1,2}\s+(.+)$", content)
    if m_h:
        title = re.sub(r"[*_`]+", "", m_h.group(1)).strip()[:80]
    elif user_prompt:
        title = re.sub(r"\s+", " ", user_prompt).strip()[:65]
    else:
        title = "Chat Export"
    return title or "Chat Export", content


class Action:
    class Valves(BaseModel):
        emit_status: bool = Field(default=True, description="Emit progress pills when exporting.")

    def __init__(self):
        self.valves = self.Valves()
        self.actions = [
            {"id": "docx", "name": "Export Word (.docx)", "icon_url": _ICON_SVG},
            {"id": "pdf", "name": "Export PDF", "icon_url": _ICON_SVG},
        ]

    async def action(
        self,
        body: dict,
        __user__: Optional[dict] = None,
        __id__: Optional[str] = None,
        __event_emitter__=None,
        __request__=None,
    ) -> None:
        fmt = "docx" if __id__ == "docx" else "pdf"
        label = "Word (.docx)" if fmt == "docx" else "PDF"
        title, content = _extract_target_message(body or {})
        if not content.strip():
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "⚠️ Empty message to export", "done": True}})
            return

        exporter_py = Path.home() / "AI" / "doc-export" / "exporter.py"
        if not exporter_py.is_file():
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "⚠️ Exporter script missing", "done": True}})
            return

        if __event_emitter__ and self.valves.emit_status:
            await __event_emitter__({"type": "status", "data": {"description": f"📄 Exporting {label}…", "done": False}})

        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-").lower()[:45] or "export"
        fname = f"{slug}_{uuid.uuid4().hex[:6]}.{fmt}"
        py312 = _find_system_python312()

        def _build() -> bytes:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                in_md = tdp / "msg.md"
                out_f = tdp / fname
                in_md.write_text(content, encoding="utf-8")
                res = subprocess.run(
                    [py312, "-I", "-X", "utf8", str(exporter_py), "--format", fmt, "--title", title, "--input-file", str(in_md), "--output", str(out_f)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    creationflags=NO_WINDOW,
                )
                if res.returncode != 0:
                    raise RuntimeError((res.stderr or res.stdout)[:300])
                return out_f.read_bytes()

        raw_bytes = await asyncio.to_thread(_build)
        ctype = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if fmt == "docx"
            else "application/pdf"
        )
        url, fid = None, None
        if _HAS_OWUI_FILES and __request__ is not None and isinstance(__user__, dict) and __user__.get("id"):
            try:
                user_obj = Users.get_user_by_id(__user__["id"])
                if hasattr(user_obj, "__await__"):
                    user_obj = await user_obj
                if user_obj is not None:
                    import io

                    upload = UploadFile(file=io.BytesIO(raw_bytes), filename=fname, headers=Headers({"content-type": ctype}))
                    file_obj = upload_file_handler(request=__request__, file=upload, metadata={"source": "export_action"}, process=False, user=user_obj)
                    if hasattr(file_obj, "__await__"):
                        file_obj = await file_obj
                    fid = getattr(file_obj, "id", None) or (file_obj.get("id") if isinstance(file_obj, dict) else None)
                    if fid:
                        url = f"/api/v1/files/{fid}/content"
            except Exception as e:
                log.warning("Action file upload failed: %s", e)

        if not url:
            fallback = Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"
            fallback.mkdir(parents=True, exist_ok=True)
            out_p = fallback / fname
            out_p.write_bytes(raw_bytes)
            url = str(out_p)

        if __event_emitter__:
            f_entry: dict[str, Any] = {"type": "file", "url": url, "name": fname}
            if fid:
                f_entry["id"] = fid
            await __event_emitter__({"type": "files", "data": {"files": [f_entry]}})
            kb = max(1, len(raw_bytes) // 1024)
            await __event_emitter__({"type": "message", "data": {"content": f"\n\n---\n📥 **Exported {label} ({kb} KB):** [Download {fname}]({url})"}})
            if self.valves.emit_status:
                await __event_emitter__({"type": "status", "data": {"description": f"📄 {label} ready · {kb} KB", "done": True}})
