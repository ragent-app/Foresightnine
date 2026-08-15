import { inflateSync } from "node:zlib";

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** Validate PNG chunk structure and the complete filtered scanline stream before decoding pixels. */
export function validatePngStream(bytes, relativePath) {
  if (bytes.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") throw new Error(`${relativePath} is not a PNG`);
  let offset = 8;
  let ihdr;
  const idat = [];
  let sawIdat = false;
  let sawNonIdatAfterIdat = false;
  let sawIend = false;
  while (offset < bytes.length) {
    if (offset + 12 > bytes.length) throw new Error(`${relativePath} has a truncated PNG chunk`);
    const length = bytes.readUInt32BE(offset);
    const type = bytes.subarray(offset + 4, offset + 8);
    const typeName = type.toString("ascii");
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    const crcEnd = dataEnd + 4;
    if (crcEnd > bytes.length) throw new Error(`${relativePath} has a truncated ${typeName} chunk`);
    const data = bytes.subarray(dataStart, dataEnd);
    const expectedCrc = bytes.readUInt32BE(dataEnd);
    const actualCrc = crc32(Buffer.concat([type, data]));
    if (actualCrc !== expectedCrc) throw new Error(`${relativePath} has an invalid ${typeName} CRC`);

    if (offset === 8 && typeName !== "IHDR") throw new Error(`${relativePath} must begin with IHDR`);
    if (typeName === "IHDR") {
      if (ihdr || data.length !== 13) throw new Error(`${relativePath} has an invalid IHDR`);
      ihdr = data;
    } else if (typeName === "IDAT") {
      if (!ihdr || sawIend || sawNonIdatAfterIdat) throw new Error(`${relativePath} has an out-of-order IDAT`);
      sawIdat = true;
      idat.push(data);
    } else if (typeName === "IEND") {
      if (!sawIdat || data.length !== 0 || sawIend) throw new Error(`${relativePath} has an invalid IEND`);
      sawIend = true;
    } else if (sawIdat && !sawIend) {
      sawNonIdatAfterIdat = true;
    }
    offset = crcEnd;
    if (sawIend && offset !== bytes.length) throw new Error(`${relativePath} has data after IEND`);
  }
  if (!sawIend || !ihdr) throw new Error(`${relativePath} is missing IHDR or IEND`);

  const width = ihdr.readUInt32BE(0);
  const height = ihdr.readUInt32BE(4);
  const bitDepth = ihdr[8];
  const colorType = ihdr[9];
  const compression = ihdr[10];
  const filter = ihdr[11];
  const interlace = ihdr[12];
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (!width || !height || bitDepth !== 8 || channels === undefined || compression !== 0 || filter !== 0 || interlace !== 0) {
    throw new Error(`${relativePath} uses an unsupported PNG encoding for strict verification`);
  }

  const compressed = Buffer.concat(idat);
  let inflated;
  let engine;
  try {
    ({ buffer: inflated, engine } = inflateSync(compressed, { info: true }));
  } catch (error) {
    throw new Error(`${relativePath} has an invalid zlib stream: ${error.message}`);
  }
  if (engine.bytesWritten !== compressed.length) {
    throw new Error(`${relativePath} has trailing or unconsumed compressed IDAT bytes`);
  }
  const expectedFilteredBytes = height * (1 + width * channels);
  if (inflated.length !== expectedFilteredBytes) {
    throw new Error(`${relativePath} has ${inflated.length} filtered bytes; expected ${expectedFilteredBytes}`);
  }
  return { width, height };
}
