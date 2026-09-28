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
    rollupOptions: {
      output: {
        // Recharts is most of the bundle and only the Analyze view needs it;
        // splitting it keeps the first paint of the live view small.
        manualChunks: {
          charts: ["recharts"],
        },
      },
    },
  },
});
