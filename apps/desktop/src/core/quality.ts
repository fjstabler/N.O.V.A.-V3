/**
 * Quality tiers, and the watchdog that enforces them.
 *
 * The spec asks for 60 FPS. On an integrated GPU driving a 4K panel, the honest
 * way to get there is to render fewer pixels rather than to drop frames — so
 * each tier caps device-pixel-ratio, bloom resolution and particle count, and
 * the watchdog steps down a tier if the measured frame time stays above budget.
 *
 * Degradation is one-way within a session. Oscillating between tiers is more
 * visible than simply running at the lower one.
 */

export type QualityName = 'low' | 'balanced' | 'high' | 'ultra';

export interface QualityTier {
  name: QualityName;
  /** Upper bound on devicePixelRatio. */
  maxDpr: number;
  /** Bloom buffer size as a fraction of the scene buffer. */
  bloomScale: number;
  /** Number of blur iterations; each is a horizontal + vertical pair. */
  blurPasses: number;
  particles: number;
  starfield: boolean;
  grain: number;
  aberration: number;
}

export const TIERS: Record<QualityName, QualityTier> = {
  low: {
    name: 'low',
    maxDpr: 1,
    bloomScale: 0.25,
    blurPasses: 1,
    particles: 0,
    starfield: false,
    grain: 0.004,
    aberration: 0,
  },
  balanced: {
    name: 'balanced',
    maxDpr: 1.25,
    bloomScale: 0.35,
    blurPasses: 1,
    particles: 220,
    starfield: true,
    grain: 0.006,
    aberration: 0.0012,
  },
  high: {
    name: 'high',
    maxDpr: 1.75,
    bloomScale: 0.5,
    blurPasses: 2,
    particles: 520,
    starfield: true,
    grain: 0.008,
    aberration: 0.002,
  },
  ultra: {
    name: 'ultra',
    maxDpr: 2,
    bloomScale: 0.5,
    blurPasses: 3,
    particles: 900,
    starfield: true,
    grain: 0.009,
    aberration: 0.0028,
  },
};

const ORDER: QualityName[] = ['low', 'balanced', 'high', 'ultra'];

export function tierFor(name: string): QualityTier {
  return TIERS[name as QualityName] ?? TIERS.high;
}

export function lowerTier(name: QualityName): QualityName | null {
  const index = ORDER.indexOf(name);
  return index > 0 ? ORDER[index - 1]! : null;
}

/**
 * Watches frame times and reports when the renderer should step down.
 *
 * Uses a rolling median rather than a mean: one 200 ms hitch from a garbage
 * collection or a window resize should not trigger a permanent downgrade, but
 * a sustained 22 ms frame time should.
 */
export class FrameWatchdog {
  private readonly samples: number[] = [];
  private downgrades = 0;
  private cooldownUntil = 0;

  constructor(
    /** Frame budget in milliseconds. */
    private readonly budgetMs = 20,
    private readonly windowSize = 90,
    /** Give the renderer time to settle before judging it. */
    graceMs = 3000,
  ) {
    this.cooldownUntil = performance.now() + graceMs;
  }

  /** Record a frame. Returns true when a downgrade is warranted. */
  record(frameMs: number): boolean {
    this.samples.push(frameMs);
    if (this.samples.length > this.windowSize) this.samples.shift();

    const now = performance.now();
    if (now < this.cooldownUntil) return false;
    if (this.samples.length < this.windowSize) return false;
    if (this.downgrades >= 3) return false;

    const sorted = [...this.samples].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)] ?? 0;
    if (median <= this.budgetMs) return false;

    this.downgrades += 1;
    this.samples.length = 0;
    this.cooldownUntil = now + 5000;
    return true;
  }

  get medianFrameMs(): number {
    if (this.samples.length === 0) return 0;
    const sorted = [...this.samples].sort((a, b) => a - b);
    return sorted[Math.floor(sorted.length / 2)] ?? 0;
  }
}

/** A rolling FPS readout for the developer overlay. */
export class FpsMeter {
  private frames = 0;
  private since = performance.now();
  private current = 0;

  tick(): number {
    this.frames += 1;
    const now = performance.now();
    const elapsed = now - this.since;
    if (elapsed >= 500) {
      this.current = (this.frames * 1000) / elapsed;
      this.frames = 0;
      this.since = now;
    }
    return this.current;
  }

  get fps(): number {
    return this.current;
  }
}
