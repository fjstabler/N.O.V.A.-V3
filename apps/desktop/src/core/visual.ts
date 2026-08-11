/**
 * What the Core looks like in each state, and how it gets there.
 *
 * Two ideas do the work:
 *
 * *Profiles* describe the Core at rest in a given state — ring speed, plasma
 * turbulence, brightness, colour. Nothing here animates; these are targets.
 *
 * *Springs* move the live values toward those targets. A critically damped
 * spring reaches its target quickly and never overshoots, which is why a state
 * change reads as the Core *responding* rather than snapping. Every visual
 * parameter is interpolated this way, so no transition is ever instant — the
 * spec's "smooth interpolation" is a property of the system, not something
 * applied case by case.
 */

import type { NovaState } from '@protocol';

export interface CoreProfile {
  /** Overall intensity: drives brightness, particle speed, glow radius. */
  energy: number;
  /** Plasma agitation in the centre. */
  turbulence: number;
  /** Base rotation speed multiplier for the rings. */
  spin: number;
  /** Radius of the luminous centre, in normalised units. */
  coreRadius: number;
  /** Overall scale of the whole assembly. */
  scale: number;
  /** How strongly particles are drawn inward. */
  converge: number;
  /** Blend toward the alert colour. */
  alert: number;
  /** Bloom multiplier. */
  bloom: number;
  /** Amplitude of the slow idle breathing. */
  breath: number;
}

const IDLE: CoreProfile = {
  energy: 0.34,
  turbulence: 0.18,
  spin: 0.55,
  coreRadius: 0.12,
  scale: 1,
  converge: 0,
  alert: 0,
  bloom: 0.85,
  breath: 1,
};

/**
 * Idle is the baseline; every other state is a deviation from it. Writing them
 * as overrides keeps the family visually coherent — you cannot accidentally
 * design a state that looks like a different application.
 */
/**
 * A quiet resting look after a long stretch with nobody talking to it —
 * dimmer, a little smaller, rotating noticeably slower than ordinary idle.
 * Not a `NovaState`: the backend has no opinion on this, it is purely a
 * front-end idea of "nothing has happened in a while", so it lives beside
 * `PROFILES` rather than in it. The renderer only ever reaches for it while
 * already idle, and drops it the instant a real state change arrives.
 */
export const IDLE_DIMMED: CoreProfile = {
  ...IDLE,
  energy: 0.24,
  turbulence: 0.06,
  spin: 0.12,
  coreRadius: 0.1,
  scale: 0.88,
  bloom: 0.6,
  breath: 0.6,
};

export const PROFILES: Record<NovaState, CoreProfile> = {
  booting: { ...IDLE, energy: 0.12, coreRadius: 0.05, scale: 0.82, bloom: 0.5, breath: 0 },
  idle: IDLE,
  // Listening: the assembly opens up and leans toward the user.
  listening: { ...IDLE, energy: 0.72, turbulence: 0.3, spin: 0.95, coreRadius: 0.15, scale: 1.06, bloom: 1.15, breath: 0.4 },
  // Thinking: rings accelerate and particles are pulled in — visibly working.
  thinking: { ...IDLE, energy: 0.86, turbulence: 0.78, spin: 2.4, coreRadius: 0.11, scale: 0.97, converge: 0.65, bloom: 1.3, breath: 0.15 },
  // Speaking: bright and steady; the audio level supplies the movement.
  speaking: { ...IDLE, energy: 0.8, turbulence: 0.42, spin: 0.8, coreRadius: 0.16, scale: 1.03, bloom: 1.25, breath: 0.3 },
  error: { ...IDLE, energy: 0.6, turbulence: 0.95, spin: 0.3, coreRadius: 0.1, scale: 0.94, alert: 1, bloom: 1.1, breath: 0.2 },
  notifying: { ...IDLE, energy: 0.62, turbulence: 0.25, spin: 0.7, coreRadius: 0.14, scale: 1.04, bloom: 1.2, breath: 0.6 },
};

/**
 * A critically damped spring.
 *
 * Chosen over an easing curve because it is interruptible: a state change
 * mid-transition retargets from wherever the value currently is, at whatever
 * velocity it currently has, with no discontinuity. Easings have to restart.
 */
export class Spring {
  private velocity = 0;

  constructor(
    public value: number,
    private stiffness = 8,
  ) {}

  /** Advance toward `target` by `dt` seconds and return the new value. */
  step(target: number, dt: number): number {
    // Clamp dt: a backgrounded window can deliver a multi-second frame, and an
    // unclamped spring would explode on the first frame after it wakes.
    const step = Math.min(dt, 1 / 20);
    const damping = 2 * Math.sqrt(this.stiffness);
    const acceleration = (target - this.value) * this.stiffness - this.velocity * damping;
    this.velocity += acceleration * step;
    this.value += this.velocity * step;
    return this.value;
  }

  snap(value: number): void {
    this.value = value;
    this.velocity = 0;
  }
}

