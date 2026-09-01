import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Canonical Bernstein orchestrator port (DEFAULT 8052 - see run_bootstrap).
      '/api': { target: 'http://127.0.0.1:8052', changeOrigin: true },
      // Proxy FastAPI's auto-generated docs surface so an operator hitting
      // ``/openapi.json``, ``/docs``, ``/redoc`` through the dev server gets
      // a real response instead of a Vite 404 + ``no fallback for /openapi.json``
      // confusion.  These paths live OUTSIDE ``/api/v1`` on the backend.
      '/openapi.json': { target: 'http://127.0.0.1:8052', changeOrigin: true },
      '/docs': { target: 'http://127.0.0.1:8052', changeOrigin: true },
      '/redoc': { target: 'http://127.0.0.1:8052', changeOrigin: true },
    },
  },
  build: {
    outDir: '../src/bernstein/gui/static',
    emptyOutDir: true,
    // The output directory IS the packaged tree, so anything emitted here ships in the
    // wheel to every install. The map was 4.1 MB - 4.5x the bundle it described, and 82%
    // of the whole GUI asset payload - for a debugging aid a browser fetches only when
    // devtools is open. Turning it off here (rather than deleting the file) is what stops
    // the next build putting it straight back; it also drops the trailing
    // sourceMappingURL comment, so opening devtools does not 404 on a map that is gone.
    sourcemap: false,
  },
});
