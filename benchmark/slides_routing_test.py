"""Offline unit tests for Generate Slides (native PPTX) routing, tool filtering, and PPTX generation."""
import asyncio
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path
from pptx import Presentation

SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import router  # noqa: E402
import web_search  # noqa: E402

OWUI_TOOLS_BASE = [
    {"type": "function", "function": {"name": "get_current_timestamp", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "calculate_timestamp", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "search_notes", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "generate_image", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "generate_slides", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "inspect_slides", "parameters": {"type": "object", "properties": {"file_id": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "edit_presentation", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}}}},
]


def _body(messages, model="auto", tools=OWUI_TOOLS_BASE):
    return {"model": model, "messages": messages, "tools": list(tools)}


def test_intent_positives():
    positives = [
        "Make me a 6-slide PowerPoint about renewable energy",
        "Create a pitch deck for a local coffee roaster",
        "Build a presentation on Q4 product roadmap",
        "Can you generate a pptx deck summarizing AI trends?",
        "Fais-moi un PowerPoint sur l'IA locale",
        "Crée une présentation de 5 slides sur Londres",
        "Prépare un diaporama sur la transition énergétique",
        "<attached_files><file id='abc' name='company_template.pptx'></file></attached_files>Build our annual review from this",
    ]
    for msg in positives:
        b = _body([{"role": "user", "content": msg}])
        assert router.has_slides_intent(b), f"Expected slides intent for: {msg!r}"
        tier, reason, forced = router.decide(b, "openai")
        assert tier == "deep" and forced, f"Expected forced deep for {msg!r}, got {(tier, reason, forced)}"
        rc = router.route_check(b)
        assert rc["dispatch"] == "chat" and rc.get("tier") == "deep", f"route_check failed for {msg!r}: {rc}"


