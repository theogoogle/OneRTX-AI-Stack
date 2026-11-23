"""Offline deterministic unit tests for Open WebUI Rich UI upgrades (`rich_ui.py`, `web_search.py`, `router.py`).

Run: `python -I rich_ui_test.py`
"""
import io
import json
import sys
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rich_ui     # noqa: E402
import router      # noqa: E402
import web_search  # noqa: E402


def test_weather_card_html_and_sources():
    meta = {
        "place": "London, England, United Kingdom <script>alert(1)</script>",
        "timezone": "Europe/London",
        "retrieved_at": "09:45",
        "url": "https://open-meteo.com/en/docs#latitude=51.5&longitude=-0.12",
        "current": {"temp": 8.4, "feels_like": 5.8, "code": 2, "desc": "partly cloudy", "wind_kmh": 14.0},
        "daily": [
            {"date_label": "Thu 19 Nov", "day_short": "Thu", "code": 2, "desc": "partly cloudy",
             "t_min": 4.0, "t_max": 10.5, "rain_prob": 20, "precip_mm": 0.2, "wind_max": 18.0,
             "sunrise": "07:25", "sunset": "16:07"},
            {"date_label": "Fri 20 Nov", "day_short": "Fri", "code": 63, "desc": "rain",
             "t_min": 6.0, "t_max": 12.0, "rain_prob": 80, "precip_mm": 6.4, "wind_max": 28.0,
             "sunrise": "07:27", "sunset": "16:05"},
        ],
    }
    html_str = rich_ui.build_weather_card_html(meta)
    assert "<script>alert(1)</script>" not in html_str, "XSS must be escaped"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_str
    assert "iframe:height" in html_str
    assert "💧 80%" in html_str
    assert "Today" in html_str and "Fri 20 Nov" in html_str

    srcs = rich_ui.source_events_for_weather(meta["url"], "London", "Now: 8.4°C")
    assert len(srcs) == 1
    assert srcs[0]["event"]["type"] == "source"
    assert srcs[0]["event"]["data"]["source"]["name"].startswith("https://open-meteo.com")


def test_search_source_events_and_markdown_image_detection():
    results = [
        {"title": "UK News Headline", "url": "https://www.bbc.co.uk/news/articles/c123",
         "content": "Summary paragraph.", "published": "2026-11-19T08:00:00Z"},
        {"title": "World Market Update", "url": "https://www.reuters.com/world/uk-2026-11-19/",
         "content": "Second snippet.", "published": None},
    ]
    evs = rich_ui.source_events_for_search(results)
    assert len(evs) == 2
    d0 = evs[0]["event"]["data"]
    # Open WebUI's stacked Google-favicon pill checks `source.name.startsWith("http")`
    assert d0["source"]["name"] == "https://www.bbc.co.uk/news/articles/c123"
    assert d0["metadata"][0]["name"] == "UK News Headline"
    assert "2026-11-19" in d0["document"][0]

    assert rich_ui.has_markdown_image("Here is a story:\n\n![Parliament](https://ichef.bbci.co.uk/photo.jpg)\n")
    assert not rich_ui.has_markdown_image("Plain text with a link [BBC](https://www.bbc.co.uk) only.")


def test_ssrf_and_junk_image_filtering():
    # Private, loopback, non-HTTPS, userinfo, non-443 port, IPv4-mapped IPv6, and tailnet hosts must be rejected without network probes
    assert not web_search._is_public_https_url("http://example.com/photo.jpg")
    assert not web_search._is_public_https_url("https://127.0.0.1:8282/secret.png")
    assert not web_search._is_public_https_url("https://localhost/a.jpg")
    assert not web_search._is_public_https_url("https://<lan-ip>/cam.jpg")
    assert not web_search._is_public_https_url("https://<tailscale-ip>/img.jpg")
    assert not web_search._is_public_https_url("https://<tailnet-host>/img.jpg")
    assert not web_search._is_public_https_url("https://user:pass@example.com/a.jpg")
    assert not web_search._is_public_https_url("https://example.com:8443/a.jpg")
    assert not web_search._is_public_https_url("https://[::ffff:127.0.0.1]/a.jpg")
    assert not web_search._is_public_https_url("https://169.254.169.254/latest/meta-data/")

    raw_candidates = [
        {"url": "https://cdn.example.com/assets/logo-white.svg", "description": "Site logo"},
        {"url": "https://cdn.example.com/favicon.ico", "description": "Favicon"},
        {"url": "https://cdn.example.com/pixel/1x1.gif", "description": "Tracker"},
        {"url": "https://docs.example.netlify.app/api/og?title=Changelog&type=generic", "description": "Generic docs OG card"},
        {"url": "https://news.example.com/2026/11/summit press photo (1).jpg", "description": "Leaders at the summit"},
        {"url": "https://news.example.com/2026/11/london-skyline.webp", "description": "London skyline at dawn"},
    ]
    with mock.patch.object(web_search, "_probe_single_image", side_effect=lambda item, timeout=2.5: item):
        verified = web_search.verify_images(raw_candidates, max_keep=3)
    assert [v["url"] for v in verified] == [
        "https://news.example.com/2026/11/summit%20press%20photo%20%281%29.jpg",
        "https://news.example.com/2026/11/london-skyline.webp",
    ]


