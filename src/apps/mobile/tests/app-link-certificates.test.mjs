import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const root = new URL("..", import.meta.url).pathname;
const env = readFileSync(join(root, ".env.qa.local"), "utf8");
const certificates = readFileSync(join(root, "certificates", "CERTIFICATE_REPORT.md"), "utf8");

describe("QA App Link certificate inputs", () => {
  it("binds the staging callback to the mobile package and actual QA signer", () => {
    expect(env).toContain("EXPO_PUBLIC_FORESIGHTNINE_OAUTH_RETURN_URI=https://staging.foresightnine.com/oauth/mobile-callback");
    expect(env).toContain("EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_PACKAGE=com.foresightnine.mobile");
    expect(env).toContain("EXPO_PUBLIC_FORESIGHTNINE_OAUTH_APP_LINK_VERIFIED=true");
    expect(env).toContain("FA:C6:17:45:DC:09:03:78:6F:B9:ED:E6:2A:96:2B:39:9F:73:48:F0:BB:6F:89:9B:83:32:66:75:91:03:3B:9C");
  });

  it("keeps the public certificate inventory explicit", () => {
    expect(certificates).toContain("deployment_cert.der");
    expect(certificates).toContain("hybrid_classical_cert.der");
    expect(certificates).toContain("hybrid_pqc_cert.der");
    expect(certificates).toContain("No private key is present");
  });
});
