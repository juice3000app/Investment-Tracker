import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';

// Builds a plain static SPA -- output goes straight into live_app/static/
// so Flask can serve it directly (see server.py's spa() route). No SSR,
// no Next.js, no Cloudflare/D1 -- this dashboard talks to the Flask JSON
// API under /api/* instead.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:5000',
    },
  },
});
