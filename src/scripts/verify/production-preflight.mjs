import { accessSync, constants, readFileSync, statSync } from "node:fs";
import { isIP } from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const REPOSITORY_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");

export const REQUIRED_APPROVALS = Object.freeze([
  "legal",
  "privacy",
  "terms",
  "risk",
  "data_license",
  "security",
  "operations",
  "rollback",
  "android_signing",
  "artifact_provenance",
]);

const LIVE_LLM_PROVIDERS = Object.freeze({
  opencode: "FORESIGHTNINE_LLM_API_KEY",
  openai_compatible: "FORESIGHTNINE_LLM_API_KEY",
  glm_coding: "FORESIGHTNINE_GLM_CODING_API_KEY",
  zhipu_glm_coding: "FORESIGHTNINE_GLM_CODING_API_KEY",
});

const LIVE_SEARCH_PROVIDERS = Object.freeze({
  glm_coding_mcp: "FORESIGHTNINE_GLM_CODING_API_KEY",
  zhipu_glm_coding_mcp: "FORESIGHTNINE_GLM_CODING_API_KEY",
});

const DISABLED_CAPTCHA_DECLARATIONS = new Set([
  "",
  "accept",
  "unavailable",
  "reject",
  "mock",
  "debug",
  "qa",
  "none",
  "off",
  "false",
]);

function envValue(env, name) {
  return typeof env?.[name] === "string" ? env[name].trim() : "";
}

function hasValue(env, name) {
  return envValue(env, name).length > 0;
}

function gate(id, passed, passReason, failReason) {
  return Object.freeze({
    id,
    status: passed ? "PASS" : "FAIL",
    reason: passed ? passReason : failReason,
  });
}

function isServiceUrl(value, protocols) {
  if (!value || /\s/.test(value)) return false;
  try {
    const parsed = new URL(value);
    return protocols.has(parsed.protocol) && Boolean(parsed.hostname);
  } catch {
    return false;
  }
}

function hasHttpsCorsOrigins(value) {
  const origins = value.split(",").map((origin) => origin.trim()).filter(Boolean);
  if (origins.length === 0) return false;

  return origins.every((origin) => {
    try {
      const parsed = new URL(origin);
      return (
        parsed.protocol === "https:" &&
        Boolean(parsed.hostname) &&
        !parsed.username &&
        !parsed.password &&
        !parsed.search &&
        !parsed.hash &&
        (parsed.pathname === "" || parsed.pathname === "/") &&
        parsed.origin === origin.replace(/\/$/, "")
      );
    } catch {
      return false;
    }
  });
}

function hasTrustedProxyCidrs(value) {
  const cidrs = value.split(",").map((cidr) => cidr.trim()).filter(Boolean);
  if (cidrs.length === 0) return false;

  return cidrs.every((cidr) => {
    const [address, prefix, ...extra] = cidr.split("/");
    if (!address || !prefix || extra.length > 0) return false;
    const addressFamily = isIP(address);
    if (addressFamily === 0 || !/^\d+$/u.test(prefix)) return false;
    const prefixLength = Number(prefix);
    // A universal /0 network would make every peer eligible to supply a
    // trusted forwarded client address and defeats the abuse boundary.
    return prefixLength > 0 && prefixLength <= (addressFamily === 4 ? 32 : 128);
  });
}

function hasDeclaredProxyTopology(env) {
  const mode = envValue(env, "FORESIGHTNINE_PROXY_MODE").toLowerCase();
  const trustedProxyCidrs = envValue(env, "FORESIGHTNINE_TRUSTED_PROXY_CIDRS");
  if (mode === "direct") return trustedProxyCidrs === "";
  if (mode === "reverse_proxy") return hasTrustedProxyCidrs(trustedProxyCidrs);
  return false;
}

function isOutsideRepositoryFrom(candidatePath, repoRoot) {
  if (!path.isAbsolute(candidatePath)) return false;
  const relative = path.relative(repoRoot, candidatePath);
  return relative !== "" && (relative.startsWith(".." + path.sep) || path.isAbsolute(relative));
}

