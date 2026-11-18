#!/usr/bin/env node
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  mkdir,
  readFile,
  writeFile,
  realpath,
  lstat,
  rename,
  rm,
  open,
} from "node:fs/promises";
import { createHash, randomUUID } from "node:crypto";
import { doctor, parseOptions } from "./doctor.mjs";
import { unpackArchive } from "./archive.mjs";
import { isDistributableSourcePath } from "./source-inventory.mjs";
import { privateEnvironment, installDependencies } from "./runtime-tools.mjs";
import { buildApplication } from "./build.mjs";
const assets = fileURLToPath(new URL("../assets/", import.meta.url));
const digest = (data) => createHash("sha256").update(data).digest("hex");
const exists = async (file) => {
  try {
    await lstat(file);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
};
export function verifySource(archive, manifest) {
  if (manifest.formatVersion !== 1 || digest(archive) !== manifest.sha256)
    throw Error("source_archive_integrity_failed");
  const files = unpackArchive(archive);
  if (files.size !== Object.keys(manifest.files ?? {}).length)
    throw Error("source_file_inventory_mismatch");
  for (const [name, data] of files) {
    if (
      !isDistributableSourcePath(name) ||
      digest(data) !== manifest.files[name]
    )
      throw Error("source_file_integrity_failed");
  }
  return files;
}
/** --target is the same workspaceRoot used by the runtime and all future task work. */
export async function install({
  workspaceRoot,
  mode,
  port = 18080,
  sourceArchive = path.join(assets, "office-source.tar.gz"),
  sourceManifest = path.join(assets, "source-manifest.json"),
}) {
  if (!workspaceRoot || !["chat", "office"].includes(mode))
    throw Error("Explicit workspaceRoot and mode (chat|office) are required");
  const check = await doctor({ workspaceRoot, port });
  if (!check.ok) throw Error(check.failures.join("; "));
  const root = check.workspaceRoot;
  for (const name of ["app", "scripts", "workspace.json", "skills", "restart-office.sh", "company", "VERSION"])
    if (await exists(path.join(root, name)))
      throw Error(
        `managed_path_already_exists: ${name}; use the update command for an existing installation`,
      );
  const archive = await readFile(sourceArchive),
    manifestBytes = await readFile(sourceManifest),
    manifest = JSON.parse(manifestBytes);
  const files = verifySource(archive, manifest);
  for (const name of ["cache", "tmp", ".install-staging"]) {
    const dest = path.join(root, name);
    await mkdir(dest, { recursive: true, mode: 0o700 });
    if ((await realpath(dest)) !== dest)
      throw Error("workspace_symlink_not_allowed");
  }
  const lockFile = path.join(root, ".install-staging/install.lock");
  let lock;
  try {
    lock = await open(lockFile, "wx", 0o600);
  } catch {
    throw Error("install_already_in_progress");
  }
  const stage = path.join(root, ".install-staging", `install-${randomUUID()}`),
    activated = [];
  try {
    await mkdir(stage, { recursive: true, mode: 0o700 });
    for (const [name, data] of files) {
      const dest = path.join(stage, name);
      await mkdir(path.dirname(dest), { recursive: true, mode: 0o700 });
      await writeFile(dest, data, { mode: 0o600 });
    }
    const app = path.join(stage, "app");
    // Preserve a complete reusable skill in the installed workspace as well.
    // The tar itself excludes these assets to avoid recursive self-packaging.
    const installedAssets = path.join(stage, "skills/web-office-skills/assets");
    await mkdir(installedAssets, { recursive: true, mode: 0o700 });
    await writeFile(
      path.join(installedAssets, "office-source.tar.gz"),
      archive,
      { mode: 0o600 },
    );
    await writeFile(
      path.join(installedAssets, "source-manifest.json"),
      manifestBytes,
      { mode: 0o600 },
    );
    const env = await privateEnvironment(root);
    await installDependencies(root, app, env);
    const built = await buildApplication({
      workspaceRoot: root,
      appDirectory: app,
      outDirectory: path.join(stage, "frontend-built"),
      inspectRuntime: false,
    });
    await rename(built.outDirectory, path.join(app, "dist-office"));
    for (const name of ["app", "scripts", "skills", "company"]) {
      if (!(await exists(path.join(stage, name)))) continue;
      await rename(path.join(stage, name), path.join(root, name));
      activated.push(name);
    }
    await rename(path.join(stage, "restart-office.sh"), path.join(root, "restart-office.sh"));
    activated.push("restart-office.sh");
    if (await exists(path.join(stage, "VERSION"))) {
      await rename(path.join(stage, "VERSION"), path.join(root, "VERSION"));
      activated.push("VERSION");
    }
    for (const name of [
      "state",
      "logs",
      "projects",
      "agents",
      "shared",
      "artifacts",
      "releases",
    ]) {
      await mkdir(path.join(root, name), { recursive: true, mode: 0o700 });
      if ((await realpath(path.join(root, name))) !== path.join(root, name))
        throw Error("workspace_symlink_not_allowed");
    }
    const config = {
      workspaceRoot: root,
      defaultView: mode,
      port,
      installed: true,
      officeVersion: manifest.officeVersion ?? null,
      sourceVersion: manifest.appVersion,
      sourceSha256: manifest.sha256,
      upstreamCommit: manifest.upstreamCommit,
    };
    await writeFile(
      path.join(stage, "workspace.json"),
      JSON.stringify(config, null, 2) + "\n",
      { mode: 0o600 },
    );
    await rename(
      path.join(stage, "workspace.json"),
      path.join(root, "workspace.json"),
    );
    return config;
  } catch (error) {
    for (const name of activated.reverse())
      await rm(path.join(root, name), { recursive: true, force: true });
    throw error;
  } finally {
    await rm(stage, { recursive: true, force: true });
    await lock.close();
    await rm(lockFile, { force: true });
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  try {
    const o = parseOptions(process.argv.slice(2), [
      "target",
      "mode",
      "port",
      "source-archive",
      "source-manifest",
    ]);
    const result = await install({
      workspaceRoot: o.target,
      mode: o.mode,
      port: Number(o.port ?? 18080),
      sourceArchive: o["source-archive"],
      sourceManifest: o["source-manifest"],
    });
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  } catch (error) {
    process.stderr.write(
      (/^[a-z0-9_]+$/.test(error.message)
        ? error.message
        : "installation_failed") + "\n",
    );
    process.exitCode = 1;
  }
}