def test_intent_followups_and_mid_loop():
    # Mid-tool-loop (assistant called generate_slides, tool returned download link)
    mid_loop = _body([
        {"role": "user", "content": "Make a deck about Paris"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "generate_slides", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "[TOOL_RESULT] [deck.pptx](/api/v1/files/123/content)"},
    ])
    assert router.has_slides_intent(mid_loop), "Mid-tool-loop after generate_slides must keep slides intent"

    # Follow-up edit turn in a chat where generate_slides ran earlier
    followup = _body([
        {"role": "user", "content": "Make a PowerPoint on solar power"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "generate_slides", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Here is the presentation: [solar.pptx](/api/v1/files/123/content)"},
        {"role": "assistant", "content": "Here is your presentation: [solar.pptx](/api/v1/files/123/content)"},
        {"role": "user", "content": "Make slide 3 shorter and change the theme to midnight"},
    ])
    assert router.has_slides_intent(followup), "Follow-up slide edit must keep slides intent"
    tier, _, forced = router.decide(followup, "openai")
    assert tier == "deep" and forced


def test_intent_negatives():
    negatives = [
        "What is the weather in London today?",
        "Explain quantum entanglement simply",
        "Write a python-pptx script to build slides from CSV",
        "Build an interactive HTML slide deck using Reveal.js",
        "Create a Three.js 3D solar system in HTML",
        "Generate an image of a sunset over London",
        "Open Chrome and check Amazon for headphones",
        "Write a Python script to parse JSON logs",
    ]
    for msg in negatives:
        b = _body([{"role": "user", "content": msg}])
        assert not router.has_slides_intent(b), f"Unexpected slides intent for: {msg!r}"


def test_tool_trimming_and_pptx_generation():
    # Verify generate_slides.py builds a real multi-slide PPTX offline into a temp export dir
    # Block open_webui top-level import so the test runs completely offline without touching live DB
    sys.modules["open_webui"] = None
    tool_path = Path.home() / "AI" / "open-webui" / "tools" / "generate_slides.py"
    spec = importlib.util.spec_from_file_location("gen_slides_test", str(tool_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    t = mod.Tools()
    with tempfile.TemporaryDirectory() as tmp:
        t.valves.pptx_export_dir = tmp
        t.valves.image_generation = False
        deck = {
            "title": "Local AI Stack Overview",
            "theme": "midnight",
            "slides": [
                {"layout": "cover", "title": "Local AI Stack", "subtitle": "Private LLM & Native PPTX Test"},
                {"layout": "kpi_row", "title": "Key Metrics", "kpis": [
                    {"value": "45 tok/s", "label": "Qwen 3.6 Pro"},
                    {"value": "12 GB", "label": "12 GB RTX GPU"},
                    {"value": "100%", "label": "Local & Private"},
                ]},
                {"layout": "bullets", "title": "Capabilities", "bullets": [
                    "Native editable PowerPoint slides",
                    "Charts, KPIs, timelines, and tables",
                    "Template inspection and post-generation edits",
                ]},
            ],
        }
        res = asyncio.run(t.generate_slides(content=json.dumps(deck)))
        assert ".pptx" in res, f"Expected .pptx link in result, got: {res[:300]}"
        files = list(Path(tmp).glob("*.pptx"))
        assert len(files) == 1, f"Expected 1 .pptx file in {tmp}, found {files}"
        prs = Presentation(str(files[0]))
        assert len(prs.slides) == 3, f"Expected 3 slides, got {len(prs.slides)}"
        for idx, sl in enumerate(prs.slides, 1):
            chars = sum(len(sh.text_frame.text.strip()) for sh in sl.shapes if sh.has_text_frame)
            assert chars >= 40, f"Slide {idx} rendered too little text ({chars} chars)"
        print(f"Generated valid PPTX: {files[0].name} ({files[0].stat().st_size} bytes, {len(prs.slides)} slides)")
    for req_tok in ("cover", "two_column_text", "kpi_row", "icon_grid_2x2", "table", "chart", "closing", "left", "right", "stats"):
        assert req_tok in router.SLIDES_SCHEMA_PROMPT, f"SLIDES_SCHEMA_PROMPT missing {req_tok!r}"


def _load_advanced_deck_mod():
    sys.modules["open_webui"] = None
    adv_path = Path.home() / "AI" / "open-webui" / "tools" / "advanced_deck.py"
    spec_adv = importlib.util.spec_from_file_location("adv_deck_test", str(adv_path))
    mod_adv = importlib.util.module_from_spec(spec_adv)
    spec_adv.loader.exec_module(mod_adv)
    return mod_adv


def test_advanced_deck_sandbox_and_appcontainer():
    """Offline AST guard, env scrubbing, SSRF block, audit-hook escape resistance, and Win32 AppContainer isolation."""
    import os as _os
    import zipfile as _zipfile
    from unittest import mock as _mock

    mod_adv = _load_advanced_deck_mod()
    if _os.name == "nt":
        import ctypes as _ct
        assert _ct.sizeof(mod_adv._JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144

    safe_code = (
        "import math\n"
        "import numpy as np\n"
        "import matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "from deck_kit import get_palette, new_deck, add_cover_slide, add_content_slide, add_picture_fit\n"
        "slug = 'a b'.replace(' ', '_')\n"
        "xs = [1, 2]\n"
        "xs.remove(1)\n"
        "pal = get_palette('executive_navy')\n"
        "prs = new_deck()\n"
        "add_cover_slide(prs, f'Demo Deck {slug}', f'Chart & Kit Sanity {xs[0]}')\n"
        "fig, ax = plt.subplots(figsize=(5, 3))\n"
        "ax.bar(['A', 'B'], [10, 20], color=pal['accent'])\n"
        "fig.savefig('assets/chart.png', dpi=300, bbox_inches='tight')\n"
        "plt.close(fig)\n"
        "sl = add_content_slide(prs, 'Chart Slide', palette=pal, page_num=2)\n"
        "add_picture_fit(sl, 'assets/chart.png', 0.6, 1.4, 12.0, 5.2)\n"
        "prs.save('deck.pptx')\n"
    )
    assert mod_adv._audit_generated_code(safe_code) == [], mod_adv._audit_generated_code(safe_code)
    for bad_code in (
        "import subprocess\nsubprocess.run(['calc'])",
        "import os\nos.system('whoami')",
        "import os as x\nx.system('whoami')",
        "import os\nos.getenv('WEBUI_SECRET_KEY')",
        "getattr(object, '__subclasses__')",
        "open('secret.txt')",
        "from pathlib import Path\nPath.home()",
        "().__class__.__bases__[0].__subclasses__()",
        "from io import open as o\no('secret.txt')",
        "import io as i\ni.open('secret.txt')",
        "import urllib.request\nurllib.request.urlopen('http://127.0.0.1:4000')",
        "eval('1+1')",
    ):
        assert mod_adv._audit_generated_code(bad_code), f"AST guard missed dangerous code: {bad_code}"

    # Env scrubbing strips Open WebUI / API secrets
    with _mock.patch.dict(_os.environ, {"WEBUI_SECRET_KEY": "super-secret-jwt", "OPENAI_API_KEY": "demo-test-key"}, clear=False):
        with tempfile.TemporaryDirectory() as env_td:
            env_build = mod_adv._safe_subprocess_env(Path(env_td), include_userprofile=False)
            assert "WEBUI_SECRET_KEY" not in env_build and "OPENAI_API_KEY" not in env_build, env_build.keys()
            assert str((Path(env_td) / "_tmp").resolve()) == env_build["USERPROFILE"]
            env_qa = mod_adv._safe_subprocess_env(include_userprofile=True)
            assert "WEBUI_SECRET_KEY" not in env_qa and "OPENAI_API_KEY" not in env_qa
            assert env_qa.get("USERPROFILE") == _os.environ.get("USERPROFILE")

    # _download_verified_photos must fail closed on loopback / private URLs
    scripts_dir = str(Path.home() / ".lmstudio" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import web_search as _ws
    with tempfile.TemporaryDirectory() as photo_td:
        saved_photos = mod_adv._download_verified_photos(
            [
                {"url": "http://127.0.0.1:4000/a.png", "description": "local"},
                {"url": "https://127.0.0.1:8282/a.png", "description": "loopback"},
                {"url": "https://<lan-ip>/a.png", "description": "lan"},
            ],
            Path(photo_td),
            _ws,
        )
        assert saved_photos == [], saved_photos

    py312 = mod_adv._find_py312()
    with tempfile.TemporaryDirectory() as sb_td:
        sb_dir = Path(sb_td)
        # .venv pyvenv.cfg ACL resolution covers Scripts dir, .venv root (site-packages), and base Python home
        fake_venv = sb_dir / "fake_venv"
        (fake_venv / "Scripts").mkdir(parents=True)
        fake_py = fake_venv / "Scripts" / "python.exe"
        fake_py.write_bytes(b"")
        fake_base = sb_dir / "fake_base"
        fake_base.mkdir()
        (fake_venv / "pyvenv.cfg").write_text(f"home = {fake_base}\ninclude-system-site-packages = false\n", encoding="utf-8")
        acl_norms = {_os.path.normcase(str(p)) for p in mod_adv._resolve_py_acl_roots(str(fake_py))}
        assert _os.path.normcase(str((fake_venv / "Scripts").resolve())) in acl_norms, acl_norms
        assert _os.path.normcase(str(fake_venv.resolve())) in acl_norms, acl_norms
        assert _os.path.normcase(str(fake_base.resolve())) in acl_norms, acl_norms

        outside_secret = sb_dir / "outside_secret.txt"
        outside_secret.write_text("TOP_SECRET_VALUE", encoding="utf-8")
        outside_trunc = sb_dir / "outside_trunc.txt"
        outside_trunc.write_text("KEEP_INTACT", encoding="utf-8")

        work_ok = sb_dir / "work_ok"
        work_ok.mkdir()
        ok_script = work_ok / "build_deck.py"
        ok_script.write_text(safe_code, encoding="utf-8")
        with _mock.patch.dict(_os.environ, {"WEBUI_SECRET_KEY": "super-secret-jwt"}, clear=False):
            # Non-Windows hosts must fail closed unless ALLOW_UNCONTAINED_DECK_EXEC=1
            clean_env = {k: v for k, v in _os.environ.items() if k != "ALLOW_UNCONTAINED_DECK_EXEC"}
            with _mock.patch.dict(_os.environ, clean_env, clear=True):
                with _mock.patch.object(mod_adv.os, "name", "posix"):
                    ok_nw, msg_nw = mod_adv._run_build_script(py312, work_ok, ok_script)
                    assert not ok_nw and "Win32 AppContainer isolation is required" in msg_nw, msg_nw

            ok_run, msg_ok = mod_adv._run_build_script(py312, work_ok, ok_script)
            assert ok_run, f"Sandboxed deck build failed: {msg_ok}"
            assert (work_ok / "deck.pptx").stat().st_size > 5000

            work_evil_env = sb_dir / "work_evil_env"
            work_evil_env.mkdir()
            evil_env = work_evil_env / "build_deck.py"
            evil_env.write_text("import os\nassert 'WEBUI_SECRET_KEY' not in os.environ, 'secret leaked'\n", encoding="utf-8")
            ok_e1, msg_e1 = mod_adv._run_build_script(py312, work_evil_env, evil_env)
            assert not ok_e1 and "secret leaked" not in msg_e1, msg_e1

            work_evil_read = sb_dir / "work_evil_read"
            work_evil_read.mkdir()
            evil_read = work_evil_read / "build_deck.py"
            evil_read.write_text(f"open({str(outside_secret)!r}, 'r').read()\n", encoding="utf-8")
            ok_e2, msg_e2 = mod_adv._run_build_script(py312, work_evil_read, evil_read)
            assert not ok_e2 and ("PermissionError" in msg_e2 or "Blocked file access" in msg_e2), msg_e2

            work_evil_sub = sb_dir / "work_evil_sub"
            work_evil_sub.mkdir()
            evil_sub = work_evil_sub / "build_deck.py"
            evil_sub.write_text("import subprocess\nsubprocess.run(['cmd', '/c', 'echo', 'hi'])\n", encoding="utf-8")
            ok_e3, msg_e3 = mod_adv._run_build_script(py312, work_evil_sub, evil_sub)
            assert not ok_e3 and ("PermissionError" in msg_e3 or "Blocked by Advanced Deck sandbox" in msg_e3), msg_e3

            work_evil_ctypes = sb_dir / "work_evil_ctypes"
            work_evil_ctypes.mkdir()
            evil_ctypes = work_evil_ctypes / "build_deck.py"
            evil_ctypes.write_text("import ctypes\nctypes.WinDLL('kernel32')\n", encoding="utf-8")
            ok_e4, msg_e4 = mod_adv._run_build_script(py312, work_evil_ctypes, evil_ctypes)
            assert not ok_e4 and ("PermissionError" in msg_e4 or "Blocked by Advanced Deck sandbox" in msg_e4), msg_e4

            for ext_name in ("evil.py", "evil.pth", "evil.zip"):
                work_evil_ext = sb_dir / f"work_evil_{ext_name.replace('.', '_')}"
                work_evil_ext.mkdir()
                evil_ext = work_evil_ext / "build_deck.py"
                evil_ext.write_text(f"open({ext_name!r}, 'w').write('x')\n", encoding="utf-8")
                ok_e5, msg_e5 = mod_adv._run_build_script(py312, work_evil_ext, evil_ext)
                assert not ok_e5 and ("PermissionError" in msg_e5 or "Blocked writing executable/code" in msg_e5), msg_e5

            # Previously uncaught Python audit-hook escapes: os.link, os.truncate, socket.sendto, zipimport + sys.path
            work_link = sb_dir / "work_evil_link"
            work_link.mkdir()
            s_link = work_link / "build_deck.py"
            s_link.write_text(f"import matplotlib\nmatplotlib.os.link({str(outside_secret)!r}, 'leak.txt')\n", encoding="utf-8")
            ok_lk, msg_lk = mod_adv._run_build_script(py312, work_link, s_link)
            assert not ok_lk and ("PermissionError" in msg_lk or "Blocked" in msg_lk) and not (work_link / "leak.txt").exists(), msg_lk

            work_trunc = sb_dir / "work_evil_trunc"
            work_trunc.mkdir()
            s_trunc = work_trunc / "build_deck.py"
            s_trunc.write_text(f"import matplotlib\nmatplotlib.os.truncate({str(outside_trunc)!r}, 0)\n", encoding="utf-8")
            ok_tr, msg_tr = mod_adv._run_build_script(py312, work_trunc, s_trunc)
            assert not ok_tr and ("PermissionError" in msg_tr or "Blocked" in msg_tr), msg_tr
            assert outside_trunc.read_text(encoding="utf-8") == "KEEP_INTACT"

            work_sock = sb_dir / "work_evil_sock"
            work_sock.mkdir()
            s_sock = work_sock / "build_deck.py"
            s_sock.write_text(
                "import matplotlib\ns = matplotlib.sys.modules['socket'].socket(2, 2)\ns.sendto(b'hi', ('127.0.0.1', 9))\n",
                encoding="utf-8",
            )
            ok_sk, msg_sk = mod_adv._run_build_script(py312, work_sock, s_sock)
            assert not ok_sk and ("PermissionError" in msg_sk or "Blocked" in msg_sk), msg_sk

            work_zip = sb_dir / "work_evil_zipimport"
            work_zip.mkdir()
            with _zipfile.ZipFile(work_zip / "mod.zip", "w") as zf:
                zf.writestr("colorsys.py", "raise RuntimeError('ZIPIMPORT_RAN')\n")
            s_zip = work_zip / "build_deck.py"
            s_zip.write_text("import matplotlib\nmatplotlib.sys.path.insert(0, 'mod.zip')\nimport colorsys\n", encoding="utf-8")
            ok_zp, msg_zp = mod_adv._run_build_script(py312, work_zip, s_zip)
            assert not ok_zp and "ZIPIMPORT_RAN" not in msg_zp and ("PermissionError" in msg_zp or "Blocked" in msg_zp), msg_zp

            work_dyn = sb_dir / "work_evil_dyn_exec"
            work_dyn.mkdir()
            s_dyn_exec = work_dyn / "build_deck.py"
            s_dyn_exec.write_text("import matplotlib\nmatplotlib.sys.modules['builtins'].exec('open(\"exec_ran.txt\", \"w\").write(\"1\")')\n", encoding="utf-8")
            ok_de, msg_de = mod_adv._run_build_script(py312, work_dyn, s_dyn_exec)
            assert not ok_de and not (work_dyn / "exec_ran.txt").exists() and ("Blocked dynamic exec" in msg_de or "PermissionError" in msg_de), msg_de

            work_dyn_eval = sb_dir / "work_evil_dyn_eval"
            work_dyn_eval.mkdir()
            s_dyn_eval = work_dyn_eval / "build_deck.py"
            s_dyn_eval.write_text("import matplotlib\nmatplotlib.sys.modules['builtins'].eval('1+1')\n", encoding="utf-8")
            ok_dv, msg_dv = mod_adv._run_build_script(py312, work_dyn_eval, s_dyn_eval)
            assert not ok_dv and ("PermissionError" in msg_dv or "Blocked" in msg_dv), msg_dv

            work_syspath = sb_dir / "work_syspath"
            work_syspath.mkdir()
            chk_syspath = work_syspath / "build_deck.py"
            chk_syspath.write_text(
                f"import os, sys\nw = os.path.normcase(os.path.realpath({str(work_syspath)!r}))\n"
                "assert not any(os.path.normcase(os.path.realpath(p)) == w for p in sys.path if p), sys.path\n"
                "assert '' not in sys.path and '.' not in sys.path, sys.path\n",
                encoding="utf-8",
            )
            ok_sp, msg_sp = mod_adv._run_build_script(py312, work_syspath, chk_syspath)
            assert not ok_sp and "AssertionError" not in msg_sp, msg_sp

            # Pure OS-level Win32 AppContainer kernel boundary tests (NO Python audit hooks installed!)
            if _os.name == "nt":
                import ctypes as _ct
                import ctypes.wintypes as _wt
                import time as _time

                work_ac = sb_dir / "work_ac_kernel"
                work_ac.mkdir()
                outside_write = sb_dir / "outside_write.txt"
                ac_env = mod_adv._safe_subprocess_env(work_ac, include_userprofile=False)
                ac_payload = (
                    "import ctypes, os, socket\n"
                    "from pathlib import Path\n"
                    "k32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                    "k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]\n"
                    "k32.CreateFileW.restype = ctypes.c_void_p\n"
                    f"h = k32.CreateFileW({str(outside_secret)!r}, 0x80000000, 1, None, 3, 0x80, None)\n"
                    "err = ctypes.get_last_error()\n"
                    "assert h in (None, 0, ctypes.c_void_p(-1).value) and err == 5, f'Unexpected CreateFileW handle={h} err={err}'\n"
                    f"for tgt in ({str(outside_secret)!r}, {str(Path.home() / 'AI' / 'secrets' / 'tavily_api_key.txt')!r}, {str(Path.home() / 'AI' / 'open-webui' / 'data' / 'webui.db')!r}):\n"
                    "    if os.path.exists(tgt) or True:\n"
                    "        try:\n"
                    "            open(tgt, 'rb').read(4)\n"
                    "            raise AssertionError(f'AppContainer read succeeded on {tgt}')\n"
                    "        except (PermissionError, FileNotFoundError, OSError):\n"
                    "            pass\n"
                    "for host_port in (('127.0.0.1', 4000), ('1.1.1.1', 443)):\n"
                    "    try:\n"
                    "        socket.create_connection(host_port, timeout=2).close()\n"
                    "        raise AssertionError(f'AppContainer network succeeded on {host_port}')\n"
                    "    except OSError:\n"
                    "        pass\n"
                    f"try:\n    open({str(outside_write)!r}, 'w').write('bad')\n    raise AssertionError('Outside write succeeded')\n"
                    "except PermissionError:\n    pass\n"
                    "open('inside_ok.txt', 'w').write('ok')\n"
                    "print('APPCONTAINER_KERNEL_OK')\n"
                )
                rc_ac, out_ac, err_ac = mod_adv._run_in_appcontainer(
                    [py312, "-I", "-X", "utf8", "-c", ac_payload], work_ac, ac_env, timeout=20
                )
                assert rc_ac == 0 and "APPCONTAINER_KERNEL_OK" in out_ac, f"rc={rc_ac} out={out_ac!r} err={err_ac!r}"
                assert (work_ac / "inside_ok.txt").read_text(encoding="utf-8") == "ok"
                assert not outside_write.exists()

                # Verify PROC_THREAD_ATTRIBUTE_HANDLE_LIST blocks inheritable parent handles
                sentinel = sb_dir / "inherited_sentinel.txt"
                sentinel.write_bytes(b"LEAKED_VIA_HANDLE_INHERITANCE")
                k32 = _ct.WinDLL("kernel32", use_last_error=True)
                sa_inh = mod_adv._SECURITY_ATTRIBUTES(_ct.sizeof(mod_adv._SECURITY_ATTRIBUTES), None, True)
                k32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, _ct.c_void_p, _wt.DWORD, _wt.DWORD, _wt.HANDLE]
                k32.CreateFileW.restype = _wt.HANDLE
                k32.CloseHandle.argtypes = [_wt.HANDLE]
                k32.CloseHandle.restype = _wt.BOOL
                h_leak = k32.CreateFileW(str(sentinel), 0x80000000, 3, _ct.byref(sa_inh), 3, 0x80, None)
                assert h_leak and h_leak != _ct.c_void_p(-1).value
                try:
                    handle_payload = (
                        "import ctypes, ctypes.wintypes as wt\n"
                        "k32 = ctypes.WinDLL('kernel32', use_last_error=True)\n"
                        "k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p]\n"
                        "k32.ReadFile.restype = wt.BOOL\n"
                        "buf = (ctypes.c_char * 64)()\n"
                        "nread = wt.DWORD(0)\n"
                        "try:\n"
                        f"    ok = k32.ReadFile(wt.HANDLE({int(h_leak)}), buf, 64, ctypes.byref(nread), None)\n"
                        "    err = ctypes.get_last_error()\n"
                        "    data = bytes(buf[:nread.value])\n"
                        "    assert b'LEAKED_VIA_HANDLE_INHERITANCE' not in data, f'Leaked handle data: {data!r}'\n"
                        "    assert not ok and err in (6, 5, 1, 87, 109), f'Expected failed ReadFile on blocked handle, got ok={ok} err={err}'\n"
                        "except OSError as exc:\n"
                        "    assert getattr(exc, 'winerror', None) in (-1073741816, 6, 5, 1, 87, 109), repr(exc)\n"
                        "print('HANDLE_INHERITANCE_BLOCKED')\n"
                    )
                    rc_hl, out_hl, err_hl = mod_adv._run_in_appcontainer(
                        [py312, "-I", "-X", "utf8", "-c", handle_payload], work_ac, ac_env, timeout=20
                    )
                    assert rc_hl == 0 and "HANDLE_INHERITANCE_BLOCKED" in out_hl, f"rc={rc_hl} out={out_hl!r} err={err_hl!r}"
                finally:
                    k32.CloseHandle(h_leak)

                # Verify JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE + TerminateJobObject kills grandchildren on timeout
                work_job = sb_dir / "work_ac_job"
                work_job.mkdir()
                job_env = mod_adv._safe_subprocess_env(work_job, include_userprofile=False)
                job_payload = (
                    "import os, subprocess, sys, time\n"
                    "gc_code = \"import os, pathlib, time; pathlib.Path('grandchild_pid.txt').write_text(str(os.getpid())); time.sleep(30)\"\n"
                    "p = subprocess.Popen([sys.executable, '-I', '-c', gc_code])\n"
                    "for _ in range(40):\n"
                    "    if os.path.exists('grandchild_pid.txt'): break\n"
                    "    time.sleep(0.05)\n"
                    "time.sleep(30)\n"
                )
                rc_jb, out_jb, err_jb = mod_adv._run_in_appcontainer(
                    [py312, "-I", "-X", "utf8", "-c", job_payload], work_job, job_env, timeout=3
                )
                assert rc_jb == 124, f"Expected timeout 124, got {rc_jb} err={err_jb!r}"
                gpid_file = work_job / "grandchild_pid.txt"
                assert gpid_file.is_file(), "Grandchild never wrote PID before timeout"
                gpid = int(gpid_file.read_text(encoding="utf-8").strip())
                k32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
                k32.OpenProcess.restype = _wt.HANDLE
                k32.GetExitCodeProcess.argtypes = [_wt.HANDLE, _ct.POINTER(_wt.DWORD)]
                k32.GetExitCodeProcess.restype = _wt.BOOL
                _time.sleep(0.25)
                h_gp = k32.OpenProcess(0x1000, False, gpid)
                if h_gp:
                    try:
                        ex = _wt.DWORD(0)
                        assert k32.GetExitCodeProcess(h_gp, _ct.byref(ex)) and ex.value != 259, f"Grandchild {gpid} STILL_ACTIVE after Job kill!"
                    finally:
                        k32.CloseHandle(h_gp)


def test_option_a_search_freshness_and_option_b_bespoke_route():
    # Option A: Tavily topic/time_range inference + RFC-2822 date formatting + for_slides context_block
    opts_week = web_search.infer_search_options("Fais-moi un PowerPoint de 5 slides sur l'actualité IA de cette semaine")
    assert opts_week == {"topic": "news", "time_range": "week"}, opts_week
    opts_day = web_search.infer_search_options("What happened in AI news in the last 24h?")
    assert opts_day == {"topic": "news", "time_range": "day"}, opts_day
    opts_gen = web_search.infer_search_options("History of the Eiffel Tower")
    assert opts_gen == {"topic": "general", "time_range": None}, opts_gen
    assert web_search._format_pub_date("Fri, 20 Nov 2026 05:00:00 GMT") == "2026-11-20"
    assert web_search._format_pub_date("2026-11-18T12:00:00Z") == "2026-11-18"

    ctx = web_search.context_block(
        "search",
        [{"title": "Gemini 4 Neon Launch", "url": "https://example.com/neon", "content": "Details", "published": "Thu, 19 Nov 2026 10:00:00 GMT"}],
        query="AI news November 2026",
        images=[{"url": "https://example.com/hero.jpg", "description": "Source [1] — Gemini 4 Neon"}],
        rich_citations=True,
        for_slides=True,
    )
    assert "(published 2026-11-19)" in ctx
    assert "Do NOT put bracketed citation numbers" in ctx
    assert "VERIFIED PHOTOS FOR SLIDES" in ctx and "https://example.com/hero.jpg" in ctx
    assert "photo://1" not in ctx

    # Both Simple Deck and Advanced Deck stay in chat (never surprise-hijacked to OpenCode)
    adv_tools = list(OWUI_TOOLS_BASE) + [
        {"type": "function", "function": {"name": "build_advanced_deck", "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}}}}
    ]
    for msg in (
        "Build me a bespoke agency-grade PowerPoint on Q4 AI trends with visual QA",
        "Fais-moi un PowerPoint sur-mesure qualité agence avec vérification visuelle",
    ):
        rc = router.route_check(_body([{"role": "user", "content": msg}], tools=adv_tools))
        assert rc["dispatch"] == "chat" and rc.get("tier") == "deep", f"Expected chat/deep: {msg!r} -> {rc}"

    # When Advanced Deck is toggled on in + -> Tools, decide() routes straight to deep even on a bare topic prompt
    tier_adv, reason_adv, forced_adv = router.decide(_body([{"role": "user", "content": "L'actualité IA de cette semaine"}], tools=adv_tools), "openai")
    assert tier_adv == "deep" and forced_adv and "Advanced Deck" in reason_adv, (tier_adv, reason_adv, forced_adv)

    mod_adv = _load_advanced_deck_mod()
    test_advanced_deck_sandbox_and_appcontainer()

    # Deterministic 16:9 widescreen injection + import sanitizer + surgical SEARCH/REPLACE patcher (incl. delete)
    ws_fixed = mod_adv._ensure_widescreen_code(
        "from pptx import Presentation\nfrom pptx.util import Inches\nfrom pptx.enum.shapes import MSO_ANCHOR\n"
        "prs = Presentation()\nshp = MSO_SHAPE.ROUNDED_RECTANGLE\nprs.save('deck.pptx')\n"
    )
    assert "prs.slide_width = Inches(13.333)" in ws_fixed and "prs.slide_height = Inches(7.5)" in ws_fixed, ws_fixed
    assert "from pptx.enum.text import MSO_ANCHOR" in ws_fixed and "from pptx.enum.shapes import MSO_ANCHOR" not in ws_fixed, ws_fixed
    assert "from pptx.enum.shapes import MSO_SHAPE" in ws_fixed, ws_fixed
    patched = mod_adv._apply_surgical_patches(
        "x = 1\ny = 2\nBAD_LINE\nprs = Presentation()\n",
        "<<<<<<< SEARCH\ny = 2\n=======\ny = 42\n>>>>>>> REPLACE\n\n<<<<<<< SEARCH\nBAD_LINE\n=======\n>>>>>>> REPLACE",
    )
    assert patched and "y = 42" in patched and "BAD_LINE" not in patched and "prs.slide_width = Inches(13.333)" in patched, patched

    # Auto slide count + editorial/visual topic detection (fashion, travel, design vs finance/tech)
    assert mod_adv._choose_slide_count(0, "Les dernières tendances vestimentaires de l'hiver 2026") == 7
    assert mod_adv._choose_slide_count(0, "Un deck rapide de résumé sur les tendances hiver 2026") == 5
    assert mod_adv._choose_slide_count(0, "Une présentation complète et détaillée sur la mode hiver 2026") == 8
    assert mod_adv._choose_slide_count(10, "Les dernières tendances vestimentaires de l'hiver 2026") == 10
    assert mod_adv._choose_slide_count(0, "Deck de 9 slides sur la mode hiver 2026") == 9
    assert mod_adv._is_editorial_visual_topic("Les dernières tendances vestimentaires de l'hiver 2026") is True
    assert mod_adv._is_editorial_visual_topic("Architecture et design intérieur minimaliste 2026") is True
    assert mod_adv._is_editorial_visual_topic("Actualité IA de cette semaine : modèles et levées de fonds") is False
    assert mod_adv._is_editorial_visual_topic("Fashion market size, revenue and CapEx valuation 2026") is False

    # Option B long session (>15 turns): OpenCode bespoke PPTX session stays on Pro (deep), never swaps to Qwen3.8
    ext_tools = [{"type": "function", "function": {"name": "execute", "parameters": {"type": "object", "properties": {}}}}]
    long_msgs = [{"role": "user", "content": "Crée un PowerPoint sur-mesure dans ai_news.pptx avec pptx_studio"}]
    for i in range(18):
        long_msgs.append({"role": "assistant", "content": f"step {i}"})
        long_msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    tier_long, _, forced_long = router.decide({"model": "local-think", "messages": long_msgs, "tools": ext_tools}, "openai")
    assert tier_long == "deep" and forced_long, f"Expected deep on 18-turn PPTX agent session, got {(tier_long, forced_long)}"


if __name__ == "__main__":
    test_intent_positives()
    print("PASS: test_intent_positives (8/8)")
    test_intent_followups_and_mid_loop()
    print("PASS: test_intent_followups_and_mid_loop (2/2)")
    test_intent_negatives()
    print("PASS: test_intent_negatives (8/8)")
    test_tool_trimming_and_pptx_generation()
    print("PASS: test_tool_trimming_and_pptx_generation")
    test_advanced_deck_sandbox_and_appcontainer()
    print("PASS: test_advanced_deck_sandbox_and_appcontainer")
    test_option_a_search_freshness_and_option_b_bespoke_route()
    print("PASS: test_option_a_search_freshness_and_option_b_bespoke_route")
    print("ALL SLIDES TESTS PASSED")
