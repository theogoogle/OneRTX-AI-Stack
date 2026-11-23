"""
title: YouTube Transcript
author: Local AI Stack
description: Fetch clean timestamped transcripts and metadata from any YouTube video URL locally using youtube-transcript-api (supports EN/FR and auto-translation).
version: 1.0.0
license: MIT
"""

import asyncio
import html
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
_MAX_LLM_TRANSCRIPT_CHARS = 32000
_SENTENCE_END_RE = re.compile(r"""[.!?…]["')\]]*$""")


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
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text, start, dur


def _safe_json_for_script(obj) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


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
    cur_chars = 0

    for sn in raw_snippets:
        txt, start, dur = _snippet_fields(sn)
        last_start = max(last_start, start + dur)
        if not txt:
            continue
        if cur_start is None:
            cur_start = start
        cur_words.append(txt)
        cur_chars += len(txt) + 1
        elapsed = start - cur_start
        at_sentence_break = bool(_SENTENCE_END_RE.search(txt)) and (elapsed >= 24.0 or cur_chars >= 300)
        at_hard_limit = elapsed >= 52.0 or cur_chars >= 620
        if at_sentence_break or at_hard_limit:
            chunks.append((cur_start, cur_words))
            cur_start = None
            cur_words = []
            cur_chars = 0
    if cur_words and cur_start is not None:
        chunks.append((cur_start, cur_words))

    paragraphs = []
    word_count = 0
    for ts_s, words in chunks:
        para_txt = " ".join(words)
        word_count += len(para_txt.split())
        paragraphs.append({
            "ts": int(max(0, round(float(ts_s)))),
            "ts_label": _format_ts(ts_s),
            "text": para_txt,
        })

    return {
        "video_id": video_id,
        "title": meta["title"],
        "author": meta["author"],
        "language": language_used,
        "translated": translated,
        "duration_s": int(round(last_start)),
        "chunks": chunks,
        "paragraphs": paragraphs,
        "word_count": word_count,
        "read_min": max(1, int(round(word_count / 200))) if word_count else 1,
    }


