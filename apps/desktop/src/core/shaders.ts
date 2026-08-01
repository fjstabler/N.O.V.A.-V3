/**
 * GLSL ES 3.00 sources for the N.O.V.A. Core.
 *
 * The scene is drawn analytically in a single fragment shader rather than from
 * geometry. Rings are signed-distance annuli evaluated per pixel, which gives
 * exact antialiasing at any resolution and lets glow fall off continuously
 * instead of banding across triangle edges — the difference between something
 * that looks rendered and something that looks drawn.
 *
 * Particles are the one exception: they are GL_POINTS whose orbits are computed
 * in the vertex shader from a per-particle seed and the clock, so the CPU never
 * touches a particle after upload.
 *
 * Post-processing is a three-pass bloom (bright pass, two separable blurs) and
 * a composite that tone-maps, vignettes and adds a little grain. The grain
 * matters more than it sounds: it hides the banding a smooth radial gradient
 * would otherwise show on an 8-bit panel.
 */

/** A fullscreen triangle. Cheaper than a quad and avoids the diagonal seam. */
export const FULLSCREEN_VERTEX = /* glsl */ `#version 300 es
precision highp float;
out vec2 vUv;
void main() {
  vec2 position = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  vUv = position;
  gl_Position = vec4(position * 2.0 - 1.0, 0.0, 1.0);
}
`;

/** Shared noise helpers. Prepended to the shaders that need them. */
const NOISE = /* glsl */ `
float hash11(float p) {
  p = fract(p * 0.1031);
  p *= p + 33.33;
  p *= p + p;
  return fract(p);
}

float hash21(vec2 p) {
  vec3 p3 = fract(vec3(p.xyx) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float valueNoise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  // Quintic interpolation: continuous second derivative, so fbm has no creases.
  vec2 u = f * f * f * (f * (f * 6.0 - 15.0) + 10.0);
  float a = hash21(i);
  float b = hash21(i + vec2(1.0, 0.0));
  float c = hash21(i + vec2(0.0, 1.0));
  float d = hash21(i + vec2(1.0, 1.0));
  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p, int octaves) {
  float sum = 0.0;
  float amplitude = 0.5;
  mat2 rotate = mat2(0.8, 0.6, -0.6, 0.8);
  for (int i = 0; i < 6; i++) {
    if (i >= octaves) break;
    sum += amplitude * valueNoise(p);
    p = rotate * p * 2.02;
    amplitude *= 0.5;
  }
  return sum;
}
`;

/**
 * The scene pass: background, core, rings.
 *
 * `uRings[i]` packs (radius, thickness, tiltCosine, rotation) and
 * `uRingStyle[i]` packs (brightness, arcStart, arcLength, dashCount). Rings are
 * flattened on Y by their tilt cosine and rotated, which reads as perspective
 * without a projection matrix; the near half is brightened so the eye resolves
 * which way each ring is facing.
 */
