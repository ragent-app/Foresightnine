import { readdir, readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { dirname, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(here, "../../..");
const skippedDirectories = new Set([".git", ".venv", "node_modules", "dist", ".pytest_cache", "__pycache__"]);
const binaryExtensions = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".pyc", ".zip", ".gz"]);
const patterns = [
  { name: "provider-style secret", value: /\bsk-[A-Za-z0-9_-]{20,}\b/g },
  { name: "private key block", value: /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g },
  { name: "credential assignment", value: /\b(?:api[_-]?key|secret|token|password)\s*[:=]\s*["'][^"'\r\n]{12,}["']/gi },
];

const findings = [];

function inspectText(text, location) {
  for (const pattern of patterns) {
    pattern.value.lastIndex = 0;
    if (pattern.value.test(text)) findings.push({ path: location, pattern: pattern.name });
  }
}

async function walk(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isDirectory() && skippedDirectories.has(entry.name)) continue;
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      await walk(path);
      continue;
    }
    if (!entry.isFile() || binaryExtensions.has(extname(entry.name).toLowerCase())) continue;
    const info = await readFile(path);
    if (info.includes(0)) continue;
    inspectText(info.toString("utf8"), path);
  }
}

await walk(projectRoot);

function inspectGitHistory() {
  let commits;
  try {
    commits = execFileSync("git", ["rev-list", "--all"], {
      cwd: projectRoot,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim().split(/\r?\n/).filter(Boolean);
  } catch {
    console.log("INFO Git history scan skipped: repository metadata unavailable");
    return;
  }

  const inspectedBlobs = new Set();
  for (const commit of commits) {
    const paths = execFileSync("git", ["ls-tree", "-r", "--name-only", "-z", commit], {
      cwd: projectRoot,
      encoding: "buffer",
      maxBuffer: 64 * 1024 * 1024,
    }).toString("utf8").split("\0").filter(Boolean);
    for (const path of paths) {
      if (binaryExtensions.has(extname(path).toLowerCase())) continue;
      const objectId = execFileSync("git", ["rev-parse", `${commit}:${path}`], {
        cwd: projectRoot,
        encoding: "utf8",
      }).trim();
      if (inspectedBlobs.has(objectId)) continue;
      inspectedBlobs.add(objectId);
      const bytes = execFileSync("git", ["cat-file", "blob", objectId], {
        cwd: projectRoot,
        encoding: "buffer",
        maxBuffer: 64 * 1024 * 1024,
      });
      if (bytes.includes(0)) continue;
      inspectText(bytes.toString("utf8"), `git:${objectId}:${path}`);
    }
  }
  console.log(`PASS inspected ${inspectedBlobs.size} unique Git-history blobs`);
}

inspectGitHistory();
if (findings.length > 0) {
  for (const finding of findings) console.error(`FAIL ${finding.pattern}: ${finding.path}`);
  process.exit(1);
}
console.log(`PASS secret scan ${projectRoot}`);
