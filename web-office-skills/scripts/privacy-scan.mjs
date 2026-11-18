#!/usr/bin/env node
import path from "node:path";
import { pathToFileURL } from "node:url";
import { constants } from "node:fs";
import {
  lstat,
  realpath,
  readdir,
  open,
  mkdir,
  rename,
  unlink,
} from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { inflateSync } from "node:zlib";
import { unpackArchive, safeName } from "./archive.mjs";
import { readZip } from "./zip.mjs";
import { isDistributableSourcePath } from "./source-inventory.mjs";
import { createRules, fingerprint } from "./privacy-rules.mjs";

const MAX_BYTES = 256 * 1024 * 1024;
const MAX_FILES = 20000;
const inside = (root, file) => {
  const relative = path.relative(root, file);
  return (
    relative !== ".." &&
    !relative.startsWith(".." + path.sep) &&
    !path.isAbsolute(relative)
  );
};
const fail = (code) => {
  throw Object.assign(new Error(code), { privacyCode: code });
};
async function readRegular(file, limit = MAX_BYTES) {
  const info = await lstat(file);
  if (
    !info.isFile() ||
    info.isSymbolicLink() ||
    info.nlink !== 1 ||
    info.size > limit
  )
    fail("unsafe-or-oversized-input");
  const handle = await open(
    file,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const actual = await handle.stat();
    if (
      actual.dev !== info.dev ||
      actual.ino !== info.ino ||
      actual.nlink !== 1
    )
      fail("input-changed-during-scan");
    const chunks = [];
    let length = 0;
    while (true) {
      const chunk = Buffer.alloc(Math.min(65536, limit - length + 1));
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
      if (!bytesRead) break;
      length += bytesRead;
      if (length > limit) fail("oversized-input");
      chunks.push(chunk.subarray(0, bytesRead));
    }
    return Buffer.concat(chunks, length);
  } finally {
    await handle.close();
  }
}
async function canonicalDirectory(directory) {
  const absolute = path.resolve(directory);
  if (
    (await realpath(absolute)) !== absolute ||
    !(await lstat(absolute)).isDirectory()
  )
    fail("directory-must-be-canonical");
  return absolute;
}
async function confinedFile(root, input) {
  const filename = path.resolve(root, input);
  if (!inside(root, filename)) fail("file-outside-explicit-root");
  if ((await realpath(filename)) !== filename) fail("symlink-input-forbidden");
  return filename;
}

export function parsePrivacyOptions(args) {
  const options = { personalMarkers: [] };
  const names = new Map([
    ["--skill", "skill"],
    ["--root", "root"],
    ["--report", "report"],
    ["--personal-markers-file", "personalMarkersFile"],
    ["--zip", "zip"],
  ]);
  for (let i = 0; i < args.length; i++) {
    const key = args[i];
    if (key === "--help") {
      options.help = true;
      continue;
    }
    if (key !== "--personal-marker" && !names.has(key)) fail("unknown-option");
    const value = args[++i];
    if (!value || value.startsWith("--")) fail("missing-option-value");
    if (key === "--personal-marker") options.personalMarkers.push(value);
    else {
      const field = names.get(key);
      if (options[field] !== undefined) fail("duplicate-option");
      options[field] = value;
    }
  }
  if (!options.help && !options.skill) fail("explicit-skill-required");
  if (
    (options.report || options.personalMarkersFile || options.zip) &&
    !options.root
  )
    fail("explicit-root-required");
  return options;
}

export async function scanPrivacy(options) {
  try {
    return await performScan(options);
  } catch (error) {
    fail(
      /^[a-z-]+$/.test(error.privacyCode ?? "")
        ? error.privacyCode
        : "privacy-scan-failed",
    );
  }
}

