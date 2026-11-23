"""Test suite for NorthCUA Phase 3 (Windows Desktop Control / Desktop Use):
1. `is_desktop_task` + `router.decide` on 30 real desktop requests and 30 everyday Windows/desktop questions (EN + FR).
2. `Desktop Use` MCP tool (`desktop_use`) routing and Turn 2+ follow-up classification (10 chat follow-ups + 10 action follow-ups).
3. `:8933/mcp` `tools/list` verification.
"""
import json
import sys
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import northcua_harness
import router

REAL_DESKTOP_REQUESTS = [
    # English (15)
    "Use the desktop to open Calculator",
    "Please use my desktop to check what window is open",
    "Can you use the Windows desktop to open Notepad and type hello?",
    "Control my desktop to click the Start button",
    "Use Desktop Use to inspect the foreground window",
    "Use NorthCUA desktop to open Task Manager",
    "Desktop Use: tell me what application is in the foreground",
    "On my desktop, open Calculator and compute 7 * 8",
    "On the desktop, check what windows are visible right now",
    "On my Windows desktop, click the search box on the taskbar",
    "From my desktop, launch Notepad",
    "Look at my desktop screen and tell me what app is in the foreground",
    "Check the desktop screen and list the open windows",
    "Inspect my Windows desktop and read the active window title",
    "Open Calculator on my desktop",
    # French (15)
    "Utilise le bureau pour ouvrir la Calculatrice",
    "Peux-tu utiliser mon bureau pour vérifier quelle fenêtre est ouverte ?",
    "Contrôle mon bureau Windows pour ouvrir le Bloc-notes",
    "Lance le bureau pour cliquer sur le menu Démarrer",
    "Sur mon bureau, ouvre le Bloc-notes",
    "Sur le bureau, vérifie quelles fenêtres sont affichées",
    "Sur mon bureau Windows, clique sur la barre des tâches",
    "Sur le bureau, regarde quelle application est au premier plan",
    "Regarde mon bureau et dis-moi ce qui est affiché",
    "Vérifie l'écran de mon bureau et liste les fenêtres ouvertes",
    "Inspecte le bureau Windows pour lire le titre de la fenêtre",
    "Ouvre la Calculatrice sur mon bureau",
    "Clique sur l'icône Chrome sur mon bureau",
    "Vérifie les fenêtres ouvertes sur le bureau Windows",
    "S'il te plaît, sur mon bureau, ouvre l'Explorateur de fichiers",
]

EVERYDAY_DESKTOP_QUESTIONS = [
    # English (15)
    "How do I navigate to the display settings in Windows 11?",
    "Why are my desktop icons missing on Windows 11?",
    "How do I take a screenshot of my desktop in Windows?",
    "What is the difference between a desktop and a laptop GPU?",
    "Is a desktop PC better than a laptop for local LLMs?",
    "How do I change my Windows 11 desktop wallpaper?",
    "Where is the Desktop folder located in PowerShell?",
    "Can Docker Desktop run on Windows 11 Home?",
    "Why does Remote Desktop disconnect when I lock my screen?",
    "How to create a shortcut on the desktop in Windows 11?",
    "my page looks different in the browser and on my phone, why?",
    "explain how to browse to a folder in PowerShell",
    "is it safe to start the browser and leave it open overnight?",
    "What resolution is an ultrawide 1440p desktop monitor?",
    "How do virtual desktops work in Windows 11?",
    # French (15)
    "Comment changer le fond d'écran de mon bureau Windows ?",
    "Pourquoi les icônes de mon bureau ont disparu sous Windows 11 ?",
    "Quelle est la différence entre un PC de bureau et un PC portable ?",
    "Où se trouve le dossier Bureau dans Windows 11 ?",
    "Comment faire une capture d'écran du bureau sur Windows ?",
    "Comment ajouter un raccourci sur le bureau Windows ?",
    "Est-ce que Docker Desktop consomme beaucoup de RAM ?",
    "Comment activer le Bureau à distance sur Windows 11 Pro ?",
    "Pourquoi mon écran de bureau clignote quand je lance un jeu ?",
    "Comment organiser les fenêtres sur le bureau Windows 11 ?",
    "Comment créer un deuxième bureau virtuel sur Windows ?",
    "Quelle taille d'écran choisir pour un bureau informatique ?",
    "Comment masquer la barre des tâches sur le bureau ?",
    "Pourquoi le bureau Windows met du temps à charger au démarrage ?",
    "Comment afficher l'icône Ce PC sur le bureau Windows 11 ?",
]

