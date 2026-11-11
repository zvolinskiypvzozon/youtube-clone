import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// В dev-режиме API и хранилище проксируются на запущенный docker-стек (порт 80).
const target = process.env.VITE_PROXY_TARGET ?? "http://localhost";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": target,
      "/yt-raw": target,
      "/yt-media": target,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    restoreMocks: true,
  },
});
