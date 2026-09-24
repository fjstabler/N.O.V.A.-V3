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
import { IDLE_DIMMED, PROFILES, RINGS, profileFor } from '@/core/visual';
import { PANEL_HOLLOW, PANEL_MIN_TILT, PANEL_SCALE_RANGE, panelCoreScale } from '@/lib/panel';

const UNIT = 480; // the short side of an Echo Show 5
const CLOCK = { halfWidth: 220 / 2, halfHeight: 84 / 2 };

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
