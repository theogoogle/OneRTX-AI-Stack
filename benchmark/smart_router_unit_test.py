"""Offline deterministic unit tests for the Smart Coding Router, Tool Bridge, Surgical Merger & Job Object."""
import base64
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import html_files  # noqa: E402
import router      # noqa: E402

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
]


def test_openai_to_anthropic_tools_roundtrip():
    body = {
        "model": "local-think",
        "stream": True,
        "tools": SAMPLE_TOOLS,
        "messages": [
            {"role": "system", "content": "Agent system prompt"},
            {"role": "user", "content": "Create app.py and test it"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "write", "arguments": '{"path":"app.py","content":"x=1"}'}},
                    {"id": "call_2", "type": "function", "function": {"name": "bash", "arguments": '{"command":"pytest"}'}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "tool", "tool_call_id": "call_2", "content": "1 passed"},
        ],
    }
    ab = router.openai_to_anthropic(body, router.MODELS["code"], router.AGENT_THINK_BUDGET)
    assert ab["thinking"] == {"type": "enabled", "budget_tokens": 8192}, ab["thinking"]
    assert len(ab["tools"]) == 2 and ab["tools"][0]["name"] == "bash"
    assert "input_schema" in ab["tools"][0]
    # Assistant tool_use followed by a single merged user message holding both tool_results
    assert [m["role"] for m in ab["messages"]] == ["user", "assistant", "user"], ab["messages"]
    tu = ab["messages"][1]["content"]
    assert [b["type"] for b in tu] == ["tool_use", "tool_use"]
    assert tu[0]["input"] == {"path": "app.py", "content": "x=1"}
    tr = ab["messages"][2]["content"]
    assert [b["type"] for b in tr] == ["tool_result", "tool_result"]
    assert [b["tool_use_id"] for b in tr] == ["call_1", "call_2"]


def test_openai_to_anthropic_dropped_tools_fallback():
    body = {
        "model": "think",
        "stream": False,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "foo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "bar"},
        ],
    }
    ab = router.openai_to_anthropic(body, router.MODELS["code"], 4000)
    assert "tools" not in ab
    assert "[called tools: foo]" in str(ab["messages"][1]["content"])
    assert "[tool result]" in str(ab["messages"][2]["content"])


