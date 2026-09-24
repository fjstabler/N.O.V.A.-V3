/**
 * The Core settles after fifteen minutes, and the clock has to survive it.
 *
 * This was easy to break without noticing. The settled profile is scaled to
 * 0.88, which moves every ring *inward* — toward the clock now sitting in the
 * middle of it. Nothing about the awake layout guarantees the settled one, and
 * the settled one is the state a wall panel spends almost all of its life in.
 *
 * The selection is tested here rather than in a browser because the end of the
 * chain cannot be reached without a running core: the dim is gated on the
 * assistant being `idle`, and a panel with nothing to connect to stays in
 * `booting` forever. `scripts/panel-probe.mjs` confirms as much.
 */

import { describe, expect, it } from 'vitest';
import type { NovaState } from '@protocol';
import { CoreMotion, IDLE_DIMMED, PROFILES, RINGS, idleClockFactor, profileFor } from '@/core/visual';
import type { CoreProfile } from '@/core/visual';
import { PANEL_HOLLOW, PANEL_MIN_TILT, PANEL_SCALE_RANGE, panelCoreScale } from '@/lib/panel';

const UNIT = 480; // the short side of an Echo Show 5
const CLOCK = { halfWidth: 220 / 2, halfHeight: 84 / 2 };

const FRAME = 1 / 60;

interface Run {
  /** Total angle every ring turned through, radians, ignoring direction. */
  turned: number;
  /** How far the internal clock advanced — the plasma, tilt and dash travel. */
  clock: number;
  /** Where the spin spring ended up. */
  spin: number;
  /** The largest change in spin between two consecutive frames. */
  biggestJump: number;
}

/**
 * Run the Core's motion for `seconds`, exactly as the renderers' frame loops
 * do, and report what actually moved.
 *
 * Driving the real `CoreMotion` matters. Comparing the two profiles' `spin`
 * numbers only says the constants differ; it says nothing about whether the
 * spring ever arrives, and a spring that never reached its target would leave
 * the Core turning at very nearly its awake rate forever while every
 * constants-only test stayed green.
 */
function run(profile: CoreProfile, seconds: number, state: NovaState = 'idle'): Run {
  const motion = new CoreMotion();
  motion.snapTo(PROFILES.idle); // it has been sitting idle; that is the start.

  let turned = 0;
  let clock = 0;
  let biggestJump = 0;
  let previous = motion.spin.value;

  for (let t = 0; t < seconds; t += FRAME) {
    motion.step(profile, 0, FRAME);
    biggestJump = Math.max(biggestJump, Math.abs(motion.spin.value - previous));
    previous = motion.spin.value;

    // Mirrors `advanceRings` in CoreRenderer and the same line in
    // FallbackRenderer: angles accumulate, they are never taken from the wall
    // clock, which is what makes a spin change ease in instead of jump.
    for (const ring of RINGS) turned += Math.abs(ring.speed * motion.spin.value * FRAME);
    clock += FRAME * idleClockFactor(state, motion.spin.value);
  }

  return { turned, clock, spin: motion.spin.value, biggestJump };
}

describe('settling after fifteen minutes', () => {
  it('uses the dimmed profile once idle and dimmed', () => {
    expect(profileFor('idle', true)).toBe(IDLE_DIMMED);
  });

  it('stays awake while idle but not yet dimmed', () => {
    expect(profileFor('idle', false)).toBe(PROFILES.idle);
  });

  it('is quieter and smaller than the awake one', () => {
    /* If these ever converge the feature has stopped doing anything, and
       nothing else in the suite would notice. */
    expect(IDLE_DIMMED.scale).toBeLessThan(PROFILES.idle.scale);
    expect(IDLE_DIMMED.energy).toBeLessThan(PROFILES.idle.energy);
    expect(IDLE_DIMMED.spin).toBeLessThan(PROFILES.idle.spin);
  });

  it.each(['listening', 'thinking', 'speaking', 'booting', 'error', 'notifying'] as const)(
    'never dims while %s, however long the timer has been running',
    (state) => {
      /* The timer runs on wall-clock time and knows nothing about what the
         assistant is doing, so it can fire mid-turn. Dimming then would be
         backwards: the Core is at its most active precisely when somebody has
         been waiting a quarter of an hour for a long answer. */
      expect(profileFor(state, true)).toBe(PROFILES[state]);
    },
  );
});

