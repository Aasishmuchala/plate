// Vite + vitest config for the PLATE canvas. Dev server is loopback-only on 5173 (strictPort, so a busy port fails
// loudly instead of drifting); `/api` is proxied to the daemon on 4610. `changeOrigin` rewrites the Host header to
// 127.0.0.1:4610 so the daemon's Host allowlist passes; the browser's Origin (127.0.0.1:5173 or localhost:5173) is left
// alone and is accepted only when the daemon runs with PLATE_DEV=1.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:4610", changeOrigin: true },
    },
  },
  build: { outDir: "dist" },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