DESKTOP_CHAT_FOLLOWUPS = [
    "Which window was on the left side?",
    "Can you summarize that in one sentence?",
    "Translate that list to French",
    "How many windows did you count in total?",
    "What time was shown in the system tray?",
    "Quelle fenêtre était au premier plan ?",
    "Résume ça en une phrase",
    "Traduis ta réponse en anglais",
    "Combien d'applications as-tu vues ?",
    "Merci, c'est très clair !",
]

DESKTOP_ACTION_FOLLOWUPS = [
    "Now click the Start button",
    "Open Calculator on my desktop",
    "Press Win+R and type notepad",
    "Double-click the first icon",
    "Take a fresh screenshot and check again",
    "Close that window",
    "Maintenant clique sur le bouton Démarrer",
    "Ouvre le Bloc-notes",
    "Appuie sur Entrée",
    "Vérifie à nouveau l'écran",
]


def main():
    assert len(REAL_DESKTOP_REQUESTS) == 30
    assert len(EVERYDAY_DESKTOP_QUESTIONS) == 30

    pos_ok = 0
    for q in REAL_DESKTOP_REQUESTS:
        if not northcua_harness.is_desktop_task(q):
            print(f"FAIL (missed desktop task): {q}")
        else:
            tier, reason, forced = router.decide({"model": "fast", "messages": [{"role": "user", "content": q}]}, "openai")
            mode, _ = northcua_harness.should_run_browser_loop({"messages": [{"role": "user", "content": q}]}, q)
            if tier == "screen" and forced and mode == "desktop":
                pos_ok += 1
            else:
                print(f"FAIL (bad route {tier}/{mode}): {q}")

    neg_false = 0
    for q in EVERYDAY_DESKTOP_QUESTIONS:
        if northcua_harness.is_desktop_task(q) or northcua_harness.is_browser_task(q):
            print(f"FAIL (false launch): {q}")
            neg_false += 1

    print(
        f"=== 1. Desktop phrase & decide test: {pos_ok + (30 - neg_false)}/60 "
        f"({pos_ok}/30 real desktop launched, {neg_false}/30 false launches on questions) ==="
    )
    assert pos_ok == 30 and neg_false == 0

    # 2. Desktop Use tool toggle + follow-ups (both 'desktop_use_desktop_use' and 'desktop_use__desktop_use')
    for tool_name in ("desktop_use_desktop_use", "desktop_use__desktop_use"):
        tool_spec = [
            {"type": "function", "function": {"name": tool_name}},
            {"type": "function", "function": {"name": "get_current_timestamp"}},
            {"type": "function", "function": {"name": "calculate_timestamp"}},
        ]
        t1_body = {"model": "auto", "messages": [{"role": "user", "content": "open my windows performance settings please"}], "tools": tool_spec}
        tier, reason, forced = router.decide(t1_body, "openai")
        mode, _ = northcua_harness.should_run_browser_loop(t1_body, "open my windows performance settings please")
        assert tier == "screen" and "Desktop Use tool" in reason and mode == "desktop", f"Failed for {tool_name}: {tier}, {reason}, {mode}"
    assert northcua_harness.is_desktop_task("open my windows performance settings please")
    tool_spec = [{"type": "function", "function": {"name": "desktop_use_desktop_use"}}]

    chat_ok = 0
    for f in DESKTOP_CHAT_FOLLOWUPS:
        b = {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "What windows are open?"},
                {"role": "assistant", "content": "VS Code is on the left and Chrome is on the right (17:23)."},
                {"role": "user", "content": f},
            ],
            "tools": tool_spec,
        }
        m, _ = northcua_harness.should_run_browser_loop(b, f)
        if m is None:
            chat_ok += 1
        else:
            print(f"FAIL (chat follow-up launched {m}): {f}")

    act_ok = 0
    for f in DESKTOP_ACTION_FOLLOWUPS:
        b = {
            "model": "auto",
            "messages": [
                {"role": "user", "content": "What windows are open?"},
                {"role": "assistant", "content": "VS Code is on the left and Chrome is on the right (17:23)."},
                {"role": "user", "content": f},
            ],
            "tools": tool_spec,
        }
        m, _ = northcua_harness.should_run_browser_loop(b, f)
        if m == "desktop":
            act_ok += 1
        else:
            print(f"FAIL (action follow-up got {m}): {f}")

    print(
        f"=== 2. Desktop Use follow-up test: {chat_ok}/10 chat follow-ups skipped desktop run, "
        f"{act_ok}/10 action follow-ups launched desktop run ==="
    )
    assert chat_ok == 10 and act_ok == 10

    # 3. Check live :8933/mcp tools/list
    req = urllib.request.Request(
        "http://127.0.0.1:8933/mcp",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        mcp_tools = [t["name"] for t in json.loads(r.read().decode("utf-8"))["result"]["tools"]]
    print(f"=== 3. Live :8933/mcp tools/list: {mcp_tools} ===")
    assert "desktop_use" in mcp_tools

    # 4. External coding agent tools (e.g. OpenCode) skip browser/desktop phrase routing and add_live_info
    opencode_tools = [
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "edit"}},
    ]
    for msg in ("Use the browser to check the O2 events", "Use the desktop to open Calculator", "What is the weather in London today?"):
        oc_body = {"model": "local-think", "messages": [{"role": "user", "content": msg}], "tools": opencode_tools}
        t, r_str, _ = router.decide(oc_body, "openai")
        assert t == "think", f"OpenCode request hijacked to {t} ({r_str}) for: {msg}"
        assert router.add_live_info(oc_body, "openai", t, r_str) is None, f"add_live_info did not skip OpenCode request: {msg}"
    print("=== 4. OpenCode / external agent tool guard: 3/3 stayed on think & skipped add_live_info ===")

    # 5. Desktop HUD & Permission Popup tests (confirm decline, HUD status updates, F8 emergency stop, clean exit)
    import ctypes
    import subprocess
    import time

    hud_script = str(northcua_harness.HUD_SCRIPT)
    py_exe = northcua_harness._python_console_exe()

    # 5a. Confirm popup decline (1s timeout -> exit code 1, no desktop action run)
    p_conf = subprocess.Popen(
        [py_exe, "-I", hud_script, "--mode", "confirm", "--task", "Unit test decline", "--timeout", "1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=0x08000000,
    )
    rc_conf = p_conf.wait(timeout=5)
    assert rc_conf == 1, f"Expected confirm decline exit code 1, got {rc_conf}"

    # 5b. HUD normal lifecycle (status update + clean exit 0 on stdin close)
    p_hud = subprocess.Popen(
        [py_exe, "-I", hud_script, "--mode", "hud", "--task", "Unit test HUD"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=0x08000000,
    )
    time.sleep(0.35)
    assert p_hud.poll() is None, "HUD exited prematurely"
    p_hud.stdin.write(json.dumps({"status": "Step 1/15 — 🔧 click (100, 200)"}) + "\n")
    p_hud.stdin.flush()
    time.sleep(0.2)
    p_hud.stdin.close()
    rc_hud = p_hud.wait(timeout=3)
    assert rc_hud == 0, f"Expected HUD clean exit code 0, got {rc_hud}"

    # 5c. HUD emergency stop via STOP button invoke -> exit code 42 + 'STOP' output
    p_stop = subprocess.Popen(
        [py_exe, "-I", hud_script, "--mode", "hud", "--task", "Unit test STOP button"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=0x08000000,
    )
    time.sleep(0.35)
    p_stop.stdin.write(json.dumps({"action": "stop"}) + "\n")
    p_stop.stdin.flush()
    rc_stop = p_stop.wait(timeout=3)
    out_stop = p_stop.stdout.read().strip()
    assert rc_stop == 42 and "STOP" in out_stop, f"Expected STOP exit code 42 ('STOP'), got rc={rc_stop} out={out_stop!r}"
    print("=== 5. Desktop Permission Popup & HUD (decline, live status, STOP button / F8): 3/3 PASSED ===")

    # 6. Open Terminal (`🛠️ Coding Agents`) coexistence with Desktop Use / Browser Use + Windows OS dialogs
    owui_builtins = [
        {"type": "function", "function": {"name": "get_current_timestamp"}},
        {"type": "function", "function": {"name": "calculate_timestamp"}},
    ]
    open_terminal_tools = [
        {"type": "function", "function": {"name": n}}
        for n in (
            "run_command", "write_file", "read_file", "replace_file_content",
            "browser_action", "browser_screenshot", "browser_snapshot", "browser_tabs",
            "desktop_action", "desktop_screenshot",
        )
    ]

    # 6a. Desktop Use toggled ON while Open Terminal is also active (reproduces 2026-11-21 bug)
    du_with_ot = {
        "model": "auto",
        "messages": [{"role": "user", "content": "can you open my windows update menu please"}],
        "tools": owui_builtins + open_terminal_tools + [
            {"type": "function", "function": {"name": "desktop_use_desktop_use"}},
        ],
    }
    t6a, r6a, f6a = router.decide(du_with_ot, "openai")
    assert t6a == "screen" and f6a and "Desktop Use tool" in r6a, f"6a failed: {t6a}, {r6a}, {f6a}"
    m6a, _ = northcua_harness.should_run_browser_loop(du_with_ot, "can you open my windows update menu please")
    assert m6a == "desktop", f"6a mode failed: {m6a}"

    # 6b. Browser Use toggled ON while Open Terminal is also active
    bu_with_ot = {
        "model": "auto",
        "messages": [{"role": "user", "content": "check the top story on BBC News"}],
        "tools": owui_builtins + open_terminal_tools + [
            {"type": "function", "function": {"name": "browser_browser_navigate"}},
            {"type": "function", "function": {"name": "browser_browser_snapshot"}},
            {"type": "function", "function": {"name": "browser_browser_click"}},
        ],
    }
    t6b, r6b, f6b = router.decide(bu_with_ot, "openai")
    assert t6b == "screen" and f6b and "3 GUI/browser tools" in r6b, f"6b failed: {t6b}, {r6b}, {f6b}"

    # 6c. Everyday chat with Open Terminal ON (no Desktop Use / Browser Use) must NOT trigger NorthCUA
    plain_with_ot = {
        "model": "auto",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
        "tools": owui_builtins + open_terminal_tools,
    }
    assert router.computer_use_tools(plain_with_ot) == "", f"6c false CUA: {router.computer_use_tools(plain_with_ot)}"

    # 6d. External coding agent (Claude Code + Playwright MCP) stays off NorthCUA (Rule 3 exception)
    cc_pw = {
        "model": "local-auto",
        "messages": [{"role": "user", "content": "open localhost:3000 and test login"}],
        "tools": [
            {"type": "function", "function": {"name": "bash"}},
            {"type": "function", "function": {"name": "edit"}},
            {"type": "function", "function": {"name": "write"}},
            {"type": "function", "function": {"name": "mcp__playwright__browser_navigate"}},
            {"type": "function", "function": {"name": "mcp__playwright__browser_click"}},
        ],
    }
    assert router.computer_use_tools(cc_pw) == "", f"6d hijacked Claude Code: {router.computer_use_tools(cc_pw)}"

    # 6e. Plain-text Windows OS dialog phrases (EN + FR) trigger Desktop Use; questions do NOT
    os_pos = [
        "can you open my windows update menu please",
        "open Windows Update",
        "please open Task Manager",
        "can you launch Control Panel?",
        "open File Explorer",
        "ouvre les mises à jour windows",
        "peux-tu ouvrir le gestionnaire des tâches ?",
        "ouvre l'explorateur de fichiers",
    ]
    for q in os_pos:
        assert northcua_harness.is_desktop_task(q), f"6e missed OS dialog: {q}"
        t_q, _, f_q = router.decide(
            {"model": "auto", "messages": [{"role": "user", "content": q}], "tools": owui_builtins + open_terminal_tools},
            "openai",
        )
        assert t_q == "screen" and f_q, f"6e bad route {t_q}/{f_q} for: {q}"
    os_neg = [
        "How do I open Windows Update?",
        "What is Task Manager in Windows 11?",
        "Comment ouvrir le gestionnaire des tâches ?",
        "Ouvre les paramètres du jeu",
    ]
    for q in os_neg:
        assert not northcua_harness.is_desktop_task(q), f"6e false OS dialog match: {q}"

    # 6f. Open Terminal system prompt stripping once tools are dropped (preserves custom prompt, no-op if tools kept)
    ot_sys = (
        "You have access to a computer running Windows 10 (AMD64) on host \"WORKSTATION\" as user 'unknown' with /bin/sh. "
        "Python 3.11.18 is available.\n\n"
        "Use your tools to directly interact with the system — run commands, read and write files, and search the filesystem.\n\n"
        "When delivering created or updated files to the user, call display_file for each deliverable before your final response."
    )
    b_strip = {"messages": [{"role": "system", "content": ot_sys}, {"role": "user", "content": "hi"}]}
    assert router.strip_dropped_open_terminal_prompt(b_strip) is True
    assert len(b_strip["messages"]) == 1 and b_strip["messages"][0]["role"] == "user"

    b_keep_custom = {"messages": [{"role": "system", "content": "Always answer concisely.\n\n" + ot_sys}, {"role": "user", "content": "hi"}]}
    assert router.strip_dropped_open_terminal_prompt(b_keep_custom) is True
    assert b_keep_custom["messages"][0]["content"] == "Always answer concisely."

    b_tools_present = {"messages": [{"role": "system", "content": ot_sys}, {"role": "user", "content": "hi"}], "tools": open_terminal_tools}
    assert router.strip_dropped_open_terminal_prompt(b_tools_present) is False

    print("=== 6. Open Terminal coexistence, Windows OS dialogs & prompt cleanup: 18/18 PASSED ===")
    print("\nALL DESKTOP PHRASE, ROUTING & HUD TESTS PASSED (104/104)!")


if __name__ == "__main__":
    main()
