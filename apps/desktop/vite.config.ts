import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import electron from 'vite-plugin-electron/simple';

/**
 * Two targets from one source.
 *
 * `--mode web` drops the Electron plugin and writes the bundle into the core's
 * package, where the bridge serves it at `/app/`. It is the same React app the
 * shell runs: the shell's extras — owning the core's lifecycle, global
 * shortcuts — are reached through an optional `window.nova` that a browser
 * simply does not have. Building a second GUI instead would mean two to keep
 * in step, and the second one would always be the poor relation.
 */
export default defineConfig(({ command, mode }) => {
  const web = mode === 'web';

  return {
  // Absolute for the web build so `/app` without a trailing slash still finds
  // its assets; relative for Electron, which loads index.html over file://.
  base: web ? '/app/' : './',
  plugins: [
    react(),
    ...(web ? [] : [electron({
      main: {
        entry: 'electron/main.ts',
        vite: {
          build: {
            outDir: 'dist-electron',
            rollupOptions: { external: ['electron'] },
          },
        },
      },
      preload: {
        input: 'electron/preload.ts',
        vite: {
          build: {
            outDir: 'dist-electron',
            rollupOptions: {
              external: ['electron'],
              // Emit CommonJS with an explicit .cjs extension. The package is
              // `"type": "module"`, so a preload named .mjs would be loaded as
              // an ES module — and the bundler emits `require("electron")`,
              // which throws there. Electron always honours .cjs as CommonJS
              // regardless of the surrounding package type, so this is the one
              // naming that cannot be misinterpreted.
              output: { format: 'cjs', entryFileNames: 'preload.cjs' },
            },
          },
        },
      },
    })]),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@protocol': fileURLToPath(new URL('../../packages/protocol/src/index.ts', import.meta.url)),
    },
  },
  build: {
    outDir: web
      ? fileURLToPath(new URL('../../services/core/nova/webapp/static', import.meta.url))
      : 'dist',
    emptyOutDir: true,
    // A panel runs a much older Chromium than an Electron 32 renderer does,
    // and a bundle it cannot parse fails as a blank screen with nothing in the
    // log worth reading.
    target: web ? 'chrome87' : 'chrome122',
    // The Core renderer relies on precise float maths; keep it readable in
    // crash reports rather than shaving a few kilobytes. Not for the web
    // bundle, which is committed — a 600 kB map per build is churn the
    // repository does not need, and a local build still produces one.
    sourcemap: !web && command === 'build',
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5273,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
  };
});