async function performScan(options) {
  const skill = await canonicalDirectory(options.skill);
  const root = options.root
    ? await canonicalDirectory(options.root)
    : undefined;
  const markers = [...(options.personalMarkers ?? [])];
  if (options.personalMarkersFile) {
    const filename = await confinedFile(root, options.personalMarkersFile);
    let configured;
    try {
      configured = JSON.parse((await readRegular(filename)).toString("utf8"));
    } catch {
      fail("invalid-personal-markers-file");
    }
    markers.push(
      ...(Array.isArray(configured)
        ? configured
        : Array.isArray(configured?.markers)
          ? configured.markers
          : [null]),
    );
  }
  if (
    markers.some(
      (value) =>
        typeof value !== "string" ||
        value.length < 2 ||
        value.length > 512 ||
        /[\r\n\0]/.test(value),
    )
  )
    fail("invalid-personal-markers");
  const rules = createRules(markers);
  const findings = [],
    inventory = [],
    archives = [];
  let binaryMetadataSections = 0;
  let bytes = 0;
  const outer = new Map();
  const add = (file, rule, value, severity = "high", status = "blocked") =>
    findings.push(rules.finding(file, 0, rule, value, severity, status));
  function inspect(name, data) {
    if (++inspect.count > MAX_FILES || (bytes += data.length) > MAX_BYTES)
      fail("scan-limit-exceeded");
    findings.push(...rules.scanPath(name));
    // Compressed bytes are inspected after decoding, rather than as random UTF-8 text.
    if (!/\.(?:tar\.gz|tgz|zip|tar|gz|7z|rar)$/i.test(name)) {
      const media = /\.(?:png|jpe?g|webp|gif|ico|woff2?|ttf|otf)$/i.test(name);
      if (!media && !data.includes(0))
        findings.push(...rules.scanText(name, data));
      else if (!media) add(name, "unexpected-binary-source", data);
      // PNG text/EXIF chunks contain meaningful metadata; IDAT compressed pixels do not.
      if (
        data
          .subarray(0, 8)
          .equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))
      ) {
        for (let offset = 8; offset + 12 <= data.length; ) {
          const length = data.readUInt32BE(offset);
          if (offset + length + 12 > data.length) {
            add(name, "invalid-image-metadata", data);
            break;
          }
          const type = data.toString("ascii", offset + 4, offset + 8);
          let payload = data.subarray(offset + 8, offset + 8 + length);
          if (["tEXt", "iTXt", "zTXt", "eXIf"].includes(type)) {
            try {
              if (type === "zTXt") {
                const zero = payload.indexOf(0);
                if (zero < 0 || payload[zero + 1] !== 0)
                  fail("invalid-png-metadata");
                payload = Buffer.concat([
                  payload.subarray(0, zero),
                  Buffer.from("\n"),
                  inflateSync(payload.subarray(zero + 2), {
                    maxOutputLength: 1024 * 1024,
                  }),
                ]);
              } else if (type === "iTXt") {
                const zero = payload.indexOf(0),
                  compressed = payload[zero + 1];
                if (
                  zero < 0 ||
                  payload[zero + 2] !== 0 ||
                  ![0, 1].includes(compressed)
                )
                  fail("invalid-png-metadata");
                let cursor = payload.indexOf(0, zero + 3);
                cursor = cursor < 0 ? -1 : payload.indexOf(0, cursor + 1);
                if (cursor < 0) fail("invalid-png-metadata");
                payload = Buffer.concat([
                  payload.subarray(0, zero),
                  Buffer.from("\n"),
                  compressed
                    ? inflateSync(payload.subarray(cursor + 1), {
                        maxOutputLength: 1024 * 1024,
                      })
                    : payload.subarray(cursor + 1),
                ]);
              }
              binaryMetadataSections++;
              findings.push(
                ...rules.scanText(
                  name + "#" + type,
                  Buffer.from(payload.toString("utf8").replaceAll("\0", "\n")),
                  { metadata: true },
                ),
              );
            } catch {
              add(name, "image-metadata-unreadable", payload);
            }
          }
          offset += length + 12;
          if (type === "IEND") break;
        }
      }
    }
    inventory.push({
      file: rules.maskName(name),
      bytes: data.length,
      sha256: fingerprint(data),
    });
  }
  inspect.count = 0;
  async function walk(relative = "") {
    const directory = path.join(skill, relative);
    if ((await realpath(directory)) !== directory)
      fail("symlink-directory-forbidden");
    for (const entry of (
      await readdir(directory, { withFileTypes: true })
    ).sort((a, b) => a.name.localeCompare(b.name))) {
      const name = relative ? relative + "/" + entry.name : entry.name;
      safeName(name);
      const file = path.join(skill, name);
      const info = await lstat(file);
      if (
        info.isSymbolicLink() ||
        (!info.isFile() && !info.isDirectory()) ||
        (info.isFile() && info.nlink !== 1)
      ) {
        add("skill/" + name, "unsafe-filesystem-entry", name);
        continue;
      }
      if (info.isDirectory()) {
        findings.push(...rules.scanPath("skill/" + name));
        await walk(name);
      } else {
        const data = await readRegular(file, MAX_BYTES - bytes);
        outer.set(name, data);
        inspect("skill/" + name, data);
      }
    }
  }
  await walk();

  function inspectNested(label, name, data, depth) {
    if (!/\.(?:tar\.gz|tgz|zip|tar|gz|7z|rar)$/i.test(name)) return;
    if (depth >= 3) {
      add(label, "nested-container-depth-exceeded", data);
      return;
    }
    if (/\.(?:tar\.gz|tgz)$/i.test(name))
      inspectArchive(label, data, depth + 1);
    else if (/\.zip$/i.test(name)) inspectZip(label, data, depth + 1);
    else add(label, "opaque-bundled-container", data);
  }
  function inspectZip(label, zip, depth = 0) {
    let files;
    try {
      files = readZip(zip, Math.max(1, MAX_BYTES - bytes));
    } catch {
      add(label, "invalid-or-unsafe-zip", zip);
      return;
    }
    archives.push({
      file: rules.maskName(label),
      sha256: fingerprint(zip),
      files: files.size,
    });
    for (const [name, data] of files) {
      inspect(label + "!" + name, data);
      inspectNested(label + "!" + name, name, data, depth);
    }
  }
  function inspectArchive(label, archive, depth = 0) {
    let files;
    try {
      files = unpackArchive(archive, Math.max(1, MAX_BYTES - bytes));
    } catch {
      add(label, "invalid-or-unsafe-source-archive", archive);
      return undefined;
    }
    archives.push({
      file: rules.maskName(label),
      sha256: fingerprint(archive),
      files: files.size,
    });
    for (const [name, data] of files) {
      inspect(label + "!" + name, data);
      inspectNested(label + "!" + name, name, data, depth);
    }
    return files;
  }
  const source = outer.get("assets/office-source.tar.gz");
  const manifestData = outer.get("assets/source-manifest.json");
  const sourceFiles = source
    ? inspectArchive("skill/assets/office-source.tar.gz", source)
    : undefined;
  if (!source)
    add(
      "skill/assets/office-source.tar.gz",
      "missing-source-archive",
      "missing",
    );
  let manifest;
  if (!manifestData)
    add(
      "skill/assets/source-manifest.json",
      "missing-source-manifest",
      "missing",
    );
  else {
    try {
      manifest = JSON.parse(manifestData.toString("utf8"));
    } catch {
      add(
        "skill/assets/source-manifest.json",
        "invalid-source-manifest",
        manifestData,
      );
    }
  }
  if (manifest) {
    if (
      manifest.formatVersion !== 1 ||
      !manifest.files ||
      typeof manifest.files !== "object" ||
      Array.isArray(manifest.files)
    )
      add(
        "skill/assets/source-manifest.json",
        "invalid-manifest-schema",
        manifestData,
      );
    else {
      const archiveMatches =
        !!source && !!sourceFiles && manifest.sha256 === fingerprint(source);
      if (source && !archiveMatches)
        add(
          "skill/assets/source-manifest.json",
          "archive-hash-mismatch",
          manifestData,
        );
      for (const [name, hash] of Object.entries(manifest.files)) {
        try {
          safeName(name);
        } catch {
          add(
            "skill/assets/source-manifest.json",
            "unsafe-manifest-path",
            name,
          );
          continue;
        }
        const pathFindings = rules.scanPath("manifest!" + name);
        findings.push(...pathFindings);
        if (
          !isDistributableSourcePath(name) ||
          pathFindings.some((entry) => entry.status === "blocked")
        ) {
          add("manifest!" + name, "manifest-path-outside-source-scope", name);
          continue;
        }
        if (typeof hash !== "string" || !/^[a-f0-9]{64}$/.test(hash)) {
          add("manifest!" + name, "invalid-manifest-hash", String(hash));
          continue;
        }
        const data = sourceFiles?.get(name);
        if (!data || fingerprint(data) !== hash) {
          add(
            "manifest!" + name,
            "source-entry-integrity-mismatch",
            String(hash),
          );
          continue;
        }
        if (root && archiveMatches) {
          try {
            const skillPrefix = "skills/web-office-skills/";
            // The skill may live outside the workspace (project code repository).
            const live = name.startsWith(skillPrefix)
              ? await confinedFile(skill, name.slice(skillPrefix.length))
              : await confinedFile(root, name);
            const current = await readRegular(live, MAX_BYTES - bytes);
            inspect("live!" + name, current);
            if (fingerprint(current) !== hash)
              add(
                "live!" + name,
                "live-source-differs-from-bundle",
                current,
                "low",
                "needsReview",
              );
          } catch {
            add("live!" + name, "live-source-missing-or-unsafe", name);
          }
        }
      }
      if (sourceFiles)
        for (const [name, data] of sourceFiles)
          if (!Object.hasOwn(manifest.files, name))
            add(
              "skill/assets/office-source.tar.gz!" + name,
              "unmanifested-source-entry",
              data,
            );
    }
  }
  // Detect all other bundled archives; opaque nested packages are never silently called clean.
  for (const [name, data] of outer) {
    if (name === "assets/office-source.tar.gz") continue;
    inspectNested("skill/" + name, name, data, 0);
  }
  if (options.zip) {
    const filename = await confinedFile(root, options.zip);
    const zip = await readRegular(filename);
    inspectZip("release.zip", zip);
  }
  const unique = [
    ...new Map(
      findings.map((entry) => [
        [entry.file, entry.line, entry.rule, entry.fingerprint].join("\0"),
        entry,
      ]),
    ).values(),
  ].sort(
    (a, b) =>
      a.file.localeCompare(b.file) ||
      a.line - b.line ||
      a.rule.localeCompare(b.rule),
  );
  const blocked = unique.filter((entry) => entry.status === "blocked").length;
  const needsReview = unique.length - blocked;
  const report = {
    formatVersion: 1,
    generatedAt: new Date().toISOString(),
    status: blocked ? "blocked" : needsReview ? "needsReview" : "clean",
    scope: {
      skillFiles: outer.size,
      sourceFiles: sourceFiles?.size ?? 0,
      manifestFiles: manifest?.files ? Object.keys(manifest.files).length : 0,
      liveComparison: Boolean(root),
      zipScanned: Boolean(options.zip),
      personalMarkerCount: markers.length,
    },
    summary: {
      filesScanned: inventory.length,
      bytesScanned: bytes,
      binaryMetadataSections,
      blocked,
      needsReview,
      high: unique.filter((entry) => entry.severity === "high").length,
      medium: unique.filter((entry) => entry.severity === "medium").length,
      low: unique.filter((entry) => entry.severity === "low").length,
    },
    limitations: [
      "Static candidate detection cannot prove that a credential is valid or that all private data was identified.",
      "Fixture/example and attribution candidates remain needsReview; no credential was validated against an external service.",
      "Only explicit skill, archive entries and manifest-listed live source are read; runtime directories and external user homes are not traversed.",
      "Copyright, author and license attribution is retained; reports contain no matching source excerpts or original candidate values.",
    ],
    archives,
    inventory,
    findings: unique,
  };
  if (options.report) await writePrivacyReport(root, options.report, report);
  return report;
}

