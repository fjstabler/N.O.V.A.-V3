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

// A core, as far as the page is concerned.
//
// Without one the shell sits in `booting` forever, and `booting` is not a
// state the settle applies to — so the whole fifteen-minute behaviour was
// unreachable from here and had to be taken on the unit tests' word. Twenty
// lines of fake server buys the real thing: a page that reaches `idle`,
// settles, and can be woken up again.
//
// The three sync requests are answered with errors on purpose. The store logs
// the failure and keeps its defaults, which is exactly what the probe has
// always rendered — feeding it invented settings would mean measuring a
// configuration nobody runs.
let core = null;
try {
  const { WebSocketServer } = await import('ws');
  const wss = new WebSocketServer({ noServer: true });
  const envelope = (kind, topic, payload, id) =>
    JSON.stringify({ v: 1, kind, topic, id: id ?? `p${Math.random().toString(36).slice(2)}`,
                     ts: Date.now(), payload });

  wss.on('connection', (socket) => {
    core = socket;
    socket.send(envelope('hello', 'hello', { version: 1 }));
    socket.send(envelope('event', 'state.changed', { state: 'idle' }));
    socket.on('message', (raw) => {
      let request;
      try { request = JSON.parse(raw.toString()); } catch { return; }
      if (request.kind !== 'request') return;
      socket.send(envelope('error', request.topic,
        { code: 'nova.unavailable', message: 'probe core has no settings' }, request.id));
    });
  });
  server.on('upgrade', (req, socket, head) =>
    wss.handleUpgrade(req, socket, head, (ws) => wss.emit('connection', ws, req)));
} catch {
  console.log('[probe] no `ws` module — the settle section will stay in booting.');
}

/** Push a state change, the way the core would. */
const setCoreState = (state) =>
  core?.send(JSON.stringify({ v: 1, kind: 'event', topic: 'state.changed',
                              id: `s${Date.now()}`, ts: Date.now(), payload: { state } }));

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


// ---------------------------------------------------------------- overlays
//
// Everything that can appear over the idle screen, measured against the clock.
// The clock used to live in the top-left corner and every one of these was
// positioned on the assumption that the middle was empty — the surface slots
// say so in a comment. Moving the clock to the centre invalidated all of it at
// once, and only some of them collide, which is exactly the sort of thing that
// ships unnoticed because it needs a notification or a camera view to show up.
const OVERLAYS = [
  // Ten is `notifications.max_visible`'s ceiling, not a typical stack.
  ['notifications top-center', 'notifications notifications--top-center', 10],
  ['notifications top-right', 'notifications notifications--top-right', 10],
  ['notifications bottom-right', 'notifications notifications--bottom-right', 10],
  ['notifications bottom-left', 'notifications notifications--bottom-left', 10],
  ['surface right-mid', 'surface surface--slot-right-mid surface--is-in', 0],
  ['surface top-right', 'surface surface--slot-top-right surface--is-in', 0],
  ['surface bottom-right', 'surface surface--slot-bottom-right surface--is-in', 0],
  ['surface bottom-left', 'surface surface--slot-bottom-left surface--is-in', 0],
  ['console', 'console', 0],
];

const collisions = await page.evaluate((overlays) => {
  const clock = document.querySelector('.clock').getBoundingClientRect();
  const overlap = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? { w: Math.round(w), h: Math.round(h) } : null;
  };

  const results = [];
  for (const [label, classes, cards] of overlays) {
    const el = document.createElement('div');
    el.className = classes;
    if (cards) {
      el.innerHTML = Array.from({ length: cards }, (_, i) =>
        `<div class="notification notification--info"><div class="notification__rule"></div>` +
        `<div class="notification__body"><div class="notification__title">Card spend ${i}</div>` +
        `<div class="notification__text">50 pounds at Currys. 940 pounds left.</div></div></div>`,
      ).join('');
    } else if (classes.startsWith('surface')) {
      el.innerHTML =
        '<div class="surface__card"><div class="surface__bar">' +
        '<span class="surface__title">Front door</span></div>' +
        '<div class="surface__placeholder" style="height:190px">camera</div></div>';
    } else {
      el.innerHTML = '<input class="console__input" placeholder="Ask N.O.V.A." />';
    }
    document.body.appendChild(el);
    const box = el.getBoundingClientRect();
    results.push({
      label,
      box: { l: Math.round(box.left), t: Math.round(box.top),
             r: Math.round(box.right), b: Math.round(box.bottom) },
      hits: overlap(clock, box),
    });
    el.remove();
  }
  return { clock: { l: Math.round(clock.left), t: Math.round(clock.top),
                    r: Math.round(clock.right), b: Math.round(clock.bottom) }, results };
}, OVERLAYS);

