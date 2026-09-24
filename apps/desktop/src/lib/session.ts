/**
 * Where a browser tab gets its bridge credentials.
 *
 * Electron hands the renderer a descriptor over IPC. A browser has no such
 * channel, so the token arrives once — in the link the core prints on first
 * run — and is kept from then on. Everything else comes from the page's own
 * address, because the core is what served the page.
 *
 * The token is stripped from the URL the moment it is read. A token left in an
 * address bar is one screenshot, shared link or browser-history sync away from
 * being someone else's, and unlike the Electron path there is nothing else
 * gating this surface.
 */

import type { BridgeDescriptor } from '@protocol';

const TOKEN_KEY = 'nova.bridge.token';

/** True when running as a page rather than inside the Electron shell. */
export function isBrowser(): boolean {
  return typeof window !== 'undefined' && !window.nova;
}

function safeRead(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? '';
  } catch {
    // Private browsing, or storage disabled entirely. A token held only in
    // memory still works for this tab; it just will not survive a reload.
    return '';
  }
}

function safeWrite(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* see safeRead */
  }
}

let memoryToken = '';

export function storedToken(): string {
  return memoryToken || safeRead(TOKEN_KEY);
}

export function storeToken(token: string): void {
  memoryToken = token;
  safeWrite(TOKEN_KEY, token);
}

export function clearToken(): void {
  memoryToken = '';
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* see safeRead */
  }
}

/**
 * Take a `?token=…` off the current URL, keep it, and remove it from history.
 *
 * Returns the token if one was there, so a caller can tell "just paired" from
 * "was already paired".
 */
export function claimTokenFromUrl(): string | null {
  if (typeof window === 'undefined') return null;
  const url = new URL(window.location.href);
  const token = url.searchParams.get('token');
  if (!token) return null;

  storeToken(token);
  url.searchParams.delete('token');
  window.history.replaceState({}, '', url.pathname + url.search + url.hash);
  return token;
}

/**
 * Connection details inferred from the page itself.
 *
 * Returns null when there is no token yet, which is what puts the UI into
 * pairing rather than into an endless silent retry.
 */
export function browserDescriptor(): BridgeDescriptor | null {
  const token = storedToken();
  if (!token) return null;

  const { hostname, port, protocol } = window.location;
  return {
    host: hostname,
    port: Number(port) || (protocol === 'https:' ? 443 : 80),
    token,
    pid: 0,
    version: 1,
    startedAt: 0,
  };
}

/** `wss:` whenever the page itself was served securely — a mixed-content
 * `ws://` from an `https://` page is blocked outright, and silently. */
export function socketScheme(): 'ws' | 'wss' {
  return typeof window !== 'undefined' && window.location.protocol === 'https:' ? 'wss' : 'ws';
}
