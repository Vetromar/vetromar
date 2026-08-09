import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Static SPA. In production the built `dist/` is bundled into the Tauri app;
// the API base URL is injected at runtime by the shell (window.__VETROMAR_API__).
export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: { port: 5173, strictPort: true },
  build: { target: "esnext", outDir: "dist", emptyOutDir: true },
});
