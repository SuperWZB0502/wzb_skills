#!/usr/bin/env node
import path from "node:path";
import { pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";
import { open, unlink, lstat } from "node:fs/promises";
import { constants } from "node:fs";
import { parseOptions } from "./doctor.mjs";
import {
  canonicalRoot,
  managedPath,
  readManaged,
  atomicWrite,
  inventory,
  backendState,
  assertEntryIsolation,
  runtimeStatus,
  digest,
  missing,
  RECEIPT_NAME,
} from "./runtime-tools.mjs";

const sameRuntime = (a, b) =>
  a.pid === b.pid &&
  a.bootId === b.bootId &&
  a.origin === b.origin &&
  b.version === "builtin-v1";
async function responseBytes(origin, relative, limit = 32 * 1024 * 1024) {
  const suffix = relative.split("/").map(encodeURIComponent).join("/");
  const response = await fetch(`${origin}/${suffix}`, {
    redirect: "error",
    signal: AbortSignal.timeout(10000),
    headers: { "cache-control": "no-cache" },
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw Error("frontend_http_validation_failed");
  }
  if (Number(response.headers.get("content-length") ?? "0") > limit) {
    await response.body?.cancel();
    throw Error("frontend_http_response_too_large");
  }
  const reader = response.body?.getReader();
  if (!reader) throw Error("frontend_http_validation_failed");
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      size += result.value.length;
      if (size > limit) {
        await reader.cancel();
        throw Error("frontend_http_response_too_large");
      }
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks);
}
async function ensureBuiltinDirectory(root, runtime, live) {
  if (runtime.version !== "builtin-v1")
    throw Error("active_frontend_is_release_use_office_update");
  const name = `office-publish-probe-${randomUUID()}.txt`,
    nonce = randomUUID();
  const probe = path.join(live, name);
  await atomicWrite(root, probe, nonce);
  try {
    if ((await responseBytes(runtime.origin, name, 1024)).toString() !== nonce)
      throw Error();
  } catch {
    throw Error("active_frontend_directory_unverified_use_office_update");
  } finally {
    await managedPath(root, probe);
    await unlink(probe);
  }
}
/** Explicit manual frontend-only publication; never restarts a service or edits backend/state/user work. */
export async function publishFrontend({ workspaceRoot, buildDirectory }) {
  const root = await canonicalRoot(workspaceRoot);
  if (!buildDirectory) throw Error("explicit_build_directory_required");
  const build = await managedPath(root, buildDirectory);
  const live = await managedPath(root, "app/dist-office");
  if (build === live || build.startsWith(live + path.sep))
    throw Error("staged_build_required");
  const receipt = JSON.parse(
    await readManaged(root, path.join(build, RECEIPT_NAME)),
  );
  if (
    receipt.formatVersion !== 1 ||
    receipt.kind !== "office-frontend-build" ||
    receipt.workspaceRoot !== root ||
    receipt.appRelative !== "app" ||
    receipt.buildRelative !==
      path.relative(root, build).split(path.sep).join("/") ||
    receipt.typecheck !== "passed" ||
    !receipt.files ||
    typeof receipt.files !== "object" ||
    Array.isArray(receipt.files)
  )
    throw Error("build_receipt_invalid");
  const files = await inventory(root, build, { omit: new Set([RECEIPT_NAME]) });
  if (
    JSON.stringify(files) !== JSON.stringify(receipt.files) ||
    !Object.values(files).every((value) => /^[a-f0-9]{64}$/.test(value))
  )
    throw Error("build_receipt_hash_mismatch");
  await assertEntryIsolation(root, build);
  const backend = await backendState(root);
  if (backend.sha256 !== receipt.backend?.sha256)
    throw Error("backend_changed_rebuild_required");
  const initial = await runtimeStatus(root);
  if (
    receipt.runtime &&
    (receipt.runtime.pid !== initial.pid ||
      receipt.runtime.bootId !== initial.bootId)
  )
    throw Error("runtime_changed_since_build_rebuild_required");
  if (backend.latestMtimeMs > initial.metadataMtimeMs)
    throw Error("backend_modified_since_start_use_restart_or_release_update");
  await managedPath(root, ".install-staging", { createDirectory: true });
  const lockPath = await managedPath(
    root,
    ".install-staging/frontend-publish.lock",
    { allowMissing: true },
  );
  let lock;
  try {
    lock = await open(
      lockPath,
      constants.O_CREAT |
        constants.O_EXCL |
        constants.O_WRONLY |
        constants.O_NOFOLLOW,
      0o600,
    );
  } catch {
    throw Error("frontend_publish_already_in_progress");
  }
  const publicationId = `frontend-${randomUUID()}`;
  const backup = path.join(
    root,
    "artifacts",
    "frontend-backups",
    publicationId,
  );
  const originals = new Map(),
    created = new Set();
  let touched = false;
  try {
    await ensureBuiltinDirectory(root, initial, live);
    const oldFiles = await inventory(root, live);
    await managedPath(root, backup, { createDirectory: true });
    for (const relative of Object.keys(oldFiles))
      await atomicWrite(
        root,
        path.join(backup, relative),
        await readManaged(root, path.join(live, relative)),
      );
    await atomicWrite(
      root,
      path.join(backup, ".office-backup.json"),
      JSON.stringify(
        {
          formatVersion: 1,
          publicationId,
          pid: initial.pid,
          bootId: initial.bootId,
          files: oldFiles,
        },
        null,
        2,
      ) + "\n",
    );
    if (!sameRuntime(initial, await runtimeStatus(root)))
      throw Error("runtime_changed_during_publication");
    const publishFile = async (relative) => {
      const destination = await managedPath(root, path.join(live, relative), {
        allowMissing: true,
      });
      let previous;
      try {
        previous = await readManaged(root, destination);
      } catch (error) {
        if (error.message !== "managed_path_missing" && !missing(error))
          throw error;
      }
      const next = await readManaged(root, path.join(build, relative));
      if (previous && digest(previous) === digest(next)) return;
      if (
        previous &&
        /^assets\/.*-[a-zA-Z0-9_-]{6,}\.(?:js|css)$/.test(relative)
      )
        throw Error("hashed_asset_name_collision");
      if (previous) originals.set(relative, previous);
      else created.add(relative);
      touched = true;
      await atomicWrite(root, destination, next);
    };
    // All new resources exist before either entry document can reference them.
    for (const relative of Object.keys(files).filter(
      (name) => !name.endsWith(".html"),
    ))
      await publishFile(relative);
    for (const relative of Object.keys(files).filter((name) =>
      name.endsWith(".html"),
    )) {
      if (!sameRuntime(initial, await runtimeStatus(root)))
        throw Error("runtime_changed_during_publication");
      await publishFile(relative);
    }
    for (const relative of Object.keys(files).filter((name) =>
      /\.(?:html|js|css)$/.test(name),
    ))
      if (
        digest(await responseBytes(initial.origin, relative)) !==
        files[relative]
      )
        throw Error("frontend_http_hash_mismatch");
    const health = JSON.parse(
      (await responseBytes(initial.origin, "healthz", 8192)).toString(),
    );
    const final = await runtimeStatus(root);
    if (
      health.ok !== true ||
      !sameRuntime(initial, final) ||
      (await backendState(root)).sha256 !== backend.sha256
    )
      throw Error("runtime_or_backend_changed_during_publication");
    const result = {
      ok: true,
      publicationId,
      backupDirectory: backup,
      buildDirectory: build,
      pid: final.pid,
      bootId: final.bootId,
      restarted: false,
      retainedOldAssets: true,
      publishedFiles: Object.keys(files).length,
    };
    await atomicWrite(
      root,
      path.join(
        root,
        "artifacts",
        "frontend-publications",
        `${publicationId}.json`,
      ),
      JSON.stringify(result, null, 2) + "\n",
    );
    return result;
  } catch (error) {
    if (touched) {
      // Restore only files this attempt replaced. Preserve new hashed assets for
      // any clients that fetched the brief intermediate HTML before rollback.
      for (const [relative, previous] of [...originals].reverse())
        await atomicWrite(root, path.join(live, relative), previous);
      for (const relative of created)
        if (relative.endsWith(".html")) {
          const candidate = await managedPath(root, path.join(live, relative), {
            allowMissing: true,
          });
          try {
            const info = await lstat(candidate);
            if (!info.isFile() || info.nlink !== 1) throw Error();
            await unlink(candidate);
          } catch (failure) {
            if (!missing(failure))
              throw Error("frontend_publication_rollback_failed");
          }
        }
    }
    throw error;
  } finally {
    await lock.close();
    await managedPath(root, lockPath);
    await unlink(lockPath);
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  try {
    const options = parseOptions(process.argv.slice(2), ["target", "build"]);
    process.stdout.write(
      JSON.stringify(
        await publishFrontend({
          workspaceRoot: options.target,
          buildDirectory: options.build,
        }),
        null,
        2,
      ) + "\n",
    );
  } catch (error) {
    process.stderr.write(
      (/^[a-z0-9_]+$/.test(error.message)
        ? error.message
        : "frontend_publication_failed") + "\n",
    );
    process.exitCode = 1;
  }
}
