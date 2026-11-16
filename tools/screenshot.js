#!/usr/bin/env node
// Render a demo page at phone and desktop width, save previews, and fail on obvious defects.
//   node tools/screenshot.js demos/<lead-id>
const path = require('path');
const fs = require('fs');
const { chromium } = require('playwright');

const VIEWPORTS = [
  { name: 'mobile', width: 390, height: 844 },
  { name: 'desktop', width: 1280, height: 800 },
];

(async () => {
  const dir = path.resolve(process.argv[2] || '.');
  const file = path.join(dir, 'index.html');
  if (!fs.existsSync(file)) {
    console.error(`No index.html in ${dir}`);
    process.exit(2);
  }
  const browser = await chromium.launch();
  const problems = [];
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage({ viewport: { width: vp.width, height: vp.height }, deviceScaleFactor: 2 });
    page.on('console', (msg) => msg.type() === 'error' && !/net::ERR/.test(msg.text()) && problems.push(`[${vp.name}] console: ${msg.text()}`));
    page.on('pageerror', (err) => problems.push(`[${vp.name}] script error: ${err.message}`));
    await page.goto('file://' + file, { waitUntil: 'load' });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 1) problems.push(`[${vp.name}] page is ${overflow}px wider than the screen (horizontal scroll)`);
    const hasViewport = await page.evaluate(() => !!document.querySelector('meta[name="viewport"]'));
    if (!hasViewport) problems.push(`[${vp.name}] missing <meta name="viewport">`);
    const out = path.join(dir, `preview-${vp.name}.png`);
    await page.screenshot({ path: out, fullPage: true });
    console.log(`wrote ${out}`);
    await page.close();
  }
  await browser.close();
  if (problems.length) {
    console.error(problems.join('\n'));
    process.exit(1);
  }
})();