def render_transcript_html_card(data: dict) -> str:
    vid = str(data.get("video_id") or "")
    vid_esc = html.escape(vid)
    title_raw = str(data.get("title") or f"YouTube Video ({vid})")
    author_raw = str(data.get("author") or "Unknown channel")
    dur_str = _format_ts(data.get("duration_s") or 0)
    lang = str(data.get("language") or "en")
    if data.get("translated"):
        lang = f"{lang} (translated)"
    paragraphs = data.get("paragraphs") or [
        {"ts": int(max(0, round(float(ts)))), "ts_label": _format_ts(ts), "text": " ".join(w)}
        for ts, w in (data.get("chunks") or [])
    ]
    wc = int(data.get("word_count") or sum(len(str(p.get("text") or "").split()) for p in paragraphs))
    read_min = int(data.get("read_min") or max(1, round(wc / 200)))
    watch_url = f"https://www.youtube.com/watch?v={vid_esc}"
    thumb_url = f"https://i.ytimg.com/vi/{vid_esc}/mqdefault.jpg"
    slug = re.sub(r"[^a-z0-9]+", "-", title_raw.lower()).strip("-")[:48] or vid or "youtube"

    payload = {
        "videoId": vid,
        "watchUrl": f"https://www.youtube.com/watch?v={vid}",
        "title": title_raw,
        "author": author_raw,
        "duration": dur_str,
        "language": lang,
        "filename": f"{slug}_transcript.txt",
        "paragraphs": paragraphs,
    }

    css = """
  :root {
    color-scheme: dark light;
    --bg: linear-gradient(145deg, rgba(15, 23, 42, 0.96), rgba(23, 31, 50, 0.92));
    --surface: rgba(30, 41, 59, 0.72);
    --border: rgba(148, 163, 184, 0.18);
    --text: #f1f5f9;
    --muted: #94a3b8;
    --divider: rgba(148, 163, 184, 0.12);
    --chip-bg: rgba(56, 189, 248, 0.13);
    --chip-fg: #38bdf8;
    --chip-hover: rgba(56, 189, 248, 0.24);
    --btn-bg: rgba(148, 163, 184, 0.12);
    --btn-hover: rgba(148, 163, 184, 0.22);
    --btn-active-bg: #f8fafc;
    --btn-active-fg: #0f172a;
    --mark-bg: rgba(250, 204, 21, 0.32);
    --mark-fg: #fef08a;
    --ok: #10b981;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: linear-gradient(145deg, rgba(255, 255, 255, 0.98), rgba(241, 245, 249, 0.96));
      --surface: rgba(226, 232, 240, 0.55);
      --border: rgba(15, 23, 42, 0.11);
      --text: #0f172a;
      --muted: #64748b;
      --divider: rgba(15, 23, 42, 0.08);
      --chip-bg: rgba(2, 132, 199, 0.10);
      --chip-fg: #0369a1;
      --chip-hover: rgba(2, 132, 199, 0.18);
      --btn-bg: rgba(15, 23, 42, 0.06);
      --btn-hover: rgba(15, 23, 42, 0.12);
      --btn-active-bg: #0f172a;
      --btn-active-fg: #f8fafc;
      --mark-bg: rgba(250, 204, 21, 0.48);
      --mark-fg: #713f12;
      --ok: #059669;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    background: transparent;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    color: var(--text);
    overflow: hidden;
    -webkit-font-smoothing: antialiased;
  }
  .card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 15px 18px 13px;
    margin: 2px 0 6px;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.20);
  }
  .head {
    display: flex;
    align-items: center;
    gap: 13px;
    padding-bottom: 11px;
    border-bottom: 1px solid var(--divider);
  }
  .thumb-box {
    position: relative;
    width: 84px;
    height: 48px;
    border-radius: 9px;
    overflow: hidden;
    background: #1e293b;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border);
  }
  .thumb-box img {
    position: relative;
    z-index: 2;
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .thumb-fallback { position: absolute; z-index: 1; opacity: 0.75; }
  .meta-col { min-width: 0; flex: 1 1 auto; }
  .yt-title {
    display: block;
    font-size: 15px;
    font-weight: 700;
    color: var(--text);
    text-decoration: none;
    line-height: 1.3;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .yt-title:hover { text-decoration: underline; }
  .yt-sub {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px 10px;
    margin-top: 4px;
    font-size: 11.5px;
    color: var(--muted);
  }
  .badge {
    display: inline-flex;
    align-items: center;
    padding: 1.5px 7px;
    border-radius: 999px;
    background: var(--btn-bg);
    font-weight: 600;
    font-size: 11px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 10px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--divider);
  }
  .search-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1 1 190px;
    min-width: 0;
  }
  #yt-search {
    width: 100%;
    max-width: 260px;
    padding: 5.5px 10px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font: inherit;
    font-size: 12.5px;
    outline: none;
  }
  #yt-search:focus { border-color: var(--chip-fg); }
  .match-count { font-size: 11px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
  .tool-btn {
    border: 1px solid var(--border);
    background: var(--btn-bg);
    color: var(--text);
    font: inherit;
    font-size: 11.5px;
    font-weight: 600;
    padding: 5px 9px;
    border-radius: 8px;
    cursor: pointer;
    transition: background-color 0.14s ease, color 0.14s ease;
    white-space: nowrap;
  }
  .tool-btn:hover { background: var(--btn-hover); }
  .tool-btn[aria-pressed="true"] {
    background: var(--btn-active-bg);
    color: var(--btn-active-fg);
    border-color: transparent;
  }
  .tool-btn.copied { color: var(--ok); border-color: var(--ok); }
  .body-scroll {
    margin-top: 10px;
    max-height: 320px;
    overflow-y: auto;
    padding-right: 6px;
    scrollbar-width: thin;
  }
  .body-scroll.expanded { max-height: none; overflow-y: visible; padding-right: 0; }
  .yt-body { display: flex; flex-direction: column; gap: 11px; }
  .para {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    font-size: 13.5px;
    line-height: 1.62;
  }
  .ts-chip {
    flex-shrink: 0;
    margin-top: 2px;
    padding: 2px 7px;
    border-radius: 6px;
    background: var(--chip-bg);
    color: var(--chip-fg);
    text-decoration: none;
    font-size: 11px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    transition: background-color 0.12s ease;
  }
  .ts-chip:hover { background: var(--chip-hover); }
  .body-scroll.hide-ts .ts-chip { display: none; }
  .para-txt { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
  mark { background: var(--mark-bg); color: var(--mark-fg); border-radius: 3px; padding: 0 2px; }
  .empty { padding: 18px 0; text-align: center; font-size: 13px; color: var(--muted); }
  @media (max-width: 520px) {
    .card { padding: 13px 13px 11px; }
    .thumb-box { width: 68px; height: 40px; }
    .para { gap: 8px; font-size: 13px; }
  }
"""

    js = r"""
  var showTs = true;
  var expanded = false;
  var searchEl = document.getElementById('yt-search');
  var matchEl = document.getElementById('yt-match-count');
  var bodyScroll = document.getElementById('body-scroll');
  var bodyEl = document.getElementById('yt-body');
  var btnTs = document.getElementById('btn-ts');
  var btnCopy = document.getElementById('btn-copy');
  var btnDl = document.getElementById('btn-dl');
  var btnExpand = document.getElementById('btn-expand');

  function escHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function highlightText(txt, q) {
    if (!q) return escHtml(txt);
    var lowTxt = txt.toLowerCase();
    var lowQ = q.toLowerCase();
    var out = '';
    var pos = 0;
    var qLen = q.length;
    while (true) {
      var idx = lowTxt.indexOf(lowQ, pos);
      if (idx === -1) {
        out += escHtml(txt.slice(pos));
        break;
      }
      out += escHtml(txt.slice(pos, idx)) + '<mark>' + escHtml(txt.slice(idx, idx + qLen)) + '</mark>';
      pos = idx + qLen;
    }
    return out;
  }
  function reportHeight() {
    var el = document.getElementById('card');
    var h = el ? Math.ceil(el.getBoundingClientRect().height + 10) : document.documentElement.scrollHeight;
    try { parent.postMessage({ type: 'iframe:height', height: h }, '*'); } catch (e) {}
  }
  function renderParagraphs() {
    var q = (searchEl.value || '').trim();
    var items = DATA.paragraphs || [];
    var rows = [];
    for (var i = 0; i < items.length; i++) {
      var p = items[i];
      var txt = String(p.text || '');
      if (q && txt.toLowerCase().indexOf(q.toLowerCase()) === -1) continue;
      rows.push(
        '<div class="para">' +
        '<a class="ts-chip" href="' + escHtml(DATA.watchUrl + '&t=' + p.ts + 's') + '" target="_blank" rel="noopener noreferrer">' + escHtml(p.ts_label) + '</a>' +
        '<p class="para-txt">' + highlightText(txt, q) + '</p>' +
        '</div>'
      );
    }
    if (!rows.length) {
      bodyEl.innerHTML = '<div class="empty">No matching lines found.</div>';
      matchEl.textContent = '0 matches';
    } else {
      bodyEl.innerHTML = rows.join('');
      matchEl.textContent = q ? (rows.length + ' / ' + items.length + ' sections') : (items.length + ' sections');
    }
    reportHeight();
  }
  function plainTranscript() {
    return (DATA.paragraphs || []).map(function (p) {
      return showTs ? ('[' + p.ts_label + '] ' + p.text) : p.text;
    }).join('\n\n');
  }

  searchEl.addEventListener('input', renderParagraphs);
  btnTs.addEventListener('click', function () {
    showTs = !showTs;
    btnTs.setAttribute('aria-pressed', showTs ? 'true' : 'false');
    bodyScroll.classList.toggle('hide-ts', !showTs);
  });
  btnExpand.addEventListener('click', function () {
    expanded = !expanded;
    bodyScroll.classList.toggle('expanded', expanded);
    btnExpand.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    btnExpand.textContent = expanded ? 'Collapse' : 'Expand';
    reportHeight();
  });
  btnCopy.addEventListener('click', function () {
    var txt = plainTranscript();
    function markCopied() {
      btnCopy.classList.add('copied');
      btnCopy.textContent = 'Copied!';
      setTimeout(function () { btnCopy.classList.remove('copied'); btnCopy.textContent = 'Copy'; }, 1800);
    }
    function legacyCopy() {
      var ta = document.createElement('textarea');
      ta.value = txt;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); markCopied(); } catch (e) {}
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(markCopied, legacyCopy);
    } else {
      legacyCopy();
    }
  });
  btnDl.addEventListener('click', function () {
    var content = DATA.title + '\n' + DATA.author + ' · ' + DATA.watchUrl + '\n' +
      'Duration: ' + DATA.duration + ' | Language: ' + DATA.language + '\n\n' + plainTranscript() + '\n';
    var blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = DATA.filename || 'transcript.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
  });

  window.addEventListener('DOMContentLoaded', reportHeight);
  window.addEventListener('load', reportHeight);
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(reportHeight).observe(document.body);
  }
  renderParagraphs();
  setTimeout(reportHeight, 60);
  setTimeout(reportHeight, 250);
"""

    doc = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{html.escape(title_raw)}</title>\n"
        "<style>__CSS__</style>\n"
        "</head>\n<body>\n"
        '<div class="card" id="card">\n'
        '  <div class="head">\n'
        f'    <a class="thumb-box" href="{watch_url}" target="_blank" rel="noopener noreferrer" aria-label="Open on YouTube">\n'
        '      <svg class="thumb-fallback" width="26" height="26" viewBox="0 0 24 24" fill="none"><polygon points="8,5 19,12 8,19" fill="#94a3b8"/></svg>\n'
        f'      <img src="{thumb_url}" alt="" loading="lazy" onerror="this.remove()">\n'
        "    </a>\n"
        '    <div class="meta-col">\n'
        f'      <a class="yt-title" href="{watch_url}" target="_blank" rel="noopener noreferrer" title="{html.escape(title_raw)}">{html.escape(title_raw)}</a>\n'
        '      <div class="yt-sub">\n'
        f"        <span>{html.escape(author_raw)}</span>\n"
        f'        <span class="badge">⏱ {html.escape(dur_str)}</span>\n'
        f'        <span class="badge">{html.escape(lang.upper())}</span>\n'
        f'        <span class="badge">{wc:,} words · ~{read_min} min read</span>\n'
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        '  <div class="toolbar">\n'
        '    <div class="search-wrap">\n'
        '      <input id="yt-search" type="search" placeholder="Search transcript…" autocomplete="off" aria-label="Search transcript">\n'
        '      <span id="yt-match-count" class="match-count"></span>\n'
        "    </div>\n"
        '    <div class="actions">\n'
        '      <button type="button" class="tool-btn" id="btn-ts" aria-pressed="true">Timestamps</button>\n'
        '      <button type="button" class="tool-btn" id="btn-copy">Copy</button>\n'
        '      <button type="button" class="tool-btn" id="btn-dl">Download .txt</button>\n'
        '      <button type="button" class="tool-btn" id="btn-expand" aria-expanded="false">Expand</button>\n'
        "    </div>\n"
        "  </div>\n"
        '  <div class="body-scroll" id="body-scroll">\n'
        '    <div class="yt-body" id="yt-body"></div>\n'
        "  </div>\n"
        "</div>\n"
        "<script>\n"
        "  var DATA = __DATA__;\n"
        "__JS__\n"
        "</script>\n</body>\n</html>"
    )
    return (
        doc.replace("__CSS__", css)
        .replace("__DATA__", _safe_json_for_script(payload))
        .replace("__JS__", js)
    )


