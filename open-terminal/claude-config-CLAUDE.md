# Standing instructions (local coding agents)

## Verify by running
- Audit deeply before coding: do not stop at obvious bugs or dismiss subtle language/runtime edge cases as minor — check concurrency, transient-failure recovery (does a failed call poison state against retries?), hostile/unusual input keys or types, and scaling across distinct keys/groups as well as item count.
- When debugging, reviewing or writing runnable JS/Python (pasted snippets included), write a **short (~25–40 line) adversarial probe** to `.agent-check/check.js` (or `.py`), run it, and delete that file in the same shell command (see Shell); if a project already has tests, run those instead. Design the probe to *break* your fix (adversarial inputs, concurrent calls, fail-then-retry, timing many distinct keys) — never build bloated test frameworks or fragile hand-computed float oracles. Never inline code with `node -e` / `python -c`, and do not search the workspace for pasted code.
- Never read or write outside the working directory. Use relative paths in shell commands; when a tool needs an absolute path, copy the working directory exactly from your environment info. Run Python as `python -I -X utf8 file.py`.
- If the same step fails twice, stop retrying: answer from your reasoning and say exactly what is unverified.

## Fix what you find
- Override stylistic conservatism: do not keep naive containers, serial bottlenecks on independent I/O, or quadratic patterns just to mirror the original snippet's style, and never leave real fixes or upgrades (parallelism, retry-safe state cleanup, safe key handling, linear scaling) as prose footnotes or secondary "alternative" snippets — ship **one unified, production-grade implementation**.
- When editing one file inside an existing multi-file project, keep exported signatures and file names that other files already call unless the task requires changing them, and report unrelated defects in untouched files in one line each instead of refactoring the whole folder (if there is no git repo, there is no undo).

## Files and apps
- In `AI\projects`, each new project gets its own subfolder (`bouncing-ball/index.html`); ignore sibling folders unless asked.
- Keep any single write under ~500 lines (the 32K output cap includes thinking) and read large files in line ranges. Split plain-JS apps into `<script defer src="...">` files. Keep `<script type="module">` / Three.js apps (including `make_3d_scene` output) single-file and grow them with edits: never split them into classic scripts (they cannot see the module's `THREE`) or local `import './x.js'` (blocked on file://).
- Browser apps must work when `index.html` is double-clicked: no console errors; start buttons, overlays and controls must work.
- Never start servers or watchers from the shell; give the user the command. For project files on disk, finish with the file path and how to run it; for standalone pasted snippets, return the complete corrected code block directly.

## 3D scenes (`make_3d_scene`, scene3d server)
- For a 3D world, room, building, landscape or object, call it once (3–10 min) with `file` (`my-project/scene.html`) and an English static-scene `description` (≤ ~1,000 chars: layout, objects, materials, colours, lighting, orbit camera; ask for named objects if your logic needs them). If it fails, retry once with a shorter, more concrete description.
- Then read the saved file, keep its geometry and Three.js import, fix reported browser-check errors first, and add interactivity on top.

## Bespoke PowerPoint presentations (`pptx_studio` server)
- When asked to create a bespoke / executive / custom `.pptx` presentation, write a clean, concise `python-pptx` script (~220–300 lines) in a dedicated subfolder (e.g. `my-deck/build_deck.py`), run `python -I -X utf8 my-deck/build_deck.py`, and follow a strict design + visual-QA loop:
  1. **Live facts first (when current/recent):** call `search_slide_facts` ONCE with crisp English dated keywords (`topic="news"`, `time_range="week"` or `"day"`) instead of guessing post-2025 facts. Never put `[1]`/`[5]` citation brackets in slide text.
  2. **Executive visual design (`13.333 × 7.5 in` widescreen):** pick one cohesive editorial palette (rich dark navy/charcoal/slate cover & closing slides paired with crisp high-contrast off-white content slides, 1 bold accent color), clean typography (`Segoe UI` / `Calibri` / `Georgia`, clear hierarchy: 28–36 pt titles, 13–15 pt body, ≥11 pt captions), generous margins (`0.6–0.7 in`) and whitespace, action-oriented insight headlines, and varied layouts across slides (asymmetric split, 3–4 KPI cards, Bento 2×2 grid, clean table, custom `matplotlib` 300-DPI chart with matched background and direct data labels, timeline/process cards). Never build monotonous bullet-only decks or put raw emojis in titles.
  3. **Custom visuals (optional, 1–2 max):** embed verified editorial photos from `search_slide_facts` or call `generate_slide_visual` once for a clean text-free cover/hero illustration.
  4. **Mandatory visual QA:** call `inspect_pptx_visually(file)` (renders every slide via LibreOffice + runs geometry & Gemma 4 Vision QA). Fix every overlap, overflow, contrast, or bland-layout issue reported, re-run `inspect_pptx_visually` once to confirm clean, then call `publish_pptx(file)` and include the returned download link and preview image in your reply.

## Shell: the Bash tool is Git Bash (not PowerShell)
- Use POSIX syntax and forward slashes. Run checks as `node .agent-check/check.js; rm -f .agent-check/check.js` (same for `.py`).
