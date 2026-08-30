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
  // Baked in at build time so the running app can show which build the
  // browser actually has -- lets the user confirm a deploy landed instead
  // of guessing from Render's own deploy log. See the Settings dialog footer.
  define: {
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
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
