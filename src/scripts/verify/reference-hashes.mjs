import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const manifestPath = resolve(here, "../../docs/verification/reference-sources.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));

let failed = false;
for (const entry of manifest.files) {
  try {
    const [info, bytes] = await Promise.all([stat(entry.path), readFile(entry.path)]);
    const digest = createHash("sha256").update(bytes).digest("hex");
    const valid = info.size === entry.size && digest === entry.sha256;
    console.log(`${valid ? "PASS" : "FAIL"} ${entry.path}`);
    if (!valid) {
      console.error(`  expected size/hash ${entry.size}/${entry.sha256}`);
      console.error(`  observed size/hash ${info.size}/${digest}`);
      failed = true;
    }
  } catch (error) {
    console.error(`FAIL ${entry.path}: ${error.message}`);
    failed = true;
  }
}

if (failed) process.exit(1);
console.log(`Verified ${manifest.files.length} immutable FM references.`);
