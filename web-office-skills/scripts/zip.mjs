import { deflateRawSync, inflateRawSync } from "node:zlib";
import { safeName } from "./archive.mjs";

const crcTable = Array.from({ length: 256 }, (_, initial) => {
  let value = initial;
  for (let bit = 0; bit < 8; bit++)
    value = (value & 1 ? 0xedb88320 : 0) ^ (value >>> 1);
  return value >>> 0;
});
function crc32(data) {
  let value = 0xffffffff;
  for (const byte of data)
    value = crcTable[(value ^ byte) & 255] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}
const fail = () => {
  throw Error("invalid_or_unsupported_zip");
};
const LIMIT = 256 * 1024 * 1024;

/** Deterministic regular-file ZIP. No credentials, timestamps or host metadata are added. */
export function writeZip(files) {
  if (!(files instanceof Map) || files.size > 20000) fail();
  const local = [],
    central = [];
  let offset = 0,
    expanded = 0,
    centralBytes = 0;
  for (const [filename, value] of [...files].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    safeName(filename);
    const name = Buffer.from(filename),
      data = Buffer.from(value);
    expanded += data.length;
    if (name.length > 65535 || expanded > 256 * 1024 * 1024) fail();
    const compressed = deflateRawSync(data, { level: 9 }),
      crc = crc32(data);
    centralBytes += 46 + name.length;
    if (
      offset + 30 + name.length + compressed.length + centralBytes + 22 >
      LIMIT
    )
      fail();
    const header = Buffer.alloc(30);
    header.writeUInt32LE(0x04034b50);
    header.writeUInt16LE(20, 4);
    header.writeUInt16LE(0x800, 6);
    header.writeUInt16LE(8, 8);
    header.writeUInt16LE(33, 12);
    header.writeUInt32LE(crc, 14);
    header.writeUInt32LE(compressed.length, 18);
    header.writeUInt32LE(data.length, 22);
    header.writeUInt16LE(name.length, 26);
    local.push(header, name, compressed);
    const directory = Buffer.alloc(46);
    directory.writeUInt32LE(0x02014b50);
    directory.writeUInt16LE(0x0314, 4);
    directory.writeUInt16LE(20, 6);
    directory.writeUInt16LE(0x800, 8);
    directory.writeUInt16LE(8, 10);
    directory.writeUInt16LE(33, 14);
    directory.writeUInt32LE(crc, 16);
    directory.writeUInt32LE(compressed.length, 20);
    directory.writeUInt32LE(data.length, 24);
    directory.writeUInt16LE(name.length, 28);
    directory.writeUInt32LE((0o100644 << 16) >>> 0, 38);
    directory.writeUInt32LE(offset, 42);
    central.push(directory, name);
    offset += header.length + name.length + compressed.length;
  }
  const index = Buffer.concat(central),
    end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50);
  end.writeUInt16LE(files.size, 8);
  end.writeUInt16LE(files.size, 10);
  end.writeUInt32LE(index.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, index, end]);
}

/** Read regular, unencrypted ZIP files with bounded inflation and CRC/path checks. */
export function readZip(input, maxBytes = 256 * 1024 * 1024) {
  if (
    !Number.isSafeInteger(maxBytes) ||
    maxBytes < 1 ||
    maxBytes > LIMIT ||
    !(input instanceof Uint8Array) ||
    input.byteLength > LIMIT
  )
    fail();
  const data = Buffer.from(input);
  if (data.length < 22) fail();
  const end = data.length - 22;
  if (
    data.readUInt32LE(end) !== 0x06054b50 ||
    data.readUInt16LE(end + 20) ||
    data.readUInt16LE(end + 4) ||
    data.readUInt16LE(end + 6)
  )
    fail();
  const count = data.readUInt16LE(end + 10),
    length = data.readUInt32LE(end + 12),
    start = data.readUInt32LE(end + 16);
  if (
    count > 20000 ||
    data.readUInt16LE(end + 8) !== count ||
    start + length !== end
  )
    fail();
  const files = new Map();
  let at = start,
    expanded = 0,
    nextLocal = 0;
  for (let entry = 0; entry < count; entry++) {
    if (at + 46 > end || data.readUInt32LE(at) !== 0x02014b50) fail();
    const flags = data.readUInt16LE(at + 8),
      method = data.readUInt16LE(at + 10);
    const crc = data.readUInt32LE(at + 16),
      packed = data.readUInt32LE(at + 20),
      size = data.readUInt32LE(at + 24);
    const nameLength = data.readUInt16LE(at + 28),
      extra = data.readUInt16LE(at + 30),
      comment = data.readUInt16LE(at + 32);
    const disk = data.readUInt16LE(at + 34),
      attributes = data.readUInt32LE(at + 38),
      mode = attributes >>> 16,
      local = data.readUInt32LE(at + 42);
    expanded += size;
    if (
      flags & ~0x800 ||
      ![0, 8].includes(method) ||
      disk ||
      extra ||
      comment ||
      data.readUInt16LE(at + 36) ||
      attributes & 0xffff ||
      local !== nextLocal ||
      expanded > maxBytes ||
      at + 46 + nameLength + extra + comment > end
    )
      fail();
    if (mode & 0o170000 && (mode & 0o170000) !== 0o100000) fail();
    const rawName = data.subarray(at + 46, at + 46 + nameLength),
      filename = rawName.toString("utf8");
    if (!Buffer.from(filename).equals(rawName)) fail();
    safeName(filename);
    if (files.has(filename)) fail();
    if (local + 30 > start || data.readUInt32LE(local) !== 0x04034b50) fail();
    const localFlags = data.readUInt16LE(local + 6),
      localMethod = data.readUInt16LE(local + 8);
    const localName = data.readUInt16LE(local + 26),
      localExtra = data.readUInt16LE(local + 28);
    const contentStart = local + 30 + localName + localExtra;
    if (
      flags !== localFlags ||
      method !== localMethod ||
      localExtra ||
      data.readUInt16LE(at + 12) !== data.readUInt16LE(local + 10) ||
      data.readUInt16LE(at + 14) !== data.readUInt16LE(local + 12) ||
      contentStart + packed > start ||
      !data.subarray(local + 30, local + 30 + localName).equals(rawName)
    )
      fail();
    if (
      data.readUInt32LE(local + 14) !== crc ||
      data.readUInt32LE(local + 18) !== packed ||
      data.readUInt32LE(local + 22) !== size
    )
      fail();
    const packedData = data.subarray(contentStart, contentStart + packed);
    const inflated =
      method === 0
        ? undefined
        : inflateRawSync(packedData, {
            maxOutputLength: Math.max(1, Math.min(size, maxBytes)),
            info: true,
          });
    if (inflated && inflated.engine.bytesWritten !== packed) fail();
    const value = inflated ? inflated.buffer : Buffer.from(packedData);
    if (value.length !== size || crc32(value) !== crc) fail();
    files.set(filename, value);
    nextLocal = contentStart + packed;
    at += 46 + nameLength + extra + comment;
  }
  if (at !== end || nextLocal !== start) fail();
  return files;
}
