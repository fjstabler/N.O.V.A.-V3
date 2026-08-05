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

  // WebKit does not hold its own strong reference to a SpeechSynthesisUtterance
  // until playback actually begins — if the only reference is a variable local
  // to the function that created it, iOS Safari's garbage collector can (and
  // routinely does) reap it before speak() gets around to it. No error, it
  // just never speaks. Keeping the live utterance here, outside any function
  // scope, is what keeps it alive long enough to actually play.
  let activeUtterance = null;

  // iOS Safari also only allows speechSynthesis.speak() while still inside a
  // user gesture's call stack. A reply always arrives after an await (the
  // network round trip), by which point that window has closed and speak()
  // silently does nothing. Speaking once, right here, synchronously on the
  // very first tap anywhere on the page, unlocks the engine for the rest of
  // the session, including later calls made from a promise callback.
  window.addEventListener(
    'pointerdown',
    () => {
      if (!('speechSynthesis' in window)) return;
      activeUtterance = new SpeechSynthesisUtterance('.');
      activeUtterance.volume = 0;
      window.speechSynthesis.speak(activeUtterance);
    },
    { once: true },
  );

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
  };

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

  /** Speaks the reply and drives the Core's "speaking" state in step with
   * actual playback, not just for the time it took to call speak(). */
  function speak(text) {
    if (!('speechSynthesis' in window) || !text.trim()) {
      setCoreState('idle');
      return;
    }
    window.speechSynthesis.cancel();
    // Assigned to the module-level activeUtterance, not a local — see the
    // comment by its declaration. A local here is exactly the shape of the
    // bug: it goes out of scope the moment this function returns, which is
    // normally fine, except WebKit's GC treats "out of scope" as "collectible"
    // even mid-utterance.
    activeUtterance = new SpeechSynthesisUtterance(text);
    activeUtterance.onstart = () => setCoreState('speaking');
    activeUtterance.onend = () => setCoreState('idle');
    activeUtterance.onerror = () => setCoreState('idle');
    window.speechSynthesis.speak(activeUtterance);
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
        speak(result.text); // moves the Core into "speaking", then back to idle
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