describe('the rings turning slower once settled', () => {
  /* The settle is not only a dimming. The rings drop from spin 0.55 to 0.12 —
     a bit under a fifth of the speed — and that is the part you notice from
     across a room, because a slow turn reads as "resting" where a dim one just
     reads as "the screen went down a notch". */

  it('turns the rings roughly a fifth as far over a minute', () => {
    const awake = run(PROFILES.idle, 60);
    const settled = run(IDLE_DIMMED, 60);
    const ratio = settled.turned / awake.turned;

    expect(ratio).toBeLessThan(0.3);
    // And not *stopped*: a frozen Core reads as a crash, which is the same
    // reason reduced motion slows the clock rather than halting it.
    expect(ratio).toBeGreaterThan(0.1);
    expect(settled.turned).toBeGreaterThan(0);
  });

  it('gets there on a spring rather than by snapping', () => {
    const settled = run(IDLE_DIMMED, 60);

    // The spring has to actually arrive — otherwise the rings would keep
    // turning at nearly their awake rate and every constants-only check would
    // still pass.
    expect(settled.spin).toBeCloseTo(IDLE_DIMMED.spin, 2);
    // A sixtieth of a second may not swallow a tenth of the whole change; the
    // slowdown should be visible as a slowdown, not as a gear change.
    expect(settled.biggestJump).toBeLessThan((PROFILES.idle.spin - IDLE_DIMMED.spin) * 0.1);
  });

  it('slows the plasma by the same factor as the rings, not separately', () => {
    /* `spin` turns the rings and nothing else. The plasma, the tilt
       oscillation and the dash travel all run off the internal clock, so
       without `idleClockFactor` the rings would ease down to a crawl while the
       middle carried on churning at full speed — two speeds arguing rather
       than one thing settling. */
    const settled = run(IDLE_DIMMED, 60);
    const awake = run(PROFILES.idle, 60);

    const rings = settled.turned / awake.turned;
    const plasma = settled.clock / awake.clock;

    expect(plasma).toBeCloseTo(rings, 3);
  });

  it.each(['listening', 'thinking', 'speaking'] as const)(
    'runs the clock at full rate while %s, whatever the spring is doing',
    (state) => {
      // Only idle has a settled form to ease into. Scaling another state's
      // clock by a ratio against idle's spin would speed `thinking` *up* —
      // 2.4/0.55 — for no reason at all.
      expect(idleClockFactor(state, PROFILES[state].spin)).toBe(1);
      expect(idleClockFactor(state, 0.01)).toBe(1);
    },
  );

  it('leaves the clock alone while idle and awake', () => {
    expect(idleClockFactor('idle', PROFILES.idle.spin)).toBe(1);
  });
});

describe('the clock while the Core is settled', () => {
  /** Every scale the settled Core can be at, across the panel's slider range. */
  const settled = [
    IDLE_DIMMED.scale * panelCoreScale(PANEL_SCALE_RANGE.min),
    IDLE_DIMMED.scale * panelCoreScale(1),
    IDLE_DIMMED.scale * panelCoreScale(PANEL_SCALE_RANGE.max),
  ];

  it.each(RINGS.map((_, index) => index))('ring %i still clears the clock', (index) => {
    const ring = RINGS[index]!;
    for (const scale of settled) {
      for (const swing of [0.86, 1.0]) {
        const a = PANEL_HOLLOW * UNIT + ring.radius * UNIT * scale;
        const b = a * Math.max(ring.tilt * swing, PANEL_MIN_TILT);
        const reach = Math.sqrt((CLOCK.halfWidth / a) ** 2 + (CLOCK.halfHeight / b) ** 2);

        expect(reach, `ring ${index} at settled scale ${scale.toFixed(2)}`).toBeLessThan(1);
      }
    }
  });

  it('is no closer to the clock settled than awake, because the hollow is fixed', () => {
    /* The reason this holds at all: the hollow is added *after* the scale
       multiply. Were it added before, a settled Core would shrink its own
       clear middle by 12% and the rings would close in on the digits. */
    const inner = (scale: number) => PANEL_HOLLOW * UNIT + RINGS[0]!.radius * UNIT * scale;

    expect(inner(IDLE_DIMMED.scale * panelCoreScale(1))).toBeGreaterThan(PANEL_HOLLOW * UNIT);
    expect(inner(0)).toBe(PANEL_HOLLOW * UNIT);
  });
});
