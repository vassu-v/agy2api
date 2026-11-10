import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Relative base path: assets in build artifacts resolve relative to page address,
  // supporting reverse proxy of the panel to any subpath (e.g. /AGY2API/).
  base: "./",
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/auth": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/creds": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/config": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/usage-logs": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
      "/logs": {
        target: "http://localhost:7861",
        changeOrigin: true,
        ws: true,
      },
      "/version": {
        target: "http://localhost:7861",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
  },
});
