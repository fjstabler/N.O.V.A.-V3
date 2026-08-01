/**
 * WebSocket client for the core service.
 *
 * Reconnection is the whole job here. The core may not be up yet when the
 * window opens, it may be restarted underneath us, or it may be a systemd unit
 * that comes and goes — so the client reconnects with exponential backoff and
 * full jitter, and re-reads the bridge descriptor each attempt in case the core
 * came back on a different port with a different token.
 *
 * Requests are correlated by message id and rejected on timeout, so a UI action
 * can never hang forever waiting on a reply that will not arrive.
 */

import {
  isEnvelope,
  PROTOCOL_VERSION,
  type BridgeDescriptor,
  type Envelope,
  type RequestTopic,
} from '@protocol';

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline';

type EventHandler = (payload: Record<string, unknown>, topic: string) => void;

interface Pending {
  resolve: (payload: Record<string, unknown>) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class BridgeError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly detail?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'BridgeError';
  }
}

const REQUEST_TIMEOUT_MS = 20_000;
const MAX_BACKOFF_MS = 15_000;

export class BridgeClient {
  private socket: WebSocket | null = null;
  private readonly pending = new Map<string, Pending>();
  private readonly handlers = new Map<string, Set<EventHandler>>();
  private readonly stateHandlers = new Set<(state: ConnectionState) => void>();
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private connectionState: ConnectionState = 'connecting';

  constructor(private readonly resolveDescriptor: () => Promise<BridgeDescriptor | null>) {}

  // ------------------------------------------------------------- connection

  async connect(): Promise<void> {
    if (this.closed) return;
    this.clearReconnect();

    const descriptor = await this.resolveDescriptor().catch(() => null);
    if (!descriptor) {
      this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting');
      this.scheduleReconnect();
      return;
    }

    const url = `ws://${descriptor.host}:${descriptor.port}/?token=${encodeURIComponent(descriptor.token)}`;

    try {
      const socket = new WebSocket(url);
      this.socket = socket;

      socket.addEventListener('open', () => {
        this.attempt = 0;
        this.setState('connected');
      });

      socket.addEventListener('message', (event) => this.handleMessage(event.data));

      socket.addEventListener('close', (event) => {
        this.socket = null;
        this.failPending(new BridgeError('connection closed', 'nova.disconnected'));
        if (this.closed) return;
        // 4401 is our own "unauthorised". The descriptor is re-read on every
        // attempt anyway, so a rotated token is picked up automatically.
        if (event.code === 4401) console.warn('[bridge] token rejected — re-reading descriptor');
        this.setState('reconnecting');
        this.scheduleReconnect();
      });

      socket.addEventListener('error', () => {
        // 'close' always follows; handle reconnection there to avoid doubling up.
      });
    } catch {
      this.setState('reconnecting');
      this.scheduleReconnect();
    }
  }

  private scheduleReconnect(): void {
    if (this.closed || this.reconnectTimer) return;
    this.attempt += 1;
    // Full jitter: several UI surfaces reconnecting in lockstep would otherwise
    // arrive at the core in a burst every time it restarts.
    const ceiling = Math.min(MAX_BACKOFF_MS, 400 * 2 ** Math.min(this.attempt, 6));
    const delay = Math.random() * ceiling;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect();
    }, delay);
  }

  private clearReconnect(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  close(): void {
    this.closed = true;
    this.clearReconnect();
    this.failPending(new BridgeError('client closed', 'nova.closed'));
    this.socket?.close();
    this.socket = null;
    this.setState('offline');
  }

  get connected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  get state(): ConnectionState {
    return this.connectionState;
  }

  private setState(state: ConnectionState): void {
    if (state === this.connectionState) return;
    this.connectionState = state;
    for (const handler of this.stateHandlers) handler(state);
  }

  onStateChange(handler: (state: ConnectionState) => void): () => void {
    this.stateHandlers.add(handler);
    return () => this.stateHandlers.delete(handler);
  }

  // ---------------------------------------------------------------- messages

  private handleMessage(raw: unknown): void {
    if (typeof raw !== 'string') return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return;
    }
    if (!isEnvelope(parsed)) return;
    const envelope = parsed as Envelope;
    if (envelope.v !== PROTOCOL_VERSION) {
      console.error(`[bridge] protocol mismatch: core speaks v${envelope.v}, UI speaks v${PROTOCOL_VERSION}`);
      return;
    }

    if (envelope.kind === 'response' || envelope.kind === 'error') {
      const pending = this.pending.get(envelope.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(envelope.id);
      if (envelope.kind === 'error') {
        const payload = envelope.payload as { code?: string; message?: string };
        pending.reject(
          new BridgeError(payload.message ?? 'request failed', payload.code ?? 'nova.error', envelope.payload),
        );
      } else {
        pending.resolve(envelope.payload);
      }
      return;
    }

    if (envelope.kind === 'event' || envelope.kind === 'hello') {
      this.emit(envelope.topic, envelope.payload);
      this.emit('*', envelope.payload, envelope.topic);
    }
  }

  private emit(topic: string, payload: Record<string, unknown>, actual = topic): void {
    const handlers = this.handlers.get(topic);
    if (!handlers) return;
    for (const handler of handlers) {
      try {
        handler(payload, actual);
      } catch (error) {
        console.error(`[bridge] handler for '${actual}' threw`, error);
      }
    }
  }

  on(topic: string, handler: EventHandler): () => void {
    let handlers = this.handlers.get(topic);
    if (!handlers) {
      handlers = new Set();
      this.handlers.set(topic, handlers);
    }
    handlers.add(handler);
    return () => {
      handlers.delete(handler);
      if (handlers.size === 0) this.handlers.delete(topic);
    };
  }

  // ---------------------------------------------------------------- requests

  request<T = Record<string, unknown>>(
    topic: RequestTopic | string,
    payload: Record<string, unknown> = {},
  ): Promise<T> {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new BridgeError('not connected to the core service', 'nova.offline'));
    }

    const id = crypto.randomUUID().replace(/-/g, '');
    const envelope: Envelope = {
      v: PROTOCOL_VERSION,
      kind: 'request',
      topic,
      id,
      ts: Date.now() / 1000,
      payload,
    };

    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BridgeError(`'${topic}' timed out`, 'nova.timeout'));
      }, REQUEST_TIMEOUT_MS);

      this.pending.set(id, {
        resolve: resolve as (payload: Record<string, unknown>) => void,
        reject,
        timer,
      });

      try {
        socket.send(JSON.stringify(envelope));
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new BridgeError(String(error), 'nova.send_failed'));
      }
    });
  }

  private failPending(error: BridgeError): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }
}

/** Read the descriptor from Electron, falling back to a dev-server default. */
export function createDescriptorResolver(): () => Promise<BridgeDescriptor | null> {
  return async () => {
    const api = window.nova;
    if (api) {
      const descriptor = await api.getBridge();
      if (descriptor) return descriptor;
    }
    // Running in a plain browser during development: assume the documented
    // defaults and let the token come from the environment.
    if (import.meta.env.DEV) {
      return {
        host: '127.0.0.1',
        port: 8765,
        token: import.meta.env.VITE_NOVA_TOKEN ?? '',
        pid: 0,
        version: 1,
        startedAt: 0,
      };
    }
    return null;
  };
}
