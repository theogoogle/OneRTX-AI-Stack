"""Test suite for explicit browser-agent / NorthCUA launch detection (`northcua_harness`).

30 real browser-driving requests (20 dev + 10 held-out, EN + FR)
30 everyday/coding/troubleshooting questions mentioning browsers, navigation, or websites (20 dev + 10 held-out, EN + FR)
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import northcua_harness  # noqa: E402
import router  # noqa: E402

# ---------------------------------------------------------------- Dev / tuning set (20 browse + 20 non-browse)
DEV_BROWSE = [
    # English: explicit instructions to use/open the browser or NorthCUA, or visit a URL/website in the browser
    "use the browser to check the O2 events on Sunday",
    "open the browser and go to metoffice.gov.uk to check the London forecast",
    "can you use the browser to look up Alan Turing on Wikipedia and tell me his alma mater?",
    "launch the browser and find out what concerts are on at the O2 this weekend",
    "please open a browser and check the front page of Hacker News",
    "use NorthCUA to find the weather in London for the next 3 days on the Met Office site",
    "go to https://www.theo2.co.uk/events and list Sunday's events",
    "navigate to https://en.wikipedia.org and search for Ada Lovelace",
    "browse to metoffice.gov.uk and check tomorrow's max temperature in London",
    "start the browser and go to bbc.co.uk/news to see the top headline",
    "with the browser, check what shows are playing at the National Theatre tonight",
    "in the browser, open https://example.com and read the text on the page",
    # French: explicit instructions to open/use the browser or visit a URL/site
    "ouvre le navigateur et va sur le site du Met Office",
    "utilise le navigateur pour chercher les événements à l'O2 dimanche",
    "peux-tu ouvrir le navigateur et aller sur wikipedia.org pour chercher Alan Turing ?",
    "lance le navigateur et regarde les titres sur bbc.co.uk",
    "va sur https://www.metoffice.gov.uk et donne-moi la météo de Londres",
    "avec le navigateur, vérifie les horaires d'ouverture du British Museum",
    "utilise NorthCUA pour aller sur le site de The O2 et voir le programme de dimanche",
    "ouvre un navigateur pour vérifier le prix sur amazon.co.uk",
]

DEV_QUESTIONS = [
    # The 4 reported false positives from the review
    "How do I navigate to the display settings in Windows 11?",
    "my page looks different in the browser and on my phone, why?",
    "explain how to browse to a folder in PowerShell",
    "is it safe to start the browser and leave it open overnight?",
    # English: questions mentioning browser, navigate to, browse to, URLs, websites
    "Why does Chrome use so much RAM when I open the browser and have 10 tabs?",
    "How can I navigate to a specific line number in VS Code?",
    "What happens when you type https://google.com in a browser and press Enter?",
    "When I navigate to https://localhost:3000 my React app shows a blank screen, how do I debug it?",
    "How do I configure Windows so links open in a browser of my choice?",
    "Is it better to use the browser to check emails or a dedicated mail client?",
    "Can a website see my files if I browse to a local HTML page?",
    "Why do I get a CORS error in the browser and not in Postman?",
    "How do I make a button in HTML that lets the user navigate to another page?",
    # French: questions mentioning navigateur, aller sur, URLs, etc.
    "Comment empêcher une application qui ouvre le navigateur toute seule au démarrage ?",
    "Pourquoi ma page s'affiche mal quand j'utilise le navigateur sur mon téléphone ?",
    "Est-ce dangereux si je lance le navigateur en mode sans échec ?",
    "Que se passe-t-il quand on va sur https://example.com au niveau DNS et TLS ?",
    "Comment faire en sorte que mon script Python n'ouvre pas un navigateur à chaque test ?",
    "Quelle est la différence entre utiliser le navigateur et l'application dédiée pour regarder Netflix ?",
    "Pourquoi j'ai une erreur de certificat quand je vais sur https://127.0.0.1:8443 ?",
]

# ---------------------------------------------------------------- Held-out test set (10 browse + 10 non-browse)
# Do NOT inspect or tune rules on individual misses from this list.
HELDOUT_BROWSE = [
    "please use the browser to check the train departures from Paddington on nationalrail.co.uk",
    "open a browser and go to https://news.ycombinator.com to tell me the #1 story",
    "can you launch the browser to see what exhibitions are on at the Tate Modern?",
    "go to https://en.wikipedia.org/wiki/Grace_Hopper and tell me where she earned her PhD",
    "browse to bbc.co.uk/weather and check the forecast for Manchester tomorrow",
    "use northcua to open the Met Office website and check Friday's temperature in London",
    "peux-tu utiliser le navigateur pour voir les prochains concerts au Royal Albert Hall ?",
    "ouvre le navigateur et va sur https://news.ycombinator.com pour lire les 3 premiers titres",
    "lance un navigateur et vérifie la météo de Paris sur le site de Météo-France",
    "va sur https://fr.wikipedia.org et cherche la date de naissance de Marie Curie",
]

HELDOUT_QUESTIONS = [
    "How do I navigate to the BIOS settings on an ASUS motherboard?",
    "Why does the audio stop when I use the browser and switch to another app on iOS?",
    "How do you browse to a network share in File Explorer on Windows 11?",
    "What command lets me open the browser to a local file from the terminal?",
    "If I go to https://haveibeenpwned.com is it safe to enter my email address?",
    "Why does my webcam light stay on after I start a browser and join a call?",
    "Comment naviguer vers un dossier caché dans l'explorateur Windows ?",
    "Pourquoi mon PC rame dès que j'ouvre un navigateur avec plusieurs onglets ?",
    "Est-ce plus sécurisé d'utiliser un navigateur différent pour sa banque en ligne ?",
    "Pourquoi ai-je une erreur 403 quand on va sur https://127.0.0.1:8931/mcp sans en-tête Host ?",
]


def detect(text):
    if hasattr(northcua_harness, "is_browser_task"):
        return bool(northcua_harness.is_browser_task(text))
    return bool(northcua_harness.BROWSER_TASK_PHRASES.search(text))


def evaluate(name, browse_items, question_items):
    tp = sum(1 for t in browse_items if detect(t))
    fn_list = [t for t in browse_items if not detect(t)]
    tn = sum(1 for t in question_items if not detect(t))
    fp_list = [t for t in question_items if detect(t)]
    total = len(browse_items) + len(question_items)
    correct = tp + tn
    print(f"=== {name}: {correct}/{total} ({tp}/{len(browse_items)} browse launched, "
          f"{len(fp_list)}/{len(question_items)} false launches on questions) ===")
    for t in fp_list:
        print(f"  [FALSE LAUNCH]  {t}")
    for t in fn_list:
        print(f"  [MISSED BROWSE] {t}")
    return tp, len(browse_items), len(fp_list), len(question_items)


if __name__ == "__main__":
    evaluate("DEV SET (is_browser_task)", DEV_BROWSE, DEV_QUESTIONS)
    print()
    evaluate("HELD-OUT SET (is_browser_task)", HELDOUT_BROWSE, HELDOUT_QUESTIONS)
    print()
    # Verify through router.decide() with model="fast" (or a direct model)
    router_b_ok = sum(
        1 for t in (DEV_BROWSE + HELDOUT_BROWSE)
        if router.decide({"model": "fast", "messages": [{"role": "user", "content": t}]}, "openai")[0] == "screen"
    )
    router_q_fp = sum(
        1 for t in (DEV_QUESTIONS + HELDOUT_QUESTIONS)
        if router.decide({"model": "fast", "messages": [{"role": "user", "content": t}]}, "openai")[0] == "screen"
    )
    print(f"=== router.decide(model='fast') across all 60 cases: "
          f"{router_b_ok}/30 browse -> screen, {router_q_fp}/30 false launches ===")

    # Verify Open WebUI Browser Use MCP toggles route via router's built-in Playwright loop
    # (collapses exploration into one thinking dropdown) + `go on timeout` detection.
    owui_headed_tools = [
        {"type": "function", "function": {"name": "browser_browser_navigate"}},
        {"type": "function", "function": {"name": "browser_browser_snapshot"}},
    ]
    owui_headless_tools = [
        {"type": "function", "function": {"name": "browser_headless_browser_navigate"}},
        {"type": "function", "function": {"name": "browser_headless_browser_snapshot"}},
    ]
    standalone_pw_tools = [
        {"type": "function", "function": {"name": "browser_navigate"}},
        {"type": "function", "function": {"name": "browser_snapshot"}},
    ]
    b_headed = {"model": "auto", "messages": [{"role": "user", "content": "check timeout london"}], "tools": owui_headed_tools}
    b_headless = {"model": "auto", "messages": [{"role": "user", "content": "check timeout london"}], "tools": owui_headless_tools}
    b_standalone = {"model": "auto", "messages": [{"role": "user", "content": "check timeout london"}], "tools": standalone_pw_tools}
    b_followup = {
        "model": "auto",
        "messages": [
            {"role": "user", "content": "go on timeout and find what's happening next weekend"},
            {"role": "assistant", "content": "Here are the weekend events from https://www.timeout.com/london ..."},
            {"role": "user", "content": "Which one is cheaper?"},
        ],
        "tools": owui_headed_tools,
    }
    assert northcua_harness.has_openwebui_playwright_tools(b_headed) == (True, True)
    assert northcua_harness.has_openwebui_playwright_tools(b_headless) == (True, False)
    assert northcua_harness.has_openwebui_playwright_tools(b_standalone) == (False, False)
    assert northcua_harness.should_run_browser_loop(b_headed, "check timeout london") == ("playwright", True)
    assert northcua_harness.should_run_browser_loop(b_headless, "check timeout london") == ("playwright", False)
    assert northcua_harness.should_run_browser_loop(b_standalone, "check timeout london") == (None, True)
    assert northcua_harness.should_run_browser_loop(b_followup, "Which one is cheaper?") == (None, True)
    assert northcua_harness.is_browser_task("go on timeout and find what's happening next weekend") is True
    assert northcua_harness.extract_start_url("go on timeout and find what's happening next weekend") == "https://www.timeout.com/london"
    assert northcua_harness.is_browser_task("can you go on a walk tomorrow?") is False
    print("=== Open WebUI Browser Use collapsible loop & Time Out phrase checks: 10/10 PASSED ===")

