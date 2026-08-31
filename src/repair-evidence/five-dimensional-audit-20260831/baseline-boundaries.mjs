import { createHash } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const baselinePath = resolve(here, "../../docs/verification/governance-baseline.json");
const baseline = JSON.parse(await readFile(baselinePath, "utf8"));

let failed = false;
for (const entry of baseline.files) {
  try {
    const [info, bytes] = await Promise.all([stat(entry.path), readFile(entry.path)]);
    const digest = createHash("sha256").update(bytes).digest("hex");
    const valid = info.size === entry.size && digest === entry.sha256;
    console.log(`${valid ? "PASS" : "FAIL"} governance ${entry.path}`);
    failed ||= !valid;
  } catch (error) {
    console.error(`FAIL governance ${entry.path}: ${error.message}`);
    failed = true;
  }
}

const srcRoot = resolve(here, "../..");
if (!srcRoot.toLowerCase().endsWith(`${String.raw`\foresightnine\src`}`)) {
  console.error(`FAIL unexpected implementation root: ${srcRoot}`);
  failed = true;
} else {
  console.log(`PASS implementation root ${srcRoot}`);
}

if (failed) process.exit(1);
console.log("Governance boundary baseline is unchanged.");