def test_anthropic_to_openai_tool_calls():
    anth = {
        "content": [
            {"type": "thinking", "thinking": "Let me inspect files."},
            {"type": "text", "text": "Checking directory."},
            {"type": "tool_use", "id": "toolu_99", "name": "bash", "input": {"command": "ls"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 120, "output_tokens": 45},
    }
    oai = router.anthropic_to_openai(anth, router.MODELS["code"])
    ch = oai["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["reasoning_content"] == "Let me inspect files."
    assert ch["message"]["tool_calls"][0]["id"] == "toolu_99"
    assert json.loads(ch["message"]["tool_calls"][0]["function"]["arguments"]) == {"command": "ls"}


def test_agent_overflow_to_pro_both_doors():
    small_oc = {"model": "local-think", "tools": SAMPLE_TOOLS, "messages": [{"role": "user", "content": "fix bug"}]}
    tier, reason, forced = router.decide(small_oc, "openai")
    assert tier == "think" and forced is True and "8192" in reason, (tier, reason)

    huge_text = "x" * (48000 * 4)
    big_oc = {"model": "local-think", "tools": SAMPLE_TOOLS, "messages": [{"role": "user", "content": huge_text}]}
    tier2, reason2, forced2 = router.decide(big_oc, "openai")
    assert tier2 == "deep" and "Pro (256K)" in reason2, (tier2, reason2)

    big_cc = {"model": "local-auto", "tools": [{"name": "Edit", "input_schema": {"type": "object"}}],
              "messages": [{"role": "user", "content": huge_text}]}
    tier3, reason3, _ = router.decide(big_cc, "anthropic")
    assert tier3 == "deep" and "256K" in reason3, (tier3, reason3)

    big_cc_forced = {"model": "local-think", "messages": [{"role": "user", "content": huge_text}]}
    tier4, reason4, _ = router.decide(big_cc_forced, "anthropic")
    assert tier4 == "deep" and "256K" in reason4, (tier4, reason4)


def test_coding_brief_modes():
    # 1. OpenCode external tools -> NO chat brief injected
    oc_body = {"messages": [{"role": "user", "content": "build cli"}], "tools": SAMPLE_TOOLS}
    ent = {"reason": "coding agent (think) -> Qwen3.8 + 8192 think cap"}
    router.Handler._coding_brief(oc_body, "think", ent)
    assert "brief" not in ent and len(oc_body["messages"]) == 1

    # 2. Fresh 3D coding chat on Pro -> THREED_SYSTEM + CODING_CHAT_SYSTEM (complete files)
    d3_body = {"messages": [{"role": "user", "content": "Build a 3D checkers game in Three.js"}]}
    ent2 = {"reason": "smart coding (3d_sim, 3D/simulation) -> Pro (Qwen3.6)"}
    router.Handler._coding_brief(d3_body, "deep", ent2)
    assert ent2.get("brief") == "complete files"
    sys_txt = d3_body["messages"][0]["content"]
    assert "three.module.min.js" in sys_txt and "COMPLETE files" in sys_txt

    # 3. Long (>=400 lines) existing HTML in chat -> CODING_SURGICAL_SYSTEM (SEARCH/REPLACE)
    long_html = "<!DOCTYPE html>\n<html><body>\n" + "\n".join(f"<div>line {i}</div>" for i in range(420)) + "\n</body></html>"
    long_body = {
        "messages": [
            {"role": "user", "content": "Make a dashboard"},
            {"role": "assistant", "content": f"```html\n{long_html}\n```"},
            {"role": "user", "content": "Change line 200 text to hello"},
        ]
    }
    ent3 = {"reason": "follow-up in a chat that already holds code, stays on think"}
    router.Handler._coding_brief(long_body, "think", ent3)
    assert "surgical patch" in ent3.get("brief", ""), ent3
    assert "<<<<<<< SEARCH" in long_body["messages"][0]["content"]

    # 4. Quick code Q&A -> NO chat brief injected
    q_body = {"messages": [{"role": "user", "content": "What regex matches digits?"}]}
    ent4 = {"reason": "LFM said 'code'; quick code Q&A -> Flash GPU"}
    router.Handler._coding_brief(q_body, "fast", ent4)
    assert "brief" not in ent4


def test_surgical_merge_and_error_excerpts():
    lines = ["<!DOCTYPE html>", "<html><body><script>", "const itemA = 1;"]
    lines += [f"const v{i} = {i};" for i in range(410)]
    lines += ["const itemA = 2;", "</script></body></html>"]
    base = "\n".join(lines)
    exc = html_files.numbered_error_excerpts(base, ["SyntaxError: Identifier 'itemA' has already been declared"])
    assert "itemA = 1" in exc and "itemA = 2" in exc and len(exc) < 4000
    patch = "Fix:\n<<<<<<< SEARCH\nconst itemA = 2;\n=======\nconst itemB = 2;\n>>>>>>> REPLACE"
    merged, count, _ = html_files.apply_search_replace(base, patch)
    assert count == 1 and "const itemB = 2;" in merged and merged.count("const itemA") == 1


def test_zero_benchmark_hardcoding():
    forbidden = (
        "pomodoro", "lighthouse", "kanban", "vibe-academy", "sqlite",
        "orbital", "cron", "markdown parser", "concurrency limiter",
    )
    inspected = "\n".join([
        router.CLASSIFIER_SYSTEM,
        router.CODING_ROUTE_SYSTEM,
        router.THREED_WORDS.pattern,
        router.SIM_3D_WORDS.pattern,
        router.MULTIFILE_AGENT_RE.pattern,
        router.SINGLE_FILE_CHAT_RE.pattern,
        router.QUICK_HOWTO_QA_RE.pattern,
        router.EXPLICIT_OPENCODE_RE.pattern,
    ]).lower()
    leaks = [w for w in forbidden if w in inspected]
    assert not leaks, f"Hardcoded benchmark terms found in routing logic: {leaks}"


def test_win_job_object_helper():
    ot_sessions = (
        Path.home() / "AppData" / "Roaming" / "uv" / "tools" / "open-terminal"
        / "Lib" / "site-packages" / "open_terminal" / "agents" / "sessions.py"
    )
    src = ot_sessions.read_text(encoding="utf-8")
    assert "_attach_win_job_object(proc)" in src and "_close_win_job_object(proc)" in src
    p = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(5)"])
    try:
        ns = {"os": __import__("os")}
        helper_part = src.split("# local patch: Win32 Job Object kill-on-close")[1].split("async def spawn(")[0]
        exec("# local patch" + helper_part, ns)
        ns["_attach_win_job_object"](p)
        assert getattr(p, "_win_job", None), "Expected _win_job handle on Windows subprocess"
        # Closing the KILL_ON_JOB_CLOSE job handle should immediately terminate child `p`!
        ns["_close_win_job_object"](p)
        rc = p.wait(timeout=3)
        assert rc is not None
    finally:
        if p.poll() is None:
            p.kill()


def test_image_generation_routing_and_tool_preservation():
    ow_tools = [
        {"type": "function", "function": {"name": "get_current_timestamp", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "calculate_timestamp", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "run_command", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "display_file", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "generate_image", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}}},
    ]
    cat_prompt = (
        "generate the picture of a cat in an anime style that is waiting in front of a japanese house, "
        "the weather around is lovely and the surroundings are rice fields and mountains"
    )
    ow_tools = ow_tools + [
        {"type": "function", "function": {"name": "edit_image", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "image_urls": {"type": "array"}}}}}
    ]
    # 1. Exact bug prompt + Pixar 3D illustration + French prompt -> image intent True
    assert router.is_image_gen_request(cat_prompt) is True
    assert router.is_image_gen_request("Create a Pixar-style 3D illustration of a tiny robot watering a plant") is True
    assert router.is_image_gen_request("Dessine-moi une image d'un chat devant une maison japonaise") is True

    # 2. Code / HTML / SVG / Three.js / Matplotlib / Screenshot -> image intent False
    assert router.is_image_gen_request("Build an interactive 3D solar system scene in Three.js") is False
    assert router.is_image_gen_request("Generate an SVG icon of a cat in HTML/CSS") is False
    assert router.is_image_gen_request("Write a Python script using Pillow to generate thumbnails") is False
    assert router.is_image_gen_request("Make a matplotlib chart of monthly token usage") is False
    assert router.is_image_gen_request("Take a screenshot of my desktop") is False

    # 3. route_check & decide on auto (even with 3D word in prompt!) -> chat / fast (forced=True), add_live_info skipped
    rc = router.route_check({"messages": [{"role": "user", "content": "Create a Pixar-style 3D illustration of a cat"}]})
    assert rc["dispatch"] == "chat" and rc.get("tier") == "fast", rc

    b_auto = {"model": "auto", "tools": [dict(t) for t in ow_tools], "messages": [{"role": "user", "content": cat_prompt}]}
    tier, reason, forced = router.decide(b_auto, "openai")
    assert tier == "fast" and forced is True and "generate_image" in reason, (tier, reason, forced)
    assert router.add_live_info(b_auto, "openai", tier, reason) is None

    # 4. Turn 2 continuation right after generate_image ran + explicit Image switch hint
    b_turn2 = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [
            {"role": "user", "content": "a fluffy cat"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": '{"status":"success"}'},
        ],
    }
    assert router.has_image_gen_intent(b_turn2) is True

    b_switch = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [
            {"role": "system", "content": router.IMAGE_MODE_HINT + ": call generate_image."},
            {"role": "user", "content": "a misty bamboo forest"},
        ],
    }
    assert router.has_image_gen_intent(b_switch) is True

    # 5. Iterative edit follow-up on generated image + uploaded image, vs negative guards
    cat_url = "/api/v1/files/11111111-2222-3333-4444-555555555555/content"
    b_edit_followup = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [
            {"role": "user", "content": "generate a picture of a ginger cat"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "generate_image", "arguments": '{"prompt":"cat"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"status": "success", "images": [{"url": cat_url}]})},
            {"role": "assistant", "content": "Here is your cat!"},
            {"role": "user", "content": "add a red wizard hat"},
        ],
    }
    assert router.has_image_gen_intent(b_edit_followup) is True
    tier_ef, _, forced_ef = router.decide(b_edit_followup, "openai")
    assert tier_ef == "fast" and forced_ef is True, (tier_ef, forced_ef)
    assert router._chat_image_file_urls(b_edit_followup["messages"]) == [cat_url]

    # Uploaded image edit intent + negative guards
    up_url = "/api/v1/files/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/content"
    b_up_edit = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": f'<attached_files>\n<file type="image" id="a" url="{up_url}" content_type="image/png" name="cat.png"/>\n</attached_files>\n\nput sunglasses on him'},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }],
    }
    assert router.has_image_gen_intent(b_up_edit) is True
    assert router._chat_image_file_urls(b_up_edit["messages"]) == [up_url]

    # _dispatch preserves generate_image & edit_image and injects the latest URL rule
    b_disp = json.loads(json.dumps(b_edit_followup))
    captured = {}

    class _H(router.Handler):
        def __init__(self):
            pass

        def _forward(self, path, b_out, entry):
            captured["body"] = b_out
            return "ok"

    _H()._dispatch("/v1/chat/completions", "openai", b_disp, "fast", router.MODELS["fast"], False, None, [], {})
    kept_names = {t["function"]["name"] for t in captured["body"]["tools"]}
    assert kept_names == {"generate_image", "edit_image"}, kept_names
    sys_txt = " ".join(str(m.get("content")) for m in captured["body"]["messages"] if m.get("role") == "system")
    assert "edit_image" in sys_txt and json.dumps([cat_url]) in sys_txt, sys_txt
    assert "Image tool rule:" in sys_txt and "display_file" in sys_txt, sys_txt

    # 5b. Regression for 0.13.4 uploaded ~950KB base64 PNG + bare UUID <file> tag + terminal prompt loop
    raw_uuid = "14c38e4d-45fd-43fb-9f11-8db546f280d2"
    pdf_uuid = "99999999-8888-7777-6666-555555555555"
    big_b64 = "iVBORw0KGgo" + ("A" * 950_000)
    b_big_b64 = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [
            {"role": "system", "content": "When delivering created or updated files, call display_file."},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f'<attached_files>\n'
                            f'<file type="file" id="{pdf_uuid}" url="{pdf_uuid}" content_type="application/pdf" name="notes.pdf"/>\n'
                            f'<file type="file" id="{raw_uuid}" url="{raw_uuid}" content_type="image/png" name="Screenshot 2026-11-17.png"/>\n'
                            f'</attached_files>\n\nAdd a surfing board to this picture'
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_b64}"}},
                ],
            },
        ],
    }
    tok_est = router.estimate_tokens(b_big_b64)
    assert 1000 < tok_est < 5000, f"Base64 PNG inflated token estimate: {tok_est}"
    anth_img = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 900_000}},
                    {"type": "text", "text": "describe"},
                ],
            }
        ]
    }
    assert router.estimate_tokens(anth_img) < 2000
    assert router.estimate_tokens({"messages": [{"role": "user", "content": "hello world"}]}) == int(
        len(json.dumps([{"role": "user", "content": "hello world"}])) / 3.5
    ) + int(len('""') / 3.5)
    assert router._chat_image_file_urls(b_big_b64["messages"]) == [raw_uuid]
    tier_b64, reason_b64, forced_b64 = router.decide(b_big_b64, "openai")
    assert tier_b64 == "fast" and forced_b64 is True, (tier_b64, forced_b64)
    fit_tier_b64, fit_reason_b64 = router.fit_context(b_big_b64, tier_b64, reason_b64, forced_b64)
    assert fit_tier_b64 == "fast", (fit_tier_b64, fit_reason_b64)

    # Round 1 dispatch sees bare UUID edit target and single-call rule
    _H()._dispatch("/v1/chat/completions", "openai", json.loads(json.dumps(b_big_b64)), "fast", router.MODELS["fast"], False, None, [], {})
    sys_r1 = " ".join(str(m.get("content")) for m in captured["body"]["messages"] if m.get("role") == "system")
    assert json.dumps([raw_uuid]) in sys_r1 and "ONCE" in sys_r1 and "display_file" in sys_r1, sys_r1

    # Round 2 dispatch after edit_image returns 0.13.4 terminal-hint tool_result keeps single-call stop rule
    b_big_r2 = json.loads(json.dumps(b_big_b64))
    b_big_r2["messages"].extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "edit_image", "arguments": "{}"}}]},
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": json.dumps({
                "status": "success",
                "message": "The edited image is already visible in chat. Use terminal_path with terminal tools or display_file when available to continue the requested task.",
                "images": [{"url": "/api/v1/files/bf8d28c3-1111-2222-3333-444444444444/content"}],
            }),
        },
    ])
    _H()._dispatch("/v1/chat/completions", "openai", b_big_r2, "fast", router.MODELS["fast"], False, None, [], {})
    sys_r2 = " ".join(str(m.get("content")) for m in captured["body"]["messages"] if m.get("role") == "system")
    assert "Image tool rule:" in sys_r2 and "stop calling tools" in sys_r2, sys_r2

    # Negative guards on analytical vision, questions, UI screenshots, and code requests
    for neg_txt in (
        "Extract all items into a table",
        "Analyze this table and calculate Net Profit",
        "What breed is this cat?",
        "What is the cat wearing?",
        "Where do I click on my screen?",
        "Add a button to this Python app",
    ):
        b_neg = json.loads(json.dumps(b_edit_followup))
        b_neg["messages"][-1]["content"] = neg_txt
        assert router.has_image_gen_intent(b_neg) is False, f"False positive image intent on: {neg_txt}"

    # 6. _parse_images_edit_request multipart/form-data + JSON parsing (stdlib email.parser, zero cgi warning)
    boundary = "----TestBoundary123"
    mp = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"prompt\"\r\n\r\nAdd a wizard hat\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nqwen-image-2.1\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"cat1.png\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode() + b"\x89PNGDATA1" + (
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"hat2.png\"\r\nContent-Type: image/png\r\n\r\n"
    ).encode() + b"\x89PNGDATA2" + f"\r\n--{boundary}--\r\n".encode()
    flds, imgs = router._parse_images_edit_request(f"multipart/form-data; boundary={boundary}", mp)
    assert flds["prompt"] == "Add a wizard hat" and flds["model"] == "qwen-image-2.1", flds
    assert len(imgs) == 2 and imgs[0][0] == b"\x89PNGDATA1" and imgs[1][0] == b"\x89PNGDATA2", imgs

    jflds, jimgs = router._parse_images_edit_request("application/json", json.dumps({"prompt": "Swap hat", "image": "data:image/png;base64," + base64.b64encode(b"ABC").decode()}).encode())
    assert jflds["prompt"] == "Swap hat" and len(jimgs) == 1 and jimgs[0][0] == b"ABC", (jflds, jimgs)

    # 7. _coerce_edit_image_urls handles list[str], JSON-stringified lists, Python literals, nested strings & bare URLs
    p_path = Path.home() / "AI" / "open-webui" / "patch_agent_names.py"
    spec = importlib.util.spec_from_file_location("patch_agent_names", p_path)
    pan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pan)
    a_url, b_url = "/api/v1/files/a/content", "/api/v1/files/b/content"
    assert pan._coerce_edit_image_urls([a_url]) == [a_url]
    assert pan._coerce_edit_image_urls(json.dumps([a_url])) == [a_url]
    assert pan._coerce_edit_image_urls(f"['{a_url}']") == [a_url]
    assert pan._coerce_edit_image_urls([json.dumps([a_url, b_url])]) == [a_url, b_url]
    assert pan._coerce_edit_image_urls(a_url) == [a_url]


