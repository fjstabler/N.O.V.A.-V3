/**
 * Bridge client behaviour against a fake WebSocket.
 *
 * The reconnection and correlation logic is where a transport gets subtly
 * wrong — a request that never rejects, or a stale handler that fires after
 * teardown — so it is worth exercising without a real socket.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PROTOCOL_VERSION } from '@protocol';
import { BridgeClient, BridgeError, createDescriptorResolver } from './bridge';

class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly OPEN = 1;

  readyState = 0;
  sent: string[] = [];
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>();

  constructor(readonly url: string) {
    FakeSocket.instances.push(this);
  }

  addEventListener(type: string, handler: (event: unknown) => void): void {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(handler);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.readyState = 3;
    this.fire('close', { code: 1000 });
  }

  /** Test helpers. */
  open(): void {
    this.readyState = FakeSocket.OPEN;
    this.fire('open', {});
  }

  receive(payload: unknown): void {
    this.fire('message', { data: JSON.stringify(payload) });
  }

  private fire(type: string, event: unknown): void {
    for (const handler of this.listeners.get(type) ?? []) handler(event);
  }
}

const descriptor = {
  host: '127.0.0.1',
  port: 8765,
  token: 'test-token',
  pid: 1,
  version: 1,
  startedAt: 0,
};

function makeClient(): BridgeClient {
  return new BridgeClient(async () => descriptor);
}

async function connected(): Promise<{ client: BridgeClient; socket: FakeSocket }> {
  const client = makeClient();
  await client.connect();
  const socket = FakeSocket.instances.at(-1)!;
  socket.open();
  return { client, socket };
}

