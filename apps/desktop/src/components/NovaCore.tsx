/**
 * The Core canvas.
 *
 * React owns the element; the renderer owns everything inside it. State reaches
 * the renderer through imperative calls in effects rather than through props,
 * because the Core animates at 60 FPS and re-rendering a React tree sixty times
 * a second to move a ring would be absurd.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { NovaState } from '@protocol';
import { CoreRenderer } from '@/core/CoreRenderer';
import { FallbackRenderer } from '@/core/FallbackRenderer';
import { supportsWebGL2 } from '@/core/gl';
import type { QualityName } from '@/core/quality';
import { useNova } from '@/state/store';

type AnyRenderer = CoreRenderer | FallbackRenderer;

/**
 * The live renderer, so surfaces outside this component can make the Core
 * react — a notification arriving, a tool finishing. A module-level handle is
 * the honest way to model this: there is exactly one Core, and threading a ref
 * through context would only disguise that.
 */
let activeRenderer: AnyRenderer | null = null;

/** How long with nobody talking to it before the Core settles into its
 *  quiet resting look. */
const IDLE_DIM_MS = 15 * 60_000;

/** Flash the Core. Safe to call before the renderer exists. */
export function pulseCore(strength = 0.8): void {
  activeRenderer?.pulse(strength);
  // A surface appearing (a map, a camera) is as much "someone just asked for
  // something" as a spoken turn is — it should wake the Core the same way.
  useNova.getState().noteInteraction();
}

export function NovaCore(): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const rendererRef = useRef<AnyRenderer | null>(null);
  /** Set when WebGL turned out not to work, so the retry picks Canvas2D. */
  const [webglFailed, setWebglFailed] = useState(false);

  const state = useNova((store) => store.state);
  const level = useNova((store) => store.level);
  const setFps = useNova((store) => store.setFps);
  const appearance = useNova((store) => store.settings?.appearance);
  const lastActiveAt = useNova((store) => store.lastActiveAt);

  // Probing WebGL support builds a throwaway context, so do it once.
  const canUseWebGL = useMemo(() => supportsWebGL2(), []);
  const useWebGL = (appearance?.gpu_acceleration ?? true) && canUseWebGL && !webglFailed;

  // Construct once. Appearance changes are pushed in imperatively below, so the
  // renderer is never torn down and rebuilt for a theme switch.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const theme = appearance?.theme ?? 'nova-blue';
    const quality = (appearance?.animation_quality ?? 'high') as QualityName;

    let renderer: AnyRenderer;
    try {
      renderer = useWebGL
        ? new CoreRenderer(canvas, {
            quality,
            theme,
            bloomIntensity: appearance?.bloom_intensity ?? 1,
            particleDensity: appearance?.particle_density ?? 1,
            coreScale: appearance?.core_scale ?? 1,
            reduceMotion: appearance?.reduce_motion ?? false,
            onFps: setFps,
          })
        : new FallbackRenderer(canvas, {
            theme,
            coreScale: appearance?.core_scale ?? 1,
            reduceMotion: appearance?.reduce_motion ?? false,
            onFps: setFps,
          });
    } catch (error) {
      // Retrying on this canvas is the one thing that cannot work: an element
      // keeps its context type for life, so a canvas that has been handed to
      // WebGL will return null for '2d' forever. Flipping the flag changes the
      // canvas `key`, React hands the next attempt a brand new element, and
      // Canvas2D gets a clean one to ask.
      if (useWebGL) {
        console.error('[core] WebGL unavailable, retrying with Canvas2D', error);
        setWebglFailed(true);
        return;
      }
      // Canvas2D failed on a fresh canvas, which means this device cannot draw
      // the Core at all. It is decoration — the assistant still works without
      // it, so log and leave the canvas blank rather than taking the interface
      // down over an animation.
      console.error('[core] no renderer available; the Core will not draw', error);
      return;
    }

    rendererRef.current = renderer;
    activeRenderer = renderer;
    renderer.resize();
    renderer.start();

    const observer = new ResizeObserver(() => renderer.resize());
    observer.observe(canvas);

    // A hidden window should not burn a GPU on frames nobody sees.
    const onVisibility = () => {
      if (document.hidden) renderer.stop();
      else renderer.start();
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      observer.disconnect();
      renderer.dispose();
      rendererRef.current = null;
      if (activeRenderer === renderer) activeRenderer = null;
    };
    // Only the rendering backend choice justifies a rebuild.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [useWebGL]);

  useEffect(() => {
    rendererRef.current?.setState(state as NovaState);
  }, [state]);

  useEffect(() => {
    rendererRef.current?.setLevel(level);
  }, [level]);

  // Wakes immediately on every interaction (the effect re-running means one
  // just happened) and re-arms a single 15-minute timer rather than polling —
  // `lastActiveAt` only changes on real activity, so a quiet room needs
  // exactly one timeout, scheduled once, to eventually settle the Core.
  useEffect(() => {
    rendererRef.current?.setIdleDimmed(false);
    const remaining = Math.max(0, IDLE_DIM_MS - (Date.now() - lastActiveAt));
    const timer = setTimeout(() => rendererRef.current?.setIdleDimmed(true), remaining);
    return () => clearTimeout(timer);
  }, [lastActiveAt]);

  useEffect(() => {
    if (!appearance) return;
    rendererRef.current?.updateOptions({
      theme: appearance.theme,
      quality: appearance.animation_quality as QualityName,
      bloomIntensity: appearance.bloom_intensity,
      particleDensity: appearance.particle_density,
      coreScale: appearance.core_scale,
      reduceMotion: appearance.reduce_motion,
    });
  }, [appearance]);

  // Keyed by backend so switching one mounts a new element. A canvas cannot
  // change context type, so reusing it across a switch guarantees a null
  // context and a crash — which is what happens when settings arrive with
  // gpu_acceleration off after the first paint defaulted it on.
  return (
    <canvas
      key={useWebGL ? 'webgl' : 'canvas2d'}
      ref={canvasRef}
      className="nova-core"
      aria-hidden="true"
    />
  );
}
