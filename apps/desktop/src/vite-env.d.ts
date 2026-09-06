/// <reference types="vite/client" />

import type { NovaBridgeApi } from '../electron/preload';

declare global {
  interface Window {
    /** Injected by the Electron preload script; absent in a plain browser. */
    nova?: NovaBridgeApi;
  }
}

interface ImportMetaEnv {
  readonly VITE_NOVA_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

export {};
