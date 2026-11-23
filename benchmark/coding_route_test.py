"""Held-out task-agnostic routing benchmark (n = 32 unseen prompts).

Verifies both:
1. POST http://127.0.0.1:4000/route-check  -> {"dispatch": "opencode" | "chat", ...}
2. router.decide(body, "openai")           -> (tier, reason) + add_coding_brief mode

Zero overlap with benchmark task names; covers English & French prompts across:
- Group A (8): Multi-file / repo / CLI / test-verified agent tasks -> dispatch="opencode"
- Group B (8): 3D / shaders / continuous physics canvas tasks     -> dispatch="chat", tier="deep" (Pro)
- Group C (8): Discrete games / stateful UI / algorithms / debug  -> dispatch="chat", tier="think" (Qwen3.8)
- Group D (8): Quick code Q&A + everyday/architecture non-coding  -> dispatch="chat", tier="fast"/"code"/"deep"
"""

import json
import sys
import urllib.request
from pathlib import Path

SCRIPTS = Path.home() / ".lmstudio" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import router  # noqa: E402

ROUTER_URL = "http://127.0.0.1:4000"

LONG_HTML_FIXTURE = "\n".join(
    ["<!DOCTYPE html><html><head><style>body{margin:0}</style></head><body><script>"]
    + [f"const row_{i} = {i};" for i in range(445)]
    + ["function resetScore() { console.log('todo'); }", "</script></body></html>"]
)