def test_context_block_includes_verified_photos_and_clean_citations():
    results = [{"title": "Story A", "url": "https://bbc.co.uk/a", "content": "Details A", "published": "2026-11-19"}]
    imgs = [
        {"url": "https://ichef.bbci.co.uk/img1.jpg", "description": "Prime Minister outside Downing Street"},
        {"url": "https://ichef.bbci.co.uk/img2.jpg", "description": " Westminster Bridge in morning mist"},
    ]
    blk = web_search.context_block("search", results, "latest news past 24 hours", images=imgs, rich_citations=True)
    assert "VERIFIED PHOTOS FROM THIS SEARCH" in blk
    assert "photo://1" in blk and "photo://2" in blk
    assert "embed 2 or 3 distinct photos" in blk
    assert "do not append a separate 'Sources' link list" in blk

    # Plain non-Open-WebUI callers get direct HTTPS URLs + classic Sources list instruction
    blk_plain = web_search.context_block("search", results, "latest news", images=imgs, rich_citations=False)
    assert "https://ichef.bbci.co.uk/img1.jpg" in blk_plain
    assert "end with a short 'Sources' list" in blk_plain


def test_visual_query_classifier():
    assert web_search.is_visual_query("Can i get all the latest news in the past 24 hours please?")
    assert web_search.is_visual_query("i would like the last 24h about SpaceX Starship please")
    assert web_search.is_visual_query("Formula 1 Grand Prix results")
    assert web_search.is_visual_query("Quelles sont les actualités aujourd'hui ?")
    assert web_search.is_visual_query("exhibitions at Tate Modern this weekend")
    assert not web_search.is_visual_query("what is the GBP to EUR exchange rate right now?")
    assert not web_search.is_visual_query("what time does the Science Museum close on Sunday?")
    assert not web_search.is_visual_query("is the Northern line running today?")


def test_declarative_recency_backstop_and_zero_hardcoding():
    # Ensure no company or benchmark-specific hardcoding exists in web_search.py or rich_ui.py
    ws_src = (SCRIPTS_DIR / "web_search.py").read_text(encoding="utf-8").lower()
    ru_src = (SCRIPTS_DIR / "rich_ui.py").read_text(encoding="utf-8").lower()
    for bad in ("anthropic", "mistral", r"\bipo\b", "|ipo|"):
        assert bad not in ws_src and bad not in ru_src, f"Hardcoded term {bad!r} found"

    # Polite declarative prompts with temporal cues ("last 24h", "latest updates") must trigger web search
    fake_lfm_no = lambda _body: {"web": False, "query": "", "weather": False, "place": ""}
    d1 = web_search.decide(fake_lfm_no, "i would like the last 24h about SpaceX please")
    assert d1["web"] is True and d1.get("backstop") is True, d1

    d2 = web_search.decide(fake_lfm_no, "please give me the latest updates on Ferrari")
    assert d2["web"] is True and d2.get("backstop") is True, d2

    # Timeless non-web prompts must still stay False
    d3 = web_search.decide(fake_lfm_no, "help me reword this sentence to sound more professional: we cant do it by friday")
    assert d3["web"] is False, d3


