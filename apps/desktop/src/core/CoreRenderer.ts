/**
 * The N.O.V.A. Core renderer.
 *
 * Five passes per frame:
 *
 *   1. scene   → HDR target (background, plasma core, rings)
 *   2. particles → same target, additive
 *   3. bright  → half-res, isolate what should bloom
 *   4. blur    → ping-pong horizontal/vertical, N iterations
 *   5. composite → default framebuffer, tone map + vignette + grain
 *
 * The animation never stops. `state` only changes where the springs are heading;
 * ring rotation, plasma flow and particle orbits all advance off a monotonic
 * clock, so there is no state in which the Core is static.
 *
 * Context loss is handled rather than ignored — a driver reset or a GPU switch
 * on a laptop will otherwise leave a permanently black screen.
 */

import type { NovaState } from '@protocol';
import {
  bindTarget,
  createRenderTarget,
  disposeRenderTarget,
  drawFullscreen,
  Program,
  resizeRenderTarget,
  type RenderTarget,
} from './gl';
import {
  BLUR_FRAGMENT,
  BRIGHT_FRAGMENT,
  COMPOSITE_FRAGMENT,
  FULLSCREEN_VERTEX,
  PARTICLE_FRAGMENT,
  PARTICLE_VERTEX,
  SCENE_FRAGMENT,
} from './shaders';
import { FpsMeter, FrameWatchdog, lowerTier, tierFor, type QualityName, type QualityTier } from './quality';
import { CoreMotion, IDLE_DIMMED, PALETTES, PROFILES, RINGS, paletteFor, type Palette } from './visual';

export interface CoreRendererOptions {
  quality: QualityName;
  theme: string;
  bloomIntensity: number;
  particleDensity: number;
  coreScale: number;
  reduceMotion: boolean;
  onQualityChange?: (quality: QualityName) => void;
  onFps?: (fps: number) => void;
}

const MAX_RINGS = 8;

export class CoreRenderer {
  private readonly gl: WebGL2RenderingContext;
  private readonly motion = new CoreMotion();
  private readonly watchdog = new FrameWatchdog();
  private readonly fpsMeter = new FpsMeter();

  private sceneProgram!: Program;
  private particleProgram!: Program;
  private brightProgram!: Program;
  private blurProgram!: Program;
  private compositeProgram!: Program;

  private sceneTarget!: RenderTarget;
  private bloomA!: RenderTarget;
  private bloomB!: RenderTarget;

  private vao: WebGLVertexArrayObject | null = null;
  private particleVao: WebGLVertexArrayObject | null = null;
  private particleBuffer: WebGLBuffer | null = null;
  private particleCount = 0;

  private readonly ringData = new Float32Array(MAX_RINGS * 4);
  private readonly ringStyle = new Float32Array(MAX_RINGS * 4);
  private readonly ringAngles = new Float32Array(MAX_RINGS);

  private tier: QualityTier;
  private palette: Palette;
  private options: CoreRendererOptions;

