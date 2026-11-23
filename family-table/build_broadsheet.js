#!/usr/bin/env node
// Build a 1-page broadsheet PDF + full-page PNG preview from an edition JSON file.
// Config-driven (no hardcoded personal details): reads optional --config <path>.
// Usage: node build_broadsheet.js <edition.json> <output-dir> [--config <path>]
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { execFileSync } = require('child_process');

let jsonPath = null;
let outDir = '.';
let configPath = null;
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === '--config' && process.argv[i + 1]) {
    configPath = process.argv[++i];
  } else if (!jsonPath) {
    jsonPath = process.argv[i];
  } else if (outDir === '.') {
    outDir = process.argv[i];
  }
}
if (!jsonPath) {
  console.error('Usage: node build_broadsheet.js <edition.json> <output-dir> [--config <path>]');
  process.exit(1);
}
if (!configPath) {
  const home = process.env.USERPROFILE || process.env.HOME || '';
  const def = path.join(home, 'AI', 'secrets', 'family_table', 'config.json');
  if (fs.existsSync(def)) configPath = def;
}
const cfg = configPath && fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath, 'utf8')) : {};
const ed = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
const ASSETS = path.join(__dirname, 'assets');

// ---------- helpers ----------
const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const rich = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<span class="hl">$1</span>');
const font = (family, file, weight, style = 'normal') => {
  const b64 = fs.readFileSync(path.join(ASSETS, 'fonts', file)).toString('base64');
  return `@font-face{font-family:"${family}";src:url(data:font/woff2;base64,${b64}) format("woff2");font-weight:${weight};font-style:${style}}`;
};
const fonts = [
  font('Masthead', 'chomsky-masthead.woff2', 400),
  font('Serif', 'source-serif-4-latin-400-normal.woff2', 400),
  font('Serif', 'source-serif-4-latin-400-italic.woff2', 400, 'italic'),
  font('Serif', 'source-serif-4-latin-600-normal.woff2', 600),
  font('Serif', 'source-serif-4-latin-700-normal.woff2', 700),
  font('Sans', 'source-sans-3-latin-400-normal.woff2', 400),
  font('Sans', 'source-sans-3-latin-600-normal.woff2', 600),
  font('Sans', 'source-sans-3-latin-700-normal.woff2', 700),
].join('\n');
const css = fs.readFileSync(path.join(ASSETS, 'style.css'), 'utf8') +
  '\n*{-webkit-print-color-adjust:exact !important;print-color-adjust:exact !important}';

const section = (title, inner, cls = '') => inner ? `<section${cls ? ` class="${cls}"` : ''}><h2>${esc(title)}</h2>${inner}</section>` : '';
const items = (list, srcKey) => (list || []).map(i =>
  `<div class="item"><h3>${esc(i.title)}</h3>${rich(i.text)}${i[srcKey] ? `<div class="src">${esc(i[srcKey])}</div>` : ''}</div>`).join('');

// ---------- blocks ----------
const mastTitle = ed.edition_title || cfg.edition_title || 'The Family Table';
const locLabel = ed.location_label || cfg.location_label || 'London';
const footerReaders = ed.footer_readers || cfg.footer_readers || 'Family edition';
const childHeaders = (cfg.children && cfg.children.length >= 2)
  ? [cfg.children[0].name, cfg.children[1].name]
  : ((ed.kids && ed.kids.length >= 2) ? [ed.kids[0].name, ed.kids[1].name] : ['Child 1', 'Child 2']);

const w = ed.weather || {};
const wxSrc = w.source || cfg.weather_source_label || 'Met Office · London';
const weather = `<div class="wx"><div class="t">${esc(w.high)}</div><div class="d"><b>${esc(w.summary)}</b><br>8 am: ${esc(w.at8)} · Rain ${esc(w.rain)}<br>Sunrise ${esc(w.sunrise)} · Sunset ${esc(w.sunset)}${w.tomorrow ? `<br><span style="color:var(--muted)">${esc(w.tomorrow)}</span>` : ''}<div class="src" style="margin-top:2px">${esc(wxSrc)}</div></div></div>`;

const week = (ed.week || []).map(d =>
  `<div class="day"><div class="k"><span>${esc(d.day)}</span><span>${esc(d.num)}</span></div><div>${rich(d.text)}${d.pickup ? ` <span class="tag">Pick-ups: ${esc(d.pickup)}</span>` : ''}</div></div>`).join('');

