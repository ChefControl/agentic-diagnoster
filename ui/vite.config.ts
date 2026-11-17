import { fileURLToPath } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiPort = process.env.API_PORT ?? "8787";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  root: "src/web",
  // "@/..." points at src/web, the layout shadcn and assistant-ui components expect.
  resolve: { alias: { "@": fileURLToPath(new URL("./src/web", import.meta.url)) } },
  build: { outDir: "../../dist/web", emptyOutDir: true },
  server: {
    port: 5173,
    proxy: { "/api/": `http://127.0.0.1:${apiPort}` },
  },
});
