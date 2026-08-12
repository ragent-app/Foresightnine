import { access, readFile, readdir, stat } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PNG } from "pngjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "../../docs/design/concepts");
const designRoot = resolve(here, "../../docs/design");
const manifest = JSON.parse(await readFile(resolve(designRoot, "phase2-manifest.json"), "utf8"));

if (!Array.isArray(manifest.assets) || manifest.assets.length === 0) {
  throw new Error("manifest.assets dimension registry is missing or empty");
}

const registry = new Map(manifest.assets.map((entry) => [entry.path, entry]));
const required = [
  "web/01-thesis-input.png",
  "web/02-challenge-evidence.png",
  "web/03-worldline-probability.png",
  "web/04-fm-progress.png",
  "web/05-portfolio-stress.png",
  "web/06-worldline-nav.png",
  "web/07-methodology-risk.png",
  "android/01-thesis-input.png",
  "android/02-challenge-conversation.png",
  "android/03-evidence-workspace.png",
  "android/04-worldline-timeline.png",
  "android/05-portfolio-nav.png",
  "android/06-share-export.png",
];

/** Decode every pixel of the PNG. A corrupt IDAT, bad CRC or truncated stream throws. */
const decodePng = async (relativePath) => {
  const bytes = await readFile(resolve(root, relativePath));
  const png = PNG.sync.read(bytes);
  if (!png.data || png.data.length !== png.width * png.height * 4) {
    throw new Error(`${relativePath} decoded but its RGBA buffer is incomplete`);
  }
  return { width: png.width, height: png.height };
};

const assertArtboard = (relativePath, width, height) => {
  if (relativePath.startsWith("web/") && (width !== 1536 || height !== 1024)) {
    throw new Error(`${relativePath} must be 1536x1024, observed ${width}x${height}`);
  }
  if (relativePath.startsWith("android/")) {
    const ratio = width / height;
    if (width < 850 || height < 1600 || Math.abs(ratio - 9 / 16) > 0.01) {
      throw new Error(`${relativePath} must be a high-resolution 9:16 Android artboard, observed ${width}x${height}`);
    }
  }
};

const onDiskPaths = [];
async function collectPngs(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) await collectPngs(path);
    if (entry.isFile() && entry.name.toLowerCase().endsWith(".png")) {
      onDiskPaths.push(relative(root, path).replaceAll("\\", "/"));
    }
  }
}
await collectPngs(root);

const registryPaths = new Set(registry.keys());
const diskSet = new Set(onDiskPaths);
const unregistered = onDiskPaths.filter((path) => !registryPaths.has(path)).sort();
const missing = [...registryPaths].filter((path) => !diskSet.has(path)).sort();
if (unregistered.length > 0) throw new Error(`Unregistered PNG assets not in manifest: ${unregistered.join(", ")}`);
if (missing.length > 0) throw new Error(`Registered PNG assets missing from disk: ${missing.join(", ")}`);

for (const entry of manifest.assets.sort((a, b) => a.path.localeCompare(b.path))) {
  const { width, height } = await decodePng(entry.path);
  if (width !== entry.width || height !== entry.height) {
    throw new Error(`${entry.path} dimension mismatch: registered ${entry.width}x${entry.height}, observed ${width}x${height}`);
  }
  console.log(`PASS decoded ${entry.role} ${entry.path} ${width}x${height}`);
}

for (const relativePath of required) {
  const entry = registry.get(relativePath);
  const { width, height } = await decodePng(relativePath);
  assertArtboard(relativePath, width, height);
  if (entry.role !== "required") throw new Error(`${relativePath} is registered as ${entry.role}, not required`);
  console.log(`PASS required design ${relativePath} ${width}x${height}`);
}

for (const relativePath of manifest.variants) {
  const entry = registry.get(relativePath);
  const { width, height } = await decodePng(relativePath);
  assertArtboard(relativePath, width, height);
  if (entry.role !== "variant") throw new Error(`${relativePath} is registered as ${entry.role}, not variant`);
  console.log(`PASS variant ${relativePath} ${width}x${height}`);
}

for (const entry of manifest.assets) {
  if (entry.role === "archive" && entry.width !== 1536 && entry.width !== 941) {
    console.log(`PASS archive historical exception ${entry.path} ${entry.width}x${entry.height}`);
  }
}

for (const relative of manifest.specifications) {
  await access(resolve(designRoot, relative));
  console.log(`PASS specification ${relative}`);
}

const promptRecord = await readFile(resolve(designRoot, "imagegen-prompts.md"), "utf8");
const promptHeadings = [
  "Thesis input",
  "Challenge and evidence",
  "Worldline and probability",
  "Local FM progress",
  "Portfolio and stress",
  "Selected-worldline NAV",
  "Methodology and risk",
  "Challenge conversation",
  "Evidence workspace",
  "Worldline timeline",
  "Portfolio NAV",
  "Share/export",
];
for (const heading of promptHeadings) {
  if (!promptRecord.includes(heading)) throw new Error(`Missing persisted prompt section: ${heading}`);
}

if (manifest.status !== "approved_for_implementation" || !manifest.approval?.user_statement) {
  throw new Error("Complete concept suite has no traceable user approval record");
}

const tokenSource = await readFile(resolve(here, "../../packages/design-system/src/index.ts"), "utf8");
const tokenHex = (name) => {
  const match = tokenSource.match(new RegExp(`${name}:\\s*"(#[0-9a-fA-F]{6})"`));
  if (!match) throw new Error(`Missing colour token ${name}`);
  return match[1];
};
const luminance = (hex) => {
  const channels = hex.match(/[0-9a-f]{2}/gi).map((value) => Number.parseInt(value, 16) / 255);
  const linear = channels.map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
};
const contrast = (first, second) => {
  const a = luminance(first);
  const b = luminance(second);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
};
const canvas = tokenHex("canvas");
for (const name of ["accent", "weak", "strong", "evidenceB", "evidenceC", "evidenceD", "evidenceE"]) {
  const ratio = contrast(tokenHex(name), canvas);
  if (ratio < 4.5) throw new Error(`${name}/canvas contrast ${ratio.toFixed(2)} is below 4.5`);
  console.log(`PASS contrast ${name}/canvas ${ratio.toFixed(2)}`);
}

for (const requiredToken of ["semanticPair", "component", "dash", "marker", "touchAndroid"]) {
  if (!tokenSource.includes(requiredToken)) throw new Error(`Missing implementation token: ${requiredToken}`);
}

console.log(`Verified ${manifest.assets.length} pixel-decoded PNG assets, ${required.length} approved full-state concepts, ${manifest.variants.length} state variants, prompts, specifications and contrast tokens.`);
