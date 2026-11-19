import path from "node:path";
import net from "node:net";
import { constants } from "node:fs";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  lstat,
  realpath,
  mkdir,
  readFile,
  readdir,
  open,
  rename,
  unlink,
} from "node:fs/promises";

export const PNPM_VERSION = "10.30.1";
export const RECEIPT_NAME = ".office-build-receipt.json";
export const digest = (bytes) =>
  createHash("sha256").update(bytes).digest("hex");
export const missing = (error) => error?.code === "ENOENT";
export async function canonicalRoot(input) {
  if (
    typeof input !== "string" ||
    !path.isAbsolute(input) ||
    input.includes("\0")
  )
    throw Error("absolute_target_required");
  const root = path.resolve(input);
  if ((await realpath(root)) !== root || !(await lstat(root)).isDirectory())
    throw Error("workspace_symlink_not_allowed");
  return root;
}
/** Never accept an inside-root symlink alias for managed inputs/outputs. */
export async function managedPath(
  root,
  input,
  { createDirectory = false, allowMissing = false } = {},
) {
  const target = path.resolve(root, input);
  const relative = path.relative(root, target);
  if (
    relative === ".." ||
    relative.startsWith(".." + path.sep) ||
    path.isAbsolute(relative) ||
    target.includes("\0")
  )
    throw Error("managed_path_outside_workspace");
  let current = root;
  for (const component of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, component);
    let info;
    try {
      info = await lstat(current);
    } catch (error) {
      if (!missing(error)) throw error;
      if (createDirectory) {
        try {
          await mkdir(current, { mode: 0o700 });
        } catch (failure) {
          if (failure.code !== "EEXIST") throw failure;
        }
        info = await lstat(current);
      } else if (allowMissing) return target;
      else throw Error("managed_path_missing");
    }
    if (
      info.isSymbolicLink() ||
      (!info.isDirectory() && current !== target) ||
      (info.isFile() && info.nlink !== 1)
    )
      throw Error("managed_path_link_or_type_forbidden");
  }
  if (createDirectory && !(await lstat(target)).isDirectory())
    throw Error("managed_directory_required");
  return target;
}
export async function readManaged(root, input) {
  const file = await managedPath(root, input);
  const handle = await open(
    file,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.nlink !== 1)
      throw Error("managed_regular_file_required");
    return await handle.readFile();
  } finally {
    await handle.close();
  }
}
export async function atomicWrite(root, input, data) {
  const target = await managedPath(root, input, { allowMissing: true });
  const parent = await managedPath(root, path.dirname(target), {
    createDirectory: true,
  });
  const temporary = path.join(parent, `.office-write-${randomUUID()}`);
  const handle = await open(
    temporary,
    constants.O_CREAT |
      constants.O_EXCL |
      constants.O_WRONLY |
      constants.O_NOFOLLOW,
    0o600,
  );
  try {
    await handle.writeFile(data);
    await handle.sync();
    await managedPath(root, target, { allowMissing: true });
    await rename(temporary, target);
    const directory = await open(
      parent,
      constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
    );
    try {
      await directory.sync();
    } catch (error) {
      if (process.platform !== "win32" || error.code !== "EPERM" || error.syscall !== "fsync")
        throw error;
    } finally {
      await directory.close();
    }
  } finally {
    await handle.close();
    await unlink(temporary).catch((error) => {
      if (!missing(error)) throw error;
    });
  }
}
export async function inventory(
  root,
  directory,
  { omit = new Set(), prefix = "" } = {},
) {
  const base = await managedPath(root, directory);
  const files = {};
  for (const name of (await readdir(base)).sort()) {
    const relative = prefix ? `${prefix}/${name}` : name;
    if (omit.has(relative)) continue;
    const full = await managedPath(root, path.join(base, name));
    const info = await lstat(full);
    if (info.isDirectory())
      Object.assign(
        files,
        await inventory(root, full, { omit, prefix: relative }),
      );
    else if (info.isFile())
      files[relative] = digest(await readManaged(root, full));
    else throw Error("inventory_special_file_forbidden");
  }
  return files;
}
export async function privateEnvironment(root) {
  for (const name of [
    "cache/runtime-tools/home",
    "cache/runtime-tools/config",
    "cache/runtime-tools/data",
    "cache/runtime-tools/state",
    "cache/corepack",
    "cache/pnpm",
    "cache/npm",
    "cache/pnpm-store",
    "cache/node",
    "tmp/runtime-tools",
  ])
    await managedPath(root, name, { createDirectory: true });
  for (const name of ["npmrc", "npm-globalrc"])
    await atomicWrite(root, `cache/runtime-tools/${name}`, "");
  return {
    PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
    LANG: "C.UTF-8",
    TZ: process.env.TZ ?? "UTC",
    CI: "1",
    NO_COLOR: "1",
    FORCE_COLOR: "0",
    HOME: path.join(root, "cache/runtime-tools/home"),
    XDG_CONFIG_HOME: path.join(root, "cache/runtime-tools/config"),
    XDG_DATA_HOME: path.join(root, "cache/runtime-tools/data"),
    XDG_STATE_HOME: path.join(root, "cache/runtime-tools/state"),
    XDG_CACHE_HOME: path.join(root, "cache"),
    COREPACK_HOME: path.join(root, "cache/corepack"),
    PNPM_HOME: path.join(root, "cache/pnpm"),
    npm_config_cache: path.join(root, "cache/npm"),
    npm_config_prefix: path.join(root, "cache/runtime-tools/npm-prefix"),
    npm_config_userconfig: path.join(root, "cache/runtime-tools/npmrc"),
    npm_config_globalconfig: path.join(
      root,
      "cache/runtime-tools/npm-globalrc",
    ),
    TMPDIR: path.join(root, "tmp/runtime-tools"),
    TMP: path.join(root, "tmp/runtime-tools"),
    TEMP: path.join(root, "tmp/runtime-tools"),
    NODE_COMPILE_CACHE: path.join(root, "cache/node"),
  };
}
/** Child output is not echoed or persisted: package-manager diagnostics can contain registry credentials. */
export async function runCommand(
  command,
  args,
  { cwd, env, label = "command", timeoutMs = 15 * 60 * 1000 } = {},
) {
  if (!env || !cwd) throw Error("private_runtime_environment_required");
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
    });
    let output = "",
      settled = false;
    const finish = (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      error ? reject(error) : resolve(output);
    };
    const timer = setTimeout(() => {
      try {
        if (child.pid && process.platform !== "win32")
          process.kill(-child.pid, "SIGKILL");
        else child.kill("SIGKILL");
      } catch {
        /* Already exited. */
      }
      finish(Error(`${label}_timeout`));
    }, timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (text) => {
      if (output.length < 65536) output += text.slice(0, 65536 - output.length);
    });
    child.stderr.resume();
    child.once("error", () => finish(Error(`${label}_unavailable`)));
    child.once("close", (code) =>
      finish(code === 0 ? null : Error(`${label}_failed`)),
    );
  });
}
async function validPnpm(root, file) {
  try {
    const resolved = await realpath(file);
    if (!resolved.startsWith(root + path.sep)) return false;
    const metadata = JSON.parse(
      await readFile(
        path.join(path.dirname(resolved), "../package.json"),
        "utf8",
      ),
    );
    return metadata.name === "pnpm" && metadata.version === PNPM_VERSION;
  } catch {
    return false;
  }
}
export async function pnpmRunner(root, app, env) {
  const candidates = [
    path.join(root, "cache/corepack/v1/pnpm", PNPM_VERSION, "bin/pnpm.cjs"),
    path.join(app, "node_modules/pnpm/bin/pnpm.cjs"),
  ];
  const npx = path.join(root, "cache/npm/_npx");
  try {
    for (const name of (await readdir(await managedPath(root, npx))).sort())
      candidates.push(path.join(npx, name, "node_modules/pnpm/bin/pnpm.cjs"));
  } catch (error) {
    if (!missing(error) && error.message !== "managed_path_missing")
      throw error;
  }
  for (const candidate of candidates)
    if (await validPnpm(root, candidate))
      return {
        command: process.execPath,
        prefix: [await realpath(candidate)],
        version: PNPM_VERSION,
      };
  try {
    if (
      (
        await runCommand("pnpm", ["--version"], {
          cwd: app,
          env,
          label: "pnpm_version",
          timeoutMs: 30000,
        })
      ).trim() === PNPM_VERSION
    )
      return { command: "pnpm", prefix: [], version: PNPM_VERSION };
  } catch {
    /* Use the pinned bootstrap path. */
  }
  try {
    await runCommand("corepack", ["--version"], {
      cwd: app,
      env,
      label: "corepack_version",
      timeoutMs: 10000,
    });
    return {
      command: "corepack",
      prefix: [`pnpm@${PNPM_VERSION}`],
      version: PNPM_VERSION,
    };
  } catch {
    return {
      command: "npm",
      prefix: ["exec", "--yes", `--package=pnpm@${PNPM_VERSION}`, "--", "pnpm"],
      version: PNPM_VERSION,
    };
  }
}
export async function installDependencies(root, app, env = null) {
  env ??= await privateEnvironment(root);
  const runner = await pnpmRunner(root, app, env);
  const version = await runCommand(
    runner.command,
    [...runner.prefix, "--version"],
    { cwd: app, env, label: "pnpm_version", timeoutMs: 180000 },
  );
  if (version.trim() !== PNPM_VERSION)
    throw Error("pinned_pnpm_version_mismatch");
  await runCommand(
    runner.command,
    [
      ...runner.prefix,
      "install",
      "--frozen-lockfile",
      "--store-dir",
      path.join(root, "cache/pnpm-store"),
    ],
    { cwd: app, env, label: "dependency_install" },
  );
  return { pnpmVersion: PNPM_VERSION };
}
export async function backendState(root, app = path.join(root, "app")) {
  const files = {},
    modified = [];
  for (const name of ["server", "shared"])
    for (const [relative, hash] of Object.entries(
      await inventory(root, path.join(app, name)),
    ))
      files[`app/${name}/${relative}`] = hash;
  for (const name of ["package.json", "pnpm-lock.yaml"])
    files[`app/${name}`] = digest(
      await readManaged(root, path.join(app, name)),
    );
  const lifecycle = path.join(path.dirname(app), "scripts/office.mjs");
  try {
    files["scripts/office.mjs"] = digest(await readManaged(root, lifecycle));
  } catch (error) {
    if (error.message !== "managed_path_missing") throw error;
  }
  for (const name of Object.keys(files)) {
    const actual = name.startsWith("app/")
      ? path.join(app, name.slice(4))
      : lifecycle;
    modified.push((await lstat(actual)).mtimeMs);
  }
  return {
    sha256: digest(JSON.stringify(files)),
    files,
    latestMtimeMs: Math.max(0, ...modified),
  };
}
export async function assertEntryIsolation(root, directory) {
  for (const name of [
    "chat.html",
    "office.html",
    ".vite/manifest.json",
    "entry-isolation-audit.json",
  ])
    await readManaged(root, path.join(directory, name));
  const audit = JSON.parse(
    await readManaged(root, path.join(directory, "entry-isolation-audit.json")),
  );
  if (
    !Array.isArray(audit.chat?.modules) ||
    !audit.chat.modules.length ||
    !Array.isArray(audit.office?.modules) ||
    !audit.office.modules.length ||
    audit.chat.modules.some((name) =>
      /pixi|OfficeCanvas|components\/office-view|src\/App\.tsx/i.test(name),
    )
  )
    throw Error("chat_entry_isolation_failed");
  return {
    chatModules: audit.chat.modules.length,
    officeModules: audit.office.modules.length,
    chatContainsOfficeRenderer: false,
  };
}
export async function runtimeStatus(root, { optional = false } = {}) {
  let expected;
  try {
    expected = JSON.parse(await readManaged(root, "state/runtime.json"));
  } catch (error) {
    if (optional && error.message === "managed_path_missing") return null;
    throw Error("runtime_metadata_missing_or_invalid");
  }
  const socketPath = await managedPath(root, "state/run/control.sock");
  if (!(await lstat(socketPath)).isSocket())
    throw Error("runtime_control_socket_invalid");
  let rootHandle;
  try {
    let socket = path.relative(process.cwd(), socketPath);
    if (process.platform === "linux") {
      rootHandle = await open(
        root,
        constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
      );
      socket = `/proc/self/fd/${rootHandle.fd}/state/run/control.sock`;
    }
    if (Buffer.byteLength(socket) > 100)
      throw Error("control_path_too_long_run_from_target");
    const status = await new Promise((resolve, reject) => {
      const connection = net.createConnection({ path: socket });
      let buffer = "",
        finished = false;
      const done = (error, result) => {
        if (finished) return;
        finished = true;
        connection.destroy();
        error ? reject(error) : resolve(result);
      };
      connection.setTimeout(10000, () => done(Error("control_timeout")));
      connection.once("error", () => done(Error("runtime_unreachable")));
      connection.once("end", () => {
        if (!finished) done(Error("invalid_control_response"));
      });
      connection.once("connect", () =>
        connection.write(JSON.stringify({ action: "status", args: {} }) + "\n"),
      );
      connection.on("data", (chunk) => {
        buffer += chunk;
        if (buffer.length > 1024 * 1024)
          return done(Error("invalid_control_response"));
        const end = buffer.indexOf("\n");
        if (end < 0) return;
        try {
          const value = JSON.parse(buffer.slice(0, end));
          if (value.ok !== true) throw Error();
          done(null, value.result);
        } catch {
          done(Error("invalid_control_response"));
        }
      });
    });
    if (
      !expected.bootId ||
      status.bootId !== expected.bootId ||
      status.pid !== expected.pid ||
      status.workspaceRoot !== root ||
      !Number.isInteger(status.pid) ||
      status.pid < 1
    )
      throw Error("runtime_identity_mismatch");
    const origin = new URL(status.origin);
    if (
      origin.protocol !== "http:" ||
      origin.hostname !== "127.0.0.1" ||
      !origin.port ||
      origin.pathname !== "/" ||
      origin.username ||
      origin.password ||
      origin.search ||
      origin.hash
    )
      throw Error("runtime_origin_invalid");
    return {
      pid: status.pid,
      bootId: status.bootId,
      origin: status.origin,
      version: status.version,
      metadataMtimeMs: (await lstat(path.join(root, "state/runtime.json")))
        .mtimeMs,
    };
  } finally {
    await rootHandle?.close();
  }
}
