import { describe, expect, it } from 'vitest';
import { FrameWatchdog, TIERS, lowerTier, tierFor } from './quality';

describe('quality tiers', () => {
  it('falls back to high for an unknown name', () => {
    expect(tierFor('nonsense')).toBe(TIERS.high);
  });

  it('costs strictly more as the tier rises', () => {
    const order = [TIERS.low, TIERS.balanced, TIERS.high, TIERS.ultra];
    for (let i = 1; i < order.length; i += 1) {
      expect(order[i]!.maxDpr).toBeGreaterThanOrEqual(order[i - 1]!.maxDpr);
      expect(order[i]!.particles).toBeGreaterThanOrEqual(order[i - 1]!.particles);
    }
  });

  it('steps down and stops at the bottom', () => {
    expect(lowerTier('ultra')).toBe('high');
    expect(lowerTier('high')).toBe('balanced');
    expect(lowerTier('balanced')).toBe('low');
    expect(lowerTier('low')).toBeNull();
  });
});

describe('FrameWatchdog', () => {
  /** Feed `count` frames of `ms` each; returns whether a downgrade fired. */
  function feed(watchdog: FrameWatchdog, ms: number, count: number): boolean {
    let fired = false;
    for (let i = 0; i < count; i += 1) {
      if (watchdog.record(ms)) fired = true;
    }
    return fired;
  }

  it('leaves a healthy frame rate alone', () => {
    const watchdog = new FrameWatchdog(20, 10, 0);
    expect(feed(watchdog, 16, 200)).toBe(false);
  });

  it('downgrades on a sustained overrun', () => {
    const watchdog = new FrameWatchdog(20, 10, 0);
    expect(feed(watchdog, 30, 40)).toBe(true);
  });

  it('ignores an isolated hitch', () => {
    // One garbage-collection pause should not permanently drop the quality.
    const watchdog = new FrameWatchdog(20, 21, 0);
    for (let i = 0; i < 20; i += 1) watchdog.record(16);
    expect(watchdog.record(400)).toBe(false);
  });

  it('respects the grace period before judging anything', () => {
    const watchdog = new FrameWatchdog(20, 5, 10_000);
    expect(feed(watchdog, 60, 50)).toBe(false);
  });

  it('reports the median frame time', () => {
    const watchdog = new FrameWatchdog(20, 5, 0);
    for (const ms of [10, 12, 14, 16, 18]) watchdog.record(ms);
    expect(watchdog.medianFrameMs).toBe(14);
  });
});