def test_voice_tts_routing_and_chunking():
    voice_dir = Path.home() / "AI" / "voice-tts"
    sys.path.insert(0, str(voice_dir))
    import text_chunks
    import voice_tts

    # 1. Positive EN & FR narration triggers in auto mode
    for pos in (
        "Read this story in my voice:\nOnce upon a time, a tiny fox lived under an oak tree.",
        "Narrate this in my voice (compare both):\nOnce upon a time...",
        "Lis cette histoire avec ma voix:\nIl était une fois un petit renard.",
        "Raconte cette histoire avec ma voix française:\nDans la forêt endormie...",
        "Compare Qwen3 and Chatterbox on this story:\nOnce upon a time there was a dragon.",
    ):
        assert voice_tts.is_voice_narration_request(pos) is True, f"Missed positive voice intent: {pos}"
        tier, _, forced = router.decide({"model": "auto", "messages": [{"role": "user", "content": pos}]}, "openai")
        assert tier == "voice" and forced is True, (tier, pos)

    # 2. Negative guards: questions, discussions, code, and Open WebUI housekeeping must NOT trigger voice TTS
    for neg in (
        "How can I clone my voice locally?",
        "Any local LLM that could clone my voice for my podcast?",
        "Write a Python script to narrate a story in my voice",
        "Explain how Qwen3-TTS and Chatterbox compare",
        "### Task:\nGenerate a concise title for the conversation",
    ):
        assert voice_tts.is_voice_narration_request(neg) is False, f"False positive voice intent: {neg}"

    # Direct model pick works for stories, never for Open WebUI housekeeping
    is_v, _ = router.is_voice_chat_request({"model": "voice", "messages": [{"role": "user", "content": "Hello bunny."}]}, "openai")
    assert is_v is True
    is_task, _ = router.is_voice_chat_request({"model": "voice", "messages": [{"role": "user", "content": "### Task:\nTitle"}]}, "openai")
    assert is_task is False

    # 3. Story back-reference resolution
    msgs = [
        {"role": "user", "content": "Write a tiny bedtime story"},
        {"role": "assistant", "content": "Once upon a time there was a sleepy little owl who loved counting silver stars in the dark winter sky every night before bed."},
        {"role": "user", "content": "Read that story in my voice"},
    ]
    resolved = router.Handler._resolve_story_input(msgs)
    assert "sleepy little owl" in resolved, resolved

    # 4. text_chunks EN/FR detection, option header parsing & chunking
    assert text_chunks.detect_language("Il était une fois, un petit chat qui rêvait sous la lune.") == "fr"
    assert text_chunks.detect_language("Once upon a time, a little cat dreamed beneath the moon.") == "en"
    opts = text_chunks.parse_story_request("Compare both in French exaggeration 0.7:\nIl était une fois un loup.")
    assert opts["engine"] == "both" and opts["language"] == "fr" and opts["exaggeration"] == 0.7, opts
    assert opts["story_text"] == "Il était une fois un loup.", opts
    prose_keep = "Once upon a time, this is the story of a tiny fox.\nHe lived in the deep green woods."
    assert text_chunks.parse_story_request(prose_keep)["story_text"] == prose_keep
    chunks = text_chunks.chunk_story(
        "# The Little Fox\n\nMr. Fox walked through the forest. He saw 3.5 glowing berries! \"Look!\" he whispered.\n\nThen he went home.",
        language="en",
        target_chars=80,
        max_chars=120,
    )
    assert len(chunks) >= 3 and all(len(c["text"]) <= 120 for c in chunks), chunks
    assert chunks[0]["is_heading"] is True and chunks[0]["pause_after_ms"] == 850

    # 5. HTTP byte range helper for audio playback/scrubbing
    assert html_files._parse_byte_range("bytes=0-99", 1000) == (0, 99)
    assert html_files._parse_byte_range("bytes=500-", 1000) == (500, 999)
    assert html_files._parse_byte_range("bytes=-200", 1000) == (800, 999)
    assert html_files._parse_byte_range("bytes=2000-2500", 1000) == "invalid"

    # 6. Inline <audio> markdown formatting for Open WebUI + Qwen3 default
    single_res = {
        "title": "The Little Fox",
        "language": "en",
        "voice": "my-voice-en",
        "has_transcript": True,
        "word_count": 20,
        "chunk_count": 2,
        "player_url": "https://example.ts.net:8443/podcast/fox/index.html",
        "tracks": [{
            "engine": "qwen3",
            "label": "Qwen3-TTS 1.7B",
            "filename": "story-qwen3.mp3",
            "url": "https://example.ts.net:8443/podcast/fox/story-qwen3.mp3",
            "duration_str": "8s",
            "elapsed_str": "4.1s",
        }],
    }
    md_single = voice_tts.format_narration_markdown(single_res)
    assert "\n\n<div><audio>https://example.ts.net:8443/podcast/fox/story-qwen3.mp3</audio></div>" in md_single, md_single
    assert "[Download story-qwen3.mp3](https://example.ts.net:8443/podcast/fox/story-qwen3.mp3?download=1)" in md_single, md_single
    assert "A/B Compare" not in md_single, md_single

    dual_res = dict(single_res, tracks=single_res["tracks"] + [{
        "engine": "chatterbox",
        "label": "Chatterbox Multilingual",
        "filename": "story-chatterbox.mp3",
        "url": "https://example.ts.net:8443/podcast/fox/story-chatterbox.mp3",
        "duration_str": "8s",
        "elapsed_str": "5.0s",
    }])
    md_dual = voice_tts.format_narration_markdown(dual_res)
    assert "A/B Compare" in md_dual and md_dual.count("<div><audio>") == 2, md_dual

    assert '"default_engine": "qwen3"' in (voice_dir / "server.py").read_text(encoding="utf-8")


