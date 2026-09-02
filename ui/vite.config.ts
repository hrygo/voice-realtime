import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite(:5173) 代理 API/WS 到 Sona 服务(:8100)
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const backendTarget = env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:8100";
  const webSocketTarget = backendTarget.replace(/^http/u, "ws");

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": backendTarget,
        "/v1": backendTarget,
        "/ws": { target: webSocketTarget, ws: true },
        "/health": backendTarget,
      },
    },
    build: { outDir: "dist" },
  };
});
