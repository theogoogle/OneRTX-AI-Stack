"""Verification suite for Tier-1 zero-regression upgrades & opt-in Gemma 4 native thinking (CPU/offline only)."""
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import northcua_harness  # noqa: E402
import router            # noqa: E402
import web_search        # noqa: E402


def test_thinking_loops_locality():
    # Genuine infinite loop (repeating 420-char reasoning block 8x -> 3,360 chars trailing) must be caught
    block = ("Wait let's inspect the door hinge offset at line 601 and map CSS grid coordinates carefully before editing the geometry node. " * 4)[:420]
    genuine = "Start planning the scene.\n" + (block * 8)
    assert router.thinking_loops(genuine), "Failed to catch genuine trailing loop in router"
    assert northcua_harness._step_thought_loops(genuine), "Failed to catch genuine trailing loop in harness"

    # Non-degenerate 3-pass draft verification (like IFEval 6K-10K chars with differing surroundings) must NOT trigger
    draft = ("Checking rule compliance: confirm zero commas remain, count 4 paragraphs carefully, verify exact closing sentence and bullet wording. " * 3)[:410]
    filler_a = "First pass brainstorm with unique phrasing alpha beta gamma delta epsilon zeta eta theta iota kappa.\n" * 28
    filler_b = "\nSecond pass structural rewrite with distinct vocabulary lambda mu nu xi omicron pi rho sigma tau upsilon.\n" * 30
    multi_pass = filler_a + draft + "\nAdjusting middle lines for tone.\n" + draft + filler_b + draft + "\nNow emitting clean final text without any repetition tail."
    assert len(multi_pass) >= 6000
    assert not router.thinking_loops(multi_pass), "False-killed valid multi-pass reasoning in router"
    assert not northcua_harness._step_thought_loops(multi_pass), "False-killed valid multi-pass reasoning in harness"


def test_software_words_and_math_guard():
    # Physical 'outer shell' (ifeval-286) must not trigger coding/software matching
    assert not router.SOFTWARE_WORDS.search("Grind the outer shell down to bare metal carefully.")
    assert router.SOFTWARE_WORDS.search("Write a shell script to back up PostgreSQL.")
    assert router.SOFTWARE_WORDS.search("Run pytest via bash in the terminal.")

    # Olympiad math must be flagged by MATH_PROBLEM_RE and never hit 'quick' code Q&A
    aime_prompt = (
        "Let $ABC$ be an acute triangle with circumcenter $O$. Find the remainder when $x$ is divided by $1000$. "
        "Put your answer in \\boxed{}."
    )
    assert router.MATH_PROBLEM_RE.search(aime_prompt)
    # Explicit coding script building AIME parser stays software
    assert router.BUILD_REQUEST.search("Write a Python script to solve triangle remainder math problems.")


def test_structured_response_format():
    schema_body = {"model": "think", "response_format": {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}, "max_tokens": 512, "messages": [{"role": "user", "content": "Extract JSON"}]}
    obj_body = {"model": "auto", "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": "Return JSON"}]}
    txt_body = {"model": "auto", "response_format": {"type": "text"}, "messages": [{"role": "user", "content": "Hi"}]}
    assert router.has_structured_response_format(schema_body)
    assert router.has_structured_response_format(obj_body)
    assert not router.has_structured_response_format(txt_body)

    # prepare_openai defaults reasoning_effort='none' for short <=2048 schema calls, preserves explicit client effort
    b1 = dict(schema_body)
    router.prepare_openai(b1, "think", router.MODELS["code"], True)
    assert b1.get("reasoning_effort") == "none", b1

    b2 = {**schema_body, "reasoning_effort": "medium"}
    router.prepare_openai(b2, "think", router.MODELS["code"], True)
    assert b2.get("reasoning_effort") == "medium", b2


def test_cc_overflow_and_background_cap():
    assert router.AGENT_PRO_OVERFLOW_TOKENS == 38000
    huge_tools = [{"name": "bash", "description": "x" * 60000}]
    big_anth = {
        "model": "local-auto",
        "tools": huge_tools,
        "messages": [{"role": "user", "content": "a" * 80000}],
    }
    tier, reason, _ = router.decide(big_anth, "anthropic")
    assert tier == "deep", (tier, reason)