def test_photo_placeholder_and_hash_typo_snapping():
    imgs = [
        {"url": "https://images.ctfassets.net/n4ncz0i02v4l/5Kcaap3DR6wfem6LMx66A1/westminster-hall.jpg", "description": "Westminster"},
        {"url": "https://ichef.bbci.co.uk/news/1024/cpsprodpb/hero2.webp", "description": "Second photo"},
    ]
    raw = (
        "### Section 1\n![Westminster](photo://1)\n\n"
        "### Section 2\n![Typo](https://images.ctfassets.net/n4ncz012v4l/5Kcaap3DR6wfem6LMx66A1/westminster-hall.jpg)\n"
    )
    resolved = rich_ui.resolve_photo_refs(raw, imgs)
    assert "photo://1" not in resolved
    assert resolved.count("https://images.ctfassets.net/n4ncz0i02v4l/5Kcaap3DR6wfem6LMx66A1/westminster-hall.jpg") == 2

    # In-flight token-by-token stream rewriter: zero `photo://` ever emitted, out-of-range stripped, adjacent images joined
    rw = rich_ui.PhotoStreamRewriter(imgs)
    emitted_chunks = []
    for tok in ["Intro!\n", "![First", " cap](pho", "to://2)\n", "\n![Second](photo://1)\n", "![Bad](photo://99)\nDone!"]:
        piece = rw.feed(tok)
        assert "photo://" not in piece, f"Leaked unresolved photo:// in chunk: {piece!r}"
        emitted_chunks.append(piece)
    tail = rw.flush()
    assert "photo://" not in tail
    emitted_chunks.append(tail)
    joined = "".join(emitted_chunks)
    assert "![First cap](https://ichef.bbci.co.uk/news/1024/cpsprodpb/hero2.webp) ![Second](https://images.ctfassets.net/n4ncz0i02v4l/5Kcaap3DR6wfem6LMx66A1/westminster-hall.jpg)" in joined, joined
    assert "Bad" not in joined

    # YouTube video result -> direct 16:9 hqdefault.jpg thumbnail without HTML scrape
    yt_img = web_search._fetch_article_og_image(
        {"title": "Keynote Highlights", "url": "https://www.youtube.com/watch?v=bLUewwVUMmg"}, 2
    )
    assert yt_img and yt_img["url"] == "https://i.ytimg.com/vi/bLUewwVUMmg/hqdefault.jpg"


def test_og_image_extraction_and_fast_tool_stripping():
    fake_html = (
        b'<html><head>'
        b'<meta property="og:image" content="https://cdn.example.com/hero.jpg?w=1200&amp;q=85">'
        b'</head><body></body></html>'
    )

    class FakeResp:
        status = 206
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, _n=32768):
            return fake_html

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    with mock.patch.object(web_search, "_is_public_https_url", return_value=True), \
            mock.patch.object(web_search._NO_REDIRECT_OPENER, "open", return_value=FakeResp()):
        og = web_search.extract_article_og_images(
            [{"title": "European Space Agency Launch Report", "url": "https://www.reuters.com/science/esa-launch/"}],
            max_pages=1,
        )
    assert len(og) == 1
    assert og[0]["url"] == "https://cdn.example.com/hero.jpg?w=1200&q=85", og[0]
    assert "Source [1] — European Space Agency Launch Report" in og[0]["description"]

    # Verify stray Open Terminal + Open WebUI built-in tools are dropped on normal `fast` Q&A turns
    owui_plus_terminal_tools = [
        {"type": "function", "function": {"name": "get_current_timestamp"}},
        {"type": "function", "function": {"name": "calculate_timestamp"}},
        {"type": "function", "function": {"name": "search_notes"}},
        {"type": "function", "function": {"name": "browser_tabs"}},
        {"type": "function", "function": {"name": "browser_action"}},
        {"type": "function", "function": {"name": "run_command"}},
    ]

    class CaptureForward(router.Handler):
        def __init__(self):
            self._sse_started = False
            self.captured_body = None

        def _forward(self, path, body, entry):
            self.captured_body = body
            return entry

    cf = CaptureForward()
    b_normal = {
        "model": "fast",
        "stream": True,
        "tools": list(owui_plus_terminal_tools),
        "messages": [{"role": "user", "content": "explain how photosynthesis works"}],
    }
    entry_n = cf._dispatch("/v1/chat/completions", "openai", b_normal, "fast", "gemma-4-12b-it-qat", False, None, {}, {"tier": "fast"})
    assert not b_normal.get("tools"), f"Expected all tools dropped, got {b_normal.get('tools')}"
    assert entry_n.get("tools_dropped") == 6

    # Personal notes prompt keeps only safe personal tools (and STILL drops terminal `browser_action`/`run_command`)
    b_notes = {
        "model": "fast",
        "stream": True,
        "tools": list(owui_plus_terminal_tools),
        "messages": [{"role": "user", "content": "search in my notes for shopping list"}],
    }
    cf._dispatch("/v1/chat/completions", "openai", b_notes, "fast", "gemma-4-12b-it-qat", False, None, {}, {"tier": "fast"})
    kept_names = {t["function"]["name"] for t in b_notes.get("tools") or []}
    assert "search_notes" in kept_names
    assert "browser_action" not in kept_names and "run_command" not in kept_names


