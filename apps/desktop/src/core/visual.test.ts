import { describe, expect, it } from 'vitest';
import { CoreMotion, IDLE_DIMMED, PALETTES, PROFILES, RINGS, Spring, paletteFor } from './visual';

const FRAME = 1 / 60;

/** Advance a spring toward a target for a number of simulated frames. */
function settle(spring: Spring, target: number, frames = 240): number {
  for (let i = 0; i < frames; i += 1) spring.step(target, FRAME);
  return spring.value;
}

describe('Spring', () => {
  it('converges on its target', () => {
    const spring = new Spring(0);
    expect(settle(spring, 1)).toBeCloseTo(1, 3);
  });

  it('never overshoots, so a state change cannot look like a bounce', () => {
    const spring = new Spring(0);
    let peak = 0;
    for (let i = 0; i < 240; i += 1) {
      peak = Math.max(peak, spring.step(1, FRAME));
    }
    // Critically damped: approaches from below and stops.
    expect(peak).toBeLessThanOrEqual(1.0001);
  });

  it('retargets mid-flight without discontinuity', () => {
    const spring = new Spring(0);
    for (let i = 0; i < 12; i += 1) spring.step(1, FRAME);
    const midpoint = spring.value;
    expect(midpoint).toBeGreaterThan(0);
    expect(midpoint).toBeLessThan(1);

    // Interrupting toward a new target must not jump the value.
    const next = spring.step(0, FRAME);
    expect(Math.abs(next - midpoint)).toBeLessThan(0.1);
  });

  it('survives a multi-second frame without exploding', () => {
    // A backgrounded window delivers exactly this; an unclamped spring diverges.
    const spring = new Spring(0);
    spring.step(1, 5);
    expect(Number.isFinite(spring.value)).toBe(true);
    expect(Math.abs(spring.value)).toBeLessThan(2);
  });

  it('snaps without carrying velocity across', () => {
    const spring = new Spring(0);
    for (let i = 0; i < 20; i += 1) spring.step(1, FRAME);
    spring.snap(0);
    expect(spring.value).toBe(0);
    // A carried-over velocity would move it on the next step even at target 0.
    expect(spring.step(0, FRAME)).toBe(0);
  });
});

describe('CoreMotion', () => {
  it('moves every parameter toward the active profile', () => {
    const motion = new CoreMotion();
    motion.snapTo(PROFILES.idle);
    for (let i = 0; i < 300; i += 1) motion.step(PROFILES.thinking, 0, FRAME);

    expect(motion.energy.value).toBeCloseTo(PROFILES.thinking.energy, 2);
    expect(motion.spin.value).toBeCloseTo(PROFILES.thinking.spin, 2);
    expect(motion.converge.value).toBeCloseTo(PROFILES.thinking.converge, 2);
  });

  it('decays a flash back to nothing', () => {
    const motion = new CoreMotion();
    motion.flash(1);
    for (let i = 0; i < 10; i += 1) motion.step(PROFILES.idle, 0, FRAME);
    expect(motion.pulse.value).toBeGreaterThan(0);

    for (let i = 0; i < 300; i += 1) motion.step(PROFILES.idle, 0, FRAME);
    expect(motion.pulse.value).toBeCloseTo(0, 2);
  });

  it('caps stacked flashes', () => {
    const motion = new CoreMotion();
    for (let i = 0; i < 20; i += 1) motion.flash(1);
    for (let i = 0; i < 30; i += 1) motion.step(PROFILES.idle, 0, FRAME);
    expect(motion.pulse.value).toBeLessThan(2);
  });

  it('tracks the audio level', () => {
    const motion = new CoreMotion();
    for (let i = 0; i < 120; i += 1) motion.step(PROFILES.speaking, 0.8, FRAME);
    expect(motion.level.value).toBeCloseTo(0.8, 1);
  });
});

describe('profiles', () => {
  it('covers every assistant state', () => {
    for (const state of [
      'booting',
      'idle',
      'listening',
      'thinking',
      'speaking',
      'error',
      'notifying',
    ] as const) {
      expect(PROFILES[state]).toBeDefined();
    }
  });

  it('makes working states more energetic than idle', () => {
    expect(PROFILES.thinking.energy).toBeGreaterThan(PROFILES.idle.energy);
    expect(PROFILES.listening.energy).toBeGreaterThan(PROFILES.idle.energy);
    expect(PROFILES.thinking.spin).toBeGreaterThan(PROFILES.idle.spin);
  });

  it('only the error state shifts to the alert colour', () => {
    for (const [name, profile] of Object.entries(PROFILES)) {
      expect(profile.alert === 0 || name === 'error').toBe(true);
    }
  });
});

describe('IDLE_DIMMED', () => {
  it('is dimmer, smaller and slower-spinning than ordinary idle', () => {
    expect(IDLE_DIMMED.bloom).toBeLessThan(PROFILES.idle.bloom);
    expect(IDLE_DIMMED.energy).toBeLessThan(PROFILES.idle.energy);
    expect(IDLE_DIMMED.scale).toBeLessThan(PROFILES.idle.scale);
    expect(IDLE_DIMMED.spin).toBeLessThan(PROFILES.idle.spin);
  });

  it('a spring settles on it exactly like any other profile', () => {
    // Not a NovaState, so it is never in PROFILES — but CoreMotion has no
    // idea of that distinction, it only ever sees a CoreProfile shape.
    const motion = new CoreMotion();
    motion.snapTo(PROFILES.idle);
    for (let i = 0; i < 300; i += 1) motion.step(IDLE_DIMMED, 0, 1 / 60);
    expect(motion.scale.value).toBeCloseTo(IDLE_DIMMED.scale, 2);
    expect(motion.spin.value).toBeCloseTo(IDLE_DIMMED.spin, 2);
  });
});

describe('rings', () => {
  it('fits the shader uniform array', () => {
    expect(RINGS.length).toBeLessThanOrEqual(8);
  });

  it('is ordered outward with no overlaps', () => {
    for (let i = 1; i < RINGS.length; i += 1) {
      expect(RINGS[i]!.radius).toBeGreaterThan(RINGS[i - 1]!.radius);
    }
  });

  it('counter-rotates neighbours, which is what reads as depth', () => {
    const directions = RINGS.map((ring) => Math.sign(ring.speed));
    expect(new Set(directions).size).toBeGreaterThan(1);
  });

  it('keeps every ring inside the viewport at default scale', () => {
    for (const ring of RINGS) expect(ring.radius).toBeLessThan(0.5);
  });
});

describe('palettes', () => {
  it('falls back to the default for an unknown theme', () => {
    expect(paletteFor('does-not-exist')).toBe(PALETTES['nova-blue']);
  });

  it('keeps every channel in range', () => {
    for (const palette of Object.values(PALETTES)) {
      for (const colour of [palette.accent, palette.accentAlt, palette.alert]) {
        for (const channel of colour) {
          expect(channel).toBeGreaterThanOrEqual(0);
          expect(channel).toBeLessThanOrEqual(1);
        }
      }
    }
  });
});