async function writePrivacyReport(root, input, report) {
  const target = path.resolve(root, input),
    artifacts = path.join(root, "artifacts");
  if (
    !inside(artifacts, target) ||
    target === artifacts ||
    !target.endsWith(".json")
  )
    fail("report-must-be-json-under-root-artifacts");
  let current = root;
  for (const component of path
    .relative(root, path.dirname(target))
    .split(path.sep)) {
    current = path.join(current, component);
    try {
      await mkdir(current, { mode: 0o700 });
    } catch (error) {
      if (error.code !== "EEXIST") throw error;
    }
    const info = await lstat(current);
    if (
      !info.isDirectory() ||
      info.isSymbolicLink() ||
      (await realpath(current)) !== current
    )
      fail("unsafe-report-directory");
  }
  try {
    const info = await lstat(target);
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1)
      fail("unsafe-report-file");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const temporary = path.join(
    path.dirname(target),
    `.privacy-${randomUUID()}.tmp`,
  );
  const handle = await open(
    temporary,
    constants.O_WRONLY |
      constants.O_CREAT |
      constants.O_EXCL |
      constants.O_NOFOLLOW,
    0o600,
  );
  try {
    await handle.writeFile(JSON.stringify(report, null, 2) + "\n");
    await handle.sync();
    await rename(temporary, target);
  } finally {
    await handle.close();
    await unlink(temporary).catch(() => {});
  }
}