def test_tool_call_dedup_and_loop_cut():
    import io
    from unittest.mock import patch

    # 1. Canonical signature ignores key order and whitespace
    s1 = router._canonical_tool_sig("read_email_thread", '{"a": 1, "b": 2}')
    s2 = router._canonical_tool_sig("read_email_thread", '{"b":2,"a":1}')
    s3 = router._canonical_tool_sig("read_email_thread", '{"a": 1, "b": 3}')
    assert s1 == s2 and s1 != s3, (s1, s2, s3)

    # 2. Non-streaming anthropic_to_openai deduplicates 30 repeated blocks and obeys max_tool_calls=6
    blocks = []
    for rep in range(3):
        for k in range(1, 11):
            blocks.append({
                "type": "tool_use",
                "id": f"tu_{rep}_{k}",
                "name": "read_email_thread",
                "input": {"thread_id": f"th_{k:02d}"},
            })
    assert len(blocks) == 30
    oai = router.anthropic_to_openai({"content": blocks, "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 200}}, "qwen3.6-35b-a3b", max_tool_calls=6)
    tcs = oai["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 6, f"Expected 6 deduplicated tool_calls, got {len(tcs)}"
    assert [json.loads(tc["function"]["arguments"])["thread_id"] for tc in tcs] == [f"th_{k:02d}" for k in range(1, 7)]

    # 3. Streaming _forward_translated buffers tool blocks, drops duplicates, and cuts runaway loop cleanly
    sse_events = [{"type": "message_start", "message": {"usage": {"input_tokens": 500}}}]
    # Emit 3 unique calls followed by a repeat of call #1 (should keep 3 unique and cut immediately on repeat)
    for bidx, tid in enumerate(["th_01", "th_02", "th_03", "th_01", "th_02", "th_03"], start=1):
        sse_events.append({
            "type": "content_block_start",
            "index": bidx,
            "content_block": {"type": "tool_use", "id": f"call_{bidx}", "name": "read_email_thread", "input": {}},
        })
        sse_events.append({
            "type": "content_block_delta",
            "index": bidx,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps({"thread_id": tid})},
        })
        sse_events.append({"type": "content_block_stop", "index": bidx})
    sse_bytes = "".join(f"data: {json.dumps(ev)}\n\n" for ev in sse_events).encode("utf-8")

    class _FakeSock:
        def settimeout(self, t):
            pass

    class _FakeResp:
        status = 200
        def __init__(self, raw):
            self._bio = io.BytesIO(raw)
        def read1(self, n):
            return self._bio.read(n)

    class _FakeConn:
        def __init__(self, raw):
            self.sock = _FakeSock()
            self._resp = _FakeResp(raw)
        def request(self, *a, **kw):
            pass
        def getresponse(self):
            return self._resp
        def close(self):
            pass

    written = io.BytesIO()
    recorded = {}

    class _StreamH(router.Handler):
        def __init__(self):
            self.connection = None
            self.headers = {}
            self.wfile = written
        def _ensure_sse_headers(self, **kw):
            pass
        def _emit_owui_preamble(self, entry):
            return {}
        def _clear_owui_loading_status(self, entry, ui_meta):
            pass
        def _chunk(self, b):
            self.wfile.write(b)
        def _record(self, entry, abody, payload, started, status):
            recorded["entry"] = dict(entry)
            recorded["status"] = status

    entry = {"tier": "deep", "asked": "auto", "_owui_stream": True}
    body = {"model": "qwen3.6-35b-a3b", "stream": True, "messages": [{"role": "user", "content": "check my emails"}], "tools": [{"type": "function", "function": {"name": "read_email_thread", "parameters": {"type": "object", "properties": {}}}}]}
    with patch.object(router.http.client, "HTTPConnection", return_value=_FakeConn(sse_bytes)), \
         patch.object(router.northcua_harness, "socket_disconnected", return_value=False), \
         patch.object(router.northcua_harness, "_close_http_conn", lambda c: None):
        _StreamH()._forward_translated(body, "qwen3.6-35b-a3b", 4096, entry)

    out_lines = [l[6:] for l in written.getvalue().decode("utf-8").splitlines() if l.startswith("data: ") and l != "data: [DONE]"]
    chunks = [json.loads(l) for l in out_lines]
    emitted_calls = [tc for ch in chunks for tc in (ch["choices"][0]["delta"].get("tool_calls") or [])]
    finishes = [ch["choices"][0]["finish_reason"] for ch in chunks if ch["choices"][0].get("finish_reason")]
    assert len(emitted_calls) == 3, f"Expected 3 unique streamed calls before cut, got {len(emitted_calls)}: {emitted_calls}"
    assert [json.loads(tc["function"]["arguments"])["thread_id"] for tc in emitted_calls] == ["th_01", "th_02", "th_03"]
    assert finishes == ["tool_calls"], finishes
    assert "3 kept, 1 dup dropped" in recorded["entry"].get("tool_loop_cut", ""), recorded["entry"]
    assert recorded["status"] == 200, recorded


def test_post_image_turn2_and_single_file_html_routing():
    """Verify Round 2 after generate_image disables re-calling and single-file HTML apps avoid quick/fast downgrade."""
    import io
    from unittest.mock import patch
    # 1. Single-file HTML app build request promoted out of LFM 'quick' -> 'logic_ui'
    pomodoro = "Build a self-contained single-file HTML Pomodoro timer with start, pause, reset buttons and a visual countdown ring."
    router._coding_route_cache.clear()
    with patch.object(router, "lfm_json", return_value={"shape": "chat", "domain": "quick"}):
        info = router.classify_coding_with_lfm(pomodoro)
    assert info["shape"] == "chat" and info["domain"] == "logic_ui", info

    # 2. Post-image Turn 2 sets tool_choice='none' and injects plain-text reply instruction
    ow_tools = [
        {"type": "function", "function": {"name": "get_current_timestamp", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "calculate_timestamp", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "generate_image", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}}},
        {"type": "function", "function": {"name": "edit_image", "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}, "image_urls": {"type": "array"}}}}},
    ]
    b_turn2 = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [
            {"role": "user", "content": "Generate an image of a sunlit Parisian bookshop"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "generate_image", "arguments": '{"prompt":"bookshop"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"status": "success", "images": ["/api/v1/files/abc/content"]})},
        ],
    }
    captured = {}

    class _H(router.Handler):
        def __init__(self):
            pass

        def _forward(self, path, b_out, entry):
            captured["body"] = b_out
            return "ok"

    _H()._dispatch("/v1/chat/completions", "openai", b_turn2, "fast", router.MODELS["fast"], False, None, [], {})
    assert captured["body"].get("tool_choice") == "none", captured["body"]
    sys_txt = " ".join(str(m.get("content")) for m in captured["body"]["messages"] if m.get("role") == "system")
    assert "already been generated" in sys_txt and "never output JSON" in sys_txt, sys_txt

    # 2b. Failed generate_image + sibling tool success must NOT trigger post_image lock
    b_turn2_img_err_sibling_ok = {
        "model": "auto",
        "tools": [dict(t) for t in ow_tools],
        "messages": [
            {"role": "user", "content": "Generate an image of a sunlit Parisian bookshop"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "generate_image", "arguments": '{"prompt":"bookshop"}'}},
                    {"id": "c2", "function": {"name": "get_current_timestamp", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"status": "error", "error": "backend failed"})},
            {"role": "tool", "tool_call_id": "c2", "content": json.dumps({"status": "success", "timestamp": 1234567890})},
        ],
    }
    captured.clear()
    _H()._dispatch("/v1/chat/completions", "openai", b_turn2_img_err_sibling_ok, "fast", router.MODELS["fast"], False, None, [], {})
    assert captured["body"].get("tool_choice") != "none", captured["body"]
    err_sys_txt = " ".join(str(m.get("content")) for m in captured["body"]["messages"] if m.get("role") == "system")
    assert "already been generated" not in err_sys_txt, err_sys_txt

    # 3. Direct OpenAI _forward runs _html_footer_with_repair on </html> answers without tool calls
    html_sse = (
        'data: {"id":"1","choices":[{"index":0,"delta":{"content":"<!DOCTYPE html><html><body>Hi</body></html>"},"finish_reason":null}]}\n\n'
        'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":12}}\n\n'
        "data: [DONE]\n\n"
    ).encode("utf-8")

    class _FR:
        def __init__(self, raw):
            self.status = 200
            self._buf = io.BytesIO(raw)
        def getheader(self, name, default=None):
            return "text/event-stream" if name.lower() == "content-type" else default
        def getheaders(self):
            return [("Content-Type", "text/event-stream")]
        def read1(self, n):
            return self._buf.read(n)
    class _FC:
        def __init__(self, raw):
            self.sock = type("_S", (), {})()
            self._r = _FR(raw)
        def request(self, *a, **kw):
            pass
        def getresponse(self):
            return self._r
        def close(self):
            pass

    out_buf = io.BytesIO()
    class _SH(router.Handler):
        def __init__(self):
            self.connection = None
            self.headers = {}
            self.wfile = out_buf
            self._sse_started = True
        def send_response(self, *a, **kw):
            pass
        def send_header(self, *a, **kw):
            pass
        def end_headers(self):
            pass
        def _ensure_sse_headers(self, **kw):
            pass
        def _emit_owui_preamble(self, entry):
            return {}
        def _clear_owui_loading_status(self, entry, ui_meta):
            pass
        def _chunk(self, b):
            self.wfile.write(b)
        def _record(self, entry, b, payload, started, status):
            pass
        @classmethod
        def _html_footer_with_repair(cls, body, text, model, entry, emit_fn=None, status_fn=None):
            return "\n\n[HTML_FOOTER_CHECKED]"

    with patch.object(router.http.client, "HTTPConnection", return_value=_FC(html_sse)), \
         patch.object(router.northcua_harness, "socket_disconnected", return_value=False):
        _SH()._forward("/v1/chat/completions", {"model": "fast", "stream": True, "messages": [{"role": "user", "content": "hi"}]}, {"tier": "fast", "model": router.MODELS["fast"]})
    assert "[HTML_FOOTER_CHECKED]" in out_buf.getvalue().decode("utf-8", "replace")


def main():
    tests = [
        test_openai_to_anthropic_tools_roundtrip,
        test_openai_to_anthropic_dropped_tools_fallback,
        test_anthropic_to_openai_tool_calls,
        test_agent_overflow_to_pro_both_doors,
        test_coding_brief_modes,
        test_surgical_merge_and_error_excerpts,
        test_zero_benchmark_hardcoding,
        test_win_job_object_helper,
        test_image_generation_routing_and_tool_preservation,
        test_voice_tts_routing_and_chunking,
        test_tool_call_dedup_and_loop_cut,
        test_post_image_turn2_and_single_file_html_routing,
    ]
    for fn in tests:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\nALL {len(tests)}/{len(tests)} OFFLINE SMART ROUTER UNIT TESTS PASSED")


if __name__ == "__main__":
    main()
