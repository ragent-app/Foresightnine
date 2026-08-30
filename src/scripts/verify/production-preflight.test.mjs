import assert from "node:assert/strict";
import test from "node:test";

import { formatReport, REQUIRED_APPROVALS, runPreflight } from "./production-preflight.mjs";

const APPROVAL_MANIFEST = Object.freeze(Object.fromEntries(REQUIRED_APPROVALS.map((key) => [key, true])));

function baseEnv() {
  return {
    FORESIGHTNINE_ENV: "production",
    FORESIGHTNINE_LLM_PROVIDER: "glm_coding",
    FORESIGHTNINE_GLM_CODING_API_KEY: "dummy-value-not-shaped-like-a-real-secret",
    FORESIGHTNINE_SEARCH_PROVIDER: "glm_coding_mcp",
    FORESIGHTNINE_POSTGRES_URL: "postgresql://db.example.test/foresightnine",
    FORESIGHTNINE_STORAGE_MODE: "postgres",
    FORESIGHTNINE_REDIS_URL: "rediss://redis.example.test",
    FORESIGHTNINE_ABUSE_MODE: "redis",
    FORESIGHTNINE_CAPTCHA_PROVIDER: "turnstile",
    FORESIGHTNINE_CAPTCHA_SECRET: "dummy-captcha-value-not-shaped-like-a-real-secret",
    FORESIGHTNINE_CORS_ORIGINS: "https://app.example.test,https://admin.example.test",
    FORESIGHTNINE_PROXY_MODE: "reverse_proxy",
    FORESIGHTNINE_TRUSTED_PROXY_CIDRS: "10.0.0.0/8",
    FORESIGHTNINE_ANDROID_RELEASE_ARTIFACT: "/deployment-owned/app-release.apk",
  };
}

function resultWithManifest(overrides = {}, options = {}) {
  return runPreflight({
    env: { ...baseEnv(), ...overrides },
    approvalManifest: APPROVAL_MANIFEST,
    ...options,
  });
}

test("synthetic production preflight passes through argv/env injection without network access", () => {
  let injectedPath = "";
  let injectedArtifactPath = "";
  const result = runPreflight({
    env: baseEnv(),
    repoRoot: "/synthetic-repository",
    argv: ["--approval-manifest", "/deployment-owned/release-approval.json"],
    readFile: (filePath) => {
      if (filePath.endsWith("Dockerfile.api")) return "COPY packages/asset-registry /app/packages/asset-registry";
      return "FORESIGHTNINE_LLM_PROVIDER: mock";
    },
    readManifest: (manifestPath) => {
      injectedPath = manifestPath;
      return APPROVAL_MANIFEST;
    },
    statArtifact: (artifactPath) => {
      injectedArtifactPath = artifactPath;
      return { exists: true, isFile: true, readable: true };
    },
  });

  assert.equal(result.ok, true);
  assert.match(injectedPath, /release-approval\.json$/);
  assert.match(injectedArtifactPath, /app-release\.apk$/);
  assert.ok(result.gates.every(({ status }) => status === "PASS"));
});

test("default empty environment fails closed and lists missing gates", () => {
  const result = runPreflight({ env: {}, argv: [] });
  const failedIds = result.gates.filter(({ status }) => status === "FAIL").map(({ id }) => id);

  assert.equal(result.ok, false);
  assert.ok(failedIds.includes("ENV_PRODUCTION"));
  assert.ok(failedIds.includes("LLM_PROVIDER"));
  assert.ok(failedIds.includes("RELEASE_APPROVAL_MANIFEST"));
});

test("mock and unknown LLM providers are rejected", () => {
  for (const provider of ["mock", "unknown-provider"]) {
    const result = resultWithManifest({ FORESIGHTNINE_LLM_PROVIDER: provider });
    assert.equal(result.ok, false);
    assert.equal(result.gates.find(({ id }) => id === "LLM_PROVIDER").status, "FAIL");
  }
});

