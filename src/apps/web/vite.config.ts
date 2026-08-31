import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const ASSET_LINKS_PATH = ".well-known/assetlinks.json";
const ANDROID_PACKAGE = "com.foresightnine.mobile";
const SHA256_CERTIFICATE_PATTERN = /^(?:[0-9a-f]{2}:){31}[0-9a-f]{2}$/i;

function assetLinksPlugin() {
  const production = process.env.FORESIGHTNINE_ENV?.trim().toLowerCase() === "production";
  const packageName = process.env.VITE_FORESIGHTNINE_ANDROID_APP_LINK_PACKAGE?.trim() || ANDROID_PACKAGE;
  const fingerprint = process.env.VITE_FORESIGHTNINE_ANDROID_APP_LINK_SHA256_CERT_FINGERPRINT?.trim() || "";

  if (production && (packageName !== ANDROID_PACKAGE || !SHA256_CERTIFICATE_PATTERN.test(fingerprint))) {
    throw new Error("Production Web build requires the actual Android release App Link package and SHA-256 certificate fingerprint.");
  }

  const statements = fingerprint && SHA256_CERTIFICATE_PATTERN.test(fingerprint)
    ? [{
        relation: ["delegate_permission/common.handle_all_urls"],
        target: {
          namespace: "android_app",
          package_name: packageName,
          sha256_cert_fingerprints: [fingerprint.toUpperCase()],
        },
      }]
    : [];

  return {
    name: "foresightnine-asset-links",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: ASSET_LINKS_PATH,
        source: `${JSON.stringify(statements, null, 2)}\n`,
      });
    },
  };
}

const stagingSecurityHeaders = {
  "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' https://api-staging.foresightnine.com; worker-src 'self'; object-src 'none'",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
};

export default defineConfig({
  plugins: [react(), assetLinksPlugin()],
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
