#!/usr/bin/env node
/**
 * Pack the distributable web-office source (whitelist from source-inventory.mjs) into the
 * skill's assets/ directory. The skill itself may live outside the workspace root, so its
 * files are read from `skillDirectory` and always archived under skills/web-office-skills/.
 */
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readFile, mkdir, writeFile, realpath, rename } from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { packArchive } from "./archive.mjs";
import { collectOfficeSource } from "./source-inventory.mjs";

const digest = (data) => createHash("sha256").update(data).digest("hex");

export async function packOfficeSource({ workspaceRoot, skillDirectory, output }) {
  const root = await realpath(workspaceRoot);
  const skill = await realpath(skillDirectory ?? fileURLToPath(new URL("..", import.meta.url)));
  const out = path.resolve(output ?? path.join(skill, "assets"));
  const files = await collectOfficeSource(root, { skillDirectory: skill });
  const archive = packArchive(files);
  const pkg = JSON.parse(await readFile(path.join(root, "app/package.json"), "utf8"));
  const officeVersion = await readFile(path.join(root, "VERSION"), "utf8")
    .then((text) => text.trim())
    .catch(() => null);
  let sourceCommit = null,
    sourceDirty = null;
  try {
    sourceCommit = execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    sourceDirty = Boolean(
      execFileSync("git", ["status", "--porcelain", "--untracked-files=normal"], {
        cwd: root,
        encoding: "utf8",
        stdio: ["ignore", "pipe", "ignore"],
      }).trim(),
    );
  } catch {}
  const manifest = {
    formatVersion: 1,
    officeVersion,
    appVersion: pkg.version,
    upstreamCommit: "66a24ea7df2435ef897c48c147deb7ec572c01c2",
    sourceCommit,
    sourceDirty,
    sha256: digest(archive),
    files: Object.fromEntries(
      [...files].sort(([a], [b]) => a.localeCompare(b)).map(([name, data]) => [name, digest(data)]),
    ),
  };
  await mkdir(out, { recursive: true });
  const suffix = randomUUID(),
    archiveTmp = path.join(out, `.source-${suffix}.tar.gz`),
    manifestTmp = path.join(out, `.manifest-${suffix}.json`);
  await writeFile(archiveTmp, archive, { flag: "wx", mode: 0o600 });
  await writeFile(manifestTmp, JSON.stringify(manifest, null, 2) + "\n", { flag: "wx", mode: 0o600 });
  await rename(archiveTmp, path.join(out, "office-source.tar.gz"));
  await rename(manifestTmp, path.join(out, "source-manifest.json"));
  return manifest;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const args = process.argv.slice(2);
  const value = (name) => {
    const index = args.indexOf(`--${name}`);
    return index >= 0 ? args[index + 1] : undefined;
  };
  try {
    const manifest = await packOfficeSource({
      workspaceRoot: value("root") ?? process.cwd(),
      skillDirectory: value("skill"),
      output: value("out"),
    });
    process.stdout.write(
      JSON.stringify({
        sha256: manifest.sha256,
        officeVersion: manifest.officeVersion,
        sourceCommit: manifest.sourceCommit,
        fileCount: Object.keys(manifest.files).length,
      }) + "\n",
    );
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  }
}
