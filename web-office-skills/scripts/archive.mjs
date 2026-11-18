import path from "node:path";
import { gzipSync, inflateRawSync } from "node:zlib";
const LIMIT = 256 * 1024 * 1024;
const crcTable = Array.from({ length: 256 }, (_, initial) => {
  let value = initial;
  for (let bit = 0; bit < 8; bit++)
    value = (value & 1 ? 0xedb88320 : 0) ^ (value >>> 1);
  return value >>> 0;
});
const invalid = () => {
  throw Error("invalid_or_unsupported_source_archive");
};
function crc32(data) {
  let value = 0xffffffff;
  for (const byte of data)
    value = crcTable[(value ^ byte) & 255] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}
export function safeName(name) {
  if (
    typeof name !== "string" ||
    !name ||
    name.includes("\\") ||
    name.includes("\0") ||
    path.posix.isAbsolute(name) ||
    name.split("/").some((p) => !p || p === "." || p === "..")
  )
    throw Error("invalid_archive_path");
  return name;
}
export function packArchive(files) {
  if (!(files instanceof Map) || files.size > 20000) invalid();
  const blocks = [];
  let total = 1024;
  for (const [filename, value] of [...files].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    safeName(filename);
    const data = Buffer.from(value),
      header = Buffer.alloc(512);
    total += 512 + Math.ceil(data.length / 512) * 512;
    if (total > LIMIT) invalid();
    let name = filename,
      prefix = "";
    if (Buffer.byteLength(name) > 100) {
      const split = filename.lastIndexOf("/");
      prefix = filename.slice(0, split);
      name = filename.slice(split + 1);
      if (
        split < 0 ||
        Buffer.byteLength(name) > 100 ||
        Buffer.byteLength(prefix) > 155
      )
        throw Error("archive_path_too_long");
    }
    const write = (value, at, length) =>
      header.write(value, at, length, "utf8");
    const octal = (value, length) =>
      value.toString(8).padStart(length - 1, "0") + "\0";
    write(name, 0, 100);
    write(octal(0o644, 8), 100, 8);
    write(octal(0, 8), 108, 8);
    write(octal(0, 8), 116, 8);
    write(octal(data.length, 12), 124, 12);
    write(octal(0, 12), 136, 12);
    header.fill(32, 148, 156);
    header[156] = 48;
    write("ustar\0", 257, 6);
    write("00", 263, 2);
    write(prefix, 345, 155);
    let checksum = 0;
    for (const value of header) checksum += value;
    write(checksum.toString(8).padStart(6, "0") + "\0 ", 148, 8);
    blocks.push(header, data, Buffer.alloc((512 - (data.length % 512)) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  const archive = gzipSync(Buffer.concat(blocks), { level: 9 });
  if (archive.length > LIMIT) invalid();
  return archive;
}
export function unpackArchive(archive, maxBytes = 256 * 1024 * 1024) {
  if (
    !Number.isSafeInteger(maxBytes) ||
    maxBytes < 1 ||
    maxBytes > LIMIT ||
    !(archive instanceof Uint8Array) ||
    archive.byteLength > LIMIT
  )
    invalid();
  const input = Buffer.from(archive);
  if (
    input.length < 18 ||
    input[0] !== 31 ||
    input[1] !== 139 ||
    input[2] !== 8 ||
    input[3] !== 0 ||
    input.readUInt32LE(4) !== 0
  )
    invalid();
  const inflated = inflateRawSync(input.subarray(10), {
    maxOutputLength: maxBytes,
    info: true,
  });
  const consumed = inflated.engine.bytesWritten;
  if (10 + consumed + 8 !== input.length) invalid();
  const data = inflated.buffer;
  if (
    crc32(data) !== input.readUInt32LE(input.length - 8) ||
    data.length !== input.readUInt32LE(input.length - 4) ||
    data.length < 1024 ||
    data.length % 512 !== 0
  )
    invalid();
  const files = new Map(),
    seen = new Set();
  let offset = 0;
  while (offset + 512 <= data.length) {
    const header = data.subarray(offset, offset + 512);
    if (header.every((v) => v === 0)) {
      if (
        offset + 1024 > data.length ||
        !data.subarray(offset).every((value) => value === 0)
      )
        invalid();
      return files;
    }
    let checksum = 0;
    for (let i = 0; i < 512; i++)
      checksum += i >= 148 && i < 156 ? 32 : header[i];
    const text = (start, end) => {
      const raw = header.subarray(start, end),
        zero = raw.indexOf(0);
      if (zero >= 0 && !raw.subarray(zero).every((value) => value === 0))
        invalid();
      const actual = zero < 0 ? raw : raw.subarray(0, zero),
        value = actual.toString("utf8");
      if (!Buffer.from(value).equals(actual)) invalid();
      return value;
    };
    const octal = (start, end) => {
      const value = header.subarray(start, end).toString("ascii");
      if (!/^[0-7]+[\0 ]*$/.test(value)) invalid();
      return parseInt(value, 8);
    };
    if (octal(148, 156) !== checksum) throw Error("invalid_archive_checksum");
    if (
      header.subarray(257, 265).toString("ascii") !== "ustar\0" + "00" ||
      !header.subarray(157, 257).every((value) => value === 0) ||
      !header.subarray(265, 345).every((value) => value === 0) ||
      !header.subarray(500).every((value) => value === 0) ||
      octal(108, 116) !== 0 ||
      octal(116, 124) !== 0 ||
      octal(136, 148) !== 0
    )
      invalid();
    let name = (text(345, 500) ? text(345, 500) + "/" : "") + text(0, 100);
    name = name.replace(/\/$/, "");
    safeName(name);
    const size = octal(124, 136),
      type = text(156, 157);
    const mode = octal(100, 108),
      next = offset + 512 + Math.ceil(size / 512) * 512;
    if (
      !Number.isSafeInteger(size) ||
      size < 0 ||
      next > data.length ||
      !["", "0", "5"].includes(type) ||
      seen.has(name) ||
      seen.size >= 20000 ||
      (type === "5"
        ? size !== 0 || ![0o644, 0o755].includes(mode)
        : mode !== 0o644) ||
      !data.subarray(offset + 512 + size, next).every((value) => value === 0)
    )
      throw Error("unsupported_archive_entry");
    seen.add(name);
    if (type !== "5")
      files.set(
        name,
        Buffer.from(data.subarray(offset + 512, offset + 512 + size)),
      );
    offset = next;
  }
  invalid();
}
