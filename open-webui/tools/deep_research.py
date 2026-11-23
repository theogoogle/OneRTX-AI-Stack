"""
title: Deep Research
author: Local AI Stack
description: Multi-step autonomous research agent (plans sub-queries, reads full public web pages, cross-checks sources, synthesizes a cited report on Pro, and exports a clean PDF).
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

log = logging.getLogger("deep_research_tool")

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


async def _upload_pdf(raw_bytes: bytes, filename: str, request: Any, user_dict: Optional[dict]) -> tuple[Optional[str], Optional[str]]:
    if _HAS_OWUI_FILES and request is not None and isinstance(user_dict, dict) and user_dict.get("id"):
        try:
            user_obj = Users.get_user_by_id(user_dict["id"])
            if hasattr(user_obj, "__await__"):
                user_obj = await user_obj
            if user_obj is not None:
                import io

                upload = UploadFile(file=io.BytesIO(raw_bytes), filename=filename, headers=Headers({"content-type": "application/pdf"}))
                file_obj = upload_file_handler(request=request, file=upload, metadata={"source": "deep_research"}, process=False, user=user_obj)
                if hasattr(file_obj, "__await__"):
                    file_obj = await file_obj
                fid = getattr(file_obj, "id", None) or (file_obj.get("id") if isinstance(file_obj, dict) else None)
                if fid:
                    return f"/api/v1/files/{fid}/content", str(fid)
        except Exception as e:
            log.warning("Deep Research PDF upload failed: %s", e)
    fallback = Path.home() / "AI" / "open-webui" / "data" / "cache" / "files"
    fallback.mkdir(parents=True, exist_ok=True)
    out_p = fallback / f"{uuid.uuid4().hex[:8]}_{filename}"
    out_p.write_bytes(raw_bytes)
    return str(out_p), None


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(default=True, description="Show live Deep Research progress pills in Open WebUI.")
        export_pdf: bool = Field(default=True, description="Also attach a formatted A4 PDF of the research report.")

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def run_deep_research(
        self,
        topic: str,
        language: str = "auto",
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Run multi-step autonomous web research (4 complementary sub-queries, parallel full-page reading, cited synthesis on Pro, plus downloadable PDF report).

        :param topic: Detailed research question or topic to investigate thoroughly.
        :param language: Output language ('auto' matches the user's language, or 'en'/'fr').
        :return: Comprehensive cited Markdown report and downloadable PDF link.
        """
        engine_py = Path.home() / "AI" / "deep-research" / "engine.py"
        if not engine_py.is_file():
            return "[TOOL_RESULT]\nDeep Research engine missing."

        await self._emit(__event_emitter__, "🔬 Starting local Deep Research…", done=False)
        py312 = _find_system_python312()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Optional[tuple[str, bool]]] = asyncio.Queue()
        stdout_holder: list[str] = []
        stderr_holder: list[str] = []

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cmd = [py312, "-I", "-X", "utf8", str(engine_py), "--topic", topic[:1000], "--language", language or "auto", "--out-dir", str(tdp)]
            if not self.valves.export_pdf:
                cmd.append("--no-pdf")

            def _run_proc() -> tuple[int, Optional[bytes]]:
                import threading

                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
                assert proc.stderr is not None and proc.stdout is not None

                def _read_out():
                    stdout_holder.append(proc.stdout.read())

                t_out = threading.Thread(target=_read_out, daemon=True)
                t_out.start()
                for raw_line in proc.stderr:
                    ln = raw_line.strip()
                    if ln.startswith("EVENT:"):
                        try:
                            ev = json.loads(ln[6:])
                            loop.call_soon_threadsafe(queue.put_nowait, (str(ev.get("description") or ""), bool(ev.get("done", False))))
                        except Exception:
                            pass
                    elif ln:
                        stderr_holder.append(ln)
                t_out.join(timeout=10)
                rc = proc.wait()
                pdf_b = None
                pdf_f = tdp / "deep_research_report.pdf"
                if pdf_f.is_file():
                    pdf_b = pdf_f.read_bytes()
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return rc, pdf_b

            fut = loop.run_in_executor(None, _run_proc)
            while True:
                item = await queue.get()
                if item is None:
                    break
                desc, is_done = item
                if desc and not is_done:
                    await self._emit(__event_emitter__, desc, done=False)
            rc, pdf_bytes = await fut

        out_txt = "".join(stdout_holder).strip()
        try:
            data = json.loads(out_txt) if out_txt else {}
        except Exception:
            data = {}

        if rc != 0 or not data.get("ok"):
            err = data.get("error") or "\n".join(stderr_holder)[:400] or f"Exit {rc}"
            await self._emit(__event_emitter__, "⚠️ Deep Research failed", done=True)
            return f"[TOOL_RESULT]\nDeep Research failed: {err}"

        pdf_url, pdf_fid = None, None
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", topic[:40]).strip("-").lower() or "research"
        fname = f"research-{slug}_{uuid.uuid4().hex[:6]}.pdf"
        if pdf_bytes:
            pdf_url, pdf_fid = await _upload_pdf(pdf_bytes, fname, __request__, __user__)
            if __event_emitter__ and pdf_url:
                f_entry: dict[str, Any] = {"type": "file", "url": pdf_url, "name": fname}
                if pdf_fid:
                    f_entry["id"] = pdf_fid
                try:
                    await __event_emitter__({"type": "files", "data": {"files": [f_entry]}})
                except Exception:
                    pass

        await self._emit(__event_emitter__, f"🔬 Deep Research complete · {data.get('sources_count', 0)} sources ({data.get('elapsed_s')}s)", done=True)

        report = str(data.get("report_markdown") or "").strip()
        if pdf_url:
            report += f"\n\n---\n📥 [Download Full Research Report ({fname})]({pdf_url})"
        return "[TOOL_RESULT — present this full cited report and PDF link cleanly to the user without dropping citations or links.]\n\n" + report
