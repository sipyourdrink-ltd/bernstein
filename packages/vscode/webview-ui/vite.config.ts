import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../dist/webview',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Single JS file — no code-splitting needed for a VS Code webview
        inlineDynamicImports: true,
        entryFileNames: 'main.js',
        assetFileNames: '[name][extname]',
      },
    },
  },
});
