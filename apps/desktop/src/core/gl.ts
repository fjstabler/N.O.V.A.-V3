/**
 * Small WebGL2 helpers.
 *
 * Enough to compile programs, cache uniform locations and manage render
 * targets, and nothing more — a general-purpose wrapper would cost more than
 * it saves for a renderer with five passes.
 */

export interface RenderTarget {
  framebuffer: WebGLFramebuffer;
  texture: WebGLTexture;
  width: number;
  height: number;
}

export class ShaderError extends Error {
  constructor(
    message: string,
    readonly source: string,
  ) {
    super(message);
    this.name = 'ShaderError';
  }
}

function compile(gl: WebGL2RenderingContext, type: number, source: string): WebGLShader {
  const shader = gl.createShader(type);
  if (!shader) throw new ShaderError('could not create shader', source);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) ?? 'unknown compile error';
    gl.deleteShader(shader);
    throw new ShaderError(log, source);
  }
  return shader;
}

/** A linked program with lazily-cached uniform locations. */
export class Program {
  readonly program: WebGLProgram;
  private readonly uniforms = new Map<string, WebGLUniformLocation | null>();

  constructor(
    private readonly gl: WebGL2RenderingContext,
    vertexSource: string,
    fragmentSource: string,
  ) {
    const vertex = compile(gl, gl.VERTEX_SHADER, vertexSource);
    const fragment = compile(gl, gl.FRAGMENT_SHADER, fragmentSource);
    const program = gl.createProgram();
    if (!program) throw new ShaderError('could not create program', '');

    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    // Shader objects are reference-counted by the program; detaching now means
    // they are freed as soon as the program is.
    gl.detachShader(program, vertex);
    gl.detachShader(program, fragment);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);

    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const log = gl.getProgramInfoLog(program) ?? 'unknown link error';
      gl.deleteProgram(program);
      throw new ShaderError(log, '');
    }
    this.program = program;
  }

  use(): void {
    this.gl.useProgram(this.program);
  }

  location(name: string): WebGLUniformLocation | null {
    if (!this.uniforms.has(name)) {
      this.uniforms.set(name, this.gl.getUniformLocation(this.program, name));
    }
    return this.uniforms.get(name) ?? null;
  }

  float(name: string, value: number): void {
    const location = this.location(name);
    if (location) this.gl.uniform1f(location, value);
  }

  int(name: string, value: number): void {
    const location = this.location(name);
    if (location) this.gl.uniform1i(location, value);
  }

  vec2(name: string, x: number, y: number): void {
    const location = this.location(name);
    if (location) this.gl.uniform2f(location, x, y);
  }

  vec3(name: string, value: readonly [number, number, number]): void {
    const location = this.location(name);
    if (location) this.gl.uniform3f(location, value[0], value[1], value[2]);
  }

  vec4Array(name: string, data: Float32Array): void {
    const location = this.location(name);
    if (location) this.gl.uniform4fv(location, data);
  }

  texture(name: string, unit: number, texture: WebGLTexture): void {
    const location = this.location(name);
    if (!location) return;
    this.gl.activeTexture(this.gl.TEXTURE0 + unit);
    this.gl.bindTexture(this.gl.TEXTURE_2D, texture);
    this.gl.uniform1i(location, unit);
  }

  dispose(): void {
    this.gl.deleteProgram(this.program);
    this.uniforms.clear();
  }
}

/**
 * Create a render target.
 *
 * Half-float storage is requested when the driver supports it, because bloom
 * needs headroom above 1.0 — clamping the scene to 8-bit before the bright pass
 * throws away exactly the highlights that are supposed to bloom.
 */
export function createRenderTarget(
  gl: WebGL2RenderingContext,
  width: number,
  height: number,
  options: { float?: boolean } = {},
): RenderTarget {
  const texture = gl.createTexture();
  const framebuffer = gl.createFramebuffer();
  if (!texture || !framebuffer) throw new Error('could not allocate a render target');

  const useFloat = options.float === true && gl.getExtension('EXT_color_buffer_half_float') !== null;

  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texImage2D(
    gl.TEXTURE_2D,
    0,
    useFloat ? gl.RGBA16F : gl.RGBA8,
    Math.max(1, width),
    Math.max(1, height),
    0,
    gl.RGBA,
    useFloat ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE,
    null,
  );
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  // Clamping stops the blur from wrapping bright pixels to the opposite edge.
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

  gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer);
  gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, texture, 0);

  const status = gl.checkFramebufferStatus(gl.FRAMEBUFFER);
  gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  if (status !== gl.FRAMEBUFFER_COMPLETE) {
    throw new Error(`incomplete framebuffer (0x${status.toString(16)})`);
  }

  return { framebuffer, texture, width, height };
}

export function resizeRenderTarget(
  gl: WebGL2RenderingContext,
  target: RenderTarget,
  width: number,
  height: number,
  options: { float?: boolean } = {},
): RenderTarget {
  if (target.width === width && target.height === height) return target;
  disposeRenderTarget(gl, target);
  return createRenderTarget(gl, width, height, options);
}

export function disposeRenderTarget(gl: WebGL2RenderingContext, target: RenderTarget): void {
  gl.deleteFramebuffer(target.framebuffer);
  gl.deleteTexture(target.texture);
}

/** Draw the fullscreen triangle the vertex shader generates from gl_VertexID. */
export function drawFullscreen(gl: WebGL2RenderingContext): void {
  gl.drawArrays(gl.TRIANGLES, 0, 3);
}

export function bindTarget(gl: WebGL2RenderingContext, target: RenderTarget | null): void {
  if (target) {
    gl.bindFramebuffer(gl.FRAMEBUFFER, target.framebuffer);
    gl.viewport(0, 0, target.width, target.height);
  } else {
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    gl.viewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
  }
}

/** Detect WebGL2 without leaking the probe context. */
export function supportsWebGL2(): boolean {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2');
    if (!gl) return false;
    gl.getExtension('WEBGL_lose_context')?.loseContext();
    return true;
  } catch {
    return false;
  }
}
