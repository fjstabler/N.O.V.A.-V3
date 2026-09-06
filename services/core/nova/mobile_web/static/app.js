/**
 * N.O.V.A. mobile web client.
 *
 * No build step, no framework — this is served directly by the core's own
 * bridge process (see transport/server.py's process_request), so it has to
 * work as a single static file set with nothing to compile. Talks the same
 * WebSocket protocol the desktop shell does (packages/protocol/src, hand
 * -mirrored here since pulling in that build pipeline for one small page
 * would be a lot of machinery for a page this size).
 *
 * Push-to-talk instead of a wake word: iOS suspends background tabs, so
 * always-listening detection from a phone browser is not achievable, and the
 * user explicitly chose hold-to-talk as the permanent design here rather
 * than a workaround to reach for later. The Core itself (core.js) is the
 * control — hold it to talk — the same way the desktop app is built around
 * it rather than a row of buttons; this page mirrors that instead of being
 * a generic chat window.
 */

(() => {
  'use strict';

  const TOKEN_KEY = 'nova_token';
  const SAMPLE_RATE = 16000; // matches nova.voice.audio.SAMPLE_RATE
  const MAX_RECORD_MS = 30_000;

  const el = {
    statusDot: document.getElementById('status-dot'),
    statusText: document.getElementById('status-text'),
    pairing: document.getElementById('pairing'),
    tokenInput: document.getElementById('token-input'),
    tokenSave: document.getElementById('token-save'),
    conversation: document.getElementById('conversation'),
    textForm: document.getElementById('text-form'),
    textInput: document.getElementById('text-input'),
    stage: document.getElementById('stage'),
    stageCaption: document.getElementById('stage-caption'),
    stageLine: document.getElementById('stage-line'),
    player: document.getElementById('player'),
    cameraView: document.getElementById('camera-view'),
    cameraViewTitle: document.getElementById('camera-view-title'),
    cameraViewClose: document.getElementById('camera-view-close'),
    cameraViewImage: document.getElementById('camera-view-image'),
    cameraViewError: document.getElementById('camera-view-error'),
    cameraViewBack: document.getElementById('camera-view-back'),
  };

  // Replies are read aloud through this one <audio> element, playing WAV
  // bytes the core synthesises server-side (the same Kokoro voice the
  // desktop hears) rather than through the browser's own speechSynthesis.
  // That API sounded like the simpler route, but proved unreliable on real
  // iOS hardware even after working around its user-gesture rule and its
  // habit of garbage-collecting an utterance before it plays — it was just
  // silent. An <audio> element unlocked once by a real play() inside a user
  // gesture is the pattern iOS actually honours for everything after, which
  // is why this reuses one element for every reply instead of creating a new
  // Audio() each time — a fresh element would not carry the unlock forward.
  window.addEventListener(
    'pointerdown',
    () => {
      el.player.src = silentWavUrl();
      el.player.play().catch(() => {});
    },
    { once: true },
  );

  function silentWavUrl() {
    const sampleRate = 8000;
    const buffer = new ArrayBuffer(46); // 44-byte header + one silent int16 sample
    const view = new DataView(buffer);
    const writeString = (offset, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
    };
    writeString(0, 'RIFF');
    view.setUint32(4, 38, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, 'data');
    view.setUint32(40, 2, true);
    view.setInt16(44, 0, true);
    return URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }));
  }

  // ------------------------------------------------------------------ core

  const CAPTIONS = {
    idle: 'Hold to talk',
    listening: 'Listening…',
    thinking: 'Thinking…',
    speaking: 'Speaking…',
    error: 'Hold to talk',
  };

  function setCoreState(state) {
    window.NovaCoreView?.setState(state);
    el.stage.classList.toggle('is-recording', state === 'listening');
    el.stageCaption.textContent = CAPTIONS[state] || CAPTIONS.idle;
  }

  // ------------------------------------------------------------- pairing

  function readToken() {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get('token');
    if (fromUrl) {
      localStorage.setItem(TOKEN_KEY, fromUrl);
      url.searchParams.delete('token');
      window.history.replaceState({}, '', url.toString());
      return fromUrl;
    }
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function showPairing(message) {
    el.pairing.hidden = false;
    if (message) el.tokenInput.placeholder = message;
  }

  el.tokenSave.addEventListener('click', () => {
    const value = el.tokenInput.value.trim();
    if (!value) return;
    localStorage.setItem(TOKEN_KEY, value);
    el.pairing.hidden = true;
    bridge.connect();
  });

  // ---------------------------------------------------------- camera view

  // A room-watch alert's push notification links straight here — see
  // security/service.py's _mobile_camera_url — with the same slug format
  // the desktop app's camera surface uses. Reading it once at load, rather
  // than wiring live ui.surface.show events the way the desktop app does,
  // is deliberate: this page only ever needs to show the one camera a
  // notification pointed at, not react to arbitrary events mid-session.
  const CAMERA_POLL_MIN_GAP_MS = 250;
  // A dropped frame or two is normal on a flaky connection; only give up
  // once frames have failed continuously for a while. The old code retried
  // silently forever on any failure (wrong/expired token, camera offline,
  // N.O.V.A. unreachable from outside the home network) — from a link saved
  // to a phone's home screen, which reopens straight into this view, that
  // read as the whole app being permanently broken: a black box with no
  // explanation and nothing telling the person anything had gone wrong.
  const CAMERA_FAILURE_TIMEOUT_MS = 8000;
  let cameraPollTimer = null;
  let cameraFailingSince = null;

  function titleFromCameraSlug(slug) {
    const name = slug.includes(':') ? slug.slice(slug.indexOf(':') + 1) : slug;
    return name.replace(/[._]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function showCameraError() {
    if (cameraPollTimer) clearTimeout(cameraPollTimer);
    cameraPollTimer = null;
    el.cameraViewImage.onload = null;
    el.cameraViewImage.onerror = null;
    el.cameraViewImage.hidden = true;
    el.cameraViewError.hidden = false;
  }

  function openCameraView(slug) {
    const token = readToken();
    if (!token) {
      showPairing('paste the token, then reopen the camera link');
      return;
    }
    el.cameraViewError.hidden = true;
    el.cameraViewImage.hidden = false;
    el.cameraView.hidden = false;
    el.cameraViewTitle.textContent = titleFromCameraSlug(slug);
    cameraFailingSince = null;
    const path = `/camera/${encodeURIComponent(slug)}`;
    const fetchFrame = () => {
      el.cameraViewImage.src = `${path}?token=${encodeURIComponent(token)}&t=${Date.now()}`;
    };
    el.cameraViewImage.onload = () => {
      cameraFailingSince = null;
      cameraPollTimer = setTimeout(fetchFrame, CAMERA_POLL_MIN_GAP_MS);
    };
    el.cameraViewImage.onerror = () => {
      if (cameraFailingSince === null) cameraFailingSince = Date.now();
      if (Date.now() - cameraFailingSince > CAMERA_FAILURE_TIMEOUT_MS) {
        showCameraError();
        return;
      }
      cameraPollTimer = setTimeout(fetchFrame, CAMERA_POLL_MIN_GAP_MS);
    };
    fetchFrame();
  }

  function closeCameraView() {
    if (cameraPollTimer) clearTimeout(cameraPollTimer);
    cameraPollTimer = null;
    cameraFailingSince = null;
    el.cameraViewImage.onload = null;
    el.cameraViewImage.onerror = null;
    el.cameraViewImage.src = '';
    el.cameraView.hidden = true;
    const url = new URL(window.location.href);
    url.searchParams.delete('camera');
    window.history.replaceState({}, '', url.toString());
  }

  el.cameraViewClose.addEventListener('click', closeCameraView);
  el.cameraViewBack.addEventListener('click', closeCameraView);

  const cameraSlug = new URL(window.location.href).searchParams.get('camera');
  if (cameraSlug) openCameraView(cameraSlug);

  // -------------------------------------------------------------- bridge

  const bridge = {
    socket: null,
    pending: new Map(),
    reconnectDelay: 1000,
    connected: false,

    connect() {
      const token = readToken();
      if (!token) {
        setStatus('error', 'Not paired');
        showPairing();
        return;
      }
      const scheme = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
      const url = `${scheme}${window.location.host}/?token=${encodeURIComponent(token)}`;
      setStatus('connecting');
      const socket = new WebSocket(url);
      this.socket = socket;

      socket.addEventListener('open', () => {
        this.reconnectDelay = 1000;
      });
      socket.addEventListener('message', (event) => this._onMessage(event));
      socket.addEventListener('close', (event) => this._onClose(event));
      socket.addEventListener('error', () => socket.close());
    },

    _onMessage(event) {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.kind === 'hello') {
        this.connected = true;
        setStatus('connected');
        return;
      }
      if (message.kind === 'event') {
        return; // this client works purely request/response; see module docstring
      }
      const waiting = this.pending.get(message.id);
      if (!waiting) return;
      this.pending.delete(message.id);
      if (message.kind === 'error') {
        waiting.reject(new Error(message.payload?.message || 'request failed'));
      } else {
        waiting.resolve(message.payload);
      }
    },

    _onClose(event) {
      this.connected = false;
      for (const { reject } of this.pending.values()) reject(new Error('connection closed'));
      this.pending.clear();
      if (event.code === 4401) {
        localStorage.removeItem(TOKEN_KEY);
        setStatus('error', 'Wrong token');
        showPairing('token rejected — paste it again');
        return;
      }
      setStatus('error', 'Reconnecting…');
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 1.6, 15_000);
    },

    request(topic, payload = {}, timeoutMs = 60_000) {
      if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
        return Promise.reject(new Error('not connected'));
      }
      const id = crypto.randomUUID();
      const envelope = { v: 1, kind: 'request', topic, id, ts: Date.now() / 1000, payload };
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error('timed out'));
        }, timeoutMs);
        this.pending.set(id, {
          resolve: (value) => { clearTimeout(timer); resolve(value); },
          reject: (err) => { clearTimeout(timer); reject(err); },
        });
        this.socket.send(JSON.stringify(envelope));
      });
    },
  };

  function setStatus(state, label) {
    el.statusDot.classList.toggle('is-connected', state === 'connected');
    el.statusDot.classList.toggle('is-error', state === 'error');
    el.statusText.textContent =
      label || { connecting: 'Connecting…', connected: 'Connected', error: 'Disconnected' }[state];
  }

  // --------------------------------------------------------- conversation UI

  function addTurn(kind, text) {
    const bubble = document.createElement('div');
    bubble.className = `turn ${kind}`;
    bubble.textContent = text;
    el.conversation.appendChild(bubble);
    el.conversation.scrollTop = el.conversation.scrollHeight;
    return bubble;
  }

  /** Plays the reply's server-synthesised audio and drives the Core's
   * "speaking" state in step with actual playback. `base64Wav` is absent
   * when TTS is unavailable server-side (Kokoro not loaded) — the reply
   * still showed as text, it just is not read aloud, the same degrade the
   * desktop app makes without Kokoro. */
  function playReply(base64Wav) {
    if (!base64Wav) {
      setCoreState('idle');
      return;
    }
    el.player.onplay = () => setCoreState('speaking');
    el.player.onended = () => setCoreState('idle');
    el.player.onerror = () => setCoreState('idle');
    el.player.src = `data:audio/wav;base64,${base64Wav}`;
    el.player.play().catch(() => setCoreState('idle'));
  }

  async function handleResult(promise, userBubbleText) {
    const userBubble = addTurn('user', userBubbleText);
    const pending = addTurn('nova pending', 'Thinking…');
    setBusy(true);
    setCoreState('thinking');
    el.stageLine.classList.remove('error');
    try {
      const result = await promise;
      // The audio path does not know what was heard until this response
      // arrives — swap the placeholder for the real transcript.
      if (result.transcript && userBubbleText === '…') {
        userBubble.textContent = result.transcript;
      }
      pending.classList.remove('pending');
      if (result.error) {
        pending.classList.add('error');
        pending.textContent = result.error;
        el.stageLine.textContent = result.error;
        el.stageLine.classList.add('error');
        setCoreState('error');
        setTimeout(() => setCoreState('idle'), 1200);
      } else if (!result.text) {
        pending.remove();
        el.stageLine.textContent = '';
        setCoreState('idle');
      } else {
        pending.textContent = result.text;
        el.stageLine.textContent = result.text;
        playReply(result.audio); // moves the Core into "speaking", then back to idle
      }
    } catch (err) {
      if (userBubbleText === '…') userBubble.remove(); // never learned what, if anything, was heard
      pending.classList.remove('pending');
      pending.classList.add('error');
      const message = err.message || 'Something went wrong.';
      pending.textContent = message;
      el.stageLine.textContent = message;
      el.stageLine.classList.add('error');
      setCoreState('error');
      setTimeout(() => setCoreState('idle'), 1200);
    } finally {
      setBusy(false);
    }
  }

  function setBusy(busy) {
    el.stage.classList.toggle('is-busy', busy);
    el.textForm.querySelector('button').disabled = busy;
  }

  // ---------------------------------------------------------------- text

  el.textForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = el.textInput.value.trim();
    if (!text || el.stage.classList.contains('is-busy')) return;
    el.textInput.value = '';
    handleResult(bridge.request('text.submit', { text, source: 'mobile' }), text);
  });

  // ----------------------------------------------------- push to talk audio

  const recorder = {
    stream: null,
    context: null,
    processor: null,
    chunks: [],
    recording: false,
    autoStopTimer: null,

    async start() {
      if (this.recording || el.stage.classList.contains('is-busy')) return;
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        });
      } catch {
        setStatus('error', 'Microphone denied');
        return;
      }
      this.context = new (window.AudioContext || window.webkitAudioContext)();
      const source = this.context.createMediaStreamSource(this.stream);
      // ScriptProcessorNode must reach a destination to fire reliably in some
      // engines; route it through a silent gain so nothing is heard back.
      const silence = this.context.createGain();
      silence.gain.value = 0;
      this.processor = this.context.createScriptProcessor(4096, 1, 1);
      this.chunks = [];
      this.processor.onaudioprocess = (event) => {
        const data = event.inputBuffer.getChannelData(0);
        this.chunks.push(new Float32Array(data));
        window.NovaCoreView?.setLevel(rmsLevel(data));
      };
      source.connect(this.processor);
      this.processor.connect(silence);
      silence.connect(this.context.destination);

      this.recording = true;
      setCoreState('listening');
      this.autoStopTimer = setTimeout(() => this.stop(), MAX_RECORD_MS);
    },

    stop() {
      if (!this.recording) return;
      this.recording = false;
      clearTimeout(this.autoStopTimer);
      window.NovaCoreView?.setLevel(0);

      const sourceRate = this.context.sampleRate; // read before close(); some engines drop it after
      this.processor?.disconnect();
      this.context?.close();
      this.stream?.getTracks().forEach((track) => track.stop());

      const audio = flattenChunks(this.chunks);
      this.chunks = [];
      if (audio.length < sourceRate * 0.3) {
        setCoreState('idle');
        return; // too short to be real speech
      }

      const resampled = resampleTo16k(audio, sourceRate);
      const base64 = pcm16ToBase64(resampled);
      handleResult(
        bridge.request('voice.audio.submit', { audio: base64 }).then((result) => {
          if (result.transcript) return result;
          throw new Error("Didn't catch that — try again.");
        }),
        '…',
      );
    },
  };

  /** Root-mean-square loudness of one audio block, scaled for a punchier
   * visual reaction — typical speech RMS is small (0.01–0.3). */
  function rmsLevel(samples) {
    let sumSquares = 0;
    for (let i = 0; i < samples.length; i += 1) sumSquares += samples[i] * samples[i];
    return Math.sqrt(sumSquares / samples.length) * 4;
  }

  function flattenChunks(chunks) {
    const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const out = new Float32Array(length);
    let offset = 0;
    for (const chunk of chunks) {
      out.set(chunk, offset);
      offset += chunk.length;
    }
    return out;
  }

  /** Linear-interpolation resample — speech, not music; simplicity is fine. */
  function resampleTo16k(samples, fromRate) {
    if (fromRate === SAMPLE_RATE) return floatToInt16(samples);
    const ratio = fromRate / SAMPLE_RATE;
    const outLength = Math.floor(samples.length / ratio);
    const out = new Int16Array(outLength);
    for (let i = 0; i < outLength; i += 1) {
      const position = i * ratio;
      const index = Math.floor(position);
      const frac = position - index;
      const a = samples[index] || 0;
      const b = samples[index + 1] ?? a;
      const value = a + (b - a) * frac;
      out[i] = Math.max(-32768, Math.min(32767, Math.round(value * 32767)));
    }
    return out;
  }

  function floatToInt16(samples) {
    const out = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i += 1) {
      out[i] = Math.max(-32768, Math.min(32767, Math.round(samples[i] * 32767)));
    }
    return out;
  }

  /** btoa() over the raw bytes, chunked so a long recording cannot blow the
   * call stack via String.fromCharCode.apply on one giant array. */
  function pcm16ToBase64(int16) {
    const bytes = new Uint8Array(int16.buffer);
    const CHUNK = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  el.stage.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    recorder.start();
  });
  el.stage.addEventListener('pointerup', () => recorder.stop());
  el.stage.addEventListener('pointercancel', () => recorder.stop());
  el.stage.addEventListener('pointerleave', () => {
    if (recorder.recording) recorder.stop();
  });

  // ------------------------------------------------------------------ boot

  bridge.connect();
})();
