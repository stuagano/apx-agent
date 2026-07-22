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
    alias: { "@": path.resolve(__dirname, "./client") },
  },
  root: "./client",
  build: {
    outDir: path.resolve(__dirname, "./__dist__"),
    emptyOutDir: true,
  },
  server: { port: 1420, strictPort: true },
});
