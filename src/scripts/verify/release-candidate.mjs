import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readdir, readFile, stat } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = resolve(here, "../..");
const projectRoot = resolve(srcRoot, "..");
const failures = [];

function relativePath(path) {
  return relative(srcRoot, path).split("\\").join("/");
}

function fail(message) {
  failures.push(message);
  console.error(`FAIL ${message}`);
}

function pass(message) {
  console.log(`PASS ${message}`);
}

function gitOutput(args) {
  return execFileSync("git", args, {
    cwd: projectRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
}

async function requireFile(relativeName) {
  const path = resolve(srcRoot, relativeName);
  try {
    const info = await stat(path);
    if (!info.isFile() || info.size === 0) {
      fail(`required non-empty file ${relativeName}`);
      return false;
    }
    pass(`required file ${relativeName}`);
    return true;
  } catch {
    fail(`required file ${relativeName}`);
    return false;
  }
}

async function hashFile(path) {
  const bytes = await readFile(path);
  return {
    bytes: bytes.length,
    sha256: createHash("sha256").update(bytes).digest("hex").toUpperCase(),
  };
}

async function collectFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await collectFiles(path)));
    else if (entry.isFile()) files.push(path);
  }
  return files.sort();
}

let head;
try {
  const root = resolve(gitOutput(["rev-parse", "--show-toplevel"]));
  if (root.toLowerCase() !== projectRoot.toLowerCase()) fail("Git root is not D:\\Foresightnine");
  head = gitOutput(["rev-parse", "--verify", "HEAD^{commit}"]);
  if (!/^[0-9a-f]{40}$/i.test(head)) fail("git HEAD is not a commit hash");
  else pass(`git HEAD ${head}`);
} catch {
  fail("git HEAD is unavailable");
}

const requiredFiles = [
  "docs/release/phase9-release-candidate.md",
  "docs/design/fidelity-qa.md",
  "docs/qa/api/phase9-mock-smoke.md",
  "docs/progress.md",
  "docs/qa/README.md",
  "infra/deployment/README.md",
  "apps/api/README.md",
  "apps/api/tests/test_phase3_providers.py",
  "apps/api/tests/test_phase4.py",
  "apps/web/qa/screenshots/web-01-thesis-current.jpg",
  "apps/web/qa/screenshots/web-02-challenge-current.jpg",
  "apps/web/qa/screenshots/web-03-worldline-current.jpg",
  "apps/web/qa/screenshots/web-06-nav-current.jpg",
  "apps/web/qa/screenshots/web-09-portfolio-desktop-fixed.png",
  "apps/web/qa/screenshots/web-11-fm-complete-fixed.png",
  "apps/web/qa/screenshots/web-12-methods-fixed.png",
  "apps/web/qa/screenshots/web-14-console-desktop.png",
  "apps/web/qa/screenshots/web-15-console-mobile.png",
  "docs/qa/web/phase8-public-default.png",
  "docs/qa/android/phase8-home.png",
  "docs/qa/android/phase8-home.xml",
  "docs/qa/android/phase8-provider-fail-closed.png",
  "docs/qa/android/phase8-public-default.png",
  "docs/qa/android/phase9-current-home.png",
  "docs/qa/android/phase9-current-home.xml",
  "docs/qa/android/phase9-metro-smoke.md",
];
for (const path of requiredFiles) await requireFile(path);

const webDist = resolve(srcRoot, "apps/web/dist");
try {
  const info = await stat(webDist);
  if (!info.isDirectory()) throw new Error("not a directory");
  const files = await collectFiles(webDist);
  if (files.length === 0) throw new Error("empty directory");
  const index = resolve(webDist, "index.html");
  if (!files.includes(index)) throw new Error("missing index.html");
  pass(`Web static QA artifact ${relativePath(webDist)} (${files.length} files)`);
  for (const path of files) {
    const digest = await hashFile(path);
    console.log(`ARTIFACT sha256=${digest.sha256} bytes=${digest.bytes} path=${relativePath(path)}`);
  }
} catch {
  fail("Web static QA artifact apps/web/dist is missing, empty, or lacks index.html");
}

const apk = resolve(srcRoot, "apps/mobile/android/app/build/outputs/apk/debug/app-debug.apk");
try {
  const info = await stat(apk);
  if (!info.isFile() || info.size === 0) throw new Error("missing or empty");
  pass(`Android debug QA artifact ${relativePath(apk)} (${info.size} bytes)`);
  const digest = await hashFile(apk);
  console.log(`ARTIFACT sha256=${digest.sha256} bytes=${digest.bytes} path=${relativePath(apk)}`);
} catch {
  fail("Android debug QA artifact app-debug.apk is missing or empty");
}

if (failures.length > 0) {
  console.error(`Release-candidate verification failed: ${failures.length} check(s).`);
  process.exitCode = 1;
} else {
  console.log("Release-candidate verification passed; QA artifacts are present and hashed.");
}
