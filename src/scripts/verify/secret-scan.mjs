import { readdir, readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { dirname, extname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const projectRoot = resolve(here, "../../..");
// Dependency stores and Android/Gradle outputs are generated and may embed source
// maps. Tracked source and the complete Git history are still scanned below.
const skippedDirectories = new Set([".git", ".venv", "node_modules", "nm", "dist", "build", ".gradle", ".cxx", ".pytest_cache", "__pycache__", "repair-evidence"]);
// This is a deployment-owned, ignored secret file. Its contents are mounted
// at runtime and must never enter the repository or this scan's output.
const skippedFiles = new Set(["src/infra/docker/staging.env"]);
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
    if (entry.isDirectory() && (skippedDirectories.has(entry.name) || entry.name.startsWith("node_modules."))) continue;
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      await walk(path);
      continue;
    }
    if (skippedFiles.has(relative(projectRoot, path))) continue;
    if (!entry.isFile() || binaryExtensions.has(extname(entry.name).toLowerCase())) continue;
    const info = await readFile(path);
    if (info.includes(0)) continue;
    inspectText(info.toString("utf8"), path);
  }
}

await walk(projectRoot);

function inspectGitHistory() {
  let listing;
  try {
    listing = execFileSync("git", ["rev-list", "--objects", "--all"], {
      cwd: projectRoot,
      encoding: "buffer",
      maxBuffer: 512 * 1024 * 1024,
    });
  } catch {
    console.error("FAIL Git history scan unavailable: repository metadata could not be read");
    return false;
  }

  const objectIds = [...new Set(listing.toString("utf8")
    .split(/\r?\n/)
    .map((line) => line.trim().split(/\s+/, 1)[0])
    .filter((objectId) => /^[0-9a-f]{40}$/.test(objectId)))];
  if (objectIds.length === 0) {
    console.error("FAIL Git history scan returned no objects");
    return false;
  }

  let batch;
  try {
    batch = execFileSync("git", ["cat-file", "--batch"], {
      cwd: projectRoot,
      input: Buffer.from(`${objectIds.join("\n")}\n`, "utf8"),
      encoding: "buffer",
      maxBuffer: 512 * 1024 * 1024,
    });
  } catch {
    console.error("FAIL Git history scan could not read the batch blob stream");
    return false;
  }

  let offset = 0;
  let inspectedBlobs = 0;
  let parseError = false;
  for (const objectId of objectIds) {
    const headerEnd = batch.indexOf(0x0a, offset);
    if (headerEnd < 0) {
      parseError = true;
      break;
    }
    const header = batch.toString("utf8", offset, headerEnd).split(" ");
    offset = headerEnd + 1;
    const type = header[1];
    const size = Number(header[2]);
    if (header[0] !== objectId || type === "missing" || !Number.isSafeInteger(size) || size < 0 || offset + size >= batch.length) {
      parseError = true;
      break;
    }
    const bytes = batch.subarray(offset, offset + size);
    if (batch[offset + size] !== 0x0a) {
      parseError = true;
      break;
    }
    offset += size + 1;
    if (type !== "blob") continue;
    inspectedBlobs += 1;
    if (bytes.includes(0)) continue;
    inspectText(bytes.toString("utf8"), `git:${objectId}`);
  }
  if (parseError || inspectedBlobs === 0) {
    console.error(`FAIL Git history scan could not fully parse ${objectIds.length} requested objects`);
    return false;
  }
  console.log(`PASS inspected ${inspectedBlobs} unique Git-history blobs`);
  return true;
}

const historyScanPassed = inspectGitHistory();
if (findings.length > 0) {
  for (const finding of findings) console.error(`FAIL ${finding.pattern}: ${finding.path}`);
  process.exit(1);
}
if (!historyScanPassed) process.exit(1);
console.log(`PASS secret scan ${projectRoot}`);