console.log('\nCLOCK', JSON.stringify(collisions.clock));
for (const r of collisions.results) {
  console.log(r.hits ? `  OVERLAP  ${r.label.padEnd(28)} by ${r.hits.w}x${r.hits.h}px  ${JSON.stringify(r.box)}`
                     : `  clear    ${r.label.padEnd(28)} ${JSON.stringify(r.box)}`);
}

const cdp = await page.context().newCDPSession(page);
const shot = await cdp.send('Page.captureScreenshot', { format: 'png', fromSurface: false });
await (await import('node:fs/promises')).writeFile(
  'panel.png', Buffer.from(shot.data, 'base64'));
console.log('wrote panel.png');

// ------------------------------------------------------- the idle settle
//
// After fifteen minutes with nobody talking to it the Core drops to its
// IDLE_DIMMED profile: quieter, slower, and — the part that matters here —
// scaled to 0.88. The rings move *inward* at that scale, toward the clock, so
// "it clears the clock" has to hold settled as well as awake.
//
// Rather than wait a quarter of an hour, any timer longer than a minute is
// shortened before the page loads. Not to milliseconds: the awake sample has
// to happen before it fires, or both readings are of the same state and the
// comparison says nothing. That is exactly what the first version of this did.
const SETTLE_AFTER_MS = 15_000;
/** How long the springs need to arrive after a state change. */
const SPRING_SETTLE_MS = 3000;

await page.close(); // so the fake core's latest connection is the one below
const settled = await browser.newPage({ viewport: { width: 960, height: 480 }, deviceScaleFactor: 1 });
await settled.addInitScript((after) => {
  localStorage.setItem('nova.bridge.token', 'x');
  const real = window.setTimeout.bind(window);
  window.setTimeout = ((fn, ms, ...rest) => real(fn, ms >= 60_000 ? after : ms, ...rest));
}, SETTLE_AFTER_MS);
await settled.goto('http://127.0.0.1:4173/app/', { waitUntil: 'domcontentloaded' });
await settled.evaluate(() => document.fonts.ready);

/**
 * Whole-screen light, to tell an awake Core from a settled one.
 *
 * `light` is what the Core itself emits: the backdrop is a flat #04060d, and
 * counting it drags every comparison toward 1:1 — most of the screen is
 * backdrop, so a real 20% drop in the Core reads as 4% once it is averaged in.
 */
const BACKDROP = 13; // max channel of #04060d

const brightness = () =>
  settled.evaluate((floor) => {
    const canvas = document.querySelector('canvas.nova-core');
    const { data } = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
    let lit = 0;
    let light = 0;
    for (let i = 0; i < data.length; i += 4) {
      const v = Math.max(data[i], data[i + 1], data[i + 2]);
      if (v > floor) light += v - floor;
      if (v > 60) lit += 1;
    }
    return { lit, light: Math.round(light / 1000) };
  }, BACKDROP);

/**
 * How fast the picture is actually moving, in change per second.
 *
 * The settle is not only a dimming — the rings drop from spin 0.55 to 0.12 —
 * and a brightness count cannot see the difference between a Core that has
 * slowed down and one that has merely gone dim. This can: it measures how much
 * the picture changes between one frame and the next.
 *
 * Every frame is divided by its own mean first. Without that the measurement
 * would fall simply because the Core got darker, and would report a slowdown
 * that had not happened — the exact mistake the brightness figures alone
 * invite. Normalised, a uniformly dimmer Core moving at the same speed scores
 * the same, and only a genuine change of pace moves the number.
 */
