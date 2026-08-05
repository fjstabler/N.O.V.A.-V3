/**
 * The surface: a map or a live camera view, put on screen by `display.show_map`
 * / `display.show_camera` rather than described in words.
 *
 * A camera is a snapshot polled on an interval, not a persistent video stream
 * — the bridge serves one JPEG per request (see transport/server.py's
 * `_camera_response`), so this just asks for a fresh one every couple of
 * seconds and swaps the `src`. Plenty for "who's at the door"; nothing here
 * pretends to be a video call.
 */

import { useEffect, useRef, useState } from 'react';
import { pulseCore } from '@/components/NovaCore';
import type { BridgeClient } from '@/lib/bridge';
import { useNova } from '@/state/store';

const CAMERA_POLL_MS = 2000;
//: Auto-dismiss so a camera left open does not poll forever after being forgotten.
const AUTO_DISMISS_MS = 90_000;

function CameraView({ client, streamPath }: { client: BridgeClient; streamPath: string }): JSX.Element {
  const [src, setSrc] = useState<string | null>(null);

  useEffect(() => {
    const refresh = () => {
      const url = client.resourceUrl(streamPath);
      if (url) setSrc(`${url}&t=${Date.now()}`);
    };
    refresh();
    const timer = setInterval(refresh, CAMERA_POLL_MS);
    return () => clearInterval(timer);
  }, [client, streamPath]);

  if (!src) return <div className="surface__placeholder">Connecting…</div>;
  return <img className="surface__image" src={src} alt="" />;
}

function MapView({ lat, lon }: { lat: number; lon: number }): JSX.Element {
  // A small bounding box around the point; no API key, OSM's own embed export.
  const span = 0.02;
  const bbox = `${lon - span},${lat - span},${lon + span},${lat + span}`;
  const src = `https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&marker=${lat},${lon}&layer=mapnik`;
  return <iframe className="surface__frame" src={src} title="Map" loading="lazy" />;
}

export function Surface({ client }: { client: BridgeClient | null }): JSX.Element | null {
  const surface = useNova((store) => store.surface);
  const dismissSurface = useNova((store) => store.dismissSurface);
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!surface) return undefined;
    pulseCore(0.5);
    dismissTimer.current = setTimeout(dismissSurface, AUTO_DISMISS_MS);
    return () => {
      if (dismissTimer.current) clearTimeout(dismissTimer.current);
    };
  }, [surface, dismissSurface]);

  // Escape closes the surface first, before it falls through to whatever
  // App.tsx's own Escape handler does (voice-cancel).
  useEffect(() => {
    if (!surface) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        dismissSurface();
      }
    };
    window.addEventListener('keydown', onKey, { capture: true });
    return () => window.removeEventListener('keydown', onKey, { capture: true });
  }, [surface, dismissSurface]);

  if (!surface || !client) return null;

  return (
    <div className="surface" role="dialog" aria-label={surface.title}>
      <div className="surface__card">
        <header className="surface__bar">
          <h2 className="surface__title">{surface.title}</h2>
          <button
            type="button"
            className="surface__close"
            onClick={dismissSurface}
            aria-label="Close"
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </header>

        {surface.kind === 'map' ? (
          <MapView lat={surface.lat} lon={surface.lon} />
        ) : (
          <CameraView client={client} streamPath={surface.streamPath} />
        )}
      </div>
    </div>
  );
}