export const SCENE_FRAGMENT = /* glsl */ `#version 300 es
precision highp float;

in vec2 vUv;
out vec4 fragColor;

uniform vec2  uResolution;
uniform float uTime;
uniform float uEnergy;       // overall intensity, 0..1
uniform float uLevel;        // microphone loudness, 0..1
uniform float uPulse;        // transient flash, 0..1
uniform float uTurbulence;   // plasma agitation
uniform float uCoreRadius;
uniform float uScale;
uniform float uErrorMix;     // 0..1 blend toward the alert colour
uniform float uBreath;       // slow idle breathing, -1..1
uniform vec3  uAccent;
uniform vec3  uAccentAlt;
uniform vec3  uAlert;
uniform int   uRingCount;
uniform vec4  uRings[8];
uniform vec4  uRingStyle[8];

const float TAU = 6.28318530718;

${NOISE}

/** Signed distance to an ellipse-flattened, rotated annulus. */
float ringField(vec2 p, float radius, float tiltCos, float rotation, out float angle) {
  float s = sin(rotation);
  float c = cos(rotation);
  vec2 q = mat2(c, -s, s, c) * p;
  q.y /= max(tiltCos, 0.04);
  angle = atan(q.y, q.x);
  return abs(length(q) - radius);
}

/** Depth cue: points on the far side of a tilted ring sit further away. */
float ringDepth(float angle, float tiltCos) {
  return mix(1.0, 0.45 + 0.55 * (0.5 + 0.5 * sin(angle)), 1.0 - tiltCos);
}

vec3 renderRings(vec2 p, float pixel) {
  vec3 accumulated = vec3(0.0);

  for (int i = 0; i < 8; i++) {
    if (i >= uRingCount) break;

    vec4 ring = uRings[i];
    vec4 style = uRingStyle[i];

    float angle;
    float distance = ringField(p, ring.x * uScale, ring.z, ring.w, angle);

    // Arc masking: a ring may be a partial sweep rather than a full circle.
    float sweep = mod(angle - style.y + TAU, TAU);
    float arc = style.z >= TAU ? 1.0 : smoothstep(0.0, 0.12, sweep) *
                                        smoothstep(style.z, style.z - 0.12, sweep);

    // Dashes ride along the ring, giving it a direction of travel.
    float dashes = style.w;
    if (dashes > 0.5) {
      float phase = (angle + uTime * (0.35 + 0.1 * float(i))) * dashes / TAU;
      float duty = 0.55 + 0.25 * sin(uTime * 0.7 + float(i));
      arc *= smoothstep(0.0, 0.08, abs(fract(phase) - 0.5) * 2.0 - (1.0 - duty));
    }

    float halfWidth = ring.y * uScale;
    // Antialias against pixel size so the line stays crisp at any resolution.
    float core = 1.0 - smoothstep(halfWidth - pixel, halfWidth + pixel, distance);
    float glow = exp(-distance * (26.0 - 12.0 * uEnergy) / uScale) * 0.55;

    float depth = ringDepth(angle, ring.z);
    vec3 tint = mix(uAccent, uAccentAlt, float(i) / max(float(uRingCount - 1), 1.0));
    tint = mix(tint, uAlert, uErrorMix);

    accumulated += tint * (core * 1.9 + glow) * arc * style.x * depth;
  }
  return accumulated;
}

/** The luminous centre: domain-warped noise inside a soft radial falloff. */
vec3 renderCore(vec2 p) {
  float radius = length(p);
  float coreRadius = uCoreRadius * uScale * (1.0 + 0.06 * uBreath + 0.22 * uLevel);

  float falloff = 1.0 - smoothstep(0.0, coreRadius, radius);
  if (falloff <= 0.001) return vec3(0.0);

  // Warping the sample position by another fbm is what turns bland clouds into
  // something that reads as churning plasma.
  vec2 flow = p * (5.5 / uScale);
  float t = uTime * (0.16 + 0.34 * uTurbulence);
  vec2 warp = vec2(
    fbm(flow + vec2(t, -t * 0.7), 4),
    fbm(flow + vec2(-t * 0.8, t * 1.1) + 5.2, 4)
  );
  float plasma = fbm(flow + warp * (1.4 + 1.8 * uTurbulence), 5);

  float body = pow(falloff, 1.7);
  float hot = pow(falloff, 5.0);

  vec3 colour = mix(uAccent, uAccentAlt, clamp(plasma * 1.3, 0.0, 1.0));
  colour = mix(colour, uAlert, uErrorMix);

  // A white-hot centre keeps the middle from looking flat once bloom is applied.
  vec3 result = colour * body * (0.55 + 0.85 * plasma) * (0.8 + 1.5 * uEnergy);
  result += vec3(0.85, 0.93, 1.0) * hot * (1.1 + 2.4 * uPulse + 1.2 * uLevel);
  return result;
}

/** A faint field of distant points, so the void is not perfectly empty. */
vec3 renderStarfield(vec2 p) {
  vec2 grid = p * 42.0;
  vec2 cell = floor(grid);
  float seed = hash21(cell);
  if (seed < 0.985) return vec3(0.0);
  vec2 offset = fract(grid) - 0.5;
  float twinkle = 0.5 + 0.5 * sin(uTime * (0.6 + seed * 2.0) + seed * 40.0);
  float point = smoothstep(0.34, 0.0, length(offset));
  return vec3(0.55, 0.7, 0.95) * point * twinkle * 0.16;
}

void main() {
  // Aspect-corrected coordinates centred on the Core, so it stays circular.
  vec2 p = (vUv * uResolution - 0.5 * uResolution) / min(uResolution.x, uResolution.y);
  float pixel = 1.4 / min(uResolution.x, uResolution.y);
  float radius = length(p);

  vec3 colour = renderStarfield(p);

  // A wide, very dim halo grounds the Core in the scene.
  float halo = exp(-radius * 3.1) * (0.10 + 0.16 * uEnergy);
  colour += mix(uAccent, uAlert, uErrorMix) * halo;

  colour += renderCore(p);
  colour += renderRings(p, pixel);

  fragColor = vec4(colour, 1.0);
}
`;

/**
 * Particle orbits.
 *
 * Each particle carries a static seed (radius, phase, speed, size). Position is
 * a pure function of that seed and the clock, so there is no per-frame buffer
 * upload and no CPU cost that scales with particle count.
 */
export const PARTICLE_VERTEX = /* glsl */ `#version 300 es
precision highp float;

layout(location = 0) in vec4 aSeed;  // x: radius, y: phase, z: speed, w: size

uniform vec2  uResolution;
uniform float uTime;
uniform float uEnergy;
uniform float uScale;
uniform float uLevel;
uniform float uConverge;   // 0 = drifting, 1 = pulled toward the Core
uniform float uDpr;

out float vAlpha;
out float vSeed;

void main() {
  float speed = aSeed.z * (0.35 + 1.65 * uEnergy);
  float angle = aSeed.y + uTime * speed;

  // Radial drift: particles breathe in and out slightly, out of phase.
  float wobble = sin(uTime * (0.5 + aSeed.z * 2.0) + aSeed.y * 3.0) * 0.035;
  float radius = aSeed.x * uScale * (1.0 + wobble) * mix(1.0, 0.42, uConverge);
  radius *= 1.0 + 0.18 * uLevel;

  // Tilt the orbital plane so particles pass in front of and behind the rings.
  float tilt = 0.30 + 0.55 * fract(aSeed.y * 0.618);
  vec2 position = vec2(cos(angle) * radius, sin(angle) * radius * tilt);

  float aspect = uResolution.x / uResolution.y;
  vec2 clip = position / vec2(max(aspect, 1.0) * 0.5, max(1.0 / aspect, 1.0) * 0.5);
  gl_Position = vec4(clip, 0.0, 1.0);

  // Depth fade: the far half of the orbit is dimmer and smaller.
  float depth = 0.5 + 0.5 * sin(angle);
  gl_PointSize = aSeed.w * uDpr * (0.55 + 0.75 * depth) * (0.8 + 0.5 * uEnergy);
  vAlpha = (0.16 + 0.5 * depth) * (0.35 + 0.65 * uEnergy);
  vSeed = aSeed.y;
}
`;

