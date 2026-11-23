"""Benchmark & regression test for the web-search labeller (`lfm3-1.7b` vs `qwen3.5-4b`).

Usage:
  python -I -X utf8 %USERPROFILE%\\.lmstudio\\benchmark\\web_label_test.py
  python -I -X utf8 %USERPROFILE%\\.lmstudio\\benchmark\\web_label_test.py --compare
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import web_search  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

ORIG_WEB = [
    "what is the weather in the next 3 days?",
    "what are the concerts this weekend at the o2 in london",
    "will it rain in Paris tomorrow?",
    "who won the Premier League match yesterday?",
    "what's the latest news on the UK budget?",
    "how much is a Tesla Model 3 in the UK right now?",
    "is the Northern line running today?",
    "what time does the Science Museum close on Sunday?",
    "what new films are out at the cinema this week?",
    "what's the GBP to EUR exchange rate today?",
    "can you look up reviews of the restaurant Dishoom Covent Garden",
    "any exhibitions on at the Tate Modern in November 2026?",
    "who is the current prime minister of Japan?",
    "what are the best toddler activities in London this weekend?",
    "search online for the release date of the next iPhone",
]
ORIG_NO_WEB = [
    "explain how photosynthesis works",
    "write a polite email asking my landlord to fix the boiler",
    "what's a good recipe for banana bread?",
    "translate 'good morning, how are you' into Spanish",
    "what is 17% of 2,340?",
    "give me ideas for a 5 year old's birthday party",
    "why did the Roman Empire fall?",
    "how do I get my toddler to sleep through the night?",
    "fix this python function: def add(a,b) return a+b",
    "summarise the plot of Pride and Prejudice",
    "what's the difference between a stock and a bond?",
    "help me reword this sentence to sound more professional: we cant do it by friday",
    "tell me a joke about cats",
    "how many bones are in the human body?",
    "what should I consider when choosing a mortgage?",
]
TECH_COMPARE_WEB = [
    "if one was running qwen 3.8 27B on their machine, roughly what would it equate to in terms of public models? Gemini, claude, chatgpt, grok etc?",
    "How does Qwen 3.8 27B compare to Claude, Gemini and GPT?",
    "What is the best open-weight local LLM for a 12GB GPU?",
    "How does Gemma 4 12B compare to Llama 4 on benchmarks?",
    "Is the RTX 5070 faster than the RTX 4070 Super?",
    "What is the context window and pricing of Claude 4.5 vs Gemini 2.5 Pro?",
    "How good is DeepSeek R2 compared to ChatGPT?",
    "Que vaut Qwen 3.8 27B par rapport à Gemini, Claude et ChatGPT ?",
    "Which electric car has the longest range in the UK?",
    "What version of Open WebUI is out and what are its new features?",
    "What is the best budget smartphone to buy?",
    "How does Qwen 3.6 35B compare to Gemini Flash on coding benchmarks?",
]
THEORY_NO_WEB = [
    "explain how transformer self-attention works in LLMs",
    "what is the difference between a MoE model and a dense model?",
    "what does 4-bit GGUF quantization mean?",
    "explain the difference between VRAM and system RAM",
    "how does gradient descent work in neural networks?",
    "why does overfitting happen in machine learning?",
    "what is the difference between TCP and UDP?",
    "explain what Python's GIL is",
]

CASES = (
    [(True, "orig", q) for q in ORIG_WEB]
    + [(False, "orig", q) for q in ORIG_NO_WEB]
    + [(True, "tech_web", q) for q in TECH_COMPARE_WEB]
    + [(False, "theory", q) for q in THEORY_NO_WEB]
)


def make_call(model_id: str):
    def call(body):
        b = {"model": model_id, **body}
        req = urllib.request.Request(
            "http://127.0.0.1:1234/v1/chat/completions",
            json.dumps(b).encode(),
            {"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(json.loads(r.read())["choices"][0]["message"]["content"])
    return call


def eval_model(model_id: str, verbose: bool = True) -> tuple[int, int, list[tuple]]:
    call = make_call(model_id)
    web_search.decide(call, "hello")  # warm KV cache
    ok = 0
    times = []
    misses = []
    for want, grp, text in CASES:
        t0 = time.perf_counter()
        d = web_search.decide(call, text)
        ms = (time.perf_counter() - t0) * 1000.0
        times.append(ms)
        good = bool(d.get("web")) == want
        ok += good
        if not good:
            misses.append((grp, want, d.get("web"), text))
        if verbose:
            extra = f"weather {d['place']!r}" if d.get("weather") else repr(d.get("query", ""))
            print(f"{'ok ' if good else 'XX '} [{grp:8s}] want={str(want):5s} got={str(bool(d.get('web'))):5s} {text[:60]:60s} {extra}")
    med = statistics.median(times)
    p95 = sorted(times)[min(len(times) - 1, int(0.95 * len(times)))]
    print(f"\n[{model_id}] {ok}/{len(CASES)} ({100.0 * ok / len(CASES):.1f}%) | median {med:.0f} ms | p95 {p95:.0f} ms")
    return ok, len(CASES), misses


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true", help="Compare lfm3-1.7b vs qwen3.5-4b on all 50 cases")
    ap.add_argument("--model", default="qwen3.5-4b")
    args = ap.parse_args()
    if args.compare:
        for mid in ("lfm3-1.7b", "qwen3.5-4b"):
            eval_model(mid, verbose=(mid == "qwen3.5-4b"))
        return 0
    ok, total, misses = eval_model(args.model, verbose=True)
    if misses:
        print("Misses:")
        for m in misses:
            print(" ", m)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