function manifestPathFromArgv(argv) {
  const flags = new Set(["--manifest", "--approval-manifest"]);
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    for (const flag of flags) {
      if (argument === flag) return argv[index + 1] ?? "";
      if (argument.startsWith(`${flag}=`)) return argument.slice(flag.length + 1);
    }
  }
  return "";
}

function loadApprovalManifest({ env, argv, repoRoot, approvalManifest, readManifest }) {
  if (approvalManifest !== undefined) return { manifest: approvalManifest };

  const requestedPath = manifestPathFromArgv(argv) || envValue(env, "FORESIGHTNINE_RELEASE_APPROVAL_MANIFEST");
  if (!requestedPath || !path.isAbsolute(requestedPath) || !isOutsideRepositoryFrom(requestedPath, repoRoot)) {
    return { error: "deployment-owned manifest is missing or not external" };
  }

  try {
    const raw = (readManifest ?? ((manifestPath) => readFileSync(manifestPath, "utf8")))(requestedPath);
    if (typeof raw === "string") return { manifest: JSON.parse(raw) };
    return { manifest: raw };
  } catch {
    return { error: "deployment-owned manifest is unreadable or invalid" };
  }
}

function readStaticFile({ repoRoot, relativePath, readFile }) {
  try {
    return (readFile ?? ((filePath) => readFileSync(filePath, "utf8")))(path.join(repoRoot, relativePath));
  } catch {
    return null;
  }
}

function stripYamlComment(line) {
  let quote = null;
  let escaped = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quote === "\"") {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === "\"") {
        quote = null;
      }
      continue;
    }
    if (quote === "'") {
      if (character === "'" && line[index + 1] === "'") {
        index += 1;
      } else if (character === "'") {
        quote = null;
      }
      continue;
    }
    if (character === "\"" || character === "'") {
      quote = character;
    } else if (character === "#" && (index === 0 || /\s/u.test(line[index - 1]))) {
      return line.slice(0, index);
    }
  }
  return line;
}

function hasDockerAssetRegistry({ repoRoot, readFile }) {
  const dockerfile = readStaticFile({ repoRoot, relativePath: "src/infra/docker/Dockerfile.api", readFile });
  return typeof dockerfile === "string" && /(^|\n)\s*COPY\s+packages\/asset-registry(?:\s|$)/m.test(dockerfile);
}

function hasValidComposeProviderConfig({ repoRoot, readFile }) {
  const compose = readStaticFile({ repoRoot, relativePath: "src/infra/docker/compose.yaml", readFile });
  if (typeof compose !== "string") return false;
  const effectiveLines = compose.split(/\r?\n/u).map(stripYamlComment).map((line) => line.trim()).filter(Boolean);
  const hasActualProvider = effectiveLines.some((line) => /^(?:-\s*)?FORESIGHTNINE_LLM_PROVIDER\s*(?::|=)/u.test(line));
  const hasLegacyProvider = effectiveLines.some((line) => /^(?:-\s*)?FORESIGHTNINE_PROVIDER\s*(?::|=)/u.test(line));
  return hasActualProvider && !hasLegacyProvider;
}

function isDebugOrQaArtifact(artifactPath) {
  const normalized = artifactPath.replaceAll("\\", "/").toLowerCase();
  const basename = path.posix.basename(normalized);
  return (
    basename.includes("debug") ||
    basename.includes("qa") ||
    /(?:^|\/)debug(?:\/|$)/.test(normalized) ||
    /(?:^|\/)qa(?:-only)?(?:\/|$)/.test(normalized)
  );
}

function defaultArtifactStat(artifactPath) {
  try {
    const stats = statSync(artifactPath);
    if (!stats.isFile()) return { exists: true, isFile: false, readable: false };
    accessSync(artifactPath, constants.R_OK);
    return { exists: true, isFile: true, readable: true };
  } catch {
    return { exists: false, isFile: false, readable: false };
  }
}

