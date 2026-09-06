/**
 * The Core, drawn with Canvas2D — a faithful port of the desktop app's own
 * software-rendering fallback (apps/desktop/src/core/FallbackRenderer.ts and
 * visual.ts), not a fresh approximation. Same ring geometry, same critically
 * damped spring motion, same palette — so this reads as the same assistant,
 * not a different one wearing its colours. Ported by hand rather than
 * bundled from the TypeScript source because this page ships as plain
 * <script> tags with no build step; the desktop's WebGL renderer (the 5-pass
 * shader pipeline) is not ported at all — that one needs a build and GPU
 * shader control this page has no reason to carry, and this fallback is
 * already designed to be "a different look in the same visual language"
 * rather than a smeared imitation of it, per its own source comment.
 */
(() => {
  'use strict';

  const canvas = document.getElementById('core');
  // alpha:true (unlike the desktop fallback, which owns the whole screen and
  // paints its own background) — this canvas is a small element inside the
  // page, so it needs to stay transparent and let the page's own background
  // show through, or it reads as a boxed-in square instead of the Core just
  // floating in the dark.
  const ctx = canvas.getContext('2d', { alpha: true });

  // ---------------------------------------------------------------- profiles

  const IDLE = {
    energy: 0.34, turbulence: 0.18, spin: 0.55, coreRadius: 0.12,
    scale: 1, converge: 0, alert: 0, bloom: 0.85, breath: 1,
  };

  const PROFILES = {
    idle: IDLE,
    listening: { ...IDLE, energy: 0.72, turbulence: 0.3, spin: 0.95, coreRadius: 0.15, scale: 1.06, bloom: 1.15, breath: 0.4 },
    thinking: { ...IDLE, energy: 0.86, turbulence: 0.78, spin: 2.4, coreRadius: 0.11, scale: 0.97, converge: 0.65, bloom: 1.3, breath: 0.15 },
    speaking: { ...IDLE, energy: 0.8, turbulence: 0.42, spin: 0.8, coreRadius: 0.16, scale: 1.03, bloom: 1.25, breath: 0.3 },
    error: { ...IDLE, energy: 0.6, turbulence: 0.95, spin: 0.3, coreRadius: 0.1, scale: 0.94, alert: 1, bloom: 1.1, breath: 0.2 },
  };

  // ------------------------------------------------------------ ring geometry

  const TAU = Math.PI * 2;

  const RINGS = [
    { radius: 0.17, thickness: 0.0032, tilt: 0.95, speed: 0.9, brightness: 0.9, arcStart: 0, arcLength: TAU, dashes: 0 },
    { radius: 0.215, thickness: 0.0024, tilt: 0.42, speed: -1.35, brightness: 1.05, arcStart: 0.4, arcLength: TAU * 0.72, dashes: 0 },
    { radius: 0.26, thickness: 0.0038, tilt: 0.78, speed: 0.55, brightness: 0.8, arcStart: 0, arcLength: TAU, dashes: 42 },
    { radius: 0.315, thickness: 0.0021, tilt: 0.3, speed: -0.7, brightness: 0.95, arcStart: 2.1, arcLength: TAU * 0.45, dashes: 0 },
    { radius: 0.355, thickness: 0.0016, tilt: 0.88, speed: 0.32, brightness: 0.55, arcStart: 0, arcLength: TAU, dashes: 64 },
    { radius: 0.42, thickness: 0.0028, tilt: 0.55, speed: -0.24, brightness: 0.65, arcStart: 4.0, arcLength: TAU * 0.6, dashes: 0 },
    { radius: 0.48, thickness: 0.0014, tilt: 0.72, speed: 0.16, brightness: 0.4, arcStart: 0, arcLength: TAU, dashes: 0 },
  ];

  // nova-blue, the default theme — this page does not read the desktop's
  // appearance.theme setting, so it always uses the default palette.
  const PALETTE = { accent: [0.24, 0.58, 1.0], accentAlt: [0.45, 0.86, 1.0], alert: [1.0, 0.33, 0.28] };

  // -------------------------------------------------------------- spring physics

  class Spring {
    constructor(value, stiffness = 8) {
      this.value = value;
      this.stiffness = stiffness;
      this.velocity = 0;
    }
    step(target, dt) {
      const step = Math.min(dt, 1 / 20); // a backgrounded tab delivers a huge dt; clamp it
      const damping = 2 * Math.sqrt(this.stiffness);
      const acceleration = (target - this.value) * this.stiffness - this.velocity * damping;
      this.velocity += acceleration * step;
      this.value += this.velocity * step;
      return this.value;
    }
    snap(value) {
      this.value = value;
      this.velocity = 0;
    }
  }

  class CoreMotion {
    constructor() {
      this.energy = new Spring(IDLE.energy, 7);
      this.turbulence = new Spring(IDLE.turbulence, 5);
      this.spin = new Spring(IDLE.spin, 6);
      this.coreRadius = new Spring(IDLE.coreRadius, 9);
      this.scale = new Spring(IDLE.scale, 8);
      this.converge = new Spring(IDLE.converge, 5);
      this.alert = new Spring(IDLE.alert, 10);
      this.bloom = new Spring(IDLE.bloom, 6);
      this.breath = new Spring(IDLE.breath, 4);
      this.level = new Spring(0, 26);
      this.pulse = new Spring(0, 14);
      this.pulseTarget = 0;
    }
    step(profile, level, dt) {
      this.energy.step(profile.energy, dt);
      this.turbulence.step(profile.turbulence, dt);
      this.spin.step(profile.spin, dt);
      this.coreRadius.step(profile.coreRadius, dt);
      this.scale.step(profile.scale, dt);
      this.converge.step(profile.converge, dt);
      this.alert.step(profile.alert, dt);
      this.bloom.step(profile.bloom, dt);
      this.breath.step(profile.breath, dt);
      this.level.step(level, dt);
      this.pulse.step(this.pulseTarget, dt);
      this.pulseTarget = Math.max(0, this.pulseTarget - dt * 2.6);
    }
    flash(strength = 1) {
      this.pulseTarget = Math.min(1.4, this.pulseTarget + strength);
    }
    snapTo(profile) {
      this.energy.snap(profile.energy);
      this.turbulence.snap(profile.turbulence);
      this.spin.snap(profile.spin);
      this.coreRadius.snap(profile.coreRadius);
      this.scale.snap(profile.scale);
      this.converge.snap(profile.converge);
      this.alert.snap(profile.alert);
      this.bloom.snap(profile.bloom);
      this.breath.snap(profile.breath);
    }
  }

  function mix(a, b, t) {
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  }
  function rgba(colour, alpha) {
    return `rgba(${Math.round(colour[0] * 255)},${Math.round(colour[1] * 255)},${Math.round(colour[2] * 255)},${alpha})`;
  }

  // ------------------------------------------------------------------ render

  const motion = new CoreMotion();
  const ringAngles = new Float32Array(RINGS.length);
  let state = 'idle';
  let level = 0;
  let clock = 0;
  let lastFrame = performance.now();
  motion.snapTo(PROFILES.idle);

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5); // matches the desktop fallback's cap
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round((rect.width || 300) * dpr));
    canvas.height = Math.max(1, Math.round((rect.height || 300) * dpr));
  }
  window.addEventListener('resize', resize);

  function frame(now) {
    requestAnimationFrame(frame);
    const dt = Math.min((now - lastFrame) / 1000, 1 / 15);
    lastFrame = now;
    clock += dt;

    const profile = PROFILES[state] || PROFILES.idle;
    motion.step(profile, level, dt);
    for (let i = 0; i < RINGS.length; i += 1) {
      ringAngles[i] = (ringAngles[i] + RINGS[i].speed * motion.spin.value * dt) % TAU;
    }
    draw();
  }

  function draw() {
    const { width, height } = canvas;
    if (!width || !height) return;
    const centreX = width / 2;
    const centreY = height / 2;
    const unit = Math.min(width, height);

    const energy = motion.energy.value;
    const alertMix = motion.alert.value;
    const accent = mix(PALETTE.accent, PALETTE.alert, alertMix);
    const accentAlt = mix(PALETTE.accentAlt, PALETTE.alert, alertMix);
    const scale = motion.scale.value;

    ctx.clearRect(0, 0, width, height);

    ctx.save();
    ctx.translate(centreX, centreY);
    ctx.globalCompositeOperation = 'lighter';

    // Outer halo.
    const haloRadius = unit * 0.5 * scale;
    const halo = ctx.createRadialGradient(0, 0, 0, 0, 0, haloRadius);
    halo.addColorStop(0, rgba(accent, 0.16 + 0.12 * energy));
    halo.addColorStop(1, rgba(accent, 0));
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(0, 0, haloRadius, 0, TAU);
    ctx.fill();

    // Rings.
    for (let i = 0; i < RINGS.length; i += 1) {
      const ring = RINGS[i];
      const radius = ring.radius * unit * scale;
      const tilt = ring.tilt * (0.86 + 0.14 * Math.sin(clock * 0.21 + i * 1.7));
      const colour = mix(accent, accentAlt, i / Math.max(RINGS.length - 1, 1));

      ctx.save();
      ctx.rotate(ringAngles[i]);
      ctx.scale(1, Math.max(tilt, 0.06));
      ctx.strokeStyle = rgba(colour, 0.32 + 0.55 * energy * ring.brightness);
      ctx.lineWidth = Math.max(1, ring.thickness * unit * scale * 1.6);
      ctx.shadowBlur = 18 * (0.5 + energy);
      ctx.shadowColor = rgba(colour, 0.8);

      if (ring.dashes > 0) {
        const circumference = 2 * Math.PI * radius;
        const segment = circumference / ring.dashes;
        ctx.setLineDash([segment * 0.45, segment * 0.55]);
        ctx.lineDashOffset = -clock * 40 * (i % 2 === 0 ? 1 : -1);
      } else {
        ctx.setLineDash([]);
      }

      ctx.beginPath();
      ctx.arc(0, 0, radius, ring.arcStart, ring.arcStart + ring.arcLength);
      ctx.stroke();
      ctx.restore();
    }

    // Luminous centre.
    const breath = 1 + 0.06 * Math.sin(clock * 0.55) * motion.breath.value;
    const coreRadius = Math.max(
      1,
      motion.coreRadius.value * unit * scale * breath * (1 + 0.25 * motion.level.value),
    );
    const core = ctx.createRadialGradient(0, 0, 0, 0, 0, coreRadius);
    core.addColorStop(0, `rgba(255,255,255,${0.85 + 0.15 * motion.pulse.value})`);
    core.addColorStop(0.35, rgba(accentAlt, 0.7));
    core.addColorStop(1, rgba(accent, 0));
    ctx.fillStyle = core;
    ctx.shadowBlur = 0;
    ctx.beginPath();
    ctx.arc(0, 0, coreRadius, 0, TAU);
    ctx.fill();

    ctx.restore();

    // The desktop fallback also paints a vignette here, darkening the rect
    // toward its edges. On its canvas that's invisible — it owns the whole
    // screen, so the fade blends into its own near-black surroundings. This
    // canvas is a 340px square sitting on a page background of its own; the
    // same fillRect would paint a visible dark box behind the rings instead
    // of a fade, so it's dropped rather than ported.
  }

  resize();
  requestAnimationFrame(frame);

  window.NovaCoreView = {
    setState(next) {
      if (next === state) return;
      if (next !== 'idle') motion.flash(0.6); // same transient flash the desktop fires on a state change
      state = next;
    },
    setLevel(next) {
      level = Math.max(0, Math.min(1, next));
    },
  };
})();