class Tools:
    class Valves(BaseModel):
        emit_status: bool = Field(
            default=True,
            description="Show live YouTube transcript status pills in Open WebUI.",
        )
        emit_transcript_card: bool = Field(
            default=True,
            description="Display an interactive searchable, copyable transcript card in Open WebUI.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _emit(self, emitter, description: str, done: bool = False) -> None:
        if emitter and self.valves.emit_status:
            try:
                await emitter({"type": "status", "data": {"description": description, "done": done}})
            except Exception:
                pass

    async def _emit_embed(self, emitter, html_card: str) -> bool:
        if not (emitter and html_card):
            return False
        try:
            await emitter({"type": "embeds", "data": {"embeds": [html_card], "replace": False}})
            return True
        except Exception:
            return False

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
        :return: Video metadata and transcript context (interactive card emitted automatically when supported).
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

        embedded = False
        if __event_emitter__ and self.valves.emit_transcript_card:
            card_html = render_transcript_html_card(data)
            embedded = await self._emit_embed(__event_emitter__, card_html)

        lines = []
        for ts, words in data["chunks"]:
            para = " ".join(words)
            lines.append(f"[{_format_ts(ts)}] {para}" if include_timestamps else para)
        body = "\n".join(lines)
        truncated = False
        if len(body) > _MAX_LLM_TRANSCRIPT_CHARS:
            body = body[:_MAX_LLM_TRANSCRIPT_CHARS].rsplit("\n", 1)[0] + "\n\n[Transcript context truncated for LLM at 32,000 chars — interactive card holds full text]"
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
            f"DURATION: {dur_str} | LANGUAGE: {lang_note} | WORDS: {data.get('word_count', 0)}" + (" | TRUNCATED: yes" if truncated else ""),
        ]
        if embedded:
            header.append(
                "TRANSCRIPT_CARD_DISPLAYED: yes (full structured, searchable, copyable, downloadable transcript "
                "is already visible in chat — DO NOT repeat or echo it verbatim)"
            )
        header.extend(["TRANSCRIPT:", body])
        return "\n".join(header)
