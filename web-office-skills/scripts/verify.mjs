#!/usr/bin/env node
import path from "node:path";
import { pathToFileURL } from "node:url";
import { readFile, readdir, lstat } from "node:fs/promises";
import { verifySource } from "./install.mjs";
import { readZip } from "./zip.mjs";
import { collectOfficeSource } from "./source-inventory.mjs";
import {
  canonicalRoot,
  managedPath,
  readManaged,
  atomicWrite,
  digest,
} from "./runtime-tools.mjs";

export const requiredFiles = [
  "app/package.json",
  "app/pnpm-lock.yaml",
  "app/LICENSE",
  "app/office-upstream.json",
  "app/chat.html",
  "app/office.html",
  "app/vite.office.config.ts",
  "app/tsconfig.json",
  "app/tsconfig.app.json",
  "app/tsconfig.node.json",
  "scripts/office.mjs",
  "restart-office.sh",
  "app/src/entries/chat.tsx",
  "app/src/entries/office.tsx",
  ...[
    "ChatWorkspace.tsx",
    "OfficeCanvas.tsx",
    "AgentEditor.tsx",
    "AgentChatDialog.tsx",
    "ChatModelControls.tsx",
    "ClaudeContextControl.tsx",
    "CompanyKnowledgeDialog.tsx",
    "ProviderList.tsx",
    "ServiceDialog.tsx",
    "AccountAuthDialog.tsx",
    "MeetingDialog.tsx",
    "ArchivedAgentsDialog.tsx",
    "TaskProgressPanel.tsx",
    "useChatAttachments.ts",
    "MessageBody.tsx",
    "MarkdownCodeBlock.tsx",
    "copy-code-text.ts",
    "useReaderFullscreen.ts",
    "theme.css",
    "message-markdown.css",
  ].map((name) => `app/src/office/${name}`),
  ...[
    "main.ts",
    "runtime-host.ts",
    "run-registry.ts",
    "model-proxy.ts",
    "model-catalog.ts",
    "claude-executor.ts",
    "claude-model-normalization.ts",
    "codex-account-driver.ts",
    "service-runtime.ts",
    "credential-vault.ts",
    "agent-repository.ts",
    "conversation-context.ts",
    "conversation-deletion.ts",
    "team-mcp.mjs",
  ].map((name) => `app/server/office/${name}`),
  "skills/web-office-skills/README.md",
  "skills/web-office-skills/SKILL.md",
  ...[
    "doctor.mjs",
    "install.mjs",
    "build.mjs",
    "publish-frontend.mjs",
    "package.mjs",
    "verify.mjs",
    "privacy-scan.mjs",
    "archive.mjs",
    "zip.mjs",
    "runtime-tools.mjs",
    "source-inventory.mjs",
    "privacy-rules.mjs",
  ].map((name) => `skills/web-office-skills/scripts/${name}`),
];
const outerAllowed =
  /^(?:README\.md|SKILL\.md|scripts\/[^/]+\.mjs|references\/[^/]+\.md|agents\/openai\.yaml|assets\/(?:office-source\.tar\.gz|source-manifest\.json|THIRD_PARTY_NOTICES\.md))$/;
export async function skillFiles(skillDirectory) {
  const skill = await canonicalRoot(skillDirectory),
    files = new Map();
  const walk = async (directory, prefix = "") => {
    for (const name of (await readdir(directory)).sort()) {
      // 仓库自身的 .git/.gitignore 等元数据、以及项目特性文档 docs/ 不属于 skill 内容
      if (name.startsWith(".") || name === "docs") continue;
      const relative = prefix ? `${prefix}/${name}` : name;
      const full = await managedPath(skill, relative),
        info = await lstat(full);
      if (info.isDirectory()) await walk(full, relative);
      else if (info.isFile() && outerAllowed.test(relative))
        files.set(relative, await readManaged(skill, full));
      else throw Error("unexpected_skill_file");
    }
  };
  await walk(skill);
  return files;
}

