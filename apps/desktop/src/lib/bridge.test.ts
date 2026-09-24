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
import { clearToken, storeToken } from './session';

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

  /** How the core turns away a token it does not accept. */
  reject(): void {
    this.readyState = 3;
    this.fire('close', { code: 4401 });
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

/**
 * For a request that is fired to inspect what went out and then abandoned.
 *
 * `close()` rejects everything still in flight, which is correct behaviour and
 * not what such a test is about — but an unhandled rejection fails the entire
 * vitest run even when every assertion passed, and the report blames whichever
 * test happened to be running when the microtask surfaced. Awaiting the
 * returned promise keeps the rejection accounted for and the failure attached
 * to the test that caused it.
 */
function expectRejected(promise: Promise<unknown>): Promise<unknown> {
  return promise.catch(() => undefined);
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

/**
 * A window with no `nova` on it, i.e. a browser. `session.ts` keeps the token
 * in a module-level variable as well as storage, so each stub gets a fresh
 * store and the resolver is re-created per test.
 */
function stubBrowser({ href, token = '' }: { href: string; token?: string }) {
  const url = new URL(href);
  const store = new Map<string, string>();
  if (token) store.set('nova.bridge.token', token);
  const replaceState = vi.fn();

  vi.stubGlobal('window', {
    location: {
      href,
      hostname: url.hostname,
      port: url.port,
      protocol: url.protocol,
      search: url.search,
    },
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => void store.set(key, value),
      removeItem: (key: string) => void store.delete(key),
    },
    history: { replaceState },
  });
  clearToken();
  if (token) storeToken(token);
  return { replaceState, store };
}

describe('descriptor resolution', () => {
  /**
   * Regression: the resolver used to fabricate a descriptor with an empty token
   * when the preload was unavailable. The core then rejected every connection
   * with 4401 forever, and the log filled with `bridge_unauthorised` while the
   * actual fault was invisible.
   *
   * There is no `window.nova` in a browser either, so this is now the ordinary
   * unpaired case rather than a broken one — but the guarantee it was added
   * for is unchanged, and matters more now that a real user can hit it.
   */
  it('never invents a token when there is neither a preload nor a stored one', async () => {
    stubBrowser({ href: 'http://panel.local:8765/app/' });
    expect(await createDescriptorResolver()()).toBeNull();
  });

  it('derives host and port from the page that served it', async () => {
    // The core is whatever answered for the page, so nothing has to be
    // configured on the device beyond the token itself.
    stubBrowser({ href: 'http://panel.local:8765/app/', token: 'stored-token' });

    expect(await createDescriptorResolver()()).toMatchObject({
      host: 'panel.local',
      port: 8765,
      token: 'stored-token',
    });
  });

  it('takes a token from the pairing link and strips it from the URL', async () => {
    // A token left in the address bar is one screenshot or shared link away
    // from being someone else's.
    const { replaceState } = stubBrowser({ href: 'http://panel.local:8765/app/?token=from-link' });

    expect(await createDescriptorResolver()()).toMatchObject({ token: 'from-link' });
    expect(replaceState).toHaveBeenCalledWith({}, '', '/app/');
  });

  it('assumes the standard port when the page did not name one', async () => {
    stubBrowser({ href: 'https://nova.example.ts.net/app/', token: 'stored-token' });
    expect(await createDescriptorResolver()()).toMatchObject({ port: 443 });
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

describe('message ids', () => {
  /**
   * Regression: ids came from `crypto.randomUUID`, which is gated on a secure
   * context. It exists on loopback and over HTTPS and is undefined on
   * `http://192.168.x.x` — so every request threw on exactly the devices this
   * is served to, and every test passed.
   */
  it('are still produced without crypto.randomUUID', async () => {
    vi.stubGlobal('crypto', {
      getRandomValues: (array: Uint8Array) => {
        for (let i = 0; i < array.length; i += 1) array[i] = i;
        return array;
      },
    });

    const { client, socket } = await connected();
    // Nothing ever answers it and `close()` below rejects it, which is fine —
    // the id is what is under test. `void` alone leaves that rejection
    // unhandled, and vitest fails the whole run on one of those even when
    // every test passes.
    const pending = expectRejected(client.request('settings.get'));

    const sent = JSON.parse(socket.sent[0]!);
    expect(sent.id).toMatch(/^[0-9a-f]{32}$/);
    client.close();
    await pending;
  });

  it('fall back again when there is no crypto at all', async () => {
    vi.stubGlobal('crypto', undefined);

    const { client, socket } = await connected();
    const pending = expectRejected(client.request('settings.get'));

    expect(JSON.parse(socket.sent[0]!).id).toBeTruthy();
    client.close();
    await pending;
  });
});

describe('a refused token', () => {
  it('tells the UI once', async () => {
    const client = makeClient();
    let rejections = 0;
    client.onUnauthorised(() => (rejections += 1));
    await client.connect();
    FakeSocket.instances.at(-1)!.reject();

    expect(rejections).toBe(1);
    client.close();
  });

  /**
   * Regression, and a miserable one to diagnose from the outside: retrying a
   * token the core has already refused is not merely wasteful. Every rejection
   * clears the stored token, so a retry loop running while someone types a
   * replacement destroys the value they are about to fix it with — the pairing
   * screen reappears the instant they submit, forever.
   */
  it('does not retry, because the answer will not change', async () => {
    vi.useFakeTimers();
    try {
      const client = makeClient();
      await client.connect();
      const opened = FakeSocket.instances.length;
      FakeSocket.instances.at(-1)!.reject();

      await vi.advanceTimersByTimeAsync(60_000);

      expect(FakeSocket.instances.length).toBe(opened);
      client.close();
    } finally {
      vi.useRealTimers();
    }
  });

  it('still reconnects when the core merely went away', async () => {
    vi.useFakeTimers();
    try {
      const client = makeClient();
      await client.connect();
      const opened = FakeSocket.instances.length;
      FakeSocket.instances.at(-1)!.close(); // an ordinary 1000

      await vi.advanceTimersByTimeAsync(60_000);

      expect(FakeSocket.instances.length).toBeGreaterThan(opened);
      client.close();
    } finally {
      vi.useRealTimers();
    }
  });

  /**
   * The other half of the same bug: a socket abandoned by a newer connect()
   * must not be able to report its own failure as this connection's.
   */
  it('ignores the death of a socket it has already replaced', async () => {
    const client = makeClient();
    let rejections = 0;
    client.onUnauthorised(() => (rejections += 1));

    await client.connect();
    const stale = FakeSocket.instances.at(-1)!;

    await client.connect(); // a new token was entered
    stale.reject(); // the old attempt lands late

    expect(rejections).toBe(0);
    client.close();
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
