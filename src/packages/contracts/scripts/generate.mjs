import { createHash } from "node:crypto";
import { readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const schemaPath = resolve(here, "../schemas/v1/contracts.schema.json");
const outputPath = resolve(here, "../src/generated.ts");
const fixtureRoot = resolve(here, "../fixtures/v1/scenarios");
const schemaText = await readFile(schemaPath, "utf8");
const schema = JSON.parse(schemaText);
const definitions = schema.$defs;

if (!definitions || typeof definitions !== "object") {
  throw new Error("Canonical schema has no $defs object");
}

const refName = (ref) => {
  const prefix = "#/$defs/";
  if (!ref.startsWith(prefix)) throw new Error(`Only local canonical refs are supported: ${ref}`);
  return ref.slice(prefix.length);
};

const union = (members) => [...new Set(members)].join(" | ");
const canonical = (value) => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
};

function renderType(node, context) {
  if (node.$ref) return refName(node.$ref);
  if (Object.hasOwn(node, "const")) return JSON.stringify(node.const);
  if (node.enum) return union(node.enum.map((value) => JSON.stringify(value)));
  if (node.oneOf) return union(node.oneOf.map((entry, index) => renderType(entry, `${context}Variant${index + 1}`)));
  if (node.anyOf) return union(node.anyOf.map((entry, index) => renderType(entry, `${context}Option${index + 1}`)));
  if (Array.isArray(node.type)) return union(node.type.map((type) => renderType({ ...node, type }, context)));
  if (node.type === "string") return "string";
  if (node.type === "number" || node.type === "integer") return "number";
  if (node.type === "boolean") return "boolean";
  if (node.type === "null") return "null";
  if (node.type === "array") return `readonly (${renderType(node.items ?? {}, `${context}Item`)})[]`;
  if (node.type === "object" || node.properties || node.allOf) {
    const shape = collectObjectShape(node);
    if (shape.properties.size > 0) {
      const fields = [...shape.properties.entries()].map(([name, value]) => {
        const optional = shape.required.has(name) ? "" : "?";
        return `readonly ${JSON.stringify(name)}${optional}: ${renderType(value, `${context}${name}`)};`;
      });
      return `{ ${fields.join(" ")} }`;
    }
    if (node.additionalProperties && typeof node.additionalProperties === "object") {
      return `Readonly<Record<string, ${renderType(node.additionalProperties, `${context}Value`)}>>`;
    }
    return "Readonly<Record<string, never>>";
  }
  throw new Error(`Unsupported schema node while generating ${context}: ${JSON.stringify(node)}`);
}

function collectObjectShape(node, seen = new Set()) {
  const properties = new Map();
  const required = new Set();

  const visit = (current) => {
    if (current.$ref) {
      const name = refName(current.$ref);
      if (seen.has(name)) return;
      seen.add(name);
      visit(definitions[name]);
      return;
    }
    for (const part of current.allOf ?? []) {
      if (part.$ref || part.type === "object" || part.properties) visit(part);
    }
    for (const [name, value] of Object.entries(current.properties ?? {})) properties.set(name, value);
    for (const name of current.required ?? []) required.add(name);
  };

  visit(node);
  return { properties, required };
}

function isObjectDefinition(node) {
  return node.type === "object" || Boolean(node.properties) || Boolean(node.allOf);
}

const digest = createHash("sha256").update(schemaText).digest("hex");
const lines = [
  "/**",
  " * GENERATED FILE — DO NOT EDIT.",
  " * Source: schemas/v1/contracts.schema.json",
  " */",
  `export const CONTRACT_VERSION = "v1" as const;`,
  `export const CONTRACT_SCHEMA_SHA256 = "${digest}" as const;`,
  ""
];

for (const [name, definition] of Object.entries(definitions)) {
  if (isObjectDefinition(definition)) {
    const shape = collectObjectShape(definition, new Set([name]));
    lines.push(`export interface ${name} {`);
    for (const [propertyName, propertySchema] of shape.properties.entries()) {
      const optional = shape.required.has(propertyName) ? "" : "?";
      lines.push(`  readonly ${JSON.stringify(propertyName)}${optional}: ${renderType(propertySchema, `${name}${propertyName}`)};`);
    }
    lines.push("}", "");
  } else {
    lines.push(`export type ${name} = ${renderType(definition, name)};`, "");
  }
}

const output = `${lines.join("\n")}\n`;
if (process.argv.includes("--check")) {
  const current = await readFile(outputPath, "utf8").catch(() => "");
  if (current !== output) {
    console.error(`FAIL generated TypeScript drift: ${outputPath}`);
    process.exitCode = 1;
  } else {
    console.log(`PASS generated TypeScript ${digest}`);
  }
} else {
  await writeFile(outputPath, output, "utf8");
  console.log(`Generated ${outputPath} from ${digest}`);
}

for (const fixtureName of (await readdir(fixtureRoot)).filter((name) => name.endsWith(".json")).sort()) {
  const fixturePath = resolve(fixtureRoot, fixtureName);
  const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
  const { created_at: _createdAt, content_hash: _oldHash, ...hashInput } = fixture;
  const expectedHash = createHash("sha256").update(canonical(hashInput)).digest("hex");
  const expectedFixture = `${JSON.stringify({ ...fixture, content_hash: expectedHash }, null, 2)}\n`;
  if (process.argv.includes("--check")) {
    const current = await readFile(fixturePath, "utf8");
    if (current !== expectedFixture) {
      console.error(`FAIL acceptance fixture hash drift: ${fixturePath}`);
      process.exitCode = 1;
    }
  } else {
    await writeFile(fixturePath, expectedFixture, "utf8");
  }
}
if (!process.argv.includes("--check")) console.log("Generated content hashes for acceptance fixtures A-G");