const motionRate = (frames) =>
  settled.evaluate(async (count) => {
    const canvas = document.querySelector('canvas.nova-core');
    const ctx = canvas.getContext('2d');
    const STRIDE = 2; // every other pixel; the rings are far wider than that

    const sample = () => {
      const { data } = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const out = new Float32Array(Math.ceil(data.length / 4 / STRIDE));
      let total = 0;
      for (let i = 0, n = 0; n < out.length; i += 4 * STRIDE, n += 1) {
        const v = Math.max(data[i], data[i + 1], data[i + 2]);
        out[n] = v;
        total += v;
      }
      const mean = total / out.length || 1;
      for (let n = 0; n < out.length; n += 1) out[n] /= mean;
      return out;
    };

    const rates = [];
    let previous = sample();
    let last = performance.now();
    for (let f = 0; f < count; f += 1) {
      await new Promise((r) => requestAnimationFrame(() => r()));
      const now = performance.now();
      const current = sample();
      let diff = 0;
      for (let n = 0; n < current.length; n += 1) diff += Math.abs(current[n] - previous[n]);
      rates.push((diff / current.length) * (1000 / Math.max(now - last, 1)));
      previous = current;
      last = now;
    }

    // The median, not the mean: a dropped frame doubles one interval and no
    // amount of averaging hides it.
    rates.sort((a, b) => a - b);
    return +rates[Math.floor(rates.length / 2)].toFixed(4);
  }, frames);

/** Wait for the shell to report a state, or give up. */
const waitForState = async (want, ms) => {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    if ((await settled.evaluate(() => document.documentElement.dataset.state)) === want) return true;
    await settled.waitForTimeout(100);
  }
  return false;
};

/**
 * The brightest thing the Core puts under the clock, across many frames.
 *
 * One frame is not enough: the rings turn, and a dashed one has bright
 * segments that sweep past. The worst frame is the one that matters.
 *
 * The threshold is taken from the picture rather than written down. A fixed
 * number cannot tell "a ring is crossing the digits" from "the digits sit on a
 * soft halo", because the halo is additive and its middle is genuinely lit —
 * that is the design. So each frame is measured against the brightest pixel
 * anywhere on the canvas, which is always on a ring: anything under the clock
 * at 60% of that is a ring line, and the `spread` of what merely clears a low
 * fixed threshold says which of the two it is. A ring crossing lights a thin
 * band; a halo lights the whole box evenly.
 */
const RING_FRACTION = 0.6;
const GLOW_FLOOR = 90;

const underClock = (frames) =>
  settled.evaluate(async ({ count, ringFraction, glowFloor }) => {
    const clock = document.querySelector('.clock');
    const canvas = document.querySelector('canvas.nova-core');
    const box = clock.getBoundingClientRect();
    clock.style.visibility = 'hidden';
    const sx = canvas.width / canvas.clientWidth;
    const sy = canvas.height / canvas.clientHeight;
    const rect = [Math.round(box.left * sx), Math.round(box.top * sy),
                  Math.round(box.width * sx), Math.round(box.height * sy)];
    const ctx = canvas.getContext('2d');

    let peakUnder = 0;
    let peakAnywhere = 0;
    let worstRingPixels = 0;
    let worstGlow = 0;
    let spread = null;
    for (let f = 0; f < count; f += 1) {
      await new Promise((r) => requestAnimationFrame(() => r()));

      // The reference: the brightest pixel the Core draws this frame. It is on
      // a ring, so it is what a ring line looks like right now.
      const whole = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
      let framePeak = 0;
      for (let i = 0; i < whole.length; i += 4) {
        const v = Math.max(whole[i], whole[i + 1], whole[i + 2]);
        if (v > framePeak) framePeak = v;
      }
      peakAnywhere = Math.max(peakAnywhere, framePeak);

      const { data } = ctx.getImageData(...rect);
      const ringLine = framePeak * ringFraction;
      let ring = 0;
      let glow = 0;
      const hit = { l: 1e9, t: 1e9, r: -1, b: -1 };
      for (let i = 0; i < data.length; i += 4) {
        const v = Math.max(data[i], data[i + 1], data[i + 2]);
        if (v > peakUnder) peakUnder = v;
        if (v >= ringLine) ring += 1;
        if (v > glowFloor) {
          glow += 1;
          const px = (i / 4) % rect[2];
          const py = Math.floor(i / 4 / rect[2]);
          hit.l = Math.min(hit.l, px); hit.r = Math.max(hit.r, px);
          hit.t = Math.min(hit.t, py); hit.b = Math.max(hit.b, py);
        }
      }
      worstRingPixels = Math.max(worstRingPixels, ring);
      if (glow > worstGlow) {
        worstGlow = glow;
        spread = glow ? { ...hit, of: `${rect[2]}x${rect[3]}` } : null;
      }
    }
    clock.style.visibility = '';
    return {
      peakUnder,
      peakAnywhere,
      ringPixels: worstRingPixels,
      glowPixels: worstGlow,
      spread,
      frames: count,
    };
  }, { count: frames, ringFraction: RING_FRACTION, glowFloor: GLOW_FLOOR });

