import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const input = resolve(here, "../openapi/v2/openapi.json");
const output = resolve(here, "../src/generated-research-v2.ts");
const source = await readFile(input, "utf8");
const schemas = JSON.parse(source).components?.schemas;
if (!schemas || typeof schemas !== "object") throw new Error("V2 OpenAPI schemas are missing");

function render(node, where) {
  if (node.$ref) {
    const prefix = "#/components/schemas/";
    if (!node.$ref.startsWith(prefix) || !schemas[node.$ref.slice(prefix.length)]) throw new Error(`Invalid reference in ${where}`);
    return node.$ref.slice(prefix.length);
  }
  if (Object.hasOwn(node, "const")) return JSON.stringify(node.const);
  if (node.enum) return node.enum.map(JSON.stringify).join(" | ");
  if (node.anyOf || node.oneOf) return (node.anyOf ?? node.oneOf).map((value) => render(value, where)).join(" | ");
  if (node.allOf) return node.allOf.map((value) => `(${render(value, where)})`).join(" & ");
  if (node.type === "string") return "string";
  if (node.type === "number" || node.type === "integer") return "number";
  if (node.type === "boolean") return "boolean";
  if (node.type === "null") return "null";
  if (node.type === "array") return `readonly (${node.items ? render(node.items, where) : "unknown"})[]`;
  if (node.type === "object" || node.properties) {
    const required = new Set(node.required ?? []);
    const fields = Object.entries(node.properties ?? {}).map(([key, value]) =>
      `readonly ${JSON.stringify(key)}${required.has(key) ? "" : "?"}: ${render(value, `${where}.${key}`)};`);
    if (node.additionalProperties && !fields.length) {
      const value = node.additionalProperties === true ? "unknown" : render(node.additionalProperties, where);
      return `Readonly<Record<string, ${value}>>`;
    }
    return `{ ${fields.join(" ")} }`;
  }
  if (Object.keys(node).every((key) => ["title", "description", "default"].includes(key))) return "unknown";
  throw new Error(`Unsupported V2 schema in ${where}: ${JSON.stringify(node)}`);
}

const hash = createHash("sha256").update(source).digest("hex");
const types = Object.entries(schemas).map(([name, schema]) => `export type ${name} = ${render(schema, name)};`).join("\n");
const generated = `/** GENERATED FILE — DO NOT EDIT. Source: openapi/v2/openapi.json\n * SHA-256: ${hash}\n */\n${types}\n`;
if (process.argv.includes("--check")) {
  if (await readFile(output, "utf8").catch(() => "") !== generated) {
    console.error("FAIL generated V2 research types drift");
    process.exitCode = 1;
  } else console.log(`PASS generated V2 research types ${hash}`);
} else {
  await writeFile(output, generated, "utf8");
  console.log(`Generated V2 research types ${hash}`);
}
