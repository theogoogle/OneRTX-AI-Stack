const { chromium } = require(process.argv[2]);
(async () => {
  const b = await chromium.launch({ channel: 'chrome', headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const p = await b.newPage({ viewport: { width: 1280, height: 800 } });
  const errs = [];
  p.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 300)); });
  p.on('pageerror', e => errs.push('PAGEERROR: ' + e.message + '\n' + (e.stack||'').split('\n').slice(0,5).join('\n')));
  await p.goto('file:///' + process.argv[3].replace(/\\/g, '/'));
  await p.waitForTimeout(12000);
  await p.screenshot({ path: process.argv[4] });
  console.log('canvas:', await p.evaluate(() => !!document.querySelector('canvas')));
  console.log('errors:', errs.length ? errs.join('\n') : 'none');
  await b.close();
})();