def test_router_openwebui_preamble_and_fallback_photos():
    class DummyHandler(router.Handler):
        def __init__(self):
            self._sse_started = False
            self.wfile = io.BytesIO()
            self.headers = {"User-Agent": "Python/3.12 aiohttp/3.11.0"}
            self.sent_status = None
            self.sent_headers = {}

        def send_response(self, code, message=None):
            self.sent_status = code

        def send_header(self, k, v):
            self.sent_headers[k] = v

        def end_headers(self):
            pass

    h = DummyHandler()
    body = {"model": "auto", "stream": True, "messages": [{"role": "user", "content": "latest news"}]}
    assert h._is_openwebui_stream("openai", body) is True
    # External-tool coding agents (OpenCode) must NOT get Open WebUI custom SSE wrappers
    oc_body = {"model": "local-think", "stream": True, "tools": [{"function": {"name": "bash"}}], "messages": []}
    assert h._is_openwebui_stream("openai", oc_body) is False

    entry = {
        "asked": "auto",
        "tier": "fast",
        "model": "gemma-4-12b-it-qat",
        "_owui_stream": True,
        "_owui_selected_model": "fast",
        "_owui_loading_status": True,
        "_ui": {
            "sources": rich_ui.source_events_for_search(
                [{"title": "BBC", "url": "https://www.bbc.co.uk/news", "content": "Top story", "published": "2026-11-19"}]
            ),
            "embed_html": rich_ui.build_weather_card_html({"place": "London", "current": {"temp": 9}, "daily": []}),
            "verified_images": [
                {"url": "https://ichef.bbci.co.uk/1.jpg", "description": "One"},
                {"url": "https://ichef.bbci.co.uk/2.jpg", "description": "Two"},
            ],
            "visual_topic": True,
            "status_done": rich_ui.status_event("Searched 1 site · 2 photos", done=True),
        },
    }
    ui_meta = h._emit_owui_preamble(entry)
    h._clear_owui_loading_status(entry, ui_meta)
    # Case 1: Model already included 2+ markdown images -> nothing extra emitted
    before_len = len(h.wfile.getvalue())
    h._emit_owui_fallback_photos(
        entry,
        ui_meta,
        "### News\n![One](https://ichef.bbci.co.uk/1.jpg)\n\n![Two](https://ichef.bbci.co.uk/2.jpg)\nBody [1].",
    )
    assert len(h.wfile.getvalue()) == before_len, "Must not duplicate photos when markdown already embeds 2+"

    # Case 2: Model only included 1 photo on a long visual answer -> appends unused photo #2 as markdown delta
    long_one_photo = "### News\n![One](https://ichef.bbci.co.uk/1.jpg)\n" + ("Detailed news bullet [1]. " * 14)
    h._emit_owui_fallback_photos(entry, ui_meta, long_one_photo)
    mid_stream = h.wfile.getvalue().decode("utf-8")
    assert "chatcmpl-photo-supp" in mid_stream and "https://ichef.bbci.co.uk/2.jpg" in mid_stream

    # Case 3: Model omitted markdown images on a substantive (>=180 char) answer -> appends 2 side-by-side Markdown
    # photos via `chatcmpl-photo-supp` and NEVER emits `type: "files"` (which triggers Open WebUI's `/favicon.png` logo)
    before_zero = len(h.wfile.getvalue())
    long_no_photo = "### Recent Updates\n" + ("Bullet summarizing recent developments [1]. " * 6)
    h._emit_owui_fallback_photos(entry, ui_meta, long_no_photo)
    zero_delta = h.wfile.getvalue()[before_zero:].decode("utf-8")
    assert '"type": "files"' not in zero_delta, "Must NEVER emit `files` with external HTTPS URLs"
    assert "![One](https://ichef.bbci.co.uk/1.jpg) ![Two](https://ichef.bbci.co.uk/2.jpg)" in zero_delta

    # Case 4: Short (<180 char) 'nothing new' reply with 0 photos -> emits nothing at all
    before_short = len(h.wfile.getvalue())
    h._emit_owui_fallback_photos(entry, ui_meta, "No new announcements in the past 24 hours [1].")
    assert len(h.wfile.getvalue()) == before_short

    # Guard on `files_event`: external HTTPS URLs rejected, local `/api/v1/files/...` paths allowed
    assert rich_ui.files_event([{"url": "https://ichef.bbci.co.uk/1.jpg"}])["event"]["data"]["files"] == []
    assert len(rich_ui.files_event([{"url": "/api/v1/files/abc/content"}])["event"]["data"]["files"]) == 1

    raw_stream = h.wfile.getvalue().decode("utf-8")
    assert '"selected_model_id": "fast"' in raw_stream
    assert '"type": "source"' in raw_stream
    assert '"type": "embeds"' in raw_stream
    assert '"type": "files"' not in raw_stream