export const PARTICLE_FRAGMENT = /* glsl */ `#version 300 es
precision highp float;

in float vAlpha;
in float vSeed;
out vec4 fragColor;

uniform vec3 uAccent;
uniform vec3 uAccentAlt;
uniform vec3 uAlert;
uniform float uErrorMix;

void main() {
  // Round the square point sprite into a soft dot.
  vec2 offset = gl_PointCoord * 2.0 - 1.0;
  float falloff = 1.0 - smoothstep(0.0, 1.0, dot(offset, offset));
  if (falloff <= 0.0) discard;

  vec3 tint = mix(uAccent, uAccentAlt, fract(vSeed * 0.618));
  tint = mix(tint, uAlert, uErrorMix);
  fragColor = vec4(tint * falloff * vAlpha, falloff * vAlpha);
}
`;

/** Bright pass: isolate what should bloom, with a soft knee to avoid popping. */
export const BRIGHT_FRAGMENT = /* glsl */ `#version 300 es
precision highp float;

in vec2 vUv;
out vec4 fragColor;

uniform sampler2D uScene;
uniform float uThreshold;

void main() {
  vec3 colour = texture(uScene, vUv).rgb;
  float luma = dot(colour, vec3(0.2126, 0.7152, 0.0722));
  // Quadratic knee: a hard cutoff makes bloom flicker as pixels cross it.
  float knee = max(luma - uThreshold, 0.0);
  float weight = knee * knee / (knee + 0.28);
  fragColor = vec4(colour * weight / max(luma, 0.0001), 1.0);
}
`;

/** Separable nine-tap gaussian; run once horizontally, once vertically. */
export const BLUR_FRAGMENT = /* glsl */ `#version 300 es
precision highp float;

in vec2 vUv;
out vec4 fragColor;

uniform sampler2D uSource;
uniform vec2 uDirection;   // texel-sized step along one axis

const float WEIGHTS[5] = float[](0.227027, 0.194594, 0.121621, 0.054054, 0.016216);

void main() {
  vec3 sum = texture(uSource, vUv).rgb * WEIGHTS[0];
  for (int i = 1; i < 5; i++) {
    vec2 offset = uDirection * float(i);
    sum += texture(uSource, vUv + offset).rgb * WEIGHTS[i];
    sum += texture(uSource, vUv - offset).rgb * WEIGHTS[i];
  }
  fragColor = vec4(sum, 1.0);
}
`;

/**
 * Composite: scene + bloom, tone mapped, vignetted, grained.
 *
 * The chromatic aberration is deliberately tiny — enough to suggest a lens,
 * not enough to read as an effect.
 */
export const COMPOSITE_FRAGMENT = /* glsl */ `#version 300 es
precision highp float;

in vec2 vUv;
out vec4 fragColor;

uniform sampler2D uScene;
uniform sampler2D uBloom;
uniform vec2  uResolution;
uniform float uTime;
uniform float uBloomStrength;
uniform float uVignette;
uniform float uGrain;
uniform float uAberration;

${NOISE}

/** ACES filmic approximation — keeps highlights from clipping to flat white. */
vec3 tonemap(vec3 x) {
  const float a = 2.51, b = 0.03, c = 2.43, d = 0.59, e = 0.14;
  return clamp((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0);
}

void main() {
  vec2 centred = vUv - 0.5;
  float radius = length(centred);

  // Aberration scales with distance from centre, as a real lens would.
  vec2 shift = centred * uAberration * radius;
  vec3 scene = vec3(
    texture(uScene, vUv + shift).r,
    texture(uScene, vUv).g,
    texture(uScene, vUv - shift).b
  );

  vec3 bloom = texture(uBloom, vUv).rgb;
  vec3 colour = scene + bloom * uBloomStrength;

  colour = tonemap(colour);

  float vignette = smoothstep(0.95, 0.28, radius);
  colour *= mix(1.0, vignette, uVignette);

  // Grain breaks up the banding a smooth gradient shows on an 8-bit panel.
  float grain = hash21(gl_FragCoord.xy + fract(uTime) * 419.0) - 0.5;
  colour += grain * uGrain;

  fragColor = vec4(max(colour, 0.0), 1.0);
}
`;
