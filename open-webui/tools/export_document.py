"""
title: Export Word & PDF
author: Local AI Stack
description: Export any chat summary, report, table, or answer into a cleanly styled Word (.docx) or A4 PDF document with one click and downloadable link.
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

log = logging.getLogger("export_document_tool")

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


def _slugify(title: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (title or "document").strip()).strip("-").lower()
    return (s[:48] or "document")


async def _upload_to_owui(
    raw_bytes: bytes,
    filename: str,
    content_type: str,
    request: Any,
    user_dict: Optional[dict],
    fallback_dir: Path,
) -> tuple[Optional[str], Optional[str]]:
    if _HAS_OWUI_FILES and request is not None and isinstance(user_dict, dict) and user_dict.get("id"):
        try:
            user_obj = Users.get_user_by_id(user_dict["id"])
            if hasattr(user_obj, "__await__"):
                user_obj = await user_obj
            if user_obj is not None:
                import io

                upload = UploadFile(
                    file=io.BytesIO(raw_bytes),
                    filename=filename,
                    headers=Headers({"content-type": content_type}),
                )
                file_obj = upload_file_handler(
                    request=request,
                    file=upload,
                    metadata={"source": "export_document"},
                    process=False,
                    user=user_obj,
                )
                if hasattr(file_obj, "__await__"):
                    file_obj = await file_obj
                fid = getattr(file_obj, "id", None) or (file_obj.get("id") if isinstance(file_obj, dict) else None)
                if fid:
                    return f"/api/v1/files/{fid}/content", str(fid)
        except Exception as e:
            log.warning("Files API upload failed (%s), using fallback", e)

    fallback_dir.mkdir(parents=True, exist_ok=True)
    out_p = fallback_dir / f"{uuid.uuid4().hex[:8]}_{filename}"
    out_p.write_bytes(raw_bytes)
    return str(out_p), None


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(default=True, description="Show export progress pills in Open WebUI.")
        export_fallback_dir: str = Field(default="", description="Fallback export folder.")

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def _export(
        self,
        fmt: str,
        title: str,
        markdown_content: str,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        exporter_py = Path.home() / "AI" / "doc-export" / "exporter.py"
        if not exporter_py.is_file():
            return "[TOOL_RESULT]\nDocument exporter not found at `AI/doc-export/exporter.py`."

        label = "Word (.docx)" if fmt == "docx" else "PDF"
        await self._emit(__event_emitter__, f"📄 Generating {label} document…", done=False)
        py312 = _find_system_python312()
        fname = f"{_slugify(title)}_{uuid.uuid4().hex[:6]}.{fmt}"

        def _run() -> dict[str, Any]:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                in_md = tdp / "input.md"
                out_f = tdp / fname
                in_md.write_text(markdown_content or "", encoding="utf-8")
                res = subprocess.run(
                    [py312, "-I", "-X", "utf8", str(exporter_py), "--format", fmt, "--title", title or "Document", "--input-file", str(in_md), "--output", str(out_f)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=60,
                    creationflags=NO_WINDOW,
                )
                if res.returncode != 0:
                    raise RuntimeError((res.stderr or res.stdout or f"Exit {res.returncode}")[:400])
                info = json.loads(res.stdout.strip().splitlines()[-1])
                info["raw_bytes"] = out_f.read_bytes()
                return info

        try:
            info = await asyncio.to_thread(_run)
        except Exception as e:
            await self._emit(__event_emitter__, f"⚠️ {label} export failed", done=True)
            return f"[TOOL_RESULT]\nFailed to export {label}: {e}"

        ctype = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if fmt == "docx"
            else "application/pdf"
        )
        fallback = Path((self.valves.export_fallback_dir or "").strip() or (Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"))
        url, fid = await _upload_to_owui(info["raw_bytes"], fname, ctype, __request__, __user__, fallback)
        if __event_emitter__ and url:
            f_entry: dict[str, Any] = {"type": "file", "url": url, "name": fname}
            if fid:
                f_entry["id"] = fid
            try:
                await __event_emitter__({"type": "files", "data": {"files": [f_entry]}})
            except Exception:
                pass
        kb = max(1, len(info["raw_bytes"]) // 1024)
        await self._emit(__event_emitter__, f"📄 {label} ready · {kb} KB", done=True)
        return (
            "[TOOL_RESULT — share the download link cleanly below in your reply and stop calling tools.]\n\n"
            f"📄 **{label} Ready** ({kb} KB)\n\n📥 [Download {fname}]({url})"
        )

    async def export_markdown_to_docx(
        self,
        title: str,
        markdown_content: str,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Export Markdown content into a cleanly formatted Microsoft Word (.docx) document with download link.

        :param title: Document title for the cover header and filename.
        :param markdown_content: Full Markdown body (headings, bullets, tables, code blocks, quotes).
        :return: Downloadable Word (.docx) file link.
        """
        return await self._export("docx", title, markdown_content, __event_emitter__, __user__, __request__)

    async def export_markdown_to_pdf(
        self,
        title: str,
        markdown_content: str,
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Export Markdown content into a cleanly styled A4 PDF document with download link.

        :param title: Document title for the header and filename.
        :param markdown_content: Full Markdown body (headings, bullets, tables, code blocks, quotes).
        :return: Downloadable PDF file link.
        """
        return await self._export("pdf", title, markdown_content, __event_emitter__, __user__, __request__)
