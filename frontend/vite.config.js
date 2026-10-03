import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The built output goes to frontend/dist, which FastAPI serves at / in
// production. In dev, /api is proxied to the backend so the app runs from a
// single origin either way.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // No manualChunks: App lazy-loads AnalyzeView, and that dynamic import is
    // what keeps Recharts (most of the bundle) off the live view's first
    // paint. A manual "charts" chunk defeated it by pulling shared modules in
    // and becoming a static dependency of the entry.
  },
});