/** Offline verification only: never installs, starts services or calls a model. */
export async function verifyDelivery({
  skillDirectory,
  workspaceRoot,
  bundle,
  autoBundle = true,
}) {
  const skill = await canonicalRoot(skillDirectory),
    root = workspaceRoot ? await canonicalRoot(workspaceRoot) : null;
  const outer = await skillFiles(skill);
  const archive = outer.get("assets/office-source.tar.gz"),
    metadata = outer.get("assets/source-manifest.json");
  if (!archive || !metadata) throw Error("source_assets_missing");
  const manifest = JSON.parse(metadata),
    files = verifySource(archive, manifest);
  for (const name of requiredFiles)
    if (!files.has(name)) throw Error("required_source_component_missing");
  const sprites = [...files.keys()].filter((name) =>
    /^app\/public\/sprites\/.+\.png$/.test(name),
  );
  if (!sprites.length) throw Error("sprite_assets_missing");
  const sourceSkillPrefix = "skills/web-office-skills/";
  for (const [name, data] of outer) {
    if (
      ["assets/source-manifest.json", "assets/office-source.tar.gz"].includes(name)
    )
      continue;
    const embedded = files.get(sourceSkillPrefix + name);
    if (!embedded || !embedded.equals(data))
      throw Error("outer_embedded_skill_mismatch");
  }
  for (const name of files.keys())
    if (
      name.startsWith(sourceSkillPrefix) &&
      !outer.has(name.slice(sourceSkillPrefix.length))
    )
      throw Error("embedded_outer_skill_mismatch");
  if (root) {
    const current = await collectOfficeSource(root, { skillDirectory: skill });
    if (
      current.size !== files.size ||
      [...current.keys()].some((name) => !files.has(name))
    )
      throw Error("workspace_source_inventory_mismatch");
    for (const [name, data] of files)
      if (!data.equals(current.get(name)))
        throw Error("workspace_source_mismatch");
  }
  let zip = null;
  let selectedBundle = bundle;
  let declaredDelivery = null;
  if (!selectedBundle && root && autoBundle) {
    try {
      const delivery = JSON.parse(
        await readManaged(root, "artifacts/skill-package.json"),
      );
      if (
        typeof delivery.bundle !== "string" ||
        path.basename(delivery.bundle) !== delivery.bundle ||
        !delivery.bundle.endsWith(".zip")
      )
        throw Error("invalid_delivery_metadata");
      if (
        !/^[a-f0-9]{64}$/.test(delivery.sha256 ?? "") ||
        !Number.isSafeInteger(delivery.bytes) ||
        delivery.bytes <= 0 ||
        delivery.sourceSha256 !== manifest.sha256 ||
        delivery.sourceFileCount !== files.size
      )
        throw Error("invalid_delivery_metadata");
      declaredDelivery = delivery;
      selectedBundle = path.join(root, "artifacts", delivery.bundle);
    } catch (error) {
      if (error.code !== "ENOENT" && error.message !== "managed_path_missing")
        throw error;
    }
  }
  if (selectedBundle) {
    const zipPath = root
      ? await managedPath(root, selectedBundle)
      : path.resolve(selectedBundle);
    const info = await lstat(zipPath);
    if (
      !info.isFile() ||
      info.isSymbolicLink() ||
      info.size > 256 * 1024 * 1024
    )
      throw Error("invalid_bundle_file");
    const bytes = root
      ? await readManaged(root, zipPath)
      : await readFile(zipPath);
    if (
      declaredDelivery &&
      (digest(bytes) !== declaredDelivery.sha256 ||
        bytes.length !== declaredDelivery.bytes)
    )
      throw Error("delivery_metadata_hash_mismatch");
    const entries = readZip(bytes);
    if (entries.size !== outer.size) throw Error("zip_file_inventory_mismatch");
    for (const [name, data] of outer)
      if (!entries.get(`web-office-skills/${name}`)?.equals(data))
        throw Error("zip_skill_mismatch");
    zip = { sha256: digest(bytes), bytes: bytes.length, files: entries.size };
  }
  return {
    ok: true,
    verification: "offline-structure-and-sha256",
    sourceSha256: manifest.sha256,
    sourceFiles: files.size,
    outerFiles: outer.size,
    spriteAssets: sprites.length,
    requiredComponents: requiredFiles.length,
    outerEmbeddedParity: true,
    workspaceParity: root ? true : null,
    zip,
    excludesRuntimeData: true,
    limitations: [
      "Does not execute installation, builds, browser interactions or real model calls.",
      "CLI binaries and package-manager downloads are machine prerequisites, not embedded credentials or dependencies.",
    ],
  };
}
export function namedOptions(argv, allowed) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index]?.replace(/^--/, "");
    if (
      !argv[index]?.startsWith("--") ||
      !allowed.includes(key) ||
      key in result ||
      !argv[index + 1] ||
      argv[index + 1].startsWith("--")
    )
      throw Error("invalid_options");
    result[key] = argv[index + 1];
  }
  return result;
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  try {
    const options = namedOptions(process.argv.slice(2), [
      "skill",
      "root",
      "bundle",
      "report",
    ]);
    if (!options.skill) throw Error("skill_directory_required");
    const result = await verifyDelivery({
      skillDirectory: options.skill,
      workspaceRoot: options.root,
      bundle: options.bundle,
    });
    if (options.report) {
      if (!options.root) throw Error("report_requires_workspace_root");
      await atomicWrite(
        await canonicalRoot(options.root),
        options.report,
        JSON.stringify(result, null, 2) + "\n",
      );
    }
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  } catch (error) {
    process.stderr.write(
      (/^[a-z0-9_]+$/.test(error.message)
        ? error.message
        : "delivery_verification_failed") + "\n",
    );
    process.exitCode = 1;
  }
}
