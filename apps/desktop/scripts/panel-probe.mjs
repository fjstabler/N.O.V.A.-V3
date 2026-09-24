/**
 * Does anything overlap the clock on a panel?
 *
 * The geometry is checked by `src/core/panel-layout.test.ts`, which does the
 * arithmetic. This does the other half: it loads the built bundle in Chromium
 * at 960x480, measures where things actually land, and then reads the Core's
 * own pixels in the rectangle the clock occupies.
 *
 * That last part is the only check that cannot be fooled. Arithmetic can agree
 * with itself and still be wrong about the thing on the screen — three real
 * faults turned up here that no amount of calculation would have found: a halo
 * gradient that filled the hollow solid, a date line that wrapped into the
 * digits, and a colon that rendered as nothing because its opacity animation
 * gave it its own paint layer inside a gradient-filled parent.
 *
 * Not wired into CI: it needs a browser and a built bundle. Run it by hand
 * after touching the panel layout, the Core's geometry, or the clock.
 *
 *   npm run build:web
 *   npm i --no-save playwright   # if it is not already about
 *   node scripts/panel-probe.mjs
 *
 * WebGL is disabled deliberately: headless Chromium falls back to software and
 * stalls on every pixel readback, and the Canvas2D path draws the same
 * geometry. A canvas also has to be readable for the pixel probe, which a
 * WebGL context is not without preserveDrawingBuffer.
 */
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join } from 'node:path';

const ROOT = new URL('../../../services/core/nova/webapp/static', import.meta.url).pathname;
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const server = createServer(async (req, res) => {
  const path = req.url.split('?')[0].replace(/^\/app/, '') || '/';
  try {
    const file = join(ROOT, path === '/' ? 'index.html' : path);
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream' });
    res.end(body);
  } catch { res.writeHead(404); res.end('no'); }
});
await new Promise((r) => server.listen(4173, r));

const browser = await chromium.launch({
  executablePath: '/opt/pw-browsers/chromium',
  args: ['--disable-webgl', '--disable-webgl2'],
});
const page = await browser.newPage({ viewport: { width: 960, height: 480 }, deviceScaleFactor: 1 });
page.on('requestfailed', (r) => console.log('  [failed]', r.url().slice(-40)));
await page.addInitScript(() => localStorage.setItem('nova.bridge.token', 'x'));
await page.goto('http://127.0.0.1:4173/app/', { waitUntil: 'domcontentloaded' });
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(800);
console.log(JSON.stringify(await page.evaluate(() => {
  const rect = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { l: Math.round(r.left), t: Math.round(r.top), r: Math.round(r.right), b: Math.round(r.bottom),
             w: Math.round(r.width), h: Math.round(r.height) };
  };
  const hm = document.querySelector('.clock__hm');
  return {
    loaded: [...document.fonts].map((f) => `${f.family} ${f.status}`),
    font: hm ? getComputedStyle(hm).fontFamily.split(',')[0] : null,
    size: hm ? getComputedStyle(hm).fontSize : null,
    clock: rect('.clock'), time: rect('.clock__time'), meta: rect('.clock__meta'),
  };
}), null, 1));
// Does anything the Core draws actually land on the clock?
// Hide the clock, then read the Core's own pixels in the rectangle it occupied.
// A ring is a bright thin line; the halo behind it is broad and dim, so a
// brightness ceiling separates "a ring crosses the clock" from "the clock sits
// on a soft glow", which is the whole point of the design.
const probe = await page.evaluate(async () => {
  const clock = document.querySelector('.clock');
  const canvas = document.querySelector('canvas.nova-core');
  if (!clock || !canvas) return null;
  const box = clock.getBoundingClientRect();
  clock.style.visibility = 'hidden';
  await new Promise((r) => setTimeout(r, 400));

  const sx = canvas.width / canvas.clientWidth;
  const sy = canvas.height / canvas.clientHeight;
  const ctx = canvas.getContext('2d');
  if (!ctx) return { unreadable: true };
  const data = ctx.getImageData(
    Math.round(box.left * sx), Math.round(box.top * sy),
    Math.round(box.width * sx), Math.round(box.height * sy),
  ).data;

  let brightest = 0;
  let bright = 0;
  for (let i = 0; i < data.length; i += 4) {
    const v = Math.max(data[i], data[i + 1], data[i + 2]);
    brightest = Math.max(brightest, v);
    if (v > 90) bright += 1;
  }
  clock.style.visibility = '';
  return { brightest, brightPixels: bright, sampled: data.length / 4,
           box: { w: Math.round(box.width), h: Math.round(box.height) } };
});
console.log('CORE PIXELS UNDER THE CLOCK', JSON.stringify(probe));

const cdp = await page.context().newCDPSession(page);
const shot = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: false });
await (await import('node:fs/promises')).writeFile(
  'panel.png', Buffer.from(shot.data, 'base64'));
console.log('wrote panel.png');
await browser.close();
server.close();
