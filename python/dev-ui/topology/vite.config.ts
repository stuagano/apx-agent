import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// The build output ships in the apx-agent wheel and is served from
// /_apx/topology by FastAPI StaticFiles. The base path matches the
// route prefix so asset URLs resolve correctly when mounted there.

export default defineConfig({
  plugins: [react()],
  base: "/_apx/topology/",
  build: {
    outDir: path.resolve(__dirname, "../../src/apx_agent/_static/topology"),
    emptyOutDir: true,
    sourcemap: false,
    // Single chunk for simplicity — the bundle is small enough not to need
    // code-splitting, and one file keeps the wheel listing tidy.
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
  },
  server: {
    // Dev server proxies API calls to a locally running apx-agent app on :8000.
    // Run `apx dev` in another terminal, then `npm run dev` here.
    port: 5174,
    proxy: {
      "/_apx/topology.json": "http://localhost:8000",
      "/_apx/topology/inspect": "http://localhost:8000",
      "/_apx/topology/tracing": "http://localhost:8000",
      "/_apx/traces": "http://localhost:8000",
      "/_apx/discover": "http://localhost:8000",
      "/_apx/workspace-context": "http://localhost:8000",
      "/_apx/setup": "http://localhost:8000",
      "/responses": "http://localhost:8000",
    },
  },
});