async function main() {
  try {
    const options = parsePrivacyOptions(process.argv.slice(2));
    if (options.help) {
      process.stdout.write(
        "privacy-scan.mjs --skill PATH [--root ROOT] [--report ROOT/artifacts/NAME.json] [--personal-marker VALUE] [--personal-markers-file ROOT/markers.json] [--zip ROOT/release.zip]\nExit: 0 clean; 2 needsReview; 1 blocked/error. Marker values and matching content are never reported.\n",
      );
      return;
    }
    const report = await scanPrivacy(options);
    process.stdout.write(
      JSON.stringify(
        options.report
          ? {
              status: report.status,
              scope: report.scope,
              summary: report.summary,
              archives: report.archives,
            }
          : report,
        null,
        2,
      ) + "\n",
    );
    process.exitCode =
      report.status === "blocked" ? 1 : report.status === "needsReview" ? 2 : 0;
  } catch (error) {
    // Never print errors from fs/JSON/archive parsers: they may contain raw paths or secrets.
    process.stdout.write(
      JSON.stringify({
        status: "blocked",
        error: /^[a-z-]+$/.test(error.privacyCode ?? "")
          ? error.privacyCode
          : "privacy-scan-failed",
      }) + "\n",
    );
    process.exitCode = 1;
  }
}
if (
  process.argv[1] &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url
)
  await main();
