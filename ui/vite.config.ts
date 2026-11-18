import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiPort = process.env.API_PORT ?? "8787";

export default defineConfig({
  plugins: [react()],
  root: "src/web",
  build: { outDir: "../../dist/web", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { "/api/": `http://127.0.0.1:${apiPort}` },
  },
});
