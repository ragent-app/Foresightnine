import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { target: "es2022" },
  server: { host: "127.0.0.1" },
  // The staging preview is reached through the explicit Cloudflare hostname;
  // keep Vite's host allowlist closed to arbitrary forwarded Host headers.
  preview: { host: "127.0.0.1", allowedHosts: ["staging.foresightnine.com"] },
});
