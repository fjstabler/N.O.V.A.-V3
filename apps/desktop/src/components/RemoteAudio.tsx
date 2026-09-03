/**
 * This device's microphone and speaker, lent to the core.
 *
 * The counterpart to `nova/voice/remote.py`. Where the mobile client records
 * while a button is held and posts one finished clip, this holds the stream
 * open: frames go up continuously and the core runs the wake word, the
 * endpointer and the follow-up window on them, exactly as it would on audio
 * from a local sound card. That is the whole difference between a remote
 * control and a device you can talk to.
 *
 * It is opt-in and needs a gesture to start, because a browser will not hand
 * over a microphone without one and because a page that grabs the mic on load
 * is a page nobody should trust. A panel skips the prompt with `?audio=1`,
 * having been set up deliberately once.
 *
 * Capture pauses while N.O.V.A. speaks. The core mutes its side too, so this
 * is not what makes it correct — it is what keeps a device from sending its
 * own voice back up a link to be thrown away, and from hearing itself at all
 * on hardware with no echo cancellation worth the name.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Requests, Topics } from '@protocol';
import type { BridgeClient } from '@/lib/bridge';

/** What the core's wake detector is trained on; anything else it cannot score. */
const TARGET_RATE = 16000;
/** 128 ms per callback at 16 kHz — eight messages a second, and about as much
 *  latency as can be added before a wake word starts to feel sluggish. */
const BUFFER_SAMPLES = 2048;

type Status = 'off' | 'starting' | 'live' | 'denied' | 'failed';

interface Session {
  stream: MediaStream;
  context: AudioContext;
  processor: ScriptProcessorNode;
  source: MediaStreamAudioSourceNode;
  sink: GainNode;
}

