"""
title: YouTube Transcript
author: Local AI Stack
description: Fetch clean timestamped transcripts and metadata from any YouTube video URL locally using youtube-transcript-api (supports EN/FR and auto-translation).
version: 1.0.0
license: MIT
"""

import asyncio
import json
import re
import urllib.parse
import urllib.request
from typing import Optional

from pydantic import BaseModel, Field

_YT_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})\b"
    r"|^\s*([A-Za-z0-9_-]{11})\s*$",
    re.I,
)
_MAX_TRANSCRIPT_CHARS = 55000


def _extract_video_id(url_or_id: str) -> Optional[str]:
    m = _YT_ID_RE.search((url_or_id or "").strip())
    if not m:
        return None
    return m.group(1) or m.group(2)


def _format_ts(seconds: float) -> str:
    total = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _fetch_oembed_meta(video_id: str) -> dict:
    url = f"https://www.youtube.com/oembed?url={urllib.parse.quote(f'https://www.youtube.com/watch?v={video_id}', safe='')}&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LocalAIStack/1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
            return {
                "title": str(data.get("title") or "").strip(),
                "author": str(data.get("author_name") or "").strip(),
            }
    except Exception:
        return {"title": "", "author": ""}


def _snippet_fields(snip) -> tuple[str, float, float]:
    if isinstance(snip, dict):
        text = str(snip.get("text") or "")
        start = float(snip.get("start") or 0.0)
        dur = float(snip.get("duration") or 0.0)
    else:
        text = str(getattr(snip, "text", "") or "")
        start = float(getattr(snip, "start", 0.0) or 0.0)
        dur = float(getattr(snip, "duration", 0.0) or 0.0)
    text = re.sub(r"\s+", " ", text).strip()
    return text, start, dur


def _fetch_transcript_sync(video_id: str, preferred_languages: str) -> dict:
    from youtube_transcript_api import YouTubeTranscriptApi

    langs = [x.strip().lower() for x in (preferred_languages or "en,fr").split(",") if x.strip()]
    if not langs:
        langs = ["en", "fr"]

    api = YouTubeTranscriptApi()
    fetched = None
    language_used = ""
    translated = False

    try:
        fetched = api.fetch(video_id, languages=langs)
        language_used = str(getattr(fetched, "language_code", "") or langs[0])
    except Exception:
        tlist = api.list(video_id)
        chosen = None
        for lang in langs:
            try:
                chosen = tlist.find_transcript([lang])
                break
            except Exception:
                continue
        if chosen is None:
            available = list(tlist)
            if not available:
                raise RuntimeError("No captions/transcripts are available for this video.")
            chosen = available[0]
            if getattr(chosen, "is_translatable", False):
                try:
                    chosen = chosen.translate(langs[0])
                    translated = True
                except Exception:
                    pass
        fetched = chosen.fetch()
        language_used = str(getattr(fetched, "language_code", "") or getattr(chosen, "language_code", "") or "")

    raw_snippets = list(fetched)
    if not raw_snippets:
        raise RuntimeError("Transcript returned empty.")

    meta = _fetch_oembed_meta(video_id)
    last_start = 0.0
    chunks: list[tuple[float, list[str]]] = []
    cur_start: Optional[float] = None
    cur_words: list[str] = []

    for sn in raw_snippets:
        txt, start, dur = _snippet_fields(sn)
        last_start = max(last_start, start + dur)
        if not txt:
            continue
        if cur_start is None:
            cur_start = start
        cur_words.append(txt)
        if start - cur_start >= 18.0:
            chunks.append((cur_start, cur_words))
            cur_start = None
            cur_words = []
    if cur_words and cur_start is not None:
        chunks.append((cur_start, cur_words))

    return {
        "video_id": video_id,
        "title": meta["title"],
        "author": meta["author"],
        "language": language_used,
        "translated": translated,
        "duration_s": int(round(last_start)),
        "chunks": chunks,
    }


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(
            default=True,
            description="Show live YouTube transcript status pills in Open WebUI.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def get_youtube_transcript(
        self,
        url: str,
        preferred_languages: str = "en,fr",
        include_timestamps: bool = True,
        __event_emitter__=None,
    ) -> str:
        """
        Fetch the full transcript and title/channel metadata for a YouTube video URL or 11-char ID.

        :param url: YouTube video URL (watch, youtu.be, shorts, live, embed) or 11-character video ID.
        :param preferred_languages: Comma-separated priority list of language codes (default 'en,fr').
        :param include_timestamps: Include [MM:SS] timestamps in each paragraph (default true).
        :return: Clean transcript text with title, channel, duration, and timestamps.
        """
        vid = _extract_video_id(url)
        if not vid:
            return "[TOOL_RESULT]\nCould not find a valid YouTube video URL or 11-character video ID."

        await self._emit(__event_emitter__, "📺 Fetching YouTube transcript…", done=False)
        try:
            data = await asyncio.to_thread(_fetch_transcript_sync, vid, preferred_languages)
        except Exception as e:
            await self._emit(__event_emitter__, "⚠️ YouTube transcript unavailable", done=True)
            return f"[TOOL_RESULT]\nCould not fetch transcript for `{vid}`: {str(e)[:300]}"

        lines = []
        for ts, words in data["chunks"]:
            para = " ".join(words)
            lines.append(f"[{_format_ts(ts)}] {para}" if include_timestamps else para)
        body = "\n".join(lines)
        truncated = False
        if len(body) > _MAX_TRANSCRIPT_CHARS:
            body = body[:_MAX_TRANSCRIPT_CHARS].rsplit("\n", 1)[0] + "\n\n[Transcript truncated at 55,000 chars]"
            truncated = True

        dur_str = _format_ts(data["duration_s"])
        title = data["title"] or f"YouTube Video ({vid})"
        channel = data["author"] or "Unknown channel"
        lang_note = data["language"] + (" (auto-translated)" if data["translated"] else "")
        await self._emit(__event_emitter__, f"📺 Transcript ready · {title[:45]} ({dur_str})", done=True)

        header = [
            f"TITLE: {title}",
            f"CHANNEL: {channel}",
            f"URL: https://www.youtube.com/watch?v={vid}",
            f"DURATION: {dur_str} | LANGUAGE: {lang_note}" + (" | TRUNCATED: yes" if truncated else ""),
            "TRANSCRIPT:",
            body,
        ]
        return "\n".join(header)
