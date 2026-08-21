import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite(:5173) 代理 API/WS 到 Voice Studio 服务(:8100)
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8100",
      "/v1": "http://127.0.0.1:8100",
      "/ws": { target: "ws://127.0.0.1:8100", ws: true },
      "/health": "http://127.0.0.1:8100",
    },
  },
  build: { outDir: "dist" },
});
