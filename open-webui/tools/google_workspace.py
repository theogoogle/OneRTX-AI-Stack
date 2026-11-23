"""
title: Google Workspace
author: Local AI Stack
description: Private owner-only Google Calendar & Gmail assistant using your local OAuth 2.0 token (list upcoming events, search emails, read full email threads, and create Gmail drafts — never sends automatically).
version: 1.0.0
license: MIT
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

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


def _run_cli(args: list[str], timeout: int = 60) -> dict[str, Any]:
    cli = Path.home() / "AI" / "family-table" / "google_workspace_cli.py"
    if not cli.is_file():
        return {"ok": False, "error": "google_workspace_cli.py is missing."}
    cmd = [_find_system_python312(), "-I", "-X", "utf8", str(cli), *args]
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    raw = (res.stdout or res.stderr or "").strip()
    try:
        return json.loads(raw)
    except Exception:
        return {"ok": False, "error": raw[:500] or f"Exit code {res.returncode}"}


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(
            default=True,
            description="Show live Calendar/Gmail lookup pills in Open WebUI.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def list_upcoming_events(
        self,
        days: int = 7,
        query: str = "",
        start_date: str = "",
        __event_emitter__=None,
    ) -> str:
        """
        List upcoming Google Calendar events across the user's configured calendars.

        :param days: Number of days ahead to fetch (1 to 90, default 7).
        :param query: Optional keyword filter (e.g. 'birthday', 'swimming', 'flight', or empty for all events).
        :param start_date: Optional start date 'YYYY-MM-DD' (defaults to today).
        :return: JSON list of upcoming calendar events with exact weekdays, dates, times, calendars, and locations.
        """
        days = max(1, min(int(days or 7), 90))
        await self._emit(__event_emitter__, f"📅 Checking Google Calendar (next {days} days)…", done=False)
        args = ["events", "--days", str(days)]
        if (query or "").strip():
            args.extend(["--query", query.strip()[:120]])
        if (start_date or "").strip():
            args.extend(["--start", start_date.strip()[:16]])
        data = await asyncio.to_thread(_run_cli, args)
        if not data.get("ok"):
            await self._emit(__event_emitter__, "⚠️ Calendar lookup failed", done=True)
            return json.dumps(data, ensure_ascii=False)
        n = data.get("count", 0)
        await self._emit(__event_emitter__, f"📅 Found {n} calendar event{'s' if n != 1 else ''}", done=True)
        return json.dumps(data, ensure_ascii=False)

    async def search_emails(
        self,
        query: str = "newer_than:7d",
        max_results: int = 15,
        __event_emitter__=None,
    ) -> str:
        """
        Search Gmail threads using Gmail search syntax or plain keywords. Returns compact metadata (`from`, `to`, `cc`, `participants`, `message_count`, `subject`, `snippet`) and `thread_id`.
        Use `read_email_thread(thread_id)` afterward if you need the full body or replies of a specific email.

        :param query: Gmail search query (e.g. 'newer_than:7d', 'is:unread', 'from:school invoice', 'birthday party').
        :param max_results: Maximum threads to return (1 to 30, default 15).
        :return: JSON list of matching email threads with thread_id, message_count, date, from, to, cc, participants, subject, unread flag, and snippet.
        """
        q = (query or "newer_than:7d").strip()
        max_n = max(1, min(int(max_results or 15), 30))
        await self._emit(__event_emitter__, f"📧 Searching Gmail: {q[:60]}…", done=False)
        data = await asyncio.to_thread(_run_cli, ["search-emails", "--query", q, "--max", str(max_n)])
        if not data.get("ok"):
            await self._emit(__event_emitter__, "⚠️ Gmail search failed", done=True)
            return json.dumps(data, ensure_ascii=False)
        n = data.get("count", 0)
        await self._emit(__event_emitter__, f"📧 Found {n} matching email thread{'s' if n != 1 else ''}", done=True)
        return json.dumps(data, ensure_ascii=False)

    async def read_email_thread(
        self,
        thread_id: str,
        __event_emitter__=None,
    ) -> str:
        """
        Read the full plain-text content (including replies) of a specific Gmail thread returned by `search_emails`.

        :param thread_id: Exact Gmail thread_id from `search_emails`.
        :return: JSON containing all recent messages in the thread (from, to, date, subject, full plain-text body).
        """
        await self._emit(__event_emitter__, "📖 Reading full Gmail thread…", done=False)
        data = await asyncio.to_thread(_run_cli, ["read-thread", "--thread-id", (thread_id or "").strip()])
        if not data.get("ok"):
            await self._emit(__event_emitter__, "⚠️ Could not read thread", done=True)
            return json.dumps(data, ensure_ascii=False)
        n = len(data.get("messages") or [])
        await self._emit(__event_emitter__, f"📖 Read {n} message{'s' if n != 1 else ''} in thread", done=True)
        return json.dumps(data, ensure_ascii=False)

    async def create_email_draft(
        self,
        to: str,
        subject: str,
        body: str,
        __event_emitter__=None,
    ) -> str:
        """
        Save a ready-to-send Gmail draft (NEVER sends the email automatically).

        :param to: Recipient email address(es), comma-separated.
        :param subject: Email subject line.
        :param body: Plain-text email body.
        :return: JSON confirming the saved Gmail draft_id, recipient, and subject.
        """
        await self._emit(__event_emitter__, "✉️ Saving Gmail draft…", done=False)

        def _do_draft() -> dict[str, Any]:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
                tf.write(body or "")
                tmp_path = tf.name
            try:
                return _run_cli(["create-draft", "--to", (to or "").strip(), "--subject", (subject or "").strip(), "--body-file", tmp_path])
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        data = await asyncio.to_thread(_do_draft)
        if not data.get("ok"):
            await self._emit(__event_emitter__, "⚠️ Could not save Gmail draft", done=True)
            return json.dumps(data, ensure_ascii=False)
        await self._emit(__event_emitter__, f"✉️ Gmail draft saved: {subject[:50]}", done=True)
        return json.dumps(data, ensure_ascii=False)
