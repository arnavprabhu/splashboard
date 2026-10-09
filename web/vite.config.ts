/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import preact from '@preact/preset-vite';

declare const process: { env: Record<string, string | undefined> };

const MANAGER = process.env.SPLASH_GUI_MANAGER ?? 'http://127.0.0.1:8000';
const proxied = ['/api', '/v1', '/status', '/metrics', '/health', '/ready'];

export default defineConfig({
  base: '/admin/',
  plugins: [preact()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: MANAGER, changeOrigin: false }])),
  },
  preview: {
    port: 4173,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: MANAGER, changeOrigin: false }])),
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    assetsInlineLimit: 0,
    cssCodeSplit: true,
    sourcemap: false,
    manifest: true,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    setupFiles: ['tests/setup.ts'],
  },
});