const kids = (ed.kids || []).map(k =>
  `<div class="kid"><div class="who"><b>${esc(k.name)}</b><span class="tag">${esc(k.tag)}</span></div>${rich(k.text)}</div>`).join('');

const watch = (ed.watch || []).length ? `<ul class="watch">${ed.watch.map(x => `<li>${x.lead ? `<span class="hl">${esc(String(x.lead).replace(/\*\*/g, ''))}</span> ` : ''}${rich(x.text)}</li>`).join('')}</ul>` : '';

let run = '';
if (ed.school_run && (ed.school_run.rows || []).length) {
  const rows = ed.school_run.rows.map(r => r.length === 4 ? [r[0], r[1], r[3], r[2], r[3]] : r);
  run = `${ed.school_run.note ? `<p class="note">${esc(ed.school_run.note)}</p>` : ''}<table class="run"><tr><th></th><th colspan="2">${esc(childHeaders[0])}</th><th colspan="2">${esc(childHeaders[1])}</th></tr>${rows.map(r => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td class="who">${esc(r[2])}</td><td>${esc(r[3])}</td><td class="who">${esc(r[4])}</td></tr>`).join('')}</table>`;
}

const coming = (ed.coming_up || []).length ? `${ed.coming_up_note ? `<p class="note">${esc(ed.coming_up_note)}</p>` : ''}${ed.coming_up.map(c =>
  `<div class="day"><div class="k"><span>${esc(c.day)}</span><span>${esc(c.num)}</span></div><div>${rich(c.text)}${c.when ? ` <span class="tag">${esc(c.when)}</span>` : ''}</div></div>`).join('')}` : '';

const talk = (ed.talk || []).length ? `<section class="talk float"><h2>Talk at the table</h2><ol>${ed.talk.map(q => `<li>${rich(q)}</li>`).join('')}</ol></section>` : '';

const weekTitle = ed.week_title || (/weekend/i.test(ed.edition_label || '') ? 'The weekend ahead' : 'The week ahead');
const html = `<!DOCTYPE html><html lang="en-GB"><head><meta charset="utf-8"><title>${esc(mastTitle)} · ${esc(ed.date_long)}</title><style>${fonts}\n${css}</style></head><body>
<div class="topbar"></div>
<header class="mast"><div class="l">${esc(ed.edition_label || 'Breakfast edition')}</div><h1>${esc(mastTitle)}</h1><div class="r">${esc(locLabel)}</div></header>
<div class="lede"><div><div class="date">${esc(ed.date_long)}</div><p>${rich(ed.lede)}</p></div><div class="vr"></div>${weather}</div>
<main class="cols">
<div>${section(weekTitle, week)}${section(ed.school_desk_title || 'School desk', items(ed.school_desk))}</div><div class="vr"></div>
<div>${section('Kid desk', kids)}${section('Parents: watch these', watch)}${section('Coming up', coming, 'float')}${section('The school run', run, 'float')}${talk}</div><div class="vr"></div>
<div>${section('News to know', items(ed.news, 'source'))}${section('Around town', items(ed.town, 'where'))}</div>
</main>
<footer class="foot"><span>${esc(footerReaders)}</span><span>Next edition: ${esc(ed.next_edition || '')}</span></footer>
<script>
window.__ftDone = document.fonts.ready.then(function () {
  var cols = [].slice.call(document.querySelectorAll('main.cols > div:not(.vr)'));
  var floats = [].slice.call(document.querySelectorAll('main.cols .float'));
  var used = function (c) { return c.lastElementChild ? c.lastElementChild.getBoundingClientRect().bottom - c.getBoundingClientRect().top : 0; };
  floats.forEach(function (f) { f.remove(); });
  floats.forEach(function (f) {
    var target = cols.reduce(function (a, c) { return used(c) < used(a) ? c : a; });
    target.appendChild(f);
  });
  var h = Math.ceil(document.body.getBoundingClientRect().height);
  var st = document.createElement('style');
  st.textContent = '@page{size:794px ' + (h + 2) + 'px;margin:0}';
  document.head.appendChild(st);
  var heights = cols.map(used);
  var spread = Math.round(Math.max.apply(null, heights) - Math.min.apply(null, heights));
  return { height: h, columns: heights.map(Math.round), spread: spread, balanced: spread < 220 };
});
<\/script>
</body></html>`;

// ---------- render ----------
fs.mkdirSync(outDir, { recursive: true });
const base = path.join(outDir, `The-Family-Table-${ed.file_date}`);
fs.writeFileSync(base + '.html', html, 'utf8');

function findChromium() {
  if (process.env.CHROMIUM_PATH && fs.existsSync(process.env.CHROMIUM_PATH)) return process.env.CHROMIUM_PATH;

  const winCandidates = [
    path.join(process.env['PROGRAMFILES'] || 'C:\\Program Files', 'Google\\Chrome\\Application\\chrome.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Google\\Chrome\\Application\\chrome.exe'),
    path.join(process.env.LOCALAPPDATA || '', 'Google\\Chrome\\Application\\chrome.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:\\Program Files (x86)', 'Microsoft\\Edge\\Application\\msedge.exe'),
    path.join(process.env['PROGRAMFILES'] || 'C:\\Program Files', 'Microsoft\\Edge\\Application\\msedge.exe'),
  ];
  for (const p of winCandidates) {
    if (p && fs.existsSync(p)) return p;
  }

  const roots = [
    path.join(process.env.LOCALAPPDATA || '', 'ms-playwright'),
    '/opt/pw-browsers',
    path.join(process.env.HOME || '', '.cache/ms-playwright')
  ];
  for (const root of roots) {
    if (!root || !fs.existsSync(root)) continue;
    for (const dir of fs.readdirSync(root).filter(d => d.startsWith('chromium')).sort().reverse()) {
      for (const rel of [
        'chrome-win/chrome.exe',
        'chrome-win64/chrome.exe',
        'chrome-linux/chrome',
        'chrome-linux/headless_shell',
        'chrome-headless-shell-linux64/chrome-headless-shell'
      ]) {
        const p = path.join(root, dir, rel);
        if (fs.existsSync(p)) return p;
      }
    }
  }
  for (const p of ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']) {
    if (fs.existsSync(p)) return p;
  }
  return undefined;
}

function loadPlaywright() {
  const sep = process.platform === 'win32' ? ';' : ':';
  const home = process.env.USERPROFILE || process.env.HOME || '';
  const paths = [
    process.cwd(),
    __dirname,
    path.join(home, 'AI', 'playwright-mcp', 'node_modules'),
    ...(process.env.NODE_PATH || '').split(sep).filter(Boolean),
    '/usr/lib/node_modules',
    '/usr/local/lib/node_modules',
    path.join(home, '.npm-global/lib/node_modules')
  ];
  for (const name of ['playwright', 'playwright-core']) {
    try { return require(require.resolve(name, { paths })); } catch { /* try next */ }
  }
  return null;
}

(async () => {
  const pw = loadPlaywright();
  const fileUrl = pathToFileURL(path.resolve(base + '.html')).href;
  const pdfPath = path.resolve(base + '.pdf');
  const pngPath = path.resolve(base + '.png');

  if (pw) {
    const browser = await pw.chromium.launch({ executablePath: findChromium() });
    try {
      const page = await browser.newPage({ viewport: { width: 794, height: 1123 }, deviceScaleFactor: 2 });
      await page.goto(fileUrl, { waitUntil: 'load' });
      const info = await page.evaluate(() => window.__ftDone);
      await page.screenshot({ path: pngPath, fullPage: true });
      await page.pdf({
        path: pdfPath,
        width: '794px',
        height: (info.height + 2) + 'px',
        printBackground: true,
        margin: { top: 0, right: 0, bottom: 0, left: 0 },
        pageRanges: '1'
      });
      console.log(JSON.stringify({ engine: 'playwright', pdf: pdfPath, preview: pngPath, html: path.resolve(base + '.html'), ...info }));
    } finally {
      await browser.close();
    }
    return;
  }

  const chrome = findChromium();
  if (!chrome) {
    console.error('No Playwright module and no Chrome/Chromium binary found.');
    process.exit(2);
  }
  execFileSync(chrome, [
    '--headless=new',
    '--disable-gpu',
    '--no-sandbox',
    '--no-pdf-header-footer',
    '--window-size=794,1400',
    '--virtual-time-budget=4000',
    '--print-to-pdf=' + pdfPath,
    fileUrl
  ], { stdio: 'ignore' });
  console.log(JSON.stringify({ engine: 'chromium-cli', binary: chrome, pdf: pdfPath, html: path.resolve(base + '.html') }));
})().catch(e => {
  console.error(e);
  process.exit(3);
});