function hasReadableReleaseArtifact({ env, repoRoot, readArtifact, statArtifact }) {
  const artifactPath = envValue(env, "FORESIGHTNINE_ANDROID_RELEASE_ARTIFACT");
  if (!artifactPath || !path.isAbsolute(artifactPath) || !isOutsideRepositoryFrom(artifactPath, repoRoot)) return false;
  if (!/\.(?:apk|aab)$/i.test(artifactPath) || isDebugOrQaArtifact(artifactPath)) return false;

  try {
    if (statArtifact) {
      const stat = statArtifact(artifactPath);
      const isFile = typeof stat?.isFile === "function" ? stat.isFile() : stat?.isFile !== false;
      return Boolean(stat) && stat.exists !== false && isFile && stat.readable !== false;
    }
    if (readArtifact) {
      readArtifact(artifactPath);
      return true;
    }
    const stat = defaultArtifactStat(artifactPath);
    return stat.exists && stat.isFile && stat.readable;
  } catch {
    return false;
  }
}

function validApprovalManifest(manifest) {
  return Boolean(
    manifest &&
    typeof manifest === "object" &&
    !Array.isArray(manifest) &&
    REQUIRED_APPROVALS.every((approval) => manifest[approval] === true),
  );
}

function evaluateLlmProvider(env) {
  const provider = envValue(env, "FORESIGHTNINE_LLM_PROVIDER").toLowerCase();
  return {
    provider,
    known: Object.hasOwn(LIVE_LLM_PROVIDERS, provider),
  };
}

function evaluateSearchProvider(env) {
  const provider = envValue(env, "FORESIGHTNINE_SEARCH_PROVIDER").toLowerCase();
  return {
    provider,
    known: Object.hasOwn(LIVE_SEARCH_PROVIDERS, provider),
  };
}

