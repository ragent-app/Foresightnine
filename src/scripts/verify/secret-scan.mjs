import { readdir, readFile } from "node:fs/promises";
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
    const text = info.toString("utf8");
    for (const pattern of patterns) {
      pattern.value.lastIndex = 0;
      if (pattern.value.test(text)) findings.push({ path, pattern: pattern.name });
    }
  }
}

await walk(projectRoot);
if (findings.length > 0) {
  for (const finding of findings) console.error(`FAIL ${finding.pattern}: ${finding.path}`);
  process.exit(1);
}
console.log(`PASS secret scan ${projectRoot}`);
