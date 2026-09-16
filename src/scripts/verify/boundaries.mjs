import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { readFile, stat } from "node:fs/promises";
import { basename, dirname, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const srcRoot = resolve(here, "../..");
const projectRoot = resolve(srcRoot, "..");
const baselinePath = resolve(here, "../../docs/verification/governance-baseline.json");
const baseline = JSON.parse(await readFile(baselinePath, "utf8"));

/** Resolve paths captured on the original Windows checkout without changing the manifest. */
export function resolveBaselinePath(recordedPath, root = projectRoot) {
  const isWindowsAbsolute = /^[A-Za-z]:[\\\\/]/u.test(recordedPath);
  if (!isAbsolute(recordedPath) && !isWindowsAbsolute) return resolve(root, recordedPath);
  if (process.platform === "win32") return recordedPath;

  const projectName = basename(root);
  const normalizedPath = recordedPath.replaceAll("\\", "/");
  const windowsPath = /^[A-Za-z]:\/([^/]+)\/(.+)$/u.exec(normalizedPath);
  if (windowsPath && windowsPath[1].toLowerCase() === projectName.toLowerCase()) {
    return resolve(root, windowsPath[2]);
  }
  return recordedPath;
}

let failed = false;
for (const entry of baseline.files) {
  const resolvedPath = resolveBaselinePath(entry.path);
  try {
    const [info, bytes] = await Promise.all([stat(resolvedPath), readFile(resolvedPath)]);
    const digest = createHash("sha256").update(bytes).digest("hex");
    const valid = info.size === entry.size && digest === entry.sha256;
    console.log(`${valid ? "PASS" : "FAIL"} governance ${entry.path} -> ${resolvedPath}`);
    failed ||= !valid;
  } catch (error) {
    console.error(`FAIL governance ${entry.path} -> ${resolvedPath}: ${error.message}`);
    failed = true;
  }
}

let gitRoot = "";
try {
  gitRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], {
    cwd: projectRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  }).trim();
} catch {
  // The root check below reports the failure without exposing a platform-specific stack trace.
}

if (resolve(gitRoot) !== projectRoot || resolve(srcRoot) !== resolve(projectRoot, "src")) {
  console.error(`FAIL unexpected implementation root: ${srcRoot} (git root: ${gitRoot || "unavailable"})`);
  failed = true;
} else {
  console.log(`PASS implementation root ${srcRoot}`);
}

if (failed) process.exit(1);
console.log("Governance boundary baseline is unchanged.");