def test_web_search_triggers_and_coding_skip():
    assert not web_search.LIVE_WORDS.search("Who won the defamation case between Johnny Depp and Amber Heard?")
    assert web_search.LIVE_WORDS.search("Who won the Premier League match yesterday?")
    # add_live_info returns None on unforced coding query
    res = router.add_live_info(
        {"model": "fast", "messages": [{"role": "user", "content": "Write a Python script for a Pomodoro timer today"}]},
        "openai",
        "fast",
        "forced by model name",
    )
    assert res is None, res
    # add_live_info returns None on multimodal image analysis unless explicit online search requested
    img_res = router.add_live_info(
        {
            "model": "fast",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                    {"type": "text", "text": "Analyze this table Price/Unit today?"},
                ],
            }],
        },
        "openai",
        "fast",
        "forced by model name",
    )
    assert img_res is None, img_res


def test_gemma_opt_in_thinking_rules():
    # Fast Vision OCR stays think-off; analytical vision math/diagram turns on native thinking
    assert not router.VISION_THINK_RE.search("Extract all items from this receipt into a markdown table.")
    assert router.VISION_THINK_RE.search("Solve the equation and calculate the total revenue in this diagram.")
    assert router.THINK_PHRASES.search("Please think step by step before answering.")
    assert router.THINK_PHRASES.search("Réfléchis pas à pas.")

    # prepare_openai sets >=8192 max_tokens floor and preserves thinking for fast+thinking
    b = {"model": "fast", "messages": [{"role": "user", "content": "Analyze diagram"}]}
    router.prepare_openai(b, "fast", router.MODELS["fast"], True)
    assert "reasoning_effort" not in b and b["max_tokens"] >= 8192, b

    b_off = {"model": "fast", "messages": [{"role": "user", "content": "Hi"}]}
    router.prepare_openai(b_off, "fast", router.MODELS["fast"], False)
    assert b_off.get("reasoning_effort") == "none", b_off


def test_unsloth_gemma_promotion_and_powershell_guidance():
    cfg_root = Path.home() / ".lmstudio" / ".internal" / "user-concrete-model-default-config"
    assert not (cfg_root / "google" / "gemma-4-12b-qat.json").exists(), "Retired google gemma config still exists"
    for rel in (
        ("gemma-4-12b-it-qat.json",),
        ("unsloth", "gemma-4-12b-it-qat.json"),
        ("unsloth", "gemma-4-12B-it-qat-GGUF", "gemma-4-12B-it-qat-UD-Q4_K_XL.gguf.json"),
    ):
        cfg_p = cfg_root.joinpath(*rel)
        assert cfg_p.exists(), f"Missing {cfg_p}"
        cfg = json.loads(cfg_p.read_text(encoding="utf-8"))
        op = {f["key"]: f["value"] for f in cfg["operation"]["fields"]}
        ld = {f["key"]: f["value"] for f in cfg["load"]["fields"]}
        assert "llm.prediction.systemPrompt" not in op, op
        assert op["llm.prediction.temperature"] == 1.0
        assert op["llm.prediction.topKSampling"] == 64
        assert ld["llm.load.contextLength"] == 196608
        assert ld["llm.load.llama.acceleration.offloadRatio"] == 1
        assert ld["llm.load.llama.kCacheQuantizationType"]["value"] == "q8_0"

    assert router.MODELS["fast"] == "gemma-4-12b-it-qat"
    r_tier, _, _ = router.decide({"model": "google/gemma-4-12b-qat", "messages": [{"role": "user", "content": "hi"}]}, "openai")
    assert r_tier == "fast", r_tier

    ps_md = (Path.home() / ".config" / "opencode" / "shell-powershell.md").read_text(encoding="utf-8")
    assert "UTF-16LE" in ps_md and "WriteAllText" in ps_md
    assert "skill" not in ps_md.lower()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL TIER-1 UNIT CHECKS PASSED (7/7)")
