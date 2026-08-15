import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { crc32, validatePngStream } from "./design-png-guards.mjs";

const sourcePath = new URL("../../docs/design/concepts/web/04-fm-progress.png", import.meta.url);
const source = await readFile(sourcePath);

function parseChunks(bytes) {
  const chunks = [];
  let offset = 8;
  while (offset < bytes.length) {
    const length = bytes.readUInt32BE(offset);
    const type = Buffer.from(bytes.subarray(offset + 4, offset + 8));
    const data = Buffer.from(bytes.subarray(offset + 8, offset + 8 + length));
    chunks.push({ type, data });
    offset += 12 + length;
  }
  return chunks;
}

function encodeChunks(chunks) {
  const encoded = [source.subarray(0, 8)];
  for (const { type, data } of chunks) {
    const length = Buffer.alloc(4);
    length.writeUInt32BE(data.length);
    const crc = Buffer.alloc(4);
    crc.writeUInt32BE(crc32(Buffer.concat([type, data])));
    encoded.push(length, type, data, crc);
  }
  return Buffer.concat(encoded);
}

const chunks = parseChunks(source);
assert.doesNotThrow(() => validatePngStream(source, "valid.png"));

const invalidHeader = encodeChunks(chunks.map(({ type, data }) => {
  if (type.toString("ascii") !== "IDAT") return { type, data };
  const changed = Buffer.from(data);
  changed[0] = 0;
  return { type, data: changed };
}));
assert.throws(
  () => validatePngStream(invalidHeader, "invalid-zlib-header.png"),
  /invalid zlib stream/,
);

let extended = false;
const trailingCompressedBytes = encodeChunks(chunks.map(({ type, data }) => {
  if (extended || type.toString("ascii") !== "IDAT") return { type, data };
  extended = true;
  return { type, data: Buffer.concat([data, Buffer.from([0x01, 0x02, 0x03])]) };
}));
assert.equal(extended, true);
assert.throws(
  () => validatePngStream(trailingCompressedBytes, "trailing-idat.png"),
  /trailing or unconsumed compressed IDAT bytes/,
);

console.log("PASS design asset regression: invalid zlib stream and trailing IDAT bytes are rejected");
