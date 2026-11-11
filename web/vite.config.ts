import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';
import { fileURLToPath } from 'url';
import { dirname } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// The daemon serves web/dist at http://127.0.0.1:8765/ (see api/server.py
// `_frontend_response`), so the built app talks to its own origin by default.
export default defineConfig({
  resolve: { alias: { '@': resolve(__dirname, 'src') } },
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