export function runPreflight({
  argv = process.argv.slice(2),
  env = process.env,
  repoRoot = REPOSITORY_ROOT,
  readFile,
  readArtifact,
  statArtifact,
  approvalManifest,
  readManifest,
} = {}) {
  const llm = evaluateLlmProvider(env);
  const search = evaluateSearchProvider(env);
  const llmSecretName = LIVE_LLM_PROVIDERS[llm.provider];
  const searchSecretName = LIVE_SEARCH_PROVIDERS[search.provider];
  const captchaDeclaration = envValue(env, "FORESIGHTNINE_CAPTCHA_PROVIDER") || envValue(env, "FORESIGHTNINE_CAPTCHA_MODE").toLowerCase();
  const loadedManifest = loadApprovalManifest({ env, argv, repoRoot, approvalManifest, readManifest });

  const gates = [
    gate(
      "ENV_PRODUCTION",
      envValue(env, "FORESIGHTNINE_ENV").toLowerCase() === "production",
      "production environment declared",
      "FORESIGHTNINE_ENV must be production",
    ),
    gate(
      "LLM_PROVIDER",
      llm.known,
      "supported live LLM provider declared",
      "LLM provider must be an explicitly supported live provider",
    ),
    gate(
      "LLM_SECRET",
      Boolean(llmSecretName) && hasValue(env, llmSecretName),
      "LLM provider credential is present",
      "LLM provider credential is missing",
    ),
    gate(
      "SEARCH_PROVIDER",
      search.known,
      "supported external search provider declared",
      "search provider must be an explicitly supported external provider",
    ),
    gate(
      "SEARCH_SECRET",
      Boolean(searchSecretName) && hasValue(env, searchSecretName),
      "search provider credential is present",
      "search provider credential is missing",
    ),
    gate(
      "POSTGRES_URL",
      isServiceUrl(envValue(env, "FORESIGHTNINE_POSTGRES_URL") || envValue(env, "DATABASE_URL"), new Set(["postgres:", "postgresql:"])),
      "Postgres URL is configured",
      "a valid Postgres URL is required",
    ),
    gate(
      "STORAGE_MODE",
      envValue(env, "FORESIGHTNINE_STORAGE_MODE").toLowerCase() === "postgres",
      "Postgres storage mode is explicitly selected",
      "FORESIGHTNINE_STORAGE_MODE must be postgres",
    ),
    gate(
      "REDIS_URL",
      isServiceUrl(envValue(env, "FORESIGHTNINE_REDIS_URL") || envValue(env, "REDIS_URL"), new Set(["rediss:"])),
      "TLS-protected Redis URL is configured",
      "a TLS-protected rediss:// Redis URL is required",
    ),
    gate(
      "ABUSE_MODE",
      envValue(env, "FORESIGHTNINE_ABUSE_MODE").toLowerCase() === "redis",
      "Redis abuse mode is explicitly selected",
      "FORESIGHTNINE_ABUSE_MODE must be redis",
    ),
    gate(
      "CAPTCHA_EXTERNAL",
      !DISABLED_CAPTCHA_DECLARATIONS.has(captchaDeclaration.toLowerCase()),
      "external CAPTCHA provider declared",
      "CAPTCHA must declare an external provider, not accept/unavailable/mock/debug/QA",
    ),
    gate(
      "CAPTCHA_SECRET",
      hasValue(env, "FORESIGHTNINE_CAPTCHA_SECRET"),
      "CAPTCHA provider credential is present",
      "CAPTCHA provider credential is missing",
    ),
    gate(
      "CORS_HTTPS",
      hasHttpsCorsOrigins(envValue(env, "FORESIGHTNINE_CORS_ORIGINS")),
      "all CORS origins are HTTPS origins",
      "CORS origins must be non-empty HTTPS origins without query or path data",
    ),
    gate(
      "PROXY_TOPOLOGY",
      hasDeclaredProxyTopology(env),
      "proxy topology is explicit and trusted proxy CIDRs match the deployment edge",
      "FORESIGHTNINE_PROXY_MODE must be direct with no trusted CIDRs, or reverse_proxy with valid FORESIGHTNINE_TRUSTED_PROXY_CIDRS",
    ),
    gate(
      "DOCKER_ASSET_REGISTRY",
      hasDockerAssetRegistry({ repoRoot, readFile }),
      "API Dockerfile copies packages/asset-registry",
      "API Dockerfile must copy packages/asset-registry",
    ),
    gate(
      "COMPOSE_PROVIDER_CONFIG",
      hasValidComposeProviderConfig({ repoRoot, readFile }),
      "Compose uses the actual LLM provider configuration variable",
      "Compose must use FORESIGHTNINE_LLM_PROVIDER and omit FORESIGHTNINE_PROVIDER",
    ),
    gate(
      "ANDROID_RELEASE_ARTIFACT",
      hasReadableReleaseArtifact({ env, repoRoot, readArtifact, statArtifact }),
      "external Android release artifact is readable",
      "a readable external release APK/AAB is required; debug and QA artifacts are rejected",
    ),
    gate(
      "RELEASE_APPROVAL_MANIFEST",
      !loadedManifest.error && validApprovalManifest(loadedManifest.manifest),
      "deployment-owned release approval manifest is complete",
      loadedManifest.error ?? "all required release approval flags must be true",
    ),
  ];

  return Object.freeze({
    ok: gates.every(({ status }) => status === "PASS"),
    gates: Object.freeze(gates),
  });
}

export function formatReport(result) {
  const lines = result.gates.map(({ id, status, reason }) => `${id} ${status} ${reason}`);
  lines.push(`PRODUCTION_PREFLIGHT ${result.ok ? "PASS" : "FAIL"} ${result.ok ? "all gates passed" : "one or more gates failed"}`);
  return lines.join("\n");
}

function main() {
  try {
    const result = runPreflight();
    process.stdout.write(`${formatReport(result)}\n`);
    if (!result.ok) process.exitCode = 1;
  } catch {
    process.stdout.write("PRODUCTION_PREFLIGHT FAIL verifier failure\n");
    process.exitCode = 1;
  }
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) main();
