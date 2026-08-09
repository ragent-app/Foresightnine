import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../..");
const commands = [
  [process.execPath, ["packages/contracts/scripts/generate.mjs", "--check"]],
  [process.execPath, ["packages/asset-registry/scripts/generate-registry.mjs", "--check"]],
  ["uv", ["run", "--project", "apps/api", "python", "apps/api/scripts/export_openapi.py", "--check"]]
];

for (const [command, args] of commands) {
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit", shell: process.platform === "win32" });
  if (result.status !== 0) process.exit(result.status ?? 1);
}
console.log("PASS generated contracts, registry and OpenAPI are drift-free.");