def test_ssrf_redirect_revalidation():
    import email.message
    import importlib.util
    import urllib.error

    dr_path = Path.home() / "AI" / "deep-research" / "engine.py"
    spec_dr = importlib.util.spec_from_file_location("dr_engine_ssrf_test", str(dr_path))
    dr_engine = importlib.util.module_from_spec(spec_dr)
    spec_dr.loader.exec_module(dr_engine)

    for target in (
        "http://127.0.0.1:4000/health",
        "https://169.254.169.254/latest/meta-data/",
        "https://<lan-ip>/internal.png",
    ):
        hdrs = email.message.Message()
        hdrs["Location"] = target
        seen_hosts = []

        def fake_open(req, timeout=8.0):
            seen_hosts.append(req.full_url)
            if len(seen_hosts) == 1:
                raise urllib.error.HTTPError(req.full_url, 302, "Found", hdrs, None)
            raise AssertionError(f"Must NEVER open redirect target {req.full_url}")

        # Allow the initial synthetic public hostname (203.0.113.10 won't be opened anyway, mock _is_public_https_url cleanly)
        orig_check = web_search._is_public_https_url

        def check_initial_only(u):
            if u == "https://public.example.com/start":
                return True
            return orig_check(u)

        with mock.patch.object(web_search, "_is_public_https_url", side_effect=check_initial_only), \
                mock.patch.object(web_search._NO_REDIRECT_OPENER, "open", side_effect=fake_open):
            try:
                web_search.safe_open_public_https("https://public.example.com/start")
            except urllib.error.URLError:
                pass
            else:
                raise AssertionError(f"safe_open_public_https allowed redirect to {target}")
            assert web_search._fetch_article_og_image({"url": "https://public.example.com/start", "title": "T"}, 1) is None
            assert web_search._probe_single_image({"url": "https://public.example.com/start", "description": "D"}) is None
            assert dr_engine._fetch_full_page("https://public.example.com/start", "SAFE_FALLBACK") == "SAFE_FALLBACK"


def main():
    tests = [
        test_weather_card_html_and_sources,
        test_search_source_events_and_markdown_image_detection,
        test_ssrf_and_junk_image_filtering,
        test_ssrf_redirect_revalidation,
        test_context_block_includes_verified_photos_and_clean_citations,
        test_visual_query_classifier,
        test_declarative_recency_backstop_and_zero_hardcoding,
        test_photo_placeholder_and_hash_typo_snapping,
        test_og_image_extraction_and_fast_tool_stripping,
        test_router_openwebui_preamble_and_fallback_photos,
    ]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nAll {len(tests)}/{len(tests)} rich_ui tests passed.")


if __name__ == "__main__":
    main()
