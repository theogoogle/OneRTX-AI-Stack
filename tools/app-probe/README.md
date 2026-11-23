# App probe (headless browser test for single-file HTML apps)

Usage (Playwright from the browser-tools install, software WebGL):

    node app_probe.js %USERPROFILE%\AI\playwright-mcp\node_modules\playwright <file.html> <screenshot-prefix>

It opens the page, screenshots the welcome screen, types a name in `#name-input`, clicks `#start-btn` (selectors are
specific to the Vibe Academy app: generalise them for the self-test loop), screenshots, walks with W, and prints
whether the loading screen is hidden, the HUD active, plus all console errors / page errors.
`scene_probe.js` prints Three.js scene facts (mesh count, camera position, draw calls).
Movement is slow headless (software rendering, ~2 fps, dt capped at 0.05): not an app bug.

Fixtures (2026-11-07, see router/CHANGES.md "Coding chats"):
- `vibe-academy-qwen-original-broken.html`: Qwen3.6 + thinking's first build. Bugs: start-button handler attached
  inside setupEvents(), which only runs from init(), which only runs from that click (stuck on "Chargement du campus
  FDE..."), loading screen above the welcome screen; camera at 4.5 m above 4 m ceilings (grey screen).
- `vibe-academy-fixed-by-hand.html`: both bugs fixed, verified.
- `...-qwen-fix-try1/2-broken.html`: Qwen3.6's two fix attempts (with the complete-files brief): still stuck.
A good coding model, given the original and "it stays on 'Chargement du campus FDE...'", should find both bugs.
