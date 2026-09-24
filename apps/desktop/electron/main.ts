/**
 * Electron main process.
 *
 * Owns the window and the core service's lifetime. Three things matter here and
 * nothing else does:
 *
 * 1. The window is frameless, fullscreen and kiosk-locked — the spec asks for
 *    something that reads as native software, not a page in a browser.
 * 2. The core is either spawned as a child process or attached to if one is
 *    already running (as a systemd unit, say). The bridge descriptor the core
 *    writes on startup is what makes both paths identical to the renderer.
 * 3. Nothing is loaded from the network and the renderer is fully isolated:
 *    context isolation on, node integration off, navigation blocked.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir, platform } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { app, BrowserWindow, globalShortcut, ipcMain, screen, shell } from 'electron';

const dirname = fileURLToPath(new URL('.', import.meta.url));

process.env.APP_ROOT = join(dirname, '..');
const RENDERER_DIST = join(process.env.APP_ROOT, 'dist');
const DEV_SERVER_URL = process.env.VITE_DEV_SERVER_URL;

interface BridgeDescriptor {
  host: string;
  port: number;
  token: string;
  pid: number;
  version: number;
  startedAt: number;
}

let mainWindow: BrowserWindow | null = null;
let coreProcess: ChildProcessWithoutNullStreams | null = null;
let descriptor: BridgeDescriptor | null = null;
let quitting = false;

// ---------------------------------------------------------------- core service

/** Where the core writes its bridge descriptor, mirroring nova/config/paths.py. */
function descriptorPath(): string {
  const override = process.env.NOVA_HOME;
  if (override) return join(override, 'bridge.json');
  if (platform() === 'win32') {
    return join(process.env.LOCALAPPDATA ?? join(homedir(), 'AppData', 'Local'), 'NOVA', 'bridge.json');
  }
  if (platform() === 'darwin') {
    return join(homedir(), 'Library', 'Application Support', 'NOVA', 'bridge.json');
  }
  return join(process.env.XDG_DATA_HOME ?? join(homedir(), '.local', 'share'), 'nova', 'bridge.json');
}

