import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const proxyTarget =
  process.env.PSKA_VITE_PROXY_TARGET || process.env.PSKA_VITE_API_TARGET || "http://127.0.0.1:8765";
const proxyPaths = [
  "/auth",
  "/login",
  "/logout",
  "/workspace",
  "/console",
  "/files",
  "/digest",
  "/review-items",
  "/knowledge-sources"
];

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: Object.fromEntries(
      proxyPaths.map((path) => [
        path,
        {
          target: proxyTarget,
          changeOrigin: true
        }
      ])
    )
  }
});