beforeEach(() => {
  FakeSocket.instances = [];
  vi.stubGlobal('WebSocket', FakeSocket);
  vi.stubGlobal('crypto', { randomUUID: () => '00000000-0000-0000-0000-000000000001' });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('connection', () => {
  it('authenticates with the descriptor token', async () => {
    const { socket } = await connected();
    expect(socket.url).toContain('ws://127.0.0.1:8765/');
    expect(socket.url).toContain('token=test-token');
  });

  it('reports its connection state', async () => {
    const client = makeClient();
    const states: string[] = [];
    client.onStateChange((state) => states.push(state));

    await client.connect();
    FakeSocket.instances.at(-1)!.open();
    expect(states).toContain('connected');

    client.close();
    expect(states).toContain('offline');
  });

  it('goes to reconnecting when there is no descriptor yet', async () => {
    const client = new BridgeClient(async () => null);
    const states: string[] = [];
    client.onStateChange((state) => states.push(state));
    await client.connect();
    expect(client.connected).toBe(false);
    client.close();
  });
});

describe('resourceUrl', () => {
  it('is null before the descriptor has ever been resolved', () => {
    const client = makeClient();
    expect(client.resourceUrl('/camera/ha:camera.front_door')).toBeNull();
  });

  it('builds a URL on the descriptor host with the token appended', async () => {
    const { client } = await connected();
    expect(client.resourceUrl('/camera/ha:camera.front_door')).toBe(
      'http://127.0.0.1:8765/camera/ha:camera.front_door?token=test-token',
    );
  });

  it('appends the token with & when the path already has a query string', async () => {
    const { client } = await connected();
    expect(client.resourceUrl('/camera/x?t=1')).toBe(
      'http://127.0.0.1:8765/camera/x?t=1&token=test-token',
    );
  });
});

describe('descriptor resolution', () => {
  /**
   * Regression: the resolver used to fabricate a descriptor with an empty token
   * when the preload was unavailable. The core then rejected every connection
   * with 4401 forever, and the log filled with `bridge_unauthorised` while the
   * actual fault — a preload that failed to load — was invisible.
   */
  it('never invents a token when the preload is missing', async () => {
    vi.stubGlobal('window', {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const resolve = createDescriptorResolver();
    expect(await resolve()).toBeNull();

    expect(error).toHaveBeenCalledWith(expect.stringContaining('preload'));
    error.mockRestore();
  });

  it('warns about a missing preload only once', async () => {
    vi.stubGlobal('window', {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const resolve = createDescriptorResolver();
    await resolve();
    await resolve();
    await resolve();

    expect(error).toHaveBeenCalledTimes(1);
    error.mockRestore();
  });

  it('passes the preload descriptor straight through', async () => {
    vi.stubGlobal('window', { nova: { getBridge: async () => descriptor } });
    expect(await createDescriptorResolver()()).toEqual(descriptor);
  });

  it('returns null while the core is still binding', async () => {
    // The preload is present but the core has not written its descriptor yet.
    // That is ordinary startup, not an error — the client just keeps retrying.
    vi.stubGlobal('window', { nova: { getBridge: async () => null } });
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    expect(await createDescriptorResolver()()).toBeNull();
    expect(error).not.toHaveBeenCalled();
    error.mockRestore();
  });
});

describe('requests', () => {
  it('resolves on a matching response', async () => {
    const { client, socket } = await connected();
    const pending = client.request('settings.get');

    const sent = JSON.parse(socket.sent[0]!);
    expect(sent.kind).toBe('request');
    expect(sent.topic).toBe('settings.get');
    expect(sent.v).toBe(PROTOCOL_VERSION);

    socket.receive({
      v: PROTOCOL_VERSION,
      kind: 'response',
      topic: 'settings.get',
      id: sent.id,
      ts: 0,
      payload: { settings: { ok: true } },
    });

    await expect(pending).resolves.toEqual({ settings: { ok: true } });
  });

  it('rejects with the core error code', async () => {
    const { client, socket } = await connected();
    const pending = client.request('voice.activate');
    const sent = JSON.parse(socket.sent[0]!);

    socket.receive({
      v: PROTOCOL_VERSION,
      kind: 'error',
      topic: 'voice.activate',
      id: sent.id,
      ts: 0,
      payload: { code: 'nova.capability.unavailable', message: 'no microphone' },
    });

    await expect(pending).rejects.toThrow('no microphone');
    await pending.catch((error: BridgeError) => {
      expect(error.code).toBe('nova.capability.unavailable');
    });
  });

  it('rejects rather than hanging when offline', async () => {
    const client = makeClient();
    await expect(client.request('settings.get')).rejects.toThrow('not connected');
  });

  it('fails in-flight requests when the socket drops', async () => {
    const { client, socket } = await connected();
    const pending = client.request('settings.get');
    socket.close();
    await expect(pending).rejects.toThrow('connection closed');
    client.close();
  });
});

describe('events', () => {
  it('dispatches to topic subscribers', async () => {
    const { client, socket } = await connected();
    const seen: unknown[] = [];
    client.on('state.changed', (payload) => seen.push(payload));

    socket.receive({
      v: PROTOCOL_VERSION,
      kind: 'event',
      topic: 'state.changed',
      id: 'e1',
      ts: 0,
      payload: { state: 'listening' },
    });

    expect(seen).toEqual([{ state: 'listening' }]);
    client.close();
  });

  it('stops dispatching after unsubscribe', async () => {
    const { client, socket } = await connected();
    const seen: unknown[] = [];
    const off = client.on('state.changed', (payload) => seen.push(payload));

    const event = {
      v: PROTOCOL_VERSION,
      kind: 'event',
      topic: 'state.changed',
      id: 'e1',
      ts: 0,
      payload: {},
    };
    socket.receive(event);
    off();
    socket.receive(event);

    expect(seen).toHaveLength(1);
    client.close();
  });

  it('ignores a protocol version it does not speak', async () => {
    const { client, socket } = await connected();
    const seen: unknown[] = [];
    client.on('state.changed', (payload) => seen.push(payload));

    socket.receive({ v: 99, kind: 'event', topic: 'state.changed', id: 'e', ts: 0, payload: {} });
    expect(seen).toHaveLength(0);
    client.close();
  });

  it('survives malformed frames', async () => {
    const { client, socket } = await connected();
    expect(() => {
      socket.receive('not an envelope');
      socket.receive({ nonsense: true });
    }).not.toThrow();
    client.close();
  });

  it('isolates a throwing handler from the others', async () => {
    const { client, socket } = await connected();
    const survivors: string[] = [];
    client.on('tick', () => {
      throw new Error('boom');
    });
    client.on('tick', () => survivors.push('ok'));

    socket.receive({ v: PROTOCOL_VERSION, kind: 'event', topic: 'tick', id: 'x', ts: 0, payload: {} });
    expect(survivors).toEqual(['ok']);
    client.close();
  });
});