CASES = [
    # ---------------- Group A: Multi-file / CLI / Repo / Test-verified -> OpenCode (8) ----------------
    {
        "id": "A1_checkers_cli_pytest",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Create a multi-file Python Checkers engine and CLI app with alpha-beta minimax AI "
            "and a pytest test suite verifying forced jump sequences and king promotion."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A2_express_rest_jest",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Build an Express + TypeScript REST API server with bookmark CRUD routes, request validation, "
            "package.json, and Jest integration tests."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A3_rust_cli_cargo_test",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Write a Rust command-line log analyzer tool with subcommands (stats, filter, export), "
            "Cargo.toml, and unit tests that pass cargo test."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A4_ts_jsonrpc_vitest",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Implement a TypeScript JSON-RPC 2.0 client and server library in a project folder "
            "with batching, timeouts, and a vitest test suite."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A5_fastapi_service_pytest",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Scaffold a FastAPI inventory backend with routers, Pydantic schemas, repository layer, "
            "and run pytest to verify every endpoint."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A6_go_shortener_gotest",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Create a Go module (go.mod) for a URL shortener package with an HTTP handler, "
            "TTL expiry, and concurrency tests verified with go test."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A7_node_cli_nodetest",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Build a Node.js CLI utility with flags (--delimiter, --pretty, --schema) that converts CSV streams "
            "to JSON Lines, and verify it with node --test."
        )}],
        "dispatch": "opencode",
    },
    {
        "id": "A8_french_explicit_opencode",
        "group": "A_opencode",
        "messages": [{"role": "user", "content": (
            "Utilise OpenCode pour créer un projet Python de gestion de bibliothèque avec plusieurs modules et des tests."
        )}],
        "dispatch": "opencode",
    },

    # ---------------- Group B: 3D / Shaders / Continuous Physics Canvas -> Chat Pro (8) ----------------
    {
        "id": "B1_checkers_3d_threejs",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Create an interactive 3D Checkers board game in Three.js inside a single HTML page "
            "with wooden pieces, warm spotlight shadows, and click-to-move."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B2_webgl2_raymarch_sdf",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Write a single-file HTML page with a WebGL2 GLSL fragment shader raymarching a glowing "
            "signed-distance crystal cavern with mouse camera controls."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B3_canvas_verlet_cloth",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Build an interactive 2D Verlet cloth physics simulation on an HTML5 Canvas where dragging "
            "pulls the fabric and right-clicking tears threads."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B4_threejs_submarine",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Make a low-poly 3D underwater submarine explorer in Three.js with swaying kelp, "
            "bioluminescent fish, and WASD steering."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B5_canvas_boids_flocking",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Create an HTML5 Canvas boids flocking simulation with alignment, cohesion, separation sliders, "
            "and a mouse-controlled predator."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B6_webgl_particle_fountain",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Build a WebGL particle physics fountain with 20,000 glowing sparks, floor bounce, "
            "and wind direction controls in one HTML file."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B7_french_3d_windmill",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Crée une scène 3D interactive d'un moulin médiéval au coucher du soleil avec Three.js "
            "et des pales qui tournent dans un seul fichier HTML."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },
    {
        "id": "B8_double_pendulum_physics",
        "group": "B_3d_sim_pro",
        "messages": [{"role": "user", "content": (
            "Build a double-pendulum physics simulator on HTML5 Canvas using RK4 integration, "
            "a fading chaotic trajectory trail, and mass/length sliders."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
    },

    # ---------------- Group C: Discrete Games / Stateful UI / Algorithms / Debug -> Chat Qwen3.8 (8) ----------------
    {
        "id": "C1_checkers_2d_html",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Build a playable 2D Checkers board game in a single HTML file with forced captures, "
            "multi-jump chains, king promotion, and turn indicator."
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C2_sudoku_ui_solver",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Create an interactive Sudoku puzzle web app in HTML/CSS/JS with pencil marks, conflict highlighting, "
            "undo stack, and a backtracking solver button."
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C3_minesweeper_html",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Make a classic Minesweeper game in one HTML file with first-click safety, flood-fill reveal, "
            "flagging, chord clicking, and difficulty presets."
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C4_expense_dashboard_html",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Build a single-file personal expense tracker dashboard in HTML/JS with category filtering, "
            "inline SVG bar charts, localStorage persistence, and CSV export."
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C5_astar_grid_visualizer",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Create an interactive grid pathfinding visualizer in HTML/JS comparing A* and Dijkstra "
            "with draw-to-place walls and weighted terrain cells."
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C6_python_async_ratelimiter",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Write a self-contained Python asyncio token-bucket rate limiter class supporting per-tenant "
            "burst limits, fair FIFO waiter queues, and clean cancellation."
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C7_debug_js_closure_bug",
        "group": "C_logic_ui_think",
        "messages": [{"role": "user", "content": (
            "Find and fix the stale-state and race-condition bugs in this debounced search controller:\n"
            "```js\n"
            "function attachSearch(input, render) {\n"
            "  let timer = null;\n"
            "  let lastSeq = 0;\n"
            "  input.addEventListener('input', () => {\n"
            "    const q = input.value;\n"
            "    clearTimeout(timer);\n"
            "    timer = setTimeout(async () => {\n"
            "      const seq = lastSeq++;\n"
            "      const res = await fetch('/api/search?q=' + q).then(r => r.json());\n"
            "      if (seq <= lastSeq) render(res);\n"
            "    }, 250);\n"
            "  });\n"
            "}\n"
            "```"
        )}],
        "dispatch": "chat",
        "tiers": ("think",),
    },
    {
        "id": "C8_long_html_surgical_followup",
        "group": "C_logic_ui_think",
        "messages": [
            {"role": "user", "content": "Build a score tracker HTML app."},
            {"role": "assistant", "content": f"```html\n{LONG_HTML_FIXTURE}\n```"},
            {"role": "user", "content": "The reset button calls resetScore() but doesn't reset the counter display; fix it."},
        ],
        "dispatch": "chat",
        "tiers": ("think",),
        "brief": "surgical patch",
    },

    # ---------------- Group D: Quick Code Q&A + Everyday / Architecture Non-Coding (8) ----------------
    {
        "id": "D1_quick_regex_postcode",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "What regex matches standard UK postcodes like SW1A 1AA?"}],
        "dispatch": "chat",
        "tiers": ("fast", "code"),
        "brief": None,
    },
    {
        "id": "D2_quick_git_undo",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "How do I undo my last git commit while keeping my changes staged?"}],
        "dispatch": "chat",
        "tiers": ("fast", "code"),
        "brief": None,
    },
    {
        "id": "D3_quick_sql_groupby",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "Write a short SQL query to sum monthly revenue grouped by plan_tier."}],
        "dispatch": "chat",
        "tiers": ("fast", "code"),
        "brief": None,
    },
    {
        "id": "D4_quick_python_yield_from",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "In Python, what is the difference between yield and yield from?"}],
        "dispatch": "chat",
        "tiers": ("fast", "code"),
        "brief": None,
    },
    {
        "id": "D5_everyday_french_recipe",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "Donne-moi une recette simple et rapide de sauce tomate maison pour les pâtes."}],
        "dispatch": "chat",
        "tiers": ("fast",),
        "brief": None,
    },
    {
        "id": "D6_everyday_hamstring_stretch",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "What are three gentle hamstring stretches after a 5k run?"}],
        "dispatch": "chat",
        "tiers": ("fast",),
        "brief": None,
    },
    {
        "id": "D7_everyday_polite_french_note",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": "Translate this politely into French: Sorry, my train is delayed so I will join the meeting 10 minutes late."}],
        "dispatch": "chat",
        "tiers": ("fast",),
        "brief": None,
    },
    {
        "id": "D8_deep_architecture_tradeoffs",
        "group": "D_quick_and_general",
        "messages": [{"role": "user", "content": (
            "Compare event sourcing versus state-oriented tables with an immutable audit log for a regulated "
            "multi-currency ledger: analyze consistency guarantees, replay cost, GDPR erasure, and schema evolution."
        )}],
        "dispatch": "chat",
        "tiers": ("deep",),
        "brief": None,
    },
]


