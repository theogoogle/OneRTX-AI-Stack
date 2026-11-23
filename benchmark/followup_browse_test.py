"""Test follow-up handling in Computer use mode (`screen` / `northcua-nano`).

Verifies:
1. First real turn in a chat (even after an initial greeting) launches the browser harness.
2. Follow-ups asking about, comparing, formatting, or translating the previous result are answered from chat (`run_h = False`).
3. Follow-ups asking for something new on the web launch a new harness run (`run_h = True`).
4. Live end-to-end streaming follow-up through `http://127.0.0.1:4000/v1/chat/completions` (`model='screen'`) answers from chat without launching Chrome.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import northcua_harness  # noqa: E402

PRIOR_CHAT = [
    {"role": "user", "content": "What events are on at The O2 on Sunday 8 November 2026?"},
    {
        "role": "assistant",
        "content": (
            "On Sunday 8 November 2026 at The O2 (https://www.theo2.co.uk/events), there are two events:\n"
            "1. **Papa Roach** at The O2 Arena — doors 18:30, tickets from £48.\n"
            "2. **Goran Bregović** at indigo at The O2 — doors 19:00, tickets from £35."
        ),
    },
]

FIRST_TURN_BROWSE = [
    [{"role": "user", "content": "What events are on at The O2 on Sunday 8 November 2026?"}],
    [{"role": "user", "content": "Quels sont les concerts à l'O2 dimanche ?"}],
    [
        {"role": "user", "content": "Bonjour"},
        {"role": "assistant", "content": "Bonjour ! Comment puis-je vous aider ?"},
        {"role": "user", "content": "Donne-moi la météo de Londres sur le site du Met Office"},
    ],
]

FOLLOWUP_CHAT_ONLY = [
    "can you explain that?",
    "which one is cheaper?",
    "which one starts earlier?",
    "put those in a markdown table",
    "translate that into French",
    "what is the price difference between the two?",
    "summarise that in one sentence",
    "who is Goran Bregović?",
    "is indigo at The O2 in the same building as The O2 Arena?",
    "how much would two tickets for each cost in total?",
    "peux-tu expliquer ça ?",
    "lequel est le moins cher ?",
    "mets ce résultat sous forme de tableau",
    "traduis la réponse en français s'il te plaît",
    "quelle est la différence de prix entre les deux ?",
]

FOLLOWUP_NEW_BROWSE = [
    "now check Saturday's events",
    "what about Monday 9 November?",
    "click on Papa Roach and check the ticket availability",
    "open the second event link to see the full schedule",
    "also check the weather in London on Sunday on metoffice.gov.uk",
    "go to https://www.bbc.co.uk/news and check the top story",
    "search Wikipedia for Goran Bregović instead",
    "look up what events are at the Royal Albert Hall instead",
    "what does BBC Weather say for Sunday?",
    "check on the site if there are VIP tickets",
    "maintenant vérifie les événements de samedi",
    "et pour lundi 9 novembre ?",
    "clique sur le premier événement pour voir les détails",
    "vérifie aussi la météo à Londres demain",
    "va sur wikipedia.org et cherche Papa Roach",
]


def check_msg_list(msgs):
    body = {"model": "screen", "messages": msgs}
    last_text = msgs[-1]["content"]
    run_h, _ = northcua_harness.should_run_harness(body, last_text, False)
    return run_h


def main():
    t1_ok = sum(1 for m in FIRST_TURN_BROWSE if check_msg_list(m) is True)
    print(f"=== 1. First real turn launches harness: {t1_ok}/{len(FIRST_TURN_BROWSE)} ===")
    assert t1_ok == len(FIRST_TURN_BROWSE)

    chat_ok = 0
    for q in FOLLOWUP_CHAT_ONLY:
        run_h = check_msg_list(PRIOR_CHAT + [{"role": "user", "content": q}])
        if not run_h:
            chat_ok += 1
        else:
            print(f"  [FALSE BROWSE] {q}")
    print(f"=== 2. Follow-up questions answered from chat (no browser): {chat_ok}/{len(FOLLOWUP_CHAT_ONLY)} ===")
    assert chat_ok == len(FOLLOWUP_CHAT_ONLY)

    browse_ok = 0
    for q in FOLLOWUP_NEW_BROWSE:
        run_h = check_msg_list(PRIOR_CHAT + [{"role": "user", "content": q}])
        if run_h:
            browse_ok += 1
        else:
            print(f"  [MISSED BROWSE] {q}")
    print(f"=== 3. Follow-up web requests launch harness: {browse_ok}/{len(FOLLOWUP_NEW_BROWSE)} ===")
    assert browse_ok == len(FOLLOWUP_NEW_BROWSE)

    # 4. Live multi-turn streaming test through the router
    print("=== 4. Live streaming follow-up through router (model='screen') ===")
    req_body = {
        "model": "screen",
        "stream": True,
        "messages": PRIOR_CHAT + [{"role": "user", "content": "Which one is cheaper and by how much?"}],
    }
    t0 = time.time()
    req = urllib.request.Request(
        "http://127.0.0.1:4000/v1/chat/completions",
        json.dumps(req_body).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    content_parts = []
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ev = json.loads(line[6:])
            delta = ev["choices"][0].get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
    elapsed = round(time.time() - t0, 1)
    answer = "".join(content_parts).strip()
    health = json.loads(urllib.request.urlopen("http://127.0.0.1:4000/health", timeout=5).read())
    last_dec = health["recent_decisions"][0]
    print(f"  -> answered in {elapsed}s (harness={last_dec.get('harness')!r}, model={last_dec.get('model')!r})")
    print(f"  -> answer: {answer[:200]}")
    assert last_dec.get("harness") == "skipped (follow-up answered from chat)", f"Unexpected decision: {last_dec}"
    assert "35" in answer or "13" in answer or "bregovi" in answer.lower(), f"Unexpected answer: {answer}"
    print("\nALL FOLLOW-UP TESTS PASSED!")


if __name__ == "__main__":
    main()
