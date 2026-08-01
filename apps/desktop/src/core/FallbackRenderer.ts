/**
 * Canvas2D fallback for machines without WebGL2.
 *
 * Deliberately simpler, not merely a downgraded copy: Canvas2D cannot do the
 * per-pixel plasma or a real bloom at 60 FPS, so instead of approximating them
 * badly this draws crisp geometry and uses layered `shadowBlur` for glow. The
 * result is a different look in the same visual language rather than a smeared
 * imitation of the WebGL one.
 *
 * Same public interface as `CoreRenderer`, so the component that owns it does
 * not care which is running.
 */

import type { NovaState } from '@protocol';
import { CoreMotion, PROFILES, RINGS, paletteFor, type Palette } from './visual';
import { FpsMeter } from './quality';

export interface FallbackOptions {
  theme: string;
  coreScale: number;
  reduceMotion: boolean;
  onFps?: (fps: number) => void;
}

function css(colour: readonly [number, number, number], alpha = 1): string {
  const [r, g, b] = colour;
  return `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${alpha})`;
}

function mix(
  a: readonly [number, number, number],
  b: readonly [number, number, number],
  t: number,
): [number, number, number] {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

export class FallbackRenderer {
  private readonly context: CanvasRenderingContext2D;
  private readonly motion = new CoreMotion();
  private readonly fpsMeter = new FpsMeter();
  private readonly ringAngles = new Float32Array(RINGS.length);

  private palette: Palette;
  private options: FallbackOptions;
  private state: NovaState = 'booting';
  private level = 0;
  private clock = 0;
  private lastFrame = 0;
  private rafHandle = 0;
  private running = false;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    options: FallbackOptions,
  ) {
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('Canvas2D is not available');
    this.context = context;
    this.options = options;
    this.palette = paletteFor(options.theme);
    this.motion.snapTo(PROFILES.booting);
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.lastFrame = performance.now();
    this.rafHandle = requestAnimationFrame(this.frame);
  }

  stop(): void {
    this.running = false;
    if (this.rafHandle) cancelAnimationFrame(this.rafHandle);
    this.rafHandle = 0;
  }

  setState(state: NovaState): void {
    if (state === this.state) return;
    if (state !== 'idle' && state !== 'booting') this.motion.flash(0.6);
    this.state = state;
  }

  setLevel(level: number): void {
    this.level = Math.max(0, Math.min(1, level));
  }

  pulse(strength = 0.8): void {
    this.motion.flash(strength);
  }

  updateOptions(options: Partial<FallbackOptions>): void {
    this.options = { ...this.options, ...options };
    if (options.theme) this.palette = paletteFor(options.theme);
  }

  resize(): void {
    // Cap DPR at 1.5: Canvas2D shadowBlur is fill-rate bound and a 4K backing
    // store makes the glow passes unaffordable.
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round((rect.width || window.innerWidth) * dpr));
    this.canvas.height = Math.max(1, Math.round((rect.height || window.innerHeight) * dpr));
  }

  get fps(): number {
    return this.fpsMeter.fps;
  }

  get quality(): string {
    return 'canvas2d';
  }

  dispose(): void {
    this.stop();
  }

  private frame = (now: number): void => {
    if (!this.running) return;
    this.rafHandle = requestAnimationFrame(this.frame);

    const dt = Math.min((now - this.lastFrame) / 1000, 1 / 15);
    this.lastFrame = now;
    this.clock += dt * (this.options.reduceMotion ? 0.35 : 1);

    const profile = PROFILES[this.state] ?? PROFILES.idle;
    this.motion.step(profile, this.level, dt);
    for (let i = 0; i < RINGS.length; i += 1) {
      this.ringAngles[i] =
        (this.ringAngles[i]! + RINGS[i]!.speed * this.motion.spin.value * dt) % (Math.PI * 2);
    }

    this.draw();
    this.options.onFps?.(this.fpsMeter.tick());
  };

  private draw(): void {
    const { context: ctx, canvas } = this;
    const width = canvas.width;
    const height = canvas.height;
    const centreX = width / 2;
    const centreY = height / 2;
    const unit = Math.min(width, height);

    const energy = this.motion.energy.value;
    const alert = this.motion.alert.value;
    const accent = mix(this.palette.accent, this.palette.alert, alert);
    const accentAlt = mix(this.palette.accentAlt, this.palette.alert, alert);
    const scale = this.motion.scale.value * this.options.coreScale;

    ctx.fillStyle = '#04060d';
    ctx.fillRect(0, 0, width, height);

    ctx.save();
    ctx.translate(centreX, centreY);
    ctx.globalCompositeOperation = 'lighter';

    // Outer halo.
    const haloRadius = unit * 0.5 * scale;
    const halo = ctx.createRadialGradient(0, 0, 0, 0, 0, haloRadius);
    halo.addColorStop(0, css(accent, 0.16 + 0.12 * energy));
    halo.addColorStop(1, css(accent, 0));
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(0, 0, haloRadius, 0, Math.PI * 2);
    ctx.fill();

    // Rings.
    for (let i = 0; i < RINGS.length; i += 1) {
      const ring = RINGS[i]!;
      const radius = ring.radius * unit * scale;
      const tilt = ring.tilt * (0.86 + 0.14 * Math.sin(this.clock * 0.21 + i * 1.7));
      const colour = mix(accent, accentAlt, i / Math.max(RINGS.length - 1, 1));

      ctx.save();
      ctx.rotate(this.ringAngles[i]!);
      ctx.scale(1, Math.max(tilt, 0.06));
      ctx.strokeStyle = css(colour, 0.32 + 0.55 * energy * ring.brightness);
      ctx.lineWidth = Math.max(1, ring.thickness * unit * scale * 1.6);
      ctx.shadowBlur = 18 * (0.5 + energy);
      ctx.shadowColor = css(colour, 0.8);

      if (ring.dashes > 0) {
        const circumference = 2 * Math.PI * radius;
        const segment = circumference / ring.dashes;
        ctx.setLineDash([segment * 0.45, segment * 0.55]);
        ctx.lineDashOffset = -this.clock * 40 * (i % 2 === 0 ? 1 : -1);
      } else {
        ctx.setLineDash([]);
      }

      ctx.beginPath();
      ctx.arc(0, 0, radius, ring.arcStart, ring.arcStart + ring.arcLength);
      ctx.stroke();
      ctx.restore();
    }

    // Luminous centre.
    const breath = 1 + 0.06 * Math.sin(this.clock * 0.55) * this.motion.breath.value;
    const coreRadius =
      this.motion.coreRadius.value * unit * scale * breath * (1 + 0.25 * this.motion.level.value);
    const core = ctx.createRadialGradient(0, 0, 0, 0, 0, coreRadius);
    core.addColorStop(0, `rgba(255, 255, 255, ${0.85 + 0.15 * this.motion.pulse.value})`);
    core.addColorStop(0.35, css(accentAlt, 0.7));
    core.addColorStop(1, css(accent, 0));
    ctx.fillStyle = core;
    ctx.shadowBlur = 0;
    ctx.beginPath();
    ctx.arc(0, 0, coreRadius, 0, Math.PI * 2);
    ctx.fill();

    ctx.restore();

    // Vignette, drawn normally rather than additively.
    const vignette = ctx.createRadialGradient(
      centreX,
      centreY,
      unit * 0.3,
      centreX,
      centreY,
      unit * 0.78,
    );
    vignette.addColorStop(0, 'rgba(0, 0, 0, 0)');
    vignette.addColorStop(1, 'rgba(0, 0, 0, 0.55)');
    ctx.fillStyle = vignette;
    ctx.fillRect(0, 0, width, height);
  }
}