def post_route_check(messages):
    data = json.dumps({"messages": messages}).encode("utf-8")
    req = urllib.request.Request(
        f"{ROUTER_URL}/route-check",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    assert len(CASES) == 32, f"Expected 32 cases, got {len(CASES)}"
    passed = 0
    group_totals = {}
    group_passed = {}
    failures = []

    for c in CASES:
        grp = c["group"]
        group_totals[grp] = group_totals.get(grp, 0) + 1
        rc = post_route_check(c["messages"])
        got_dispatch = rc.get("dispatch")

        body = {"model": "auto", "messages": [dict(m) for m in c["messages"]]}
        tier, reason, _ = router.decide(body, "openai")
        ent = {"reason": reason}
        router.Handler._coding_brief(body, tier, ent)
        brief = ent.get("brief")

        ok_dispatch = (got_dispatch == c["dispatch"])
        ok_tier = True if "tiers" not in c else (tier in c["tiers"])
        if "brief" not in c:
            ok_brief = True
        elif c["brief"] is None:
            ok_brief = brief is None
        else:
            ok_brief = c["brief"] in (brief or "")
        ok = ok_dispatch and ok_tier and ok_brief

        if ok:
            passed += 1
            group_passed[grp] = group_passed.get(grp, 0) + 1
            mark = "PASS"
        else:
            mark = "FAIL"
            failures.append((c, rc, tier, reason, brief))

        print(
            f"{mark:4s}  {c['id']:30s}  dispatch={got_dispatch:8s} (exp {c['dispatch']:8s})  "
            f"tier={tier:5s}  brief={str(brief):14s}  reason={reason}"
        )

    print("\n--- Summary by group ---")
    for grp in sorted(group_totals):
        gp = group_passed.get(grp, 0)
        gt = group_totals[grp]
        print(f"  {grp:22s}: {gp}/{gt}")
    print(f"\nTOTAL: {passed}/{len(CASES)} ({100.0 * passed / len(CASES):.1f}%)")

    if failures:
        print("\nFailures detail:")
        for c, rc, tier, reason, brief in failures:
            print(f"  - {c['id']}: rc={rc} tier={tier} brief={brief} reason={reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
