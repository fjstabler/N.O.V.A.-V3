/**
 * The clock sits inside the Core on a wall panel. Nothing may touch it.
 *
 * That is a geometry claim, and geometry claims are exactly the sort of thing
 * that looks fine on the machine it was written on and collides on the device
 * it ships to. The rings are tilted ellipses that rotate and whose tilt
 * oscillates, and several of them are flattened enough to sweep straight
 * through the horizontal middle of the screen — so "put the clock in the
 * centre" is not safe merely because the centre looks empty.
 *
 * These tests do the arithmetic instead: for every ring, at every tilt it
 * passes through, with the Core breathing in and out, is the clock's bounding
 * box strictly inside that ring's ellipse?
 */

import { describe, expect, it } from 'vitest';
import { RINGS } from '@/core/visual';
import { IDLE_DIMMED, PROFILES } from '@/core/visual';
import { PANEL_HOLLOW, PANEL_MIN_TILT, PANEL_SCALE_RANGE, panelCoreScale } from '@/lib/panel';

/** An Echo Show 5. The short side is what the Core is measured against. */
const SCREEN = { width: 960, height: 480 };
const UNIT = Math.min(SCREEN.width, SCREEN.height);

/**
 * The clock's box, in pixels.
 *
 * Measured in Chromium at 960×480 against the built bundle, where it lays out
 * as 210 × 80 centred on the screen. Rounded up to the `max-width: 220px` cap
 * in `interface.css` and a little more height, so the check is against
 * slightly more clock than actually appears rather than slightly less.
 */
const CLOCK = { halfWidth: 220 / 2, halfHeight: 84 / 2 };

/**
 * Every scale the assembly can actually be at: each state's own scale, times
 * both ends of what the Core Scale setting is allowed to do on a panel.
 *
 * Taken from the profiles rather than written down, because a number copied
 * into a test stops tracking the thing it was copied from — and the failure
 * that causes here is a ring drawn through the time.
 */
const SCALES = [...Object.values(PROFILES), IDLE_DIMMED].flatMap((profile) => [
  profile.scale * panelCoreScale(PANEL_SCALE_RANGE.min),
  profile.scale * panelCoreScale(PANEL_SCALE_RANGE.max),
  // And what a setting outside the range would ask for, which the clamp is
  // there to refuse.
  profile.scale * panelCoreScale(0.5),
  profile.scale * panelCoreScale(1.6),
]);

/** The tilt oscillation in `advanceRings`: `tilt * (0.86 + 0.14 * sin(...))`. */
const TILT_SWING = [0.86, 0.93, 1.0];

interface Ellipse {
  a: number;
  b: number;
}

function ringEllipse(index: number, scale: number, swing: number): Ellipse {
  const ring = RINGS[index]!;
  const a = PANEL_HOLLOW * UNIT + ring.radius * UNIT * scale;
  return { a, b: a * Math.max(ring.tilt * swing, PANEL_MIN_TILT) };
}

/**
 * How far the clock's nearest corner reaches toward the ring, as a fraction.
 * Below 1 the corner is inside the ellipse and the ring passes around it;
 * at 1 they touch.
 */
function reach({ a, b }: Ellipse): number {
  return Math.sqrt((CLOCK.halfWidth / a) ** 2 + (CLOCK.halfHeight / b) ** 2);
}

describe('the clock inside the Core', () => {
  it.each(RINGS.map((_, index) => index))('ring %i never crosses the clock', (index) => {
    for (const scale of SCALES) {
      for (const swing of TILT_SWING) {
        const overlap = reach(ringEllipse(index, scale, swing));
        expect(
          overlap,
          `ring ${index} reaches ${overlap.toFixed(2)} of the way to the clock ` +
            `at scale ${scale.toFixed(2)}, tilt swing ${swing}`,
        ).toBeLessThan(1);
      }
    }
  });

  it('keeps a real margin rather than only just clearing', () => {
    // A ring that merely misses looks like it is touching once it has a glow
    // around it, and every ring here has one.
    const worst = Math.max(
      ...RINGS.flatMap((_, index) =>
        SCALES.flatMap((scale) => TILT_SWING.map((swing) => reach(ringEllipse(index, scale, swing)))),
      ),
    );

    expect(worst).toBeLessThan(0.8);
  });

  it('leaves the middle genuinely empty before the first ring', () => {
    /* The hollow has to be bigger than the clock, or the layout depends on the
       rings being tilted — which they stop being the moment somebody changes a
       tilt value. */
    const hollowRadius = PANEL_HOLLOW * UNIT;
    const corner = Math.hypot(CLOCK.halfWidth, CLOCK.halfHeight);

    expect(corner).toBeLessThan(hollowRadius);
  });
});

describe('the Core on a 480 px screen', () => {
  it('fits on the screen it was sized for', () => {
    const biggest = Math.max(...SCALES);
    const widest = Math.max(...RINGS.map((_, i) => ringEllipse(i, biggest, 1).a));
    const tallest = Math.max(...RINGS.map((_, i) => ringEllipse(i, biggest, 1).b));

    expect(widest * 2).toBeLessThanOrEqual(SCREEN.width);
    expect(tallest * 2).toBeLessThanOrEqual(SCREEN.height);
  });

  it('is bigger than it was before the clock moved in', () => {
    /* The point of the change: "make the Core bigger". Pushing every ring out
       past the hollow does that even with the scale wound back. */
    const before = Math.max(...RINGS.map((ring) => ring.radius)) * UNIT;
    const after = Math.max(...RINGS.map((_, i) => ringEllipse(i, panelCoreScale(1), 1).a));

    expect(after).toBeGreaterThan(before * 1.2);
  });
});

describe('the rest of the panel', () => {
  it('leaves the conversation clear of the clock', () => {
    /* `bottom: 3.5vh` with `max-height: 29vh`, against a clock centred on the
       middle of the screen. The reply is clamped to three lines for this
       reason: without the cap it grows upward into the time, and only once
       somebody actually asks a question. */
    const conversationTop = SCREEN.height - (0.035 + 0.24) * SCREEN.height;
    const clockBottom = SCREEN.height / 2 + CLOCK.halfHeight;

    expect(conversationTop).toBeGreaterThan(clockBottom);
    // Measured: the clock ends at 280 and a three-line reply starts at 384.
    expect(conversationTop - clockBottom).toBeGreaterThan(60);
  });
});
