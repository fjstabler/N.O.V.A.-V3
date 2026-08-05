/**
 * A hand-drawn Canvas2D "Core" for the mobile client.
 *
 * Deliberately not a port of the desktop's WebGL renderer
 * (apps/desktop/src/core/) — that one needs a build step and per-pixel
 * shader control this page has no reason to carry, and the mobile client is
 * built to ship as plain <script> tags with nothing to compile. Same visual
 * language instead: a glowing centre and a few rotating rings, reacting to
 * state and, while listening, to the microphone level — close enough to
 * feel like the same assistant without dragging its rendering pipeline
 * along.
 */
(() => {
  'use strict';

  const canvas = document.getElementById('core');
  const ctx = canvas.getContext('2d');

  const PALETTE = {
    idle: [79, 184, 255],
    listening: [255, 255, 255],
    thinking: [79, 184, 255],
    speaking: [126, 224, 179],
    error: [255, 107, 107],
  };

  const RINGS = [
    { radius: 0.43, speed: 0.16, width: 0.011, dash: 0 },
    { radius: 0.33, speed: -0.24, width: 0.015, dash: 9 },
    { radius: 0.23, speed: 0.33, width: 0.02, dash: 0 },
  ];

  let state = 'idle';
  let level = 0; // 0..1 microphone loudness, fed in while listening
  const angle = RINGS.map(() => Math.random() * Math.PI * 2);
  let pulse = 0;
  let lastFrame = performance.now();

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
  }
  window.addEventListener('resize', resize);

  function speedScaleFor(currentState) {
    if (currentState === 'listening') return 1 + level * 1.6;
    if (currentState === 'thinking') return 1.7;
    if (currentState === 'speaking') return 1.3;
    if (currentState === 'error') return 0.6;
    return 0.55;
  }

  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min((now - lastFrame) / 1000, 1 / 15);
    lastFrame = now;

    const scale = speedScaleFor(state);
    for (let i = 0; i < RINGS.length; i += 1) angle[i] += RINGS[i].speed * scale * dt;
    pulse += dt * (state === 'speaking' ? 3.4 : state === 'listening' ? 2.6 : 1.1);

    draw();
  }

  function draw() {
    const { width, height } = canvas;
    if (!width || !height) return;
    const cx = width / 2;
    const cy = height / 2;
    const unit = Math.min(width, height);
    const [r, g, b] = PALETTE[state] || PALETTE.idle;

    ctx.clearRect(0, 0, width, height);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.globalCompositeOperation = 'lighter';

    const haloRadius = unit * 0.48;
    const halo = ctx.createRadialGradient(0, 0, 0, 0, 0, haloRadius);
    const glow = 0.12 + 0.08 * Math.sin(pulse) + (state === 'listening' ? level * 0.25 : 0);
    halo.addColorStop(0, `rgba(${r},${g},${b},${glow})`);
    halo.addColorStop(1, `rgba(${r},${g},${b},0)`);
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(0, 0, haloRadius, 0, Math.PI * 2);
    ctx.fill();

    RINGS.forEach((ring, i) => {
      ctx.save();
      ctx.rotate(angle[i]);
      ctx.strokeStyle = `rgba(${r},${g},${b},${0.32 + 0.24 * Math.sin(pulse + i)})`;
      ctx.lineWidth = Math.max(1, ring.width * unit);
      if (ring.dash) {
        const circumference = 2 * Math.PI * ring.radius * unit;
        const segment = circumference / ring.dash;
        ctx.setLineDash([segment * 0.5, segment * 0.5]);
      } else {
        ctx.setLineDash([]);
      }
      ctx.beginPath();
      ctx.arc(0, 0, ring.radius * unit, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();
    });

    const coreRadius = unit * (0.09 + 0.02 * Math.sin(pulse * 1.3) + (state === 'listening' ? level * 0.06 : 0));
    const core = ctx.createRadialGradient(0, 0, 0, 0, 0, Math.max(1, coreRadius));
    core.addColorStop(0, 'rgba(255,255,255,0.95)');
    core.addColorStop(0.5, `rgba(${r},${g},${b},0.7)`);
    core.addColorStop(1, `rgba(${r},${g},${b},0)`);
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(0, 0, Math.max(1, coreRadius), 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();
  }

  resize();
  requestAnimationFrame(frame);

  window.NovaCoreView = {
    setState(next) {
      state = next;
    },
    setLevel(next) {
      level = Math.max(0, Math.min(1, next));
    },
  };
})();