export function RemoteAudio({ client }: { client: BridgeClient | null }): JSX.Element | null {
  const [status, setStatus] = useState<Status>('off');
  const sessionRef = useRef<Session | null>(null);
  const sessionIdRef = useRef('');
  const capturingRef = useRef(true);
  const playerRef = useRef<HTMLAudioElement | null>(null);

  const teardown = useCallback(() => {
    const session = sessionRef.current;
    sessionRef.current = null;
    sessionIdRef.current = '';
    if (!session) return;
    session.processor.onaudioprocess = null;
    session.processor.disconnect();
    session.source.disconnect();
    session.sink.disconnect();
    for (const track of session.stream.getTracks()) track.stop();
    void session.context.close().catch(() => undefined);
  }, []);

  const start = useCallback(async () => {
    if (!client || sessionRef.current) return;
    setStatus('starting');

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch {
      // Denied, or no microphone, or — the one that wastes the most time —
      // an insecure origin, where the API is simply absent.
      setStatus(window.isSecureContext ? 'denied' : 'failed');
      return;
    }

    let attached: { sessionId?: unknown };
    try {
      attached = await client.request(Requests.AudioSourceAttach);
    } catch {
      for (const track of stream.getTracks()) track.stop();
      setStatus('failed');
      return;
    }
    sessionIdRef.current = String(attached.sessionId ?? '');

    // Asking for 16 kHz outright means no resampling at all where the engine
    // honours it, which is most of them; the fallback below covers the rest.
    let context: AudioContext;
    try {
      context = new AudioContext({ sampleRate: TARGET_RATE });
    } catch {
      context = new AudioContext();
    }
    await context.resume().catch(() => undefined);

    const source = context.createMediaStreamSource(stream);
    const processor = context.createScriptProcessor(BUFFER_SAMPLES, 1, 1);
    // A ScriptProcessorNode only fires reliably once it reaches a destination;
    // a muted gain node gives it one without anything being heard back.
    const sink = context.createGain();
    sink.gain.value = 0;

    processor.onaudioprocess = (event) => {
      if (!capturingRef.current || !sessionIdRef.current) return;
      const samples = event.inputBuffer.getChannelData(0);
      const pcm = toPcm16(samples, context.sampleRate);
      client.notify(Requests.AudioSourceFrame, {
        sessionId: sessionIdRef.current,
        pcm: pcm16ToBase64(pcm),
      });
    };

    source.connect(processor);
    processor.connect(sink);
    sink.connect(context.destination);

    sessionRef.current = { stream, context, processor, source, sink };
    capturingRef.current = true;
    setStatus('live');
  }, [client]);

  const stop = useCallback(() => {
    client?.request(Requests.AudioSourceDetach).catch(() => undefined);
    teardown();
    setStatus('off');
  }, [client, teardown]);

  // Playback, and the capture gate that goes with it.
  useEffect(() => {
    if (!client) return;

    const player = new Audio();
    player.autoplay = true;
    playerRef.current = player;

    const offPlay = client.on(Topics.RemotePlay, (payload) => {
      if (payload.sessionId && payload.sessionId !== sessionIdRef.current) return;
      const wav = typeof payload.wav === 'string' ? payload.wav : '';
      if (!wav) return;
      player.src = `data:audio/wav;base64,${wav}`;
      void player.play().catch(() => undefined);
    });

    const offStop = client.on(Topics.RemoteStop, () => {
      player.pause();
      player.currentTime = 0;
    });

    const offCapture = client.on(Topics.RemoteCapture, (payload) => {
      if (payload.sessionId && payload.sessionId !== sessionIdRef.current) return;
      capturingRef.current = payload.capture !== false;
    });

    return () => {
      offPlay();
      offStop();
      offCapture();
      player.pause();
      playerRef.current = null;
    };
  }, [client]);

  // A panel is set up once and then left alone, so it says so in its URL
  // rather than needing someone to walk over and tap a button after a reboot.
  useEffect(() => {
    if (!client) return;
    const wanted = new URLSearchParams(window.location.search).get('audio') === '1';
    if (wanted && status === 'off') void start();
  }, [client, status, start]);

  // Re-attach after the core restarts: the session it knew about is gone.
  useEffect(() => {
    if (!client) return;
    return client.onStateChange((next) => {
      if (next === 'connected' && sessionRef.current) {
        void client
          .request(Requests.AudioSourceAttach)
          .then((result) => {
            sessionIdRef.current = String((result as { sessionId?: unknown }).sessionId ?? '');
          })
          .catch(() => undefined);
      }
    });
  }, [client]);

  useEffect(() => teardown, [teardown]);

  if (status === 'live') {
    return (
      <button type="button" className="mic-toggle is-live" onClick={stop} aria-label="Stop using this microphone">
        <MicIcon />
      </button>
    );
  }

  return (
    <button
      type="button"
      className="mic-toggle"
      onClick={() => void start()}
      disabled={!client || status === 'starting'}
      aria-label="Use this device's microphone"
      title={
        status === 'denied'
          ? 'Microphone permission was denied'
          : status === 'failed'
            ? 'Microphone unavailable — this page must be served over HTTPS or localhost'
            : "Use this device's microphone"
      }
    >
      <MicIcon muted />
    </button>
  );
}

function MicIcon({ muted = false }: { muted?: boolean }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0014 0M12 18v4" />
      {muted && <path d="M4 4l16 16" />}
    </svg>
  );
}

/** Linear interpolation down to 16 kHz — speech, not music; simplicity is fine. */
function toPcm16(samples: Float32Array, fromRate: number): Int16Array {
  if (fromRate === TARGET_RATE) {
    const out = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) out[i] = clamp(samples[i] ?? 0);
    return out;
  }
  const ratio = fromRate / TARGET_RATE;
  const length = Math.floor(samples.length / ratio);
  const out = new Int16Array(length);
  for (let i = 0; i < length; i += 1) {
    const position = i * ratio;
    const index = Math.floor(position);
    const a = samples[index] ?? 0;
    const b = samples[index + 1] ?? a;
    out[i] = clamp(a + (b - a) * (position - index));
  }
  return out;
}

function clamp(value: number): number {
  return Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
}

function pcm16ToBase64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  // Chunked so String.fromCharCode.apply is never handed an array long enough
  // to overflow the call stack.
  const CHUNK = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + CHUNK)));
  }
  return btoa(binary);
}