const reachedIdle = await waitForState('idle', 8000);
// Long enough for the springs to arrive. Every value is sprung, so a sample
// taken straight after the state change is still on its way up out of
// `booting` — which is dimmer and smaller than idle, so an early baseline
// understates the awake Core and flatters the settle. Six hundred ms was not
// enough, and the figures it produced said the Core barely dimmed at all.
await settled.waitForTimeout(SPRING_SETTLE_MS);
const awake = await brightness();
const awakeMotion = await motionRate(60);
const awakeUnder = await underClock(60);

// Past the (shortened) fifteen minutes, plus a moment for the springs.
await settled.waitForTimeout(SETTLE_AFTER_MS + 6000);
const quiet = await brightness();
const quietMotion = await motionRate(60);
const quietUnder = await underClock(60);

// And back: the other half of the feature is that it wakes up again. A turn
// starting is any state that is not idle, which is what resets the timer.
setCoreState('listening');
await settled.waitForTimeout(400);
setCoreState('idle');
await settled.waitForTimeout(SPRING_SETTLE_MS);
const woken = await brightness();
const wokenMotion = await motionRate(60);

const observedState = await settled.evaluate(() => document.documentElement.dataset.state);

const report = (label, whole, motion, under) => {
  console.log(`  ${label.padEnd(9)} light ${String(whole.light).padStart(5)}k  ` +
              `lit ${String(whole.lit).padStart(5)}  motion ${motion}/s`);
  if (!under) return;
  const area = under.spread ? (under.spread.r - under.spread.l + 1) * (under.spread.b - under.spread.t + 1) : 0;
  console.log(
    `    under the clock: peak ${under.peakUnder} against a ring at ${under.peakAnywhere}; ` +
      `${under.ringPixels} ring-bright pixel(s) over ${under.frames} frames`,
  );
  console.log(
    `    the ${under.glowPixels} pixel(s) over ${GLOW_FLOOR} cover ${JSON.stringify(under.spread)} ` +
      `— ${area ? Math.round((under.glowPixels / area) * 100) : 0}% of that box filled, ` +
      `${under.ringPixels === 0 ? 'a halo, not a line' : 'A LINE'}`,
  );
};

console.log('\nIDLE SETTLE');
report('awake', awake, awakeMotion, awakeUnder);
report('settled', quiet, quietMotion, quietUnder);
report('woken', woken, wokenMotion, null);
// What is actually behind the clock, to look at rather than to total up.
await settled.evaluate(() => { document.querySelector('.clock').style.visibility = 'hidden'; });
await settled.waitForTimeout(200);
const cdp2 = await settled.context().newCDPSession(settled);
const bare = await cdp2.send('Page.captureScreenshot', { format: 'png', fromSurface: false });
await (await import('node:fs/promises')).writeFile('panel-no-clock.png', Buffer.from(bare.data, 'base64'));
console.log('  wrote panel-no-clock.png');
console.log('  assistant state:', observedState);
if (!reachedIdle) {
  // The settled profile is only ever chosen while the assistant is `idle`, so
  // without that this section measured nothing. Say so rather than reporting a
  // settle that never happened.
  console.log('  never reached idle — nothing below was measured in the settled state.');
} else {
  const pc = (a, b) => `${Math.round((b / a) * 100)}%`;
  console.log(`  dims:   light ${awake.light}k -> ${quiet.light}k (${pc(awake.light, quiet.light)})`,
              quiet.light < awake.light * 0.9 ? 'YES' : 'NO');
  console.log(`  slows:  motion ${awakeMotion} -> ${quietMotion} (${pc(awakeMotion, quietMotion)}/s),`,
              `spin target ${Math.round((0.12 / 0.55) * 100)}%`,
              quietMotion < awakeMotion * 0.6 ? 'YES' : 'NO');
  console.log(`  wakes:  motion back to ${wokenMotion}/s (${pc(awakeMotion, wokenMotion)} of awake)`,
              wokenMotion > awakeMotion * 0.8 ? 'YES' : 'NO');
}
await settled.close();
await browser.close();
server.close();
