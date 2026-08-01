/**
 * The only bridge between the renderer and Node.
 *
 * Deliberately tiny: the renderer talks to the core over a WebSocket, so the
 * preload surface only needs to answer "where is the core" and relay the global
 * shortcuts the main process owns. Everything else stays out of reach.
 */

import { contextBridge, ipcRenderer } from 'electron';

export interface BridgeDescriptor {
  host: string;
  port: number;
  token: string;
  pid: number;
  version: number;
  startedAt: number;
}

type Unsubscribe = () => void;

function on(channel: string, handler: (...args: unknown[]) => void): Unsubscribe {
  const listener = (_event: unknown, ...args: unknown[]) => handler(...args);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.off(channel, listener);
}

const api = {
  /** Connection details for the core service, or null if it has not started. */
  getBridge: (): Promise<BridgeDescriptor | null> => ipcRenderer.invoke('nova:get-bridge'),

  /** Host details, for the diagnostics panel. */
  getPlatform: (): Promise<{
    platform: string;
    version: string;
    electron: string;
    chrome: string;
  }> => ipcRenderer.invoke('nova:platform'),

  quit: (): Promise<void> => ipcRenderer.invoke('nova:quit'),
  toggleFullscreen: (): Promise<boolean> => ipcRenderer.invoke('nova:toggle-fullscreen'),

  onBridgeReady: (handler: (descriptor: BridgeDescriptor) => void): Unsubscribe =>
    on('nova:bridge-ready', (descriptor) => handler(descriptor as BridgeDescriptor)),
  onCoreExited: (handler: (code: number | null) => void): Unsubscribe =>
    on('nova:core-exited', (code) => handler(code as number | null)),
  onActivateVoice: (handler: () => void): Unsubscribe => on('nova:activate-voice', handler),
  onToggleSettings: (handler: () => void): Unsubscribe => on('nova:toggle-settings', handler),
  onToggleConsole: (handler: () => void): Unsubscribe => on('nova:toggle-console', handler),
};

export type NovaBridgeApi = typeof api;

contextBridge.exposeInMainWorld('nova', api);