test("memory or absent production storage and abuse modes are rejected", () => {
  for (const [name, values] of [
    ["STORAGE_MODE", { FORESIGHTNINE_STORAGE_MODE: "memory" }],
    ["STORAGE_MODE", { FORESIGHTNINE_STORAGE_MODE: "" }],
    ["ABUSE_MODE", { FORESIGHTNINE_ABUSE_MODE: "memory" }],
    ["ABUSE_MODE", { FORESIGHTNINE_ABUSE_MODE: "" }],
  ]) {
    const result = resultWithManifest(values);
    assert.equal(result.ok, false);
    assert.equal(result.gates.find(({ id }) => id === name).status, "FAIL");
  }
});

test("Dockerfile and Compose static gates reject missing or legacy configuration", () => {
  const repoRoot = "C:\\synthetic-repository";
  const validDocker = "COPY packages/asset-registry /app/packages/asset-registry";
  const validCompose = "FORESIGHTNINE_LLM_PROVIDER: mock";
  const validEnv = baseEnv();
  const injected = (dockerfile, compose) => runPreflight({
    env: validEnv,
    repoRoot,
    approvalManifest: APPROVAL_MANIFEST,
    statArtifact: () => ({ exists: true, isFile: true, readable: true }),
    readFile: (filePath) => filePath.endsWith("Dockerfile.api") ? dockerfile : compose,
  });

  const missingCopy = injected("FROM base", validCompose);
  assert.equal(missingCopy.gates.find(({ id }) => id === "DOCKER_ASSET_REGISTRY").status, "FAIL");

  const legacyCompose = injected(validDocker, "FORESIGHTNINE_PROVIDER: mock");
  assert.equal(legacyCompose.gates.find(({ id }) => id === "COMPOSE_PROVIDER_CONFIG").status, "FAIL");

  for (const commentedCompose of [
    "# FORESIGHTNINE_LLM_PROVIDER: mock",
    "# FORESIGHTNINE_PROVIDER: mock",
  ]) {
    const commented = injected(validDocker, commentedCompose);
    assert.equal(commented.gates.find(({ id }) => id === "COMPOSE_PROVIDER_CONFIG").status, "FAIL");
  }

  const inlineComment = injected(validDocker, "FORESIGHTNINE_LLM_PROVIDER: mock # FORESIGHTNINE_PROVIDER: mock");
  assert.equal(inlineComment.gates.find(({ id }) => id === "COMPOSE_PROVIDER_CONFIG").status, "PASS");

  const actual = resultWithManifest({}, { statArtifact: () => ({ exists: true, isFile: true, readable: true }) });
  assert.equal(actual.gates.find(({ id }) => id === "DOCKER_ASSET_REGISTRY").status, "PASS");
  assert.equal(actual.gates.find(({ id }) => id === "COMPOSE_PROVIDER_CONFIG").status, "PASS");
});

test("missing and debug/QA Android artifacts are rejected", () => {
  const missing = resultWithManifest({ FORESIGHTNINE_ANDROID_RELEASE_ARTIFACT: "" });
  assert.equal(missing.gates.find(({ id }) => id === "ANDROID_RELEASE_ARTIFACT").status, "FAIL");

  for (const artifact of [
    "C:\\deployment-owned\\app-debug.apk",
    "C:\\deployment-owned\\qa-only\\app-release.apk",
  ]) {
    const result = resultWithManifest({ FORESIGHTNINE_ANDROID_RELEASE_ARTIFACT: artifact }, {
      statArtifact: () => ({ exists: true, isFile: true, readable: true }),
    });
    assert.equal(result.gates.find(({ id }) => id === "ANDROID_RELEASE_ARTIFACT").status, "FAIL");
  }
});

