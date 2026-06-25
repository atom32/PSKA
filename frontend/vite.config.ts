import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/workspace": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true
      },
      "/console": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true
      },
      "/files": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true
      },
      "/digest": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true
      },
      "/review-items": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true
      },
      "/knowledge-sources": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true
      }
    }
  }
});
