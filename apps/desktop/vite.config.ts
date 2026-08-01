import { fileURLToPath, URL } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import electron from 'vite-plugin-electron/simple';

export default defineConfig(({ command }) => ({
  plugins: [
    react(),
    electron({
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
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@protocol': fileURLToPath(new URL('../../packages/protocol/src/index.ts', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    target: 'chrome122',
    // The Core renderer relies on precise float maths; keep it readable in
    // crash reports rather than shaving a few kilobytes.
    sourcemap: command === 'build',
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
}));
