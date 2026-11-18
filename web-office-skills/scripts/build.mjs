#!/usr/bin/env node
import path from "node:path";
import { pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";
import { readdir, lstat } from "node:fs/promises";
import { parseOptions } from "./doctor.mjs";
import {
  canonicalRoot,
  managedPath,
  privateEnvironment,
  installDependencies,
  runCommand,
  backendState,
  assertEntryIsolation,
  inventory,
  atomicWrite,
  runtimeStatus,
  RECEIPT_NAME,
} from "./runtime-tools.mjs";

/** Shared by installation and later builds; this never activates a live frontend. */
export async function buildApplication({
  workspaceRoot,
  appDirectory,
  outDirectory,
  inspectRuntime = true,
}) {
  const root = await canonicalRoot(workspaceRoot);
  const app = await managedPath(root, appDirectory ?? "app");
  const out = path.resolve(
    root,
    outDirectory ??
      path.join("artifacts", "frontend-builds", `build-${randomUUID()}`),
  );
  const relative = path.relative(root, out);
  if (
    !relative ||
    relative === ".." ||
    relative.startsWith(".." + path.sep) ||
    path.isAbsolute(relative) ||
    [
      "app",
      "state",
      "projects",
      "agents",
      "shared",
      "logs",
      "cache",
      "tmp",
      "releases",
      "scripts",
      "skills",
      "docs",
    ].includes(relative.split(path.sep)[0])
  )
    throw Error("staged_build_output_required");
  await managedPath(root, out, { createDirectory: true });
  if ((await readdir(out)).length)
    throw Error("build_output_must_be_new_or_empty");
  const env = await privateEnvironment(root);
  let ready = true;
  for (const name of ["typescript/bin/tsc", "vite/bin/vite.js"]) {
    try {
      const resolved = await import("node:fs/promises").then((fs) =>
        fs.realpath(path.join(app, "node_modules", name)),
      );
      if (
        !resolved.startsWith(root + path.sep) ||
        !(await lstat(resolved)).isFile()
      )
        throw Error();
    } catch {
      ready = false;
    }
  }
  if (!ready) await installDependencies(root, app, env);
  const before = await backendState(root, app);
  const runtime = inspectRuntime
    ? await runtimeStatus(root, { optional: true })
    : null;
  await runCommand(
    process.execPath,
    [path.join(app, "node_modules/typescript/bin/tsc"), "-b"],
    { cwd: app, env, label: "typecheck" },
  );
  await runCommand(
    process.execPath,
    [
      path.join(app, "node_modules/vite/bin/vite.js"),
      "build",
      "--config",
      "vite.office.config.ts",
      "--outDir",
      out,
      "--emptyOutDir",
    ],
    { cwd: app, env, label: "frontend_build" },
  );
  const isolation = await assertEntryIsolation(root, out);
  const backend = await backendState(root, app);
  if (before.sha256 !== backend.sha256)
    throw Error("backend_changed_during_build");
  const files = await inventory(root, out);
  const receipt = {
    formatVersion: 1,
    kind: "office-frontend-build",
    workspaceRoot: root,
    appRelative: path.relative(root, app).split(path.sep).join("/"),
    buildRelative: relative.split(path.sep).join("/"),
    builtAt: new Date().toISOString(),
    nodeVersion: process.version,
    typecheck: "passed",
    isolation,
    backend,
    files,
    runtime: runtime
      ? { pid: runtime.pid, bootId: runtime.bootId, version: runtime.version }
      : null,
  };
  await atomicWrite(
    root,
    path.join(out, RECEIPT_NAME),
    JSON.stringify(receipt, null, 2) + "\n",
  );
  return { outDirectory: out, receipt };
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  try {
    const options = parseOptions(process.argv.slice(2), ["target", "out"]);
    const root = await canonicalRoot(options.target);
    const result = await buildApplication({
      workspaceRoot: root,
      outDirectory: options.out,
    });
    process.stdout.write(
      JSON.stringify(
        {
          ok: true,
          buildDirectory: result.outDirectory,
          receipt: path.join(result.outDirectory, RECEIPT_NAME),
          typecheck: "passed",
          entryIsolation: "passed",
          activated: false,
        },
        null,
        2,
      ) + "\n",
    );
  } catch (error) {
    process.stderr.write(
      (/^[a-z0-9_]+$/.test(error.message)
        ? error.message
        : "frontend_build_failed") + "\n",
    );
    process.exitCode = 1;
  }
}
