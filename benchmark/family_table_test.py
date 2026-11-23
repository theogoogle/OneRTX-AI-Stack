"""
Offline & deterministic test suite for the 100% local Family Table pipeline.
Uses strictly generic fixture data (no personal info) so it is safe to sync to Git.

Run:
  python -I -X utf8 %USERPROFILE%\\.lmstudio\\benchmark\\family_table_test.py
"""
from __future__ import annotations

import email
import email.header
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

HOME = Path.home()
FT_DIR = HOME / "AI" / "family-table"
SCRIPTS_DIR = HOME / ".lmstudio" / "scripts"
for p in (str(FT_DIR), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import google_connector as gc  # noqa: E402
import pipeline as ft  # noqa: E402
import router  # noqa: E402


def _example_cfg() -> dict:
    return json.loads((FT_DIR / "config.example.json").read_text(encoding="utf-8"))


def _sample_met(is_weekend: bool = False) -> dict:
    if is_weekend:
        return {
            "file_date": "2026-11-22",
            "date_long": "Sunday 22 November 2026",
            "subject_date": "Sun 22 Nov",
            "dow_idx": 0,
            "is_weekend": True,
            "edition_label": "Weekend edition",
            "week_title": "The weekend ahead",
            "next_edition": "Monday, 8:00 am",
            "children": [
                {"name": "Child One", "tag": "Age 10 · Year 6"},
                {"name": "Child Two", "tag": "22 months"},
            ],
            "week_days": [
                {"iso": "2026-11-22", "day": "SUN", "num": "22", "weekday": "Sunday", "default_pickup": ""},
                {"iso": "2026-11-23", "day": "MON", "num": "23", "weekday": "Monday", "default_pickup": "Parent A"},
            ],
            "weather": {
                "high": "10°",
                "summary": "Sunny intervals",
                "at8": "7°",
                "rain": "10%",
                "sunrise": "7:28 am",
                "sunset": "4:02 pm",
                "tomorrow": "Monday: 8° · Sunny",
                "source": "Met Office · London",
            },
        }
    return {
        "file_date": "2026-11-23",
        "date_long": "Monday 23 November 2026",
        "subject_date": "Mon 23 Nov",
        "dow_idx": 1,
        "is_weekend": False,
        "edition_label": "Breakfast edition",
        "week_title": "The week ahead",
        "next_edition": "Tuesday, 8:00 am",
        "children": [
            {"name": "Child One", "tag": "Age 10 · Year 6"},
            {"name": "Child Two", "tag": "22 months"},
        ],
        "week_days": [
            {"iso": f"2026-11-{23 + i}", "day": d, "num": str(23 + i), "weekday": w, "default_pickup": p}
            for i, (d, w, p) in enumerate([
                ("MON", "Monday", "Parent A"),
                ("TUE", "Tuesday", "Parent B"),
                ("WED", "Wednesday", "Parent A"),
                ("THU", "Thursday", "Parent B"),
                ("FRI", "Friday", "Parent A"),
                ("SAT", "Saturday", ""),
                ("SUN", "Sunday", ""),
            ])
        ],
        "weather": {
            "high": "8°",
            "summary": "Sunny",
            "at8": "3°",
            "rain": "under 5%",
            "sunrise": "7:32 am",
            "sunset": "4:01 pm",
            "tomorrow": "Tuesday: 9° · Cloudy",
            "source": "Met Office · London",
        },
    }


def _sample_edition(is_weekend: bool = False) -> dict:
    met = _sample_met(is_weekend)
    if is_weekend:
        week = [
            {
                "day": "SUN",
                "num": "22",
                "text": "Morning sports club 10:00–11:30. Open afternoon under sunny skies. **Suggestion:** Visit Kew Gardens for a crisp riverside stroll.",
            },
            {"day": "MON", "num": "23", "text": "Back to school. Spelling check and PE.", "pickup": "Parent A"},
        ]
        town = [
            {
                "title": "Historic Castle and Hall",
                "text": "Historic mansion and medieval hall. **Open Sat–Sun, winter season Nov – Mar, 10:00–16:00.** Free with your membership.",
                "where": "Heritage Pass · Greenwich",
            },
            {
                "title": "City Science Space Show",
                "text": "Hands-on rocket exhibits for ages 5–10. **Runs 15 Nov – 10 Jan.**",
                "where": "Central Museum District",
            },
            {
                "title": "Riverside Winter Trail",
                "text": "Riverside lantern walk for all ages. **Runs 20 Nov – 3 Jan.**",
                "where": "Riverside Park",
            },
            {
                "title": "Puppet Theatre Story",
                "text": "Cosy indoor theatre for toddlers and older siblings. **Runs 18 Nov – 20 Dec.**",
                "where": "Arts Quarter",
            },
        ]
        school_run = None
    else:
        week = [
            {"day": d["day"], "num": d["num"], "text": f"Planned events for {d['weekday']}.", **({"pickup": d["default_pickup"]} if d["default_pickup"] else {})}
            for d in met["week_days"]
        ]
        town = [
            {
                "title": "Botanic Villa and Gardens",
                "text": "Historic villa and playground. **Open daily, winter hours 10:00–16:00.** Free with your membership.",
                "where": "Heritage Pass · West Park",
            },
            {
                "title": "Design Family Studio",
                "text": "Drop-in crafts in the courtyard. **Runs 1 Nov – 3 Jan.**",
                "where": "Central Museum District",
            },
            {
                "title": "Natural Wildlife Photo Show",
                "text": "Award-winning animal photography. **Runs 10 Oct – 25 Jun.**",
                "where": "Natural History Hall",
            },
        ]
        school_run = {
            "note": "Mornings: Parent A drops off at 8:30.",
            "rows": [
                ["Mon", "3:30", "Parent A", "4:00", "Parent B"],
                ["Tue", "3:30", "Parent B", "4:00", "Parent A"],
                ["Wed", "3:30", "Parent A", "4:00", "Parent B"],
                ["Thu", "3:30", "Parent B", "4:00", "Parent A"],
                ["Fri", "3:30", "Parent A", "4:00", "Parent B"],
            ],
        }
    return {
        "lede": "Crisp winter sunshine sets up an active day ahead.",
        "week": week,
        "school_desk": [
            {"title": "Weekly maths check", "text": "Review multiplication tables and fractions before Monday morning."}
        ],
        "kids": [
            {"name": "Child One", "tag": "wrong-tag-should-be-locked", "text": "Ready for the science fair project and football practice."},
            {"name": "Child Two", "tag": "wrong-tag", "text": "Loved painting and music circle at nursery this week."},
        ],
        "watch": [
            {"lead": "Return trip form:", "text": "Sign and hand in the winter museum permission slip by Wednesday."}
        ],
        "school_run": school_run,
        "coming_up_note": "Key dates, next 4 weeks.",
        "coming_up": [
            {"day": "SAT", "num": "28", "when": "In 6 days", "text": "Winter concert at 4:00 pm."}
        ],
        "talk": [
            "What gadget would you bolt onto a Mars rover?",
            "Which ocean creature would make the funniest pet?",
            "What dish should we cook together this weekend?",
        ],
        "news": [
            {"title": "Rover spots organic molecules", "text": "Scientists announced new Martian rock findings this week.", "source": "NASA"},
            {"title": "Deep-sea coral reef mapped", "text": "Marine biologists mapped a thriving reef off the coast.", "source": "ScienceDaily"},
            {"title": "Solar ferry breaks record", "text": "A quiet electric ferry crossed the channel on solar power.", "source": "BBC News"},
        ],
        "town": town,
    }


def test_metoffice_script():
    cfg_path = FT_DIR / "config.example.json"
    met = ft.fetch_metoffice("--today", cfg_path)
    assert met["file_date"] and len(met["file_date"]) == 10
    assert met["weather"]["high"] is not None, f"Missing Met Office high: {met['weather']}"
    assert met["weather"]["at8"] is not None, f"Missing Met Office 8am temp: {met['weather']}"
    assert len(met["children"]) == 2
    assert "Year 6" in met["children"][0]["tag"]
    assert "months" in met["children"][1]["tag"]
    print(f"  [PASS] fetch_metoffice.js live ({met['file_date']}, high={met['weather']['high']}, 8am={met['weather']['at8']})")


def test_lock_and_clean_audit():
    cfg = _example_cfg()
    memory = {"excluded_permanent": [{"title": "Old Attended Place", "status": "attended"}], "excluded_recent_14d": []}
    for is_wknd in (False, True):
        met = _sample_met(is_wknd)
        sample = _sample_edition(is_wknd)
        if is_wknd:
            sample["coming_up"] = [
                {"day": "MON", "num": "02", "when": "In 99 days", "text": "Dec birthday"},
                {"day": "THU", "num": "06", "when": "In 99 days", "text": "Dec playdate"},
            ]
            sample["coming_up_note"] = "Dumped list of events that should be replaced"
            sample["watch"][0]["lead"] = "**Bold Lead:**"
            sample["town"][0]["where"] = "<Heritage Pass> · Greenwich"
        ed = ft.lock_deterministic_fields(sample, met, cfg)
        assert ed["kids"][0]["tag"] == "Age 10 · Year 6"
        assert ed["kids"][1]["tag"] == "22 months"
        assert ed["weather"]["at8"] == met["weather"]["at8"]
        if is_wknd:
            assert ed["coming_up"][0]["day"] == "WED" and ed["coming_up"][0]["num"] == "2 Dec" and ed["coming_up"][0]["when"] == "In 10 days", ed["coming_up"][0]
            assert ed["coming_up"][1]["day"] == "SUN" and ed["coming_up"][1]["num"] == "6 Dec" and ed["coming_up"][1]["when"] == "In 14 days", ed["coming_up"][1]
            assert ed["coming_up_note"] == "Birthdays, parties and key dates, next 4 weeks."
            assert "**" not in ed["watch"][0]["lead"]
            assert "<" not in ed["town"][0]["where"] and ">" not in ed["town"][0]["where"]
            assert ed["week"][1].get("pickup") == "Parent A"
        issues = ft.audit_edition(ed, met, cfg, memory)
        assert not issues, f"Unexpected issues (weekend={is_wknd}): {issues}"
    print("  [PASS] lock_deterministic_fields (incl. coming_up weekday fixer & angle-bracket/lead cleanup) + clean audit")


def test_audit_catches_all_violations():
    cfg = _example_cfg()
    cfg["audit"]["banned_kid_filler_terms"] = ["piano session"]
    met = _sample_met(is_weekend=True)
    ed = ft.lock_deterministic_fields(_sample_edition(is_weekend=True), met, cfg)
    # Inject 8 distinct violations
    ed["week"][0]["text"] = "Morning sports club. Open afternoon."  # missing **Suggestion:**
    ed["kids"][1]["text"] += " Routine piano session on Tuesday."  # banned filler
    ed["town"][0]["where"] = "Greenwich"  # missing Heritage Pass
    ed["town"][1]["text"] = "Fun exhibit downtown with a blue plaque outside."  # missing run date + blue plaque
    ed["town"][2]["title"] = "Kew Gardens Christmas Trail"  # repeats recent memory
    ed["news"][0]["text"] += " More details [1]."  # citation bracket
    ed["watch"][0]["text"] += " Contact test@example.com about job interview."  # email + interview privacy leak

    memory = {
        "excluded_permanent": [],
        "excluded_recent_14d": [{"title": "Kew Gardens Christmas", "where": "Kew", "date": "2026-11-18"}],
    }
    issues = ft.audit_edition(ed, met, cfg, memory)
    joined = "\n".join(issues).lower()
    for expected in ("suggestion", "piano session", "heritage pass", "blue plaque", "missing explicit run dates", "repeats excluded", "citation brackets", "privacy violation"):
        assert expected in joined, f"Audit failed to catch `{expected}`! Got:\n{joined}"
    print(f"  [PASS] audit_edition caught all {len(issues)} injected violations")


def test_broadsheet_playwright_render_and_mime():
    cfg = _example_cfg()
    met = _sample_met(is_weekend=False)
    ed = ft.lock_deterministic_fields(_sample_edition(is_weekend=False), met, cfg)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ed_path = tdp / "edition.json"
        ed_path.write_text(json.dumps(ed, ensure_ascii=False, indent=2), encoding="utf-8")
        info = ft.render_broadsheet(ed_path, tdp, FT_DIR / "config.example.json")
        assert info.get("engine") == "playwright", f"Expected playwright engine, got {info}"
        pdf_p = Path(info["pdf"])
        png_p = Path(info["preview"])
        assert pdf_p.is_file() and pdf_p.stat().st_size > 20_000
        assert png_p.is_file() and png_p.stat().st_size > 40_000
        assert isinstance(info.get("columns"), list) and len(info["columns"]) == 3

        body_md = ft.build_email_body(ed)
        assert "**Around town:**" in body_md
        raw_mime = gc.build_draft_mime(["a@example.com", "b@example.com"], "The Family Table · Mon 23 Nov", body_md, pdf_p)
        parsed = email.message_from_bytes(raw_mime)
        subj = str(email.header.make_header(email.header.decode_header(parsed["Subject"])))
        assert subj == "The Family Table · Mon 23 Nov", f"Decoded subject mismatch: {subj!r}"
        assert "a@example.com" in parsed["To"] and "b@example.com" in parsed["To"]
        types = [part.get_content_type() for part in parsed.walk()]
        assert "text/plain" in types and "text/html" in types and "application/pdf" in types
        print(f"  [PASS] build_broadsheet.js (playwright, {info['height']}px, spread={info['spread']}px) + MIME draft")


def test_router_family_table_wiring():
    ft_tool = {"type": "function", "function": {"name": "generate_family_table", "parameters": {"type": "object", "properties": {}}}}
    body = {
        "model": "auto",
        "messages": [{"role": "user", "content": "Generate today's Family Table newsletter"}],
        "tools": [ft_tool],
    }
    rc = router.route_check(body)
    assert rc.get("dispatch") == "chat" and rc.get("tier") == "deep", f"Bad route_check: {rc}"
    tier, reason, forced = router.decide(body, "openai")
    assert tier == "deep" and forced and "Family Table" in reason, f"Bad decide: {(tier, reason, forced)}"
    assert router.add_live_info(body, "openai", "deep", reason) is None
    internal_body = {"model": "deep", "_internal_family_table": True, "messages": [{"role": "user", "content": "write json"}]}
    assert router.add_live_info(internal_body, "openai", "deep", "forced") is None
    print("  [PASS] router.py FAMILY_TABLE_TOOLS + _internal_family_table wiring")


def test_google_workspace_routing():
    gw_tools = [
        {"type": "function", "function": {"name": n, "parameters": {"type": "object", "properties": {}}}}
        for n in ("list_upcoming_events", "search_emails", "read_email_thread", "create_email_draft")
    ]
    positives = [
        "What's on my calendar this week?",
        "List upcoming events for the next 14 days",
        "Any recent emails from school?",
        "Check my inbox for unpaid invoices",
        "If you look at my emails, can you list all the persons I interviewed with at ExampleCorp?",
        "Can you identify some emails from ExampleCorp?",
        "Look through my emails for invoices",
        "Who emailed me about the nursery fees?",
        "Do I have any meetings tomorrow?",
        "Draft an email to Alice about Saturday's lunch",
        "Qu'est-ce que j'ai sur mon agenda cette semaine ?",
        "Vérifie mes emails récents",
        "Identifie les emails concernant la crèche",
    ]
    for q in positives:
        body = {"model": "auto", "messages": [{"role": "user", "content": q}], "tools": gw_tools}
        assert router.has_google_workspace_intent(body), f"Missed positive GW query: {q!r}"
        rc = router.route_check(body)
        assert rc.get("dispatch") == "chat" and rc.get("tier") == "deep", f"Bad GW route_check for {q!r}: {rc}"
        tier, reason, forced = router.decide(body, "openai")
        assert tier == "deep" and forced and "Google Workspace" in reason, f"Bad GW decide for {q!r}: {(tier, reason, forced)}"
        assert router.add_live_info(body, "openai", "deep", reason) is None

    negatives = [
        "Explain how black holes form",
        "Write a Python script to parse an email MIME file",
        "Build a React calendar component with drag and drop",
        "What is the weather in London tomorrow?",
        "Proofread my email before I send it: Hi Alex, thanks for sending the slides.",
        "Rewrite this email to sound more polite",
        "How do I change my email password?",
        "Explain how SMTP email protocols work",
        "Write a cold outreach email template",
    ]
    for q in negatives:
        body = {"model": "auto", "messages": [{"role": "user", "content": q}], "tools": gw_tools}
        assert not router.has_google_workspace_intent(body), f"False positive GW query: {q!r}"

    # Follow-up in a chat where search_emails already ran
    followup = {
        "model": "auto",
        "tools": gw_tools,
        "messages": [
            {"role": "user", "content": "Check my inbox"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search_emails", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{}"},
            {"role": "assistant", "content": "Found 3 emails."},
            {"role": "user", "content": "Read the second one please"},
        ],
    }
    assert router.has_google_workspace_intent(followup), "Failed follow-up GW detection"
    print(f"  [PASS] router.py GOOGLE_WORKSPACE_TOOLS ({len(positives)} positives, {len(negatives)} negatives, follow-up)")


def test_pipeline_aborts_on_unresolved_privacy_violation():
    met = _sample_met(is_weekend=False)
    leaked = ft.lock_deterministic_fields(_sample_edition(is_weekend=False), met, _example_cfg())
    leaked["watch"][0]["text"] += " Send payment to leak@example.com (IBAN GB29NWBK60161331926819)."
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        snap_path = tdp / "snap.json"
        snap_path.write_text(
            json.dumps(
                {
                    "metoffice": met,
                    "calendars": {},
                    "gmail": {},
                    "memory": {"excluded_permanent": [], "excluded_recent_14d": []},
                    "research": {},
                    "family_md": "",
                    "ideas_md": "",
                }
            ),
            encoding="utf-8",
        )
        out_dir = tdp / "out"
        with mock.patch.object(ft, "_ask_pro_json", side_effect=[dict(leaked), dict(leaked)]), \
                mock.patch.object(ft, "render_broadsheet", side_effect=AssertionError("render_broadsheet must not run on privacy failure")):
            try:
                ft.run_pipeline(
                    config_path=FT_DIR / "config.example.json",
                    create_draft=False,
                    update_memory=False,
                    enable_vision_qa=False,
                    wait_stack=False,
                    snapshot_in=snap_path,
                    output_dir_override=out_dir,
                )
            except RuntimeError as exc:
                assert "unresolved privacy violation" in str(exc).lower(), str(exc)
            else:
                raise AssertionError("Expected RuntimeError when privacy violation persisted after LLM repair pass")
        assert not list(out_dir.rglob("edition-*.json")), "Unresolved privacy violation edition must not be written to disk"
    print("  [PASS] run_pipeline aborted delivery when privacy violation persisted across LLM repair pass")


def main() -> int:
    tests = [
        test_metoffice_script,
        test_lock_and_clean_audit,
        test_audit_catches_all_violations,
        test_pipeline_aborts_on_unresolved_privacy_violation,
        test_broadsheet_playwright_render_and_mime,
        test_router_family_table_wiring,
        test_google_workspace_routing,
    ]
    for fn in tests:
        fn()
    print(f"\nALL {len(tests)}/{len(tests)} FAMILY TABLE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