  private state: NovaState = 'booting';
  private idleDimmed = false;
  private level = 0;
  private clock = 0;
  private lastFrame = 0;
  private rafHandle = 0;
  private running = false;
  private contextLost = false;
  private disposed = false;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    options: CoreRendererOptions,
  ) {
    const gl = canvas.getContext('webgl2', {
      alpha: false,
      antialias: false, // The SDF rings antialias themselves; MSAA would only cost fill rate.
      depth: false,
      stencil: false,
      premultipliedAlpha: false,
      powerPreference: 'high-performance',
      preserveDrawingBuffer: false,
      desynchronized: true,
    });
    if (!gl) throw new Error('WebGL2 is not available');

    this.gl = gl;
    this.options = options;
    this.tier = tierFor(options.quality);
    this.palette = paletteFor(options.theme);

    canvas.addEventListener('webglcontextlost', this.handleContextLost);
    canvas.addEventListener('webglcontextrestored', this.handleContextRestored);

    this.build();
    this.motion.snapTo(PROFILES.booting);
  }

  // ------------------------------------------------------------------- setup

  private build(): void {
    const { gl } = this;
    this.sceneProgram = new Program(gl, FULLSCREEN_VERTEX, SCENE_FRAGMENT);
    this.particleProgram = new Program(gl, PARTICLE_VERTEX, PARTICLE_FRAGMENT);
    this.brightProgram = new Program(gl, FULLSCREEN_VERTEX, BRIGHT_FRAGMENT);
    this.blurProgram = new Program(gl, FULLSCREEN_VERTEX, BLUR_FRAGMENT);
    this.compositeProgram = new Program(gl, FULLSCREEN_VERTEX, COMPOSITE_FRAGMENT);

    // The fullscreen triangle is generated from gl_VertexID, but WebGL2 still
    // requires *some* VAO to be bound for a draw call to be valid.
    this.vao = gl.createVertexArray();

    this.buildParticles();

    const { width, height } = this.drawingSize();
    this.sceneTarget = createRenderTarget(gl, width, height, { float: true });
    const bloomWidth = Math.max(1, Math.round(width * this.tier.bloomScale));
    const bloomHeight = Math.max(1, Math.round(height * this.tier.bloomScale));
    this.bloomA = createRenderTarget(gl, bloomWidth, bloomHeight, { float: true });
    this.bloomB = createRenderTarget(gl, bloomWidth, bloomHeight, { float: true });

    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.CULL_FACE);
    gl.clearColor(0, 0, 0, 1);
  }

  /**
   * Upload the particle field.
   *
   * Each particle is four floats — orbit radius, phase, angular speed, size —
   * and never changes again. All motion is derived in the vertex shader.
   */
  private buildParticles(): void {
    const { gl } = this;
    const count = Math.round(this.tier.particles * this.options.particleDensity);
    this.particleCount = Math.max(0, count);
    if (this.particleCount === 0) return;

    const seeds = new Float32Array(this.particleCount * 4);
    for (let i = 0; i < this.particleCount; i += 1) {
      // Bias radii outward: a uniform random radius clumps particles at the
      // centre, because area grows with r².
      const t = Math.random();
      seeds[i * 4 + 0] = 0.19 + Math.sqrt(t) * 0.34;
      seeds[i * 4 + 1] = Math.random() * Math.PI * 2;
      seeds[i * 4 + 2] = (0.08 + Math.random() * 0.5) * (Math.random() < 0.28 ? -1 : 1);
      seeds[i * 4 + 3] = 1.1 + Math.random() * 2.6;
    }

    this.particleVao ??= gl.createVertexArray();
    this.particleBuffer ??= gl.createBuffer();
    gl.bindVertexArray(this.particleVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.particleBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, seeds, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 4, gl.FLOAT, false, 0, 0);
    gl.bindVertexArray(null);
  }

  private drawingSize(): { width: number; height: number } {
    const dpr = Math.min(window.devicePixelRatio || 1, this.tier.maxDpr);
    const rect = this.canvas.getBoundingClientRect();
    return {
      width: Math.max(1, Math.round((rect.width || window.innerWidth) * dpr)),
      height: Math.max(1, Math.round((rect.height || window.innerHeight) * dpr)),
    };
  }

  // ------------------------------------------------------------------ public

  start(): void {
    if (this.running || this.disposed) return;
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
    // A visible transition deserves a flash; idle settling does not.
    if (state !== 'idle' && state !== 'booting') {
      this.motion.flash(state === 'error' ? 1.2 : 0.55);
      // Any real activity ends the quiet-resting look immediately — "comes
      // alive again" should read as instant, not another slow spring.
      this.idleDimmed = false;
    }
    this.state = state;
  }

  setLevel(level: number): void {
    this.level = Math.max(0, Math.min(1, level));
  }

  /** Fire a one-off flash — a notification, a wake word, a tool completing. */
  pulse(strength = 0.8): void {
    this.motion.flash(strength);
  }

  /** Settle into (or leave) the quiet resting look after long inactivity.
   *  Only has any effect while `state` is `idle` — see the profile choice
   *  in `frame()`. */
  setIdleDimmed(dimmed: boolean): void {
    this.idleDimmed = dimmed;
  }

  updateOptions(options: Partial<CoreRendererOptions>): void {
    const previousQuality = this.options.quality;
    const previousDensity = this.options.particleDensity;
    this.options = { ...this.options, ...options };

    if (options.theme) this.palette = paletteFor(options.theme);
    if (options.quality && options.quality !== previousQuality) {
      this.tier = tierFor(options.quality);
      this.buildParticles();
      this.resize();
    } else if (
      options.particleDensity !== undefined &&
      options.particleDensity !== previousDensity
    ) {
      this.buildParticles();
    }
  }

  resize(): void {
    if (this.contextLost || this.disposed) return;
    const { gl } = this;
    const { width, height } = this.drawingSize();
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
    }
    this.sceneTarget = resizeRenderTarget(gl, this.sceneTarget, width, height, { float: true });
    const bloomWidth = Math.max(1, Math.round(width * this.tier.bloomScale));
    const bloomHeight = Math.max(1, Math.round(height * this.tier.bloomScale));
    this.bloomA = resizeRenderTarget(gl, this.bloomA, bloomWidth, bloomHeight, { float: true });
    this.bloomB = resizeRenderTarget(gl, this.bloomB, bloomWidth, bloomHeight, { float: true });
  }

  get fps(): number {
    return this.fpsMeter.fps;
  }

  get quality(): QualityName {
    return this.tier.name;
  }

  dispose(): void {
    this.stop();
    this.disposed = true;
    const { gl } = this;
    this.canvas.removeEventListener('webglcontextlost', this.handleContextLost);
    this.canvas.removeEventListener('webglcontextrestored', this.handleContextRestored);

    for (const program of [
      this.sceneProgram,
      this.particleProgram,
      this.brightProgram,
      this.blurProgram,
      this.compositeProgram,
    ]) {
      program?.dispose();
    }
    for (const target of [this.sceneTarget, this.bloomA, this.bloomB]) {
      if (target) disposeRenderTarget(gl, target);
    }
    if (this.vao) gl.deleteVertexArray(this.vao);
    if (this.particleVao) gl.deleteVertexArray(this.particleVao);
    if (this.particleBuffer) gl.deleteBuffer(this.particleBuffer);
  }

  // ------------------------------------------------------------ context loss

  private handleContextLost = (event: Event): void => {
    // Without preventDefault the context is never restored.
    event.preventDefault();
    this.contextLost = true;
    this.stop();
    console.warn('[core] WebGL context lost — waiting for restore');
  };

  private handleContextRestored = (): void => {
    console.info('[core] WebGL context restored — rebuilding');
    this.contextLost = false;
    try {
      this.build();
      this.resize();
      this.start();
    } catch (error) {
      console.error('[core] could not rebuild after context restore', error);
    }
  };

  // ------------------------------------------------------------------- frame

  private frame = (now: number): void => {
    if (!this.running || this.contextLost) return;
    this.rafHandle = requestAnimationFrame(this.frame);

    const frameMs = now - this.lastFrame;
    this.lastFrame = now;
    const dt = Math.min(frameMs / 1000, 1 / 15);

    // Reduced motion slows the clock rather than freezing it: the spec asks for
    // motion that never stops, and a still Core would read as a crash.
    this.clock += dt * (this.options.reduceMotion ? 0.35 : 1);

    const profile =
      this.state === 'idle' && this.idleDimmed ? IDLE_DIMMED : (PROFILES[this.state] ?? PROFILES.idle);
    this.motion.step(profile, this.level, dt);
    this.advanceRings(dt);
    this.render();

    const fps = this.fpsMeter.tick();
    this.options.onFps?.(fps);

    if (this.watchdog.record(frameMs)) this.downgrade();
  };

  private downgrade(): void {
    const next = lowerTier(this.tier.name);
    if (!next) return;
    console.info(`[core] frame budget exceeded — dropping to '${next}'`);
    this.tier = tierFor(next);
    this.buildParticles();
    this.resize();
    this.options.onQualityChange?.(next);
  }

  /** Integrate ring rotation. Angles accumulate; they are never derived from
   *  absolute time, so a spin change eases in instead of jumping. */
  private advanceRings(dt: number): void {
    const spin = this.motion.spin.value;
    for (let i = 0; i < RINGS.length && i < MAX_RINGS; i += 1) {
      const ring = RINGS[i]!;
      this.ringAngles[i] = (this.ringAngles[i]! + ring.speed * spin * dt) % (Math.PI * 2);

      const scale = this.options.coreScale;
      this.ringData[i * 4 + 0] = ring.radius * scale;
      this.ringData[i * 4 + 1] = ring.thickness;
      // Tilt oscillates slowly so the assembly never settles into a fixed pose.
      this.ringData[i * 4 + 2] =
        ring.tilt * (0.86 + 0.14 * Math.sin(this.clock * 0.21 + i * 1.7));
      this.ringData[i * 4 + 3] = this.ringAngles[i]!;

      this.ringStyle[i * 4 + 0] = ring.brightness * (0.55 + 0.75 * this.motion.energy.value);
      this.ringStyle[i * 4 + 1] = ring.arcStart;
      this.ringStyle[i * 4 + 2] = ring.arcLength;
      this.ringStyle[i * 4 + 3] = ring.dashes;
    }
  }

  private render(): void {
    const { gl } = this;
    const ringCount = Math.min(RINGS.length, MAX_RINGS);

    // ---- pass 1: scene -----------------------------------------------------
    bindTarget(gl, this.sceneTarget);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.disable(gl.BLEND);

    gl.bindVertexArray(this.vao);
    this.sceneProgram.use();
    this.sceneProgram.vec2('uResolution', this.sceneTarget.width, this.sceneTarget.height);
    this.sceneProgram.float('uTime', this.clock);
    this.sceneProgram.float('uEnergy', this.motion.energy.value);
    this.sceneProgram.float('uLevel', this.motion.level.value);
    this.sceneProgram.float('uPulse', this.motion.pulse.value);
    this.sceneProgram.float('uTurbulence', this.motion.turbulence.value);
    this.sceneProgram.float('uCoreRadius', this.motion.coreRadius.value);
    this.sceneProgram.float('uScale', this.motion.scale.value * this.options.coreScale);
    this.sceneProgram.float('uErrorMix', this.motion.alert.value);
    this.sceneProgram.float(
      'uBreath',
      Math.sin(this.clock * 0.55) * this.motion.breath.value,
    );
    this.sceneProgram.vec3('uAccent', this.palette.accent);
    this.sceneProgram.vec3('uAccentAlt', this.palette.accentAlt);
    this.sceneProgram.vec3('uAlert', this.palette.alert);
    this.sceneProgram.int('uRingCount', ringCount);
    this.sceneProgram.vec4Array('uRings', this.ringData);
    this.sceneProgram.vec4Array('uRingStyle', this.ringStyle);
    drawFullscreen(gl);

    // ---- pass 2: particles, additive over the scene ------------------------
    if (this.particleCount > 0) {
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
      gl.bindVertexArray(this.particleVao);
      this.particleProgram.use();
      this.particleProgram.vec2('uResolution', this.sceneTarget.width, this.sceneTarget.height);
      this.particleProgram.float('uTime', this.clock);
      this.particleProgram.float('uEnergy', this.motion.energy.value);
      this.particleProgram.float('uScale', this.motion.scale.value * this.options.coreScale);
      this.particleProgram.float('uLevel', this.motion.level.value);
      this.particleProgram.float('uConverge', this.motion.converge.value);
      this.particleProgram.float('uDpr', Math.min(window.devicePixelRatio || 1, this.tier.maxDpr));
      this.particleProgram.vec3('uAccent', this.palette.accent);
      this.particleProgram.vec3('uAccentAlt', this.palette.accentAlt);
      this.particleProgram.vec3('uAlert', this.palette.alert);
      this.particleProgram.float('uErrorMix', this.motion.alert.value);
      gl.drawArrays(gl.POINTS, 0, this.particleCount);
      gl.disable(gl.BLEND);
    }

    // ---- pass 3: bright ----------------------------------------------------
    gl.bindVertexArray(this.vao);
    bindTarget(gl, this.bloomA);
    this.brightProgram.use();
    this.brightProgram.texture('uScene', 0, this.sceneTarget.texture);
    this.brightProgram.float('uThreshold', 0.62);
    drawFullscreen(gl);

    // ---- pass 4: separable blur, ping-ponging between the bloom targets -----
    this.blurProgram.use();
    let source = this.bloomA;
    let destination = this.bloomB;
    for (let pass = 0; pass < this.tier.blurPasses; pass += 1) {
      // Widening the radius each iteration approximates a much larger kernel
      // for the cost of a few extra taps.
      const spread = 1 + pass * 1.6;
      for (const axis of [0, 1]) {
        bindTarget(gl, destination);
        this.blurProgram.texture('uSource', 0, source.texture);
        this.blurProgram.vec2(
          'uDirection',
          axis === 0 ? spread / source.width : 0,
          axis === 0 ? 0 : spread / source.height,
        );
        drawFullscreen(gl);
        [source, destination] = [destination, source];
      }
    }

    // ---- pass 5: composite to the screen -----------------------------------
    bindTarget(gl, null);
    this.compositeProgram.use();
    this.compositeProgram.texture('uScene', 0, this.sceneTarget.texture);
    this.compositeProgram.texture('uBloom', 1, source.texture);
    this.compositeProgram.vec2('uResolution', gl.drawingBufferWidth, gl.drawingBufferHeight);
    this.compositeProgram.float('uTime', this.clock);
    this.compositeProgram.float(
      'uBloomStrength',
      this.motion.bloom.value * this.options.bloomIntensity,
    );
    this.compositeProgram.float('uVignette', 0.85);
    this.compositeProgram.float('uGrain', this.tier.grain);
    this.compositeProgram.float('uAberration', this.tier.aberration);
    drawFullscreen(gl);

    gl.bindVertexArray(null);
  }
}

export { PALETTES };
