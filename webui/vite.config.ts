import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.SJTUCLAW_API_URL ?? "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    build: {
      outDir: path.resolve(__dirname, "../web"),
      emptyOutDir: true,
      sourcemap: false,
      target: "es2020",
      cssMinify: "esbuild",
      rollupOptions: {
        output: {
          manualChunks: {
            "vendor-react": ["react", "react-dom"],
            "vendor-markdown": [
              "react-markdown",
              "remark-gfm",
              "remark-breaks",
              "remark-math",
              "rehype-katex",
              "katex",
            ],
            "vendor-syntax": ["react-syntax-highlighter"],
            "vendor-icons": ["lucide-react"],
          },
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        // Proxy all backend API paths used by the WebUI.  Without this the
        // Vite dev server serves index.html for unknown routes, causing JSON
        // parse errors and the "界面出错了" crash.
        "^/(sessions|chat|stop|command|workspace|admin|memories|cron|approvals|skills|downloads|qq|reflect|pet)(/.*)?$": {
          target,
          changeOrigin: true,
        },
        "/api": { target, changeOrigin: true },
      },
    },
  };
});