test("missing or incomplete approval manifest is rejected", () => {
  const missing = runPreflight({ env: baseEnv(), argv: [] });
  assert.equal(missing.gates.find(({ id }) => id === "RELEASE_APPROVAL_MANIFEST").status, "FAIL");

  const incomplete = resultWithManifest({}, { approvalManifest: { ...APPROVAL_MANIFEST, security: false } });
  assert.equal(incomplete.gates.find(({ id }) => id === "RELEASE_APPROVAL_MANIFEST").status, "FAIL");
});

test("plaintext Redis, HTTP, query-bearing, and empty CORS declarations are rejected", () => {
  const plaintextRedis = resultWithManifest({ FORESIGHTNINE_REDIS_URL: "redis://redis.example.test" });
  assert.equal(plaintextRedis.gates.find(({ id }) => id === "REDIS_URL").status, "FAIL");

  for (const cors of ["http://app.example.test", "https://app.example.test/?query=bad", ""]) {
    const result = resultWithManifest({ FORESIGHTNINE_CORS_ORIGINS: cors });
    assert.equal(result.gates.find(({ id }) => id === "CORS_HTTPS").status, "FAIL");
  }
});

test("accept and unavailable CAPTCHA declarations are rejected", () => {
  for (const declaration of ["accept", "unavailable"]) {
    const result = resultWithManifest({
      FORESIGHTNINE_CAPTCHA_PROVIDER: "",
      FORESIGHTNINE_CAPTCHA_MODE: declaration,
    });
    assert.equal(result.gates.find(({ id }) => id === "CAPTCHA_EXTERNAL").status, "FAIL");
  }
});

test("proxy topology is explicit and trusted CIDRs are validated", () => {
  const missing = resultWithManifest({ FORESIGHTNINE_PROXY_MODE: "" });
  assert.equal(missing.gates.find(({ id }) => id === "PROXY_TOPOLOGY").status, "FAIL");

  const invalidCidrs = resultWithManifest({ FORESIGHTNINE_TRUSTED_PROXY_CIDRS: "not-a-cidr" });
  assert.equal(invalidCidrs.gates.find(({ id }) => id === "PROXY_TOPOLOGY").status, "FAIL");

  for (const universalCidr of ["0.0.0.0/0", "::/0"]) {
    const universal = resultWithManifest({ FORESIGHTNINE_TRUSTED_PROXY_CIDRS: universalCidr });
    assert.equal(universal.gates.find(({ id }) => id === "PROXY_TOPOLOGY").status, "FAIL");
  }

  const direct = resultWithManifest({
    FORESIGHTNINE_PROXY_MODE: "direct",
    FORESIGHTNINE_TRUSTED_PROXY_CIDRS: "",
  });
  assert.equal(direct.gates.find(({ id }) => id === "PROXY_TOPOLOGY").status, "PASS");

  const directWithForwarders = resultWithManifest({
    FORESIGHTNINE_PROXY_MODE: "direct",
    FORESIGHTNINE_TRUSTED_PROXY_CIDRS: "127.0.0.1/32",
  });
  assert.equal(directWithForwarders.gates.find(({ id }) => id === "PROXY_TOPOLOGY").status, "FAIL");
});

test("formatted output contains no injected secret or URL query values", () => {
  const secretMarker = "dummy-value-marker-must-never-print";
  const queryMarker = "query-marker-must-never-print";
  const result = resultWithManifest({
    FORESIGHTNINE_GLM_CODING_API_KEY: secretMarker,
    FORESIGHTNINE_CORS_ORIGINS: `https://app.example.test/?token=${queryMarker}`,
    FORESIGHTNINE_POSTGRES_URL: `postgresql://db.example.test/app?password=${queryMarker}`,
  });
  const report = formatReport(result);

  assert.equal(report.includes(secretMarker), false);
  assert.equal(report.includes(queryMarker), false);
  assert.match(report, /^(?:[A-Z0-9_]+) (?:PASS|FAIL) .+$/m);
});
