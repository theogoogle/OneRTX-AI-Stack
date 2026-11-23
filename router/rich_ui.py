"""Rich UI helpers for Open WebUI 0.13+: native SSE events (status, source/favicons, embeds, files) & HTML cards.

Standard library only. Safe under `pythonw -I` (no external packages, all dynamic text HTML-escaped).
"""
import base64
import difflib
import functools
import html
import re
from pathlib import Path

_INTER_WOFF2_PATH = Path.home() / "AI" / "family-table" / "assets" / "fonts" / "inter-ui-latin.woff2"


@functools.lru_cache(maxsize=1)
def _inter_font_face_css() -> str:
    try:
        if _INTER_WOFF2_PATH.is_file():
            b64 = base64.b64encode(_INTER_WOFF2_PATH.read_bytes()).decode("ascii")
            return (
                '@font-face{font-family:"Inter";'
                f'src:url("data:font/woff2;base64,{b64}") format("woff2");'
                'font-weight:400 700;font-style:normal;font-display:swap;}\n'
            )
    except Exception:
        pass
    return ""

MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*((?:https?|photo)://[^\s)]+)", re.I)
PHOTO_PLACEHOLDER_RE = re.compile(r"photo://(\d+)", re.I)
MD_IMG_TAG_RE = re.compile(r"(!\[[^\]]*\]\(\s*)([^\s)]+)(\s*\))")
BROKEN_PHOTO_TAG_RE = re.compile(r"!\[[^\]]*\]\(\s*photo://[^\s)]*\s*\)", re.I)
ADJACENT_MD_IMAGES_RE = re.compile(r"(!\[[^\]]*\]\([^\s)]+\))[ \t]*\r?\n(?:[ \t]*\r?\n)*[ \t]*(?=!\[)")


def resolve_photo_refs(text: str, images: list[dict] | None) -> str:
    """Replace `photo://1`..`photo://N` placeholders, snap near-miss LLM hash typos (`>= 0.88` similarity)
    onto exact live-verified image URLs, strip any unresolvable `photo://` tags so Open WebUI's fallback logo
    never appears, and join consecutive standalone images onto the same paragraph line so they sit side-by-side."""
    if not text:
        return text
    urls = [str((im or {}).get("url") or "").strip() for im in (images or []) if str((im or {}).get("url") or "").strip().startswith("https://")]

    def _sub_placeholder(m):
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(urls):
            return urls[idx]
        return m.group(0)

    out = PHOTO_PLACEHOLDER_RE.sub(_sub_placeholder, text)
    # Strip any out-of-range/unresolvable `![...](photo://...)` tag so Open WebUI never shows its broken-image logo
    out = BROKEN_PHOTO_TAG_RE.sub("", out)

    if urls and "![" in out:
        def _snap_md_url(m):
            prefix, raw_u, suffix = m.group(1), m.group(2), m.group(3)
            if raw_u in urls or not raw_u.startswith("https://"):
                return m.group(0)
            best_u, best_score = None, 0.0
            for vu in urls:
                score = difflib.SequenceMatcher(None, raw_u, vu).ratio()
                if score > best_score:
                    best_u, best_score = vu, score
            if best_u and best_score >= 0.88:
                return f"{prefix}{best_u}{suffix}"
            return m.group(0)

        out = MD_IMG_TAG_RE.sub(_snap_md_url, out)

    # Collapse consecutive markdown images (`![a](u1)\n\n![b](u2)`) onto one line (`![a](u1) ![b](u2)`) for side-by-side layout
    if "![" in out:
        out = ADJACENT_MD_IMAGES_RE.sub(r"\1 ", out)
    return out


class PhotoStreamRewriter:
    """In-flight streaming rewriter for `delta.content` chunks so `photo://N` and adjacent image newlines are
    resolved BEFORE reaching Open WebUI's browser stream (zero broken-image logos, zero post-stream overwrite)."""

    _COMPLETE_IMG_AT_END_RE = re.compile(r"!\[[^\]]*\]\([^\s)]+\)([ \t]*\r?\n[ \t\r\n]*)$")

    def __init__(self, images: list[dict] | None):
        self.images = list(images or [])
        self._buf = ""

    def feed(self, piece: str) -> str:
        if not piece:
            return ""
        self._buf += piece

        # 1. Hold if the buffer ends with `"!"` (could be the start of `"!["`)
        if self._buf.endswith("!"):
            emit_part, self._buf = self._buf[:-1], "!"
            return resolve_photo_refs(emit_part, self.images) if emit_part else ""

        # 2. Hold from the last unclosed `"!["` while the markdown image tag `< 360` chars is still being typed
        last_open = self._buf.rfind("![")
        if last_open != -1:
            tail = self._buf[last_open:]
            if not re.match(r"^!\[[^\]]*\]\([^\s)]+\)", tail) and "\n\n" not in tail and len(tail) < 360:
                emit_part, self._buf = self._buf[:last_open], tail
                return resolve_photo_refs(emit_part, self.images) if emit_part else ""

        # 3. Resolve complete `photo://N` refs & collapse any adjacent `![a](u1)\n\n![b](u2)` already in buffer
        resolved = resolve_photo_refs(self._buf, self.images)

        # 4. If `resolved` ends right after a complete `![caption](url)` + trailing newlines (up to 6 chars),
        #    hold that image+newlines briefly to see if the next token starts a second `![caption2](...)` side-by-side
        m_end = self._COMPLETE_IMG_AT_END_RE.search(resolved)
        if m_end and len(m_end.group(1)) <= 6:
            cut = m_end.start()
            emit_part, self._buf = resolved[:cut], resolved[cut:]
            return emit_part

        self._buf = ""
        return resolved

    def flush(self) -> str:
        if not self._buf:
            return ""
        out = resolve_photo_refs(self._buf, self.images)
        self._buf = ""
        return out


def replace_event(content: str) -> dict:
    """Build an Open WebUI `replace` SSE wrapper to persist resolved URLs cleanly into chat & DB."""
    return {"event": {"type": "replace", "data": {"content": str(content or "")}}}


def extract_markdown_image_urls(text: str, images: list[dict] | None = None) -> list[str]:
    """Return all image URLs embedded via `![caption](url)` in `text` (after resolving `photo://N`)."""
    resolved = resolve_photo_refs(text or "", images) if images else (text or "")
    return [m.group(1).strip() for m in MD_IMAGE_RE.finditer(resolved) if m.group(1).strip().startswith(("http://", "https://"))]


def has_markdown_image(text: str, images: list[dict] | None = None) -> bool:
    """True when markdown `text` already embeds at least one `![caption](https://...)` image."""
    return bool(extract_markdown_image_urls(text, images=images))


def status_event(
    description: str,
    *,
    done: bool = False,
    action: str | None = None,
    query: str | None = None,
    items: list[dict] | None = None,
    urls: list[str] | None = None,
    queries: list[str] | None = None,
    count: int | None = None,
    hidden: bool = False,
) -> dict:
    """Build an Open WebUI `status` SSE wrapper (`data: {"event": ...}`)."""
    data: dict = {"description": str(description or ""), "done": bool(done)}
    if action:
        data["action"] = action
    if query:
        data["query"] = str(query)
    if items is not None:
        data["items"] = items
    if urls is not None:
        data["urls"] = urls
    if queries is not None:
        data["queries"] = queries
    if count is not None:
        data["count"] = int(count)
    if hidden:
        data["hidden"] = True
    return {"event": {"type": "status", "data": data}}


def embeds_event(html_snippets: list[str], *, replace: bool = True) -> dict:
    """Build an Open WebUI `embeds` SSE wrapper (`FullHeightIframe` sandboxed srcdoc cards)."""
    return {
        "event": {
            "type": "embeds",
            "data": {"embeds": [str(h) for h in (html_snippets or []) if h], "replace": bool(replace)},
        }
    }


def files_event(images: list[dict]) -> dict:
    """Build an Open WebUI `files` SSE wrapper for local Open WebUI `/api/v1/files/...` or `data:` images only.
    Never pass external `http(s)://` URLs here: Open WebUI renders `message.files` with `allowExternal=false`,
    which replaces external URLs with `/favicon.png` (use inline Markdown `![caption](https://...)` instead)."""
    files = []
    for img in images or []:
        url = str((img or {}).get("url") or "").strip()
        if not (url.startswith("/") or url.startswith("data:")):
            continue
        item = {"type": "image", "url": url}
        desc = str((img or {}).get("description") or "").strip()
        if desc:
            item["name"] = desc[:140]
        files.append(item)
    return {"event": {"type": "files", "data": {"files": files}}}


def source_events_for_search(results: list[dict]) -> list[dict]:
    """Build 1-indexed Open WebUI `source` events so `[1]`, `[2]` become clickable pills + stacked site favicons."""
    events = []
    for r in results or []:
        url = str((r or {}).get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        title = str((r or {}).get("title") or url).strip() or url
        pub = str((r or {}).get("published") or "").strip()[:10]
        snippet = str((r or {}).get("content") or "").strip()[:1600]
        header = f"**{title}**" + (f" · *{pub}*" if pub else "")
        doc_md = f"{header}\n\n{snippet}" if snippet else header
        # Note: omitting `external: True` ensures Open WebUI's Citations bar keeps `source.name` starting with `http`
        # so its stacked Google-favicon pill (`32px` domain icons + `N Sources` drawer) renders automatically.
        events.append({
            "event": {
                "type": "source",
                "data": {
                    "source": {"name": url, "url": url},
                    "document": [doc_md],
                    "metadata": [{"source": url, "name": title}],
                },
            }
        })
    return events


def source_events_for_weather(url: str, place: str, summary_text: str) -> list[dict]:
    """Build a single `[1]` Open-Meteo `source` event for weather replies."""
    if not url or not url.startswith(("http://", "https://")):
        return []
    title = f"Open-Meteo 7-Day Forecast — {place}"
    return [{
        "event": {
            "type": "source",
            "data": {
                "source": {"name": url, "url": url},
                "document": [f"**{title}**\n\n{summary_text}"],
                "metadata": [{"source": url, "name": title}],
            },
        }
    }]


def _weather_svg(code: int, size: int = 24) -> str:
    """Inline multi-tone SVG icon for WMO weather codes (zero external network fetches)."""
    c = int(code if code is not None else -1)
    s = int(size)
    if c == 0:  # clear sky
        return (
            f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
            '<circle cx="12" cy="12" r="5" fill="#fbbf24"/>'
            '<g stroke="#fbbf24" stroke-width="1.8" stroke-linecap="round">'
            '<path d="M12 2.5v2.2M12 19.3v2.2M4.9 4.9l1.6 1.6M17.5 17.5l1.6 1.6M2.5 12h2.2M19.3 12h2.2'
            'M4.9 19.1l1.6-1.6M17.5 6.5l1.6-1.6"/></g></svg>'
        )
    if c in (1, 2):  # partly cloudy
        return (
            f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
            '<circle cx="9" cy="9" r="3.8" fill="#fbbf24"/>'
            '<path d="M9 2.8v1.5M4.6 4.6l1.1 1.1M2.8 9h1.5M13.4 4.6l-1.1 1.1" stroke="#fbbf24" stroke-width="1.6" stroke-linecap="round"/>'
            '<path d="M8.5 18.5h8.2a3.8 3.8 0 0 0 .4-7.6 5.2 5.2 0 0 0-9.9-1.1A3.6 3.6 0 0 0 8.5 18.5Z" '
            'fill="#cbd5e1" fill-opacity="0.92"/></svg>'
        )
    if c in (3, 45, 48):  # overcast / fog
        fog = (
            '<path d="M5 20.5h14M7 22.5h10" stroke="#94a3b8" stroke-width="1.5" stroke-linecap="round"/>'
            if c in (45, 48)
            else ""
        )
        return (
            f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
            '<path d="M6.8 17.8h10.4a4 4 0 0 0 .5-8 5.6 5.6 0 0 0-10.8-1.2A3.9 3.9 0 0 0 6.8 17.8Z" '
            f'fill="#94a3b8" fill-opacity="0.92"/>{fog}</svg>'
        )
    if c in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):  # drizzle / rain / showers
        heavy = c in (55, 63, 65, 67, 81, 82)
        drops = (
            '<path d="M8.5 18.2l-1 3M12.5 18.2l-1 3M16.5 18.2l-1 3" stroke="#38bdf8" stroke-width="1.8" stroke-linecap="round"/>'
            if heavy
            else '<path d="M9.8 18.2l-.9 2.6M14.6 18.2l-.9 2.6" stroke="#38bdf8" stroke-width="1.7" stroke-linecap="round"/>'
        )
        return (
            f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
            '<path d="M6.8 16.2h10.4a3.8 3.8 0 0 0 .5-7.6 5.4 5.4 0 0 0-10.5-1.1A3.7 3.7 0 0 0 6.8 16.2Z" '
            f'fill="#94a3b8" fill-opacity="0.9"/>{drops}</svg>'
        )
    if c in (71, 73, 75, 77, 85, 86):  # snow
        return (
            f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
            '<path d="M6.8 16h10.4a3.8 3.8 0 0 0 .5-7.6 5.4 5.4 0 0 0-10.5-1.1A3.7 3.7 0 0 0 6.8 16Z" '
            'fill="#cbd5e1" fill-opacity="0.9"/>'
            '<circle cx="9" cy="19.2" r="1.1" fill="#e0f2fe"/><circle cx="13" cy="20.2" r="1.1" fill="#e0f2fe"/>'
            '<circle cx="16.5" cy="18.9" r="1.1" fill="#e0f2fe"/></svg>'
        )
    if c in (95, 96, 99):  # thunderstorm
        return (
            f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
            '<path d="M6.8 15.8h10.4a3.8 3.8 0 0 0 .5-7.6 5.4 5.4 0 0 0-10.5-1.1A3.7 3.7 0 0 0 6.8 15.8Z" '
            'fill="#64748b" fill-opacity="0.95"/>'
            '<path d="M13 14.5l-2.4 3.7h2.3l-1.1 3.5 3.6-4.4h-2.3l1.2-2.8Z" fill="#facc15"/></svg>'
        )
    return (
        f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none">'
        '<circle cx="12" cy="12" r="6" stroke="#94a3b8" stroke-width="1.8"/></svg>'
    )


def _temp_bar_gradient(t_min: float, t_max: float) -> str:
    def _color(t: float) -> str:
        if t <= 0:
            return "#38bdf8"
        if t <= 8:
            return "#60a5fa"
        if t <= 15:
            return "#34d399"
        if t <= 22:
            return "#fbbf24"
        if t <= 28:
            return "#fb923c"
        return "#f87171"

    return f"linear-gradient(90deg, {_color(t_min)}, {_color(t_max)})"


def build_weather_card_html(meta: dict) -> str:
    """Render a self-contained, auto-resizing 7-day forecast widget for Open WebUI's `embeds` container."""
    place = html.escape(str(meta.get("place") or "Forecast"))
    tz = html.escape(str(meta.get("timezone") or ""))
    retrieved = html.escape(str(meta.get("retrieved_at") or ""))
    cur = meta.get("current") or {}
    daily = meta.get("daily") or []

    c_temp = cur.get("temp")
    c_temp_txt = f"{round(float(c_temp))}°" if isinstance(c_temp, (int, float)) else "—°"
    c_feels = cur.get("feels_like")
    c_feels_txt = f"{float(c_feels):.1f}°C" if isinstance(c_feels, (int, float)) else "—"
    c_desc = html.escape(str(cur.get("desc") or "").capitalize())
    c_wind = cur.get("wind_kmh")
    c_wind_txt = f"{round(float(c_wind))} km/h" if isinstance(c_wind, (int, float)) else "—"
    c_icon = _weather_svg(cur.get("code", 1), size=44)

    mins = [float(d["t_min"]) for d in daily if isinstance(d.get("t_min"), (int, float))]
    maxs = [float(d["t_max"]) for d in daily if isinstance(d.get("t_max"), (int, float))]
    week_min = min(mins) if mins else 0.0
    week_max = max(maxs) if maxs else 20.0
    span = max(week_max - week_min, 1.0)

    today_sun = ""
    if daily:
        sr = html.escape(str(daily[0].get("sunrise") or ""))
        ss = html.escape(str(daily[0].get("sunset") or ""))
        if sr and ss:
            today_sun = f"<span>🌅 {sr}</span><span>🌇 {ss}</span>"

    rows_html = []
    for idx, d in enumerate(daily[:7]):
        label = "Today" if idx == 0 else html.escape(str(d.get("date_label") or ""))
        desc = html.escape(str(d.get("desc") or "").capitalize())
        icon = _weather_svg(d.get("code", 2), size=20)
        tmin = float(d.get("t_min") or 0.0)
        tmax = float(d.get("t_max") or 0.0)
        left_pct = max(0.0, min(92.0, ((tmin - week_min) / span) * 100.0))
        width_pct = max(8.0, min(100.0 - left_pct, ((tmax - tmin) / span) * 100.0))
        grad = _temp_bar_gradient(tmin, tmax)
        rain_p = int(round(float(d.get("rain_prob") or 0)))
        rain_badge = (
            f'<span class="rain">💧 {rain_p}%</span>'
            if rain_p >= 15
            else '<span class="rain muted"></span>'
        )
        rows_html.append(
            f'<div class="row" title="{desc}">'
            f'<div class="day">{label}</div>'
            f'<div class="cond">{icon}{rain_badge}</div>'
            f'<div class="t-min">{round(tmin)}°</div>'
            f'<div class="bar-track"><div class="bar-fill" style="left:{left_pct:.1f}%;width:{width_pct:.1f}%;background:{grad}"></div></div>'
            f'<div class="t-max">{round(tmax)}°</div>'
            f"</div>"
        )

    sub_bits = [x for x in (f"Updated {retrieved}" if retrieved else "", tz) if x]
    sub_line = " · ".join(sub_bits)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
{_inter_font_face_css()}  :root {{
    color-scheme: dark light;
    --bg: rgba(29, 29, 29, 0.90);
    --border: rgba(255, 255, 255, 0.09);
    --text: #ececec;
    --muted: #9b9b9b;
    --track: rgba(255, 255, 255, 0.09);
    --divider: rgba(255, 255, 255, 0.07);
    --rain: #38bdf8;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --bg: #fafafa;
      --border: rgba(0, 0, 0, 0.09);
      --text: #171717;
      --muted: #666666;
      --track: rgba(0, 0, 0, 0.09);
      --divider: rgba(0, 0, 0, 0.07);
      --rain: #0284c7;
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    background: transparent;
    font-family: "Inter", ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--text);
    overflow: hidden;
    -webkit-font-smoothing: antialiased;
  }}
  .card {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 15px 18px 13px;
    margin: 2px 0 6px;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.16);
  }}
  .hero {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--divider);
  }}
  .place {{ font-size: 15px; font-weight: 650; letter-spacing: -0.01em; }}
  .sub {{ font-size: 11.5px; color: var(--muted); margin-top: 2px; }}
  .meta-pills {{
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 7px;
    font-size: 12px;
    color: var(--muted);
  }}
  .now-right {{
    display: flex;
    align-items: center;
    gap: 10px;
    text-align: right;
    flex-shrink: 0;
  }}
  .now-temp {{ font-size: 34px; font-weight: 700; letter-spacing: -0.03em; line-height: 1; }}
  .now-desc {{ font-size: 12px; color: var(--muted); margin-top: 3px; }}
  .rows {{ margin-top: 8px; display: flex; flex-direction: column; }}
  .row {{
    display: grid;
    grid-template-columns: 76px 86px 32px 1fr 32px;
    align-items: center;
    gap: 8px;
    padding: 5.5px 0;
    border-bottom: 1px solid var(--divider);
    font-size: 13px;
  }}
  .row:last-child {{ border-bottom: none; }}
  .day {{ font-weight: 550; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .cond {{ display: flex; align-items: center; gap: 6px; }}
  .rain {{ font-size: 11px; font-weight: 600; color: var(--rain); white-space: nowrap; }}
  .rain.muted {{ visibility: hidden; }}
  .t-min {{ color: var(--muted); text-align: right; font-variant-numeric: tabular-nums; font-size: 12.5px; }}
  .t-max {{ font-weight: 600; text-align: left; font-variant-numeric: tabular-nums; font-size: 12.5px; }}
  .bar-track {{
    position: relative;
    height: 6px;
    border-radius: 999px;
    background: var(--track);
    overflow: hidden;
  }}
  .bar-fill {{
    position: absolute;
    top: 0;
    bottom: 0;
    border-radius: 999px;
  }}
  @media (max-width: 460px) {{
    .card {{ padding: 13px 13px 11px; }}
    .row {{ grid-template-columns: 64px 78px 28px 1fr 28px; gap: 6px; font-size: 12.5px; }}
    .now-temp {{ font-size: 29px; }}
  }}
</style>
</head>
<body>
  <div class="card" id="card">
    <div class="hero">
      <div>
        <div class="place">📍 {place}</div>
        <div class="sub">{sub_line}</div>
        <div class="meta-pills">
          <span>🌡️ Feels {c_feels_txt}</span>
          <span>💨 Wind {c_wind_txt}</span>
          {today_sun}
        </div>
      </div>
      <div class="now-right">
        {c_icon}
        <div>
          <div class="now-temp">{c_temp_txt}</div>
          <div class="now-desc">{c_desc}</div>
        </div>
      </div>
    </div>
    <div class="rows">
      {"".join(rows_html)}
    </div>
  </div>
<script>
  function reportHeight() {{
    var el = document.getElementById('card');
    var h = el ? Math.ceil(el.getBoundingClientRect().height + 10) : document.documentElement.scrollHeight;
    parent.postMessage({{ type: 'iframe:height', height: h }}, '*');
  }}
  window.addEventListener('DOMContentLoaded', reportHeight);
  window.addEventListener('load', reportHeight);
  if (typeof ResizeObserver !== 'undefined') {{
    new ResizeObserver(reportHeight).observe(document.body);
  }}
  setTimeout(reportHeight, 60);
  setTimeout(reportHeight, 250);
</script>
</body>
</html>"""
