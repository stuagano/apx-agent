import path from "path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { TanStackRouterVite } from "@tanstack/router-plugin/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    TanStackRouterVite({ routesDirectory: "./routes", generatedRouteTree: "./types/routeTree.gen.ts" }),
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src/agent_hub/ui") },
  },
  root: "./src/agent_hub/ui",
  build: {
    outDir: path.resolve(__dirname, "./src/agent_hub/__dist__"),
    emptyOutDir: true,
  },
  define: {
    __APP_NAME__: JSON.stringify("Agent Hub"),
  },
  server: { port: 1420, strictPort: true },
});
