// Vite + vitest config for the PLATE canvas. Dev server is loopback-only on 5173 (see package.json scripts);
// the daemon on 4610 is a separate process for now.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