function readDescriptor(): BridgeDescriptor | null {
  const path = descriptorPath();
  if (!existsSync(path)) return null;
  try {
    const parsed = JSON.parse(readFileSync(path, 'utf-8')) as BridgeDescriptor;
    if (!parsed.port || !parsed.token) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** True if a core is already listening — then we attach instead of spawning. */
async function coreIsRunning(candidate: BridgeDescriptor | null): Promise<boolean> {
  if (!candidate) return false;
  try {
    // A running core answers the HTTP upgrade endpoint; anything else means stale.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 800);
    await fetch(`http://${candidate.host}:${candidate.port}/`, { signal: controller.signal }).catch(
      () => undefined,
    );
    clearTimeout(timer);
    return await isPortOpen(candidate.host, candidate.port);
  } catch {
    return false;
  }
}

function isPortOpen(host: string, port: number): Promise<boolean> {
  return new Promise((resolvePromise) => {
    import('node:net').then(({ Socket }) => {
      const socket = new Socket();
      const done = (open: boolean) => {
        socket.destroy();
        resolvePromise(open);
      };
      socket.setTimeout(700);
      socket.once('connect', () => done(true));
      socket.once('timeout', () => done(false));
      socket.once('error', () => done(false));
      socket.connect(port, host);
    });
  });
}

/** Locate the Python interpreter that has nova-core installed. */
function resolvePython(): string {
  if (process.env.NOVA_PYTHON) return process.env.NOVA_PYTHON;
  const root = resolve(process.env.APP_ROOT ?? '.', '..', '..');
  const candidates =
    platform() === 'win32'
      ? [join(root, '.venv', 'Scripts', 'python.exe'), 'python']
      : [join(root, '.venv', 'bin', 'python'), 'python3'];
  return candidates.find((candidate) => candidate.includes('venv') && existsSync(candidate)) ?? candidates[candidates.length - 1]!;
}

async function startCore(): Promise<BridgeDescriptor | null> {
  const existing = readDescriptor();
  if (await coreIsRunning(existing)) {
    console.info('[nova] attaching to a core already running on port', existing!.port);
    return existing;
  }

  const python = resolvePython();
  const cwd = resolve(process.env.APP_ROOT ?? '.', '..', '..', 'services', 'core');
  console.info('[nova] starting core:', python, '-m nova');

  return new Promise((resolvePromise) => {
    let settled = false;
    const child = spawn(python, ['-m', 'nova'], {
      cwd: existsSync(cwd) ? cwd : undefined,
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });
    coreProcess = child;

    // The core prints its descriptor on stdout as soon as the bridge is bound.
    child.stdout.on('data', (chunk: Buffer) => {
      const text = chunk.toString();
      for (const line of text.split('\n')) {
        if (line.startsWith('NOVA_BRIDGE_READY ') && !settled) {
          settled = true;
          try {
            resolvePromise(JSON.parse(line.slice('NOVA_BRIDGE_READY '.length)) as BridgeDescriptor);
          } catch {
            resolvePromise(null);
          }
        }
      }
      process.stdout.write(text);
    });
    child.stderr.on('data', (chunk: Buffer) => process.stderr.write(chunk));

    child.on('error', (error) => {
      console.error('[nova] could not start the core service:', error.message);
      if (!settled) {
        settled = true;
        resolvePromise(null);
      }
    });

    child.on('exit', (code) => {
      coreProcess = null;
      if (!quitting) {
        console.error(`[nova] core exited with code ${code}`);
        mainWindow?.webContents.send('nova:core-exited', code);
      }
      if (!settled) {
        settled = true;
        resolvePromise(null);
      }
    });

    // If the core is slow to bind, fall back to whatever descriptor exists so
    // the renderer can keep retrying rather than showing nothing at all.
    setTimeout(() => {
      if (!settled) {
        settled = true;
        resolvePromise(readDescriptor());
      }
    }, 30_000);
  });
}

function stopCore(): void {
  if (!coreProcess) return;
  // The core installs SIGTERM/SIGINT handlers and shuts down its services in
  // dependency order; killing it outright would leave the audio device open.
  coreProcess.kill('SIGTERM');
  const child = coreProcess;
  setTimeout(() => {
    if (!child.killed) child.kill('SIGKILL');
  }, 5000);
  coreProcess = null;
}

// --------------------------------------------------------------------- window

function createWindow(): BrowserWindow {
  const { width, height } = screen.getPrimaryDisplay().bounds;

  const window = new BrowserWindow({
    width,
    height,
    show: false,
    frame: false,
    fullscreen: true,
    kiosk: !DEV_SERVER_URL,
    backgroundColor: '#04060d',
    autoHideMenuBar: true,
    title: 'N.O.V.A.',
    icon: join(process.env.APP_ROOT!, 'build', 'icon.png'),
    webPreferences: {
      // .cjs, not .mjs — see the preload build config in vite.config.ts.
      preload: join(dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      // The Core is a WebGL2 renderer; without this a background window drops
      // to 1 FPS and the animation stutters when it regains focus.
      backgroundThrottling: false,
    },
  });

  // Painting only once the first frame is ready avoids a white flash over a
  // near-black interface.
  window.once('ready-to-show', () => {
    window.show();
    window.focus();
  });

  // Nothing in this app should ever navigate or open a browser window.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) void shell.openExternal(url);
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event) => event.preventDefault());

  if (DEV_SERVER_URL) {
    void window.loadURL(DEV_SERVER_URL);
  } else {
    void window.loadFile(join(RENDERER_DIST, 'index.html'));
  }

  return window;
}

/**
 * Register only the shortcuts that must work when the window is NOT focused.
 *
 * A global shortcut is consumed system-wide: the key never reaches the web
 * contents. Registering the settings and console keys here therefore *prevented*
 * the renderer's own handler from ever seeing them — they were swallowed by the
 * main process and forwarded over IPC, which is a strictly worse path because it
 * fails silently if anything upstream is wrong.
 *
 * So: push-to-talk and quit are global, because you may want them while another
 * application has focus. Everything else is handled in the renderer, where the
 * key actually arrives and nothing can intercept it.
 */
function registerShortcuts(window: BrowserWindow): void {
  const bindings: Record<string, () => void> = {
    'CommandOrControl+Shift+Q': () => app.quit(),
    'CommandOrControl+Shift+Space': () => window.webContents.send('nova:activate-voice'),
  };

  for (const [accelerator, handler] of Object.entries(bindings)) {
    if (!globalShortcut.register(accelerator, handler)) {
      console.warn(`[nova] global shortcut unavailable: ${accelerator}`);
    }
  }

  // Window-level keys, handled before the page sees them. These work regardless
  // of what the desktop environment has claimed globally.
  window.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const modifier = input.control || input.meta;

    if (input.key === 'F11' || (modifier && input.shift && input.key.toLowerCase() === 'f')) {
      window.setFullScreen(!window.isFullScreen());
      event.preventDefault();
    } else if (input.key === 'F12' || (modifier && input.shift && input.key.toLowerCase() === 'i')) {
      window.webContents.toggleDevTools();
      event.preventDefault();
    } else if (modifier && input.shift && input.key.toLowerCase() === 'r') {
      window.reload();
      event.preventDefault();
    }
  });
}

// ----------------------------------------------------------------------- ipc

function registerIpc(): void {
  ipcMain.handle('nova:get-bridge', () => descriptor ?? readDescriptor());
  ipcMain.handle('nova:quit', () => app.quit());
  ipcMain.handle('nova:toggle-fullscreen', () => {
    if (!mainWindow) return false;
    const next = !mainWindow.isFullScreen();
    mainWindow.setFullScreen(next);
    return next;
  });
  ipcMain.handle('nova:platform', () => ({
    platform: process.platform,
    version: app.getVersion(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
  }));
}

// -------------------------------------------------------------------- startup

// A second instance would fight the first over the audio device and the bridge.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    registerIpc();
    mainWindow = createWindow();
    registerShortcuts(mainWindow);

    descriptor = await startCore();
    if (descriptor) {
      mainWindow.webContents.send('nova:bridge-ready', descriptor);
    } else {
      console.error('[nova] no core service — the interface will retry in the background');
    }
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) mainWindow = createWindow();
  });

  app.on('before-quit', () => {
    quitting = true;
    stopCore();
  });

  app.on('will-quit', () => globalShortcut.unregisterAll());
}
