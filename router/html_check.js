// Headless browser check of one generated HTML file (called by html_files.py).
// node html_check.js <playwright module path> <file.html>  ->  one JSON line:
// {"errors": [...], "clicked": "Start" | null, "covered": "#loading-screen (\"...\")" | null, "canvas": true|false}
const [, , pwPath, file] = process.argv;
const { chromium } = require(pwPath);

(async () => {
  const out = { errors: [], clicked: null, covered: null, canvas: false };
  const add = (msg) => {        // one short line: shader logs are long multi-line dumps
    msg = String(msg).replace(/\s+/g, ' ').replace(/Program Info Log: |FRAGMENT |VERTEX /g, '').trim();
    if (msg.length > 260) msg = msg.slice(0, 257) + '...';
    if (!out.errors.includes(msg) && out.errors.length < 8) out.errors.push(msg);
  };
  // where it failed AND who called it: the failing line is often a helper (box(parent...)), the bug is in the caller
  const line = (stack) => {
    const nums = [...new Set([...(stack || '').matchAll(/\.html:(\d+):\d+/g)].map((m) => m[1]))];
    if (!nums.length) return '';
    return nums.length > 1 ? ` (line ${nums[0]}, called from line ${nums[1]})` : ` (line ${nums[0]})`;
  };
  let browser;
  try {
    browser = await chromium.launch({ channel: 'chrome', headless: true,
      args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });   // software WebGL: works without the GPU
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    page.on('pageerror', (e) => add(`${e.name || 'Error'}: ${e.message}${line(e.stack)}`));
    page.on('console', (m) => {
      if (m.type() !== 'error') return;
      const loc = m.location() || {};
      add(m.text().slice(0, 300) + (loc.lineNumber && /\.html$/.test(loc.url || '') ? ` (line ${loc.lineNumber + 1})` : ''));
    });
    page.on('requestfailed', (r) => add(`could not load ${r.url()} (${(r.failure() || {}).errorText})`));
    await page.goto('file:///' + file.replace(/\\/g, '/'), { waitUntil: 'load', timeout: 30000 });
    await page.waitForTimeout(6000);

    // an obvious start control (welcome screens): click it like a user would
    const start = page.locator('button, a, [role=button]')
      .filter({ hasText: /\b(start|begin|play|enter|launch|explore|commencer|entrer|jouer|lancer|d[ée]marrer|explorer)\b/i })
      .first();
    if (await start.count() && await start.isVisible()) {
      out.clicked = (await start.innerText()).trim().replace(/\s+/g, ' ').slice(0, 60);
      await start.click({ timeout: 5000 }).catch(() => add(`the "${out.clicked}" button cannot be clicked: something is covering it`));
      await page.waitForTimeout(5000);
    }

    // 3D apps: is the canvas visible in the middle of the screen, or still covered by a full-screen element?
    const view = await page.evaluate(() => {
      const canvas = document.querySelector('canvas');
      if (!canvas) return { canvas: false };
      const el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
      if (!el || el.tagName === 'CANVAS') return { canvas: true };
      let e = el, big = null;
      while (e && e !== document.body && e !== document.documentElement) {
        const r = e.getBoundingClientRect();
        if (r.width >= innerWidth * 0.8 && r.height >= innerHeight * 0.8) big = e;
        e = e.parentElement;
      }
      if (!big) return { canvas: true };
      const name = big.id ? '#' + big.id
        : big.tagName.toLowerCase() + (typeof big.className === 'string' && big.className.trim() ? '.' + big.className.trim().split(/\s+/)[0] : '');
      const text = (big.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 70);
      return { canvas: true, covered: name + (text ? ` ("${text}")` : '') };
    });
    out.canvas = view.canvas;
    out.covered = view.covered || null;
  } catch (e) {
    add(`the check itself failed: ${e.message.split('\n')[0]}`);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  console.log(JSON.stringify(out));
})();
