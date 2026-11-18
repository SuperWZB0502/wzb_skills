#!/usr/bin/env node
import path from "node:path";
import net from "node:net";
import { spawnSync } from "node:child_process";
import { mkdir, realpath, open, rm } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { managedPath, readManaged } from "./runtime-tools.mjs";
export function parseOptions(argv, allowed) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, "");
    if (
      !argv[i]?.startsWith("--") ||
      !allowed.includes(key) ||
      key in options ||
      !argv[i + 1] ||
      argv[i + 1].startsWith("--")
    )
      throw Error("invalid_options");
    options[key] = argv[i + 1];
  }
  if (!options.target)
    throw Error(
      "--target is required after explicitly choosing the workspace directory",
    );
  return options;
}
export async function doctor({ workspaceRoot, port = 18080 }) {
  if (!workspaceRoot) throw Error("workspace_root_required");
  const root = path.resolve(workspaceRoot);
  let ancestor = root;
  for (;;) {
    try {
      if ((await realpath(ancestor)) !== ancestor)
        throw Error("workspace_symlink_not_allowed");
      break;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      const parent = path.dirname(ancestor);
      if (parent === ancestor) throw error;
      ancestor = parent;
    }
  }
  await mkdir(root, { recursive: true, mode: 0o700 });
  const canonical = await realpath(root);
  if (canonical !== root) throw Error("workspace_symlink_not_allowed");
  const failures = [];
  if (Number(process.versions.node.split(".")[0]) < 22)
    failures.push("Node.js 22 or newer is required");
  try {
    await import("node:sqlite");
  } catch {
    failures.push("This Node.js build must provide node:sqlite");
  }
  if (!["linux", "darwin", "win32"].includes(process.platform))
    failures.push("Unsupported operating system");
  if (process.platform === "win32")
    failures.push("Unix control sockets require Linux/macOS or WSL");
  const probe = path.join(root, `.doctor-${process.pid}`);
  try {
    const handle = await open(probe, "wx", 0o600);
    await handle.close();
    await rm(probe);
  } catch {
    failures.push("Workspace is not writable");
  }
  const inspection = path.join(root, "state", "doctor");
  for (const name of ["home", "codex", "claude", "tmp"])
    await managedPath(root, path.join(inspection, name), {
      createDirectory: true,
    });
  const toolEnv = {
    PATH: process.env.PATH,
    LANG: process.env.LANG ?? "C.UTF-8",
    HOME: path.join(inspection, "home"),
    CODEX_HOME: path.join(inspection, "codex"),
    CLAUDE_CONFIG_DIR: path.join(inspection, "claude"),
    TMPDIR: path.join(inspection, "tmp"),
    TMP: path.join(inspection, "tmp"),
    TEMP: path.join(inspection, "tmp"),
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    DISABLE_AUTOUPDATER: "1",
  };
  let configuredClaude;
  let configuredCodex;
  try {
    const configuration = JSON.parse(await readManaged(root, "workspace.json"));
    if (typeof configuration.cliPaths?.["claude-code"] === "string")
      configuredClaude = configuration.cliPaths["claude-code"];
    if (typeof configuration.cliPaths?.codex === "string")
      configuredCodex = path.resolve(root, configuration.cliPaths.codex);
  } catch (error) {
    if (error.code !== "ENOENT" && error.message !== "managed_path_missing")
      throw Error("unsafe_or_invalid_workspace_configuration");
  }
  // Same precedence as the runtime resolver: CODEX_CLI, workspace cliPaths.codex, then PATH.
  let codexVersion = null;
  let codexBinary = null;
  for (const candidate of [process.env.CODEX_CLI, configuredCodex, "codex"].filter(Boolean)) {
    const value = spawnSync(candidate, ["--version"], {
      encoding: "utf8",
      env: toolEnv,
      timeout: 10000,
    });
    if (value.status === 0) {
      codexVersion = value.stdout.trim();
      codexBinary = candidate;
      break;
    }
  }
  const claudeCandidates = [
    process.env.CLAUDE_CLI,
    configuredClaude,
    "claude",
  ].filter(Boolean);
  let claudeVersion = null;
  for (const binary of claudeCandidates) {
    const value = spawnSync(binary, ["--version"], {
      encoding: "utf8",
      env: toolEnv,
      timeout: 10000,
    });
    if (value.status === 0) {
      claudeVersion = value.stdout.trim();
      break;
    }
  }
  if (!codexVersion && !claudeVersion)
    failures.push(
      "Install Codex CLI or Claude Code; the selected service requires its matching CLI (CLAUDE_CLI may select a private Claude binary)",
    );
  const portAvailable = await new Promise((resolve) => {
    if (!Number.isInteger(port) || port < 1 || port > 65535)
      return resolve(false);
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
  });
  if (!portAvailable)
    failures.push("Port is unavailable; choose a different port");
  return {
    ok: failures.length === 0,
    workspaceRoot: root,
    nodeVersion: process.version,
    codexVersion,
    codexBinary,
    claudeVersion,
    platform: process.platform,
    arch: process.arch,
    portAvailable,
    failures,
  };
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  try {
    const options = parseOptions(process.argv.slice(2), ["target", "port"]);
    const result = await doctor({
      workspaceRoot: options.target,
      port: Number(options.port ?? 18080),
    });
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
    if (!result.ok) process.exitCode = 1;
  } catch (error) {
    process.stderr.write(
      (/^[a-z0-9_]+$/.test(error.message)
        ? error.message
        : "environment_check_failed") + "\n",
    );
    process.exitCode = 1;
  }
}
