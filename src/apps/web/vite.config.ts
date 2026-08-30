import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const stagingSecurityHeaders = {
  "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://api-staging.foresightnine.com; worker-src 'self'; object-src 'none'",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
};

export default defineConfig({
  plugins: [react()],
  build: { target: "es2022" },
  server: { host: "127.0.0.1" },
  // The staging preview is reached through the explicit Cloudflare hostname;
  // keep Vite's host allowlist closed to arbitrary forwarded Host headers.
  preview: {
    host: "127.0.0.1",
    allowedHosts: ["staging.foresightnine.com"],
    headers: stagingSecurityHeaders,
  },
});
