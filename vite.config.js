import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/auth": "http://localhost:5050",
      "/projects": "http://localhost:5050",
      "/llm": "http://localhost:5050",
      "/mcp": "http://localhost:5050",
      "/download": "http://localhost:5050",
      "/status": "http://localhost:5050",
    },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-react": ["react", "react-dom"],
          "vendor-flow": ["reactflow", "dagre"],
          "vendor-sql": ["sql.js"],
          "vendor-motion": ["framer-motion"],
          "vendor-markdown": ["react-markdown"],
        },
      },
    },
  },
});