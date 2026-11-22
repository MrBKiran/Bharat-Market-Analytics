import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

// In development the browser talks to Vite (port 5173) and Vite forwards /api to the FastAPI server, so the page and the API
// share one origin: no CORS needed, and no server address is hard-coded into the code. Set VITE_API_TARGET (in the environment
// or a frontend/.env file) to point the dev server at a different API.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_");
  const apiTarget = env.VITE_API_TARGET || "http://127.0.0.1:8000";
  const proxy = { "/api": { target: apiTarget, changeOrigin: false } };
  return {
    plugins: [react()],
    server: { port: 5173, strictPort: true, proxy },
    preview: { port: 5173, proxy },
    build: { sourcemap: false, chunkSizeWarningLimit: 900 },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      css: false,
      restoreMocks: true,
    },
  };
});
