import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } } },
  test: { environment: "jsdom", globals: true, env: { NODE_ENV: "development" } },
});
