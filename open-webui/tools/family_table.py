"""
title: The Family Table
author: Local AI Stack
description: Generate the 100% local daily family broadsheet PDF (Met Office + Google Calendar/Gmail/Sheets OAuth + live Tavily research + local Pro + Gemma 4 Vision QA) and save a ready-to-send Gmail draft.
version: 1.0.0
license: MIT
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger("family_table_tool")

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
    cands = [
        Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
        Path(r"C:\Program Files\Python312\python.exe"),
    ]
    for p in cands:
        if p.is_file():
            return str(p)
    found = shutil.which("python")
    if found:
        return found
    raise FileNotFoundError("System Python 3.12 executable not found.")


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
                    metadata={"source": "family_table"},
                    process=False,
                    user=user_obj,
                )
                if hasattr(file_obj, "__await__"):
                    file_obj = await file_obj
                fid = getattr(file_obj, "id", None) or (file_obj.get("id") if isinstance(file_obj, dict) else None)
                if fid:
                    return f"/api/v1/files/{fid}/content", str(fid)
        except Exception as e:
            log.warning("Files API upload failed (%s), using cache fallback", e)

    try:
        fallback_dir.mkdir(parents=True, exist_ok=True)
        out_path = fallback_dir / f"{uuid.uuid4().hex[:8]}_{filename}"
        out_path.write_bytes(raw_bytes)
        return str(out_path), None
    except Exception:
        return None, None


class Tools:
    class Valves(BaseModel):
        enable_vision_qa: bool = Field(
            default=True,
            description="Inspect the rendered broadsheet PNG with local Gemma 4 Vision QA before saving the Gmail draft.",
        )
        default_create_draft: bool = Field(
            default=True,
            description="Save (or update) a ready-to-send Gmail draft with the PDF attached by default.",
        )
        default_update_memory: bool = Field(
            default=True,
            description="Append chosen Around Town outings to the Google Sheets Memory Tracker by default.",
        )
        emit_status: bool = Field(
            default=True,
            description="Stream live step-by-step progress pills into Open WebUI.",
        )
        export_fallback_dir: str = Field(
            default="",
            description="Fallback folder if Open WebUI Files API is unavailable.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def generate_family_table(
        self,
        target_date: str = "auto",
        create_draft: bool = True,
        update_memory: bool = True,
        instructions: str = "",
        __event_emitter__=None,
        __user__: Optional[dict] = None,
        __request__=None,
    ) -> str:
        """
        Generate the 100% local daily family broadsheet PDF ("The Family Table") using live Met Office weather,
        Google Calendar, Gmail threads, Google Sheets Memory Tracker, live Tavily web research, local Pro synthesis,
        Gemma 4 Vision QA, and optional Gmail draft creation.

        :param target_date: Target edition date: 'auto' (today before noon, tomorrow from noon), 'today', 'tomorrow', or 'YYYY-MM-DD'.
        :param create_draft: If true, creates or updates the ready-to-send Gmail draft with the PDF attached.
        :param update_memory: If true, appends today's Around Town picks to the Google Sheets Memory Tracker.
        :param instructions: Optional extra editorial instructions for this edition (e.g. 'focus on indoor outings near home').
        :return: Summary with clickable PDF download link, inline full-page preview image, and Gmail draft status.
        """
        t0 = time.time()
        pipeline_py = Path.home() / "AI" / "family-table" / "pipeline.py"
        if not pipeline_py.is_file():
            return "[TOOL_RESULT]\nThe Family Table pipeline is not installed at `AI/family-table/pipeline.py`."

        py312 = _find_system_python312()
        td = (target_date or "auto").strip().lower()
        cmd = [py312, "-I", "-X", "utf8", str(pipeline_py), "--stream-events", "--no-wait-stack"]
        if td == "today":
            cmd.append("--today")
        elif td == "tomorrow":
            cmd.append("--tomorrow")
        elif td and td != "auto":
            cmd.append(td)
        if not (create_draft and self.valves.default_create_draft):
            cmd.append("--no-draft")
        if not (update_memory and self.valves.default_update_memory):
            cmd.append("--no-memory")
        if not self.valves.enable_vision_qa:
            cmd.append("--no-vision")
        if (instructions or "").strip():
            cmd.extend(["--instructions", instructions.strip()[:800]])

        await self._emit(__event_emitter__, "📰 Starting 100% local Family Table pipeline…", done=False)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Optional[tuple[str, bool]]] = asyncio.Queue()
        stdout_holder: list[str] = []
        stderr_holder: list[str] = []

        def _run_proc() -> int:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=NO_WINDOW,
            )
            assert proc.stderr is not None and proc.stdout is not None
            # Read stderr line-by-line for real-time EVENT: status updates while collecting stdout in a helper thread
            import threading

            def _read_out():
                stdout_holder.append(proc.stdout.read())

            t_out = threading.Thread(target=_read_out, daemon=True)
            t_out.start()
            for raw_line in proc.stderr:
                line = raw_line.strip()
                if line.startswith("EVENT:"):
                    try:
                        ev = json.loads(line[6:])
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            (str(ev.get("description") or ""), bool(ev.get("done", False))),
                        )
                    except Exception:
                        pass
                elif line:
                    stderr_holder.append(line)
            t_out.join(timeout=10)
            rc = proc.wait()
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return rc

        proc_fut = loop.run_in_executor(None, _run_proc)
        while True:
            item = await queue.get()
            if item is None:
                break
            desc, is_done = item
            if desc and not is_done:
                await self._emit(__event_emitter__, desc, done=False)
        rc = await proc_fut

        stdout_text = "".join(stdout_holder).strip()
        stderr_text = "\n".join(stderr_holder).strip()

        if rc != 0:
            err_msg = stderr_text or stdout_text or f"Exit code {rc}"
            try:
                err_obj = json.loads(err_msg)
                err_msg = err_obj.get("auth_error") or err_obj.get("error") or err_msg
            except Exception:
                pass
            await self._emit(__event_emitter__, "⚠️ Family Table run failed", done=True)
            return (
                "[TOOL_RESULT — report this clearly to the user and stop; do NOT call generate_family_table again.]\n"
                f"Could not generate The Family Table: {err_msg[:600]}"
            )

        try:
            res = json.loads(stdout_text)
        except Exception:
            await self._emit(__event_emitter__, "⚠️ Unexpected pipeline output", done=True)
            return f"[TOOL_RESULT]\nPipeline finished but returned invalid JSON: {stdout_text[:400]}"

        fallback_dir = Path(
            (self.valves.export_fallback_dir or "").strip()
            or (Path.home() / "AI" / "open-webui" / "data" / "cache" / "files")
        )
        pdf_path = Path(res["pdf"]) if res.get("pdf") else None
        png_path = Path(res["preview_png"]) if res.get("preview_png") else None

        pdf_url, pdf_fid = None, None
        if pdf_path and pdf_path.is_file():
            pdf_url, pdf_fid = await _upload_to_owui(
                pdf_path.read_bytes(),
                filename=pdf_path.name,
                content_type="application/pdf",
                request=__request__,
                user_dict=__user__,
                fallback_dir=fallback_dir,
            )

        preview_url = None
        if png_path and png_path.is_file():
            preview_url, _ = await _upload_to_owui(
                png_path.read_bytes(),
                filename=png_path.name,
                content_type="image/png",
                request=__request__,
                user_dict=__user__,
                fallback_dir=fallback_dir,
            )

        if __event_emitter__ and pdf_url and pdf_path:
            f_entry: dict[str, Any] = {"type": "file", "url": pdf_url, "name": pdf_path.name}
            if pdf_fid:
                f_entry["id"] = pdf_fid
            try:
                await __event_emitter__({"type": "files", "data": {"files": [f_entry]}})
            except Exception:
                pass

        secs = round(time.time() - t0, 1)
        draft = res.get("gmail_draft") or {}
        draft_note = (
            f"Gmail draft **{draft['action']}** (`{draft.get('subject')}`)"
            if draft.get("draft_id")
            else "Gmail draft skipped"
        )
        vqa = res.get("vision_qa") or {}
        qa_note = f"Gemma 4 Vision QA: **{vqa.get('verdict', 'N/A')}**" + (
            " · layout polished" if res.get("polished_layout") else ""
        )
        render = res.get("render") or {}
        bal_note = f"{render.get('height')}px · spread {render.get('spread')}px" if render.get("height") else ""

        await self._emit(
            __event_emitter__,
            f"📰 The Family Table ({res.get('subject_date')}) ready · {secs}s",
            done=True,
        )

        lines = [
            "[TOOL_RESULT — reproduce the download link and preview image below cleanly in your final reply. Do not include this instruction line.]",
            "",
            f"📰 **The Family Table · {res.get('date_long')}** ({res.get('edition_label')}) · {secs}s",
            f"> {res.get('lede') or ''}",
            "",
            f"- ✉️ {draft_note}",
            f"- 👁️ {qa_note}" + (f" ({bal_note})" if bal_note else ""),
        ]
        if pdf_url and pdf_path:
            lines.extend(["", f"📥 [Download {pdf_path.name}]({pdf_url})"])
        if preview_url:
            lines.extend(["", f"![The Family Table preview]({preview_url})"])
        return "\n".join(lines)