/** Every animated scalar, sprung together so states blend as a whole. */
export class CoreMotion {
  readonly energy = new Spring(IDLE.energy, 7);
  readonly turbulence = new Spring(IDLE.turbulence, 5);
  readonly spin = new Spring(IDLE.spin, 6);
  readonly coreRadius = new Spring(IDLE.coreRadius, 9);
  readonly scale = new Spring(IDLE.scale, 8);
  readonly converge = new Spring(IDLE.converge, 5);
  readonly alert = new Spring(IDLE.alert, 10);
  readonly bloom = new Spring(IDLE.bloom, 6);
  readonly breath = new Spring(IDLE.breath, 4);
  /** Audio level is sprung hard so the Core tracks speech without jitter. */
  readonly level = new Spring(0, 26);
  /** Transient flash on wake, notification or error. */
  readonly pulse = new Spring(0, 14);

  private pulseTarget = 0;

  step(profile: CoreProfile, level: number, dt: number): void {
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
    // Decay the pulse target so a flash always falls back to nothing.
    this.pulseTarget = Math.max(0, this.pulseTarget - dt * 2.6);
  }

  /** Fire a transient flash — wake word, notification, error. */
  flash(strength = 1): void {
    this.pulseTarget = Math.min(1.4, this.pulseTarget + strength);
  }

  snapTo(profile: CoreProfile): void {
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

/** One concentric ring's fixed geometry; the shader animates the rest. */
export interface RingDefinition {
  radius: number;
  thickness: number;
  /** 1 = face-on, toward 0 = edge-on. Drives the perceived 3D tilt. */
  tilt: number;
  /** Radians per second at spin = 1. Negative counter-rotates. */
  speed: number;
  brightness: number;
  /** Start angle of the arc, radians. */
  arcStart: number;
  /** Arc length in radians; TAU is a full ring. */
  arcLength: number;
  /** Dash count around the ring; 0 for a solid line. */
  dashes: number;
}

const TAU = Math.PI * 2;

/**
 * The ring assembly.
 *
 * Counter-rotating neighbours and mismatched tilts are what stop this reading
 * as a flat target symbol: at any instant the rings are at different angles to
 * each other, so the eye reconstructs depth.
 */
// Thicknesses are in true screen units. They were raised when `ringField` was
// corrected to measure real distance — the old anisotropic metric was inflating
// every line by 1/tilt, so these values look about right on screen rather than
// about right on paper.
export const RINGS: RingDefinition[] = [
  { radius: 0.17, thickness: 0.0032, tilt: 0.95, speed: 0.9, brightness: 0.9, arcStart: 0, arcLength: TAU, dashes: 0 },
  { radius: 0.215, thickness: 0.0024, tilt: 0.42, speed: -1.35, brightness: 1.05, arcStart: 0.4, arcLength: TAU * 0.72, dashes: 0 },
  { radius: 0.26, thickness: 0.0038, tilt: 0.78, speed: 0.55, brightness: 0.8, arcStart: 0, arcLength: TAU, dashes: 42 },
  { radius: 0.315, thickness: 0.0021, tilt: 0.3, speed: -0.7, brightness: 0.95, arcStart: 2.1, arcLength: TAU * 0.45, dashes: 0 },
  // 96 dashes at this radius sat close to the pixel grid and shimmered; 64 is
  // comfortably above it at every quality tier.
  { radius: 0.355, thickness: 0.0016, tilt: 0.88, speed: 0.32, brightness: 0.55, arcStart: 0, arcLength: TAU, dashes: 64 },
  { radius: 0.42, thickness: 0.0028, tilt: 0.55, speed: -0.24, brightness: 0.65, arcStart: 4.0, arcLength: TAU * 0.6, dashes: 0 },
  { radius: 0.48, thickness: 0.0014, tilt: 0.72, speed: 0.16, brightness: 0.4, arcStart: 0, arcLength: TAU, dashes: 0 },
];

export interface Palette {
  accent: readonly [number, number, number];
  accentAlt: readonly [number, number, number];
  alert: readonly [number, number, number];
}

/** Themes. Each is a two-colour ramp plus the alert colour it shifts to. */
export const PALETTES: Record<string, Palette> = {
  'nova-blue': { accent: [0.24, 0.58, 1.0], accentAlt: [0.45, 0.86, 1.0], alert: [1.0, 0.33, 0.28] },
  'arc-cyan': { accent: [0.1, 0.83, 0.86], accentAlt: [0.55, 1.0, 0.92], alert: [1.0, 0.42, 0.3] },
  ember: { accent: [1.0, 0.46, 0.16], accentAlt: [1.0, 0.78, 0.36], alert: [1.0, 0.24, 0.24] },
  monochrome: { accent: [0.62, 0.68, 0.78], accentAlt: [0.92, 0.95, 1.0], alert: [0.95, 0.5, 0.45] },
  aurora: { accent: [0.35, 0.9, 0.62], accentAlt: [0.55, 0.68, 1.0], alert: [1.0, 0.4, 0.55] },
};

export function paletteFor(theme: string): Palette {
  return PALETTES[theme] ?? PALETTES['nova-blue']!;
}
