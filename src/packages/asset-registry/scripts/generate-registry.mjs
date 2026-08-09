import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const sourcePath = resolve(here, "../registry/v1/assets.source.json");
const outputPath = resolve(here, "../registry/v1/assets.json");
const source = JSON.parse(await readFile(sourcePath, "utf8"));
const createdAt = "2026-08-10T00:00:00Z";
const sourceRefs = [{ ref_id: "registry:mock-v1", source_type: "registry", label: "Deterministic development registry" }];
const license = { license_id: "mock-development-v1", redistribution: "allowed", usage: "development_mock", attribution: "Synthetic metadata; no live market data." };
const canonical = (value) => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
};
const hash = (value) => createHash("sha256").update(canonical(value)).digest("hex");

const assets = source.assets.map((asset) => {
  const payload = { ...asset, data_license: license };
  return { schema_version: "v1", created_at: createdAt, source_refs: sourceRefs, content_hash: hash({ schema_version: "v1", source_refs: sourceRefs, ...payload }), ...payload };
});
const snapshotPayload = { registry_version: source.registry_version, as_of: source.as_of, assets };
const snapshot = { schema_version: "v1", created_at: createdAt, source_refs: sourceRefs, content_hash: hash({ schema_version: "v1", source_refs: sourceRefs, ...snapshotPayload }), ...snapshotPayload };
const output = `${JSON.stringify(snapshot, null, 2)}\n`;

if (process.argv.includes("--check")) {
  const current = await readFile(outputPath, "utf8").catch(() => "");
  if (current !== output) {
    console.error(`FAIL generated asset registry drift: ${outputPath}`);
    process.exitCode = 1;
  } else {
    console.log(`PASS generated asset registry ${snapshot.content_hash}`);
  }
} else {
  await writeFile(outputPath, output, "utf8");
  console.log(`Generated ${assets.length} registry entries at ${outputPath}`);
}
