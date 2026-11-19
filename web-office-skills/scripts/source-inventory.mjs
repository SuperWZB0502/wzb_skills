import path from "node:path";
import { readdir, lstat, readFile } from "node:fs/promises";
import { canonicalRoot, managedPath, readManaged } from "./runtime-tools.mjs";

const excluded =
  /(^|\/)(node_modules|\.git|\.cache|\.tmp|state|logs|artifacts|releases|cache|tmp|\.install-staging|research|dist-office)(\/|$)|(^|\/)(\.env[^/]*|auth\.json|workspace\.json|runtime\.json|[^/]*\.(sqlite|sqlite3|db|sock|tsbuildinfo))$/;
const appRoot =
  /^(?:package\.json|pnpm-lock\.yaml|LICENSE|office-upstream\.json|(?:chat|office|index)\.html|tsconfig(?:\.[a-z]+)?\.json|vite(?:\.office)?\.config\.ts|vitest\.config\.ts)$/;
export function isDistributableSourcePath(name) {
  if (
    typeof name !== "string" ||
    !name ||
    name.includes("\\") ||
    name.includes("\0") ||
    name.startsWith("/") ||
    name.split("/").some((part) => !part || part === "." || part === "..") ||
    excluded.test(name)
  )
    return false;
  // 测试与上游开发/QA 脚本不参与复现运行环境，不进分发包。
  if (/(?:\.test|\.spec)\.[cm]?[jt]sx?$/.test(name)) return false;
  if (/^app\/scripts\//.test(name))
    return /^app\/scripts\/(?:lib\/archive\.mjs|office-backup\.mjs|office-migrate-layout\.mjs|office-repair-codex-paths\.mjs|package-office-release\.mjs|skills-index\.mjs|work-layout\.mjs)$/.test(
      name,
    );
  if (/^app\/(?:src|server|shared|public)\/.+/.test(name)) return true;
  if (name.startsWith("app/") && appRoot.test(name.slice(4))) return true;
  if (/^company\/[A-Za-z0-9._-]+\.md$/.test(name)) return true;
  if (name === "docs/dev-work-layout.md") return true;
  if (name === "VERSION") return true;
  return /^(?:scripts\/office\.mjs|restart-office\.sh|skills\/web-office-skills\/(?:scripts\/[^/]+\.mjs|references\/[^/]+\.md|agents\/openai\.yaml|assets\/THIRD_PARTY_NOTICES\.md|SKILL\.md|README\.md))$/.test(
    name,
  );
}
/**
 * One read-only inventory drives both packaging and reverse completeness checks.
 * `skillDirectory` lets the skill live outside the workspace (e.g. in a project code
 * repository); its files are still archived under skills/web-office-skills/.
 */
export async function collectOfficeSource(workspaceRoot, { skillDirectory } = {}) {
  const root = await canonicalRoot(workspaceRoot),
    files = new Map();
  const skillPrefix = "skills/web-office-skills";
  const walk = async (relative) => {
    if (excluded.test(relative)) return;
    const full = await managedPath(root, relative),
      info = await lstat(full);
    if (info.isDirectory())
      for (const name of (await readdir(full)).sort())
        await walk(`${relative}/${name}`);
    else if (info.isFile()) {
      if (!isDistributableSourcePath(relative)) {
        // 明确排除的测试与上游开发脚本直接跳过；其它未识别文件仍视为错误。
        if (/(?:\.test|\.spec)\.[cm]?[jt]sx?$/.test(relative) || /^app\/scripts\//.test(relative))
          return;
        throw Error("unsupported_source_file");
      }
      files.set(relative, await readManaged(root, full));
    } else throw Error("source_special_file_forbidden");
  };
  for (const name of ["src", "server", "shared", "public", "scripts"])
    await walk(`app/${name}`);
  for (const name of (await readdir(path.join(root, "app"))).sort())
    if (appRoot.test(name)) await walk(`app/${name}`);
  await walk("scripts/office.mjs");
  await walk("restart-office.sh");
  await walk("company");
  await walk("docs/dev-work-layout.md").catch((error) => {
    if (error.code !== "ENOENT" && error.message !== "managed_path_missing") throw error;
  });
  await walk("VERSION");
  if (!skillDirectory) {
    for (const relative of [
      "scripts",
      "references",
      "agents",
      "SKILL.md",
      "README.md",
      "assets/THIRD_PARTY_NOTICES.md",
    ])
      await walk(`${skillPrefix}/${relative}`);
    return files;
  }
  const external = path.resolve(skillDirectory);
  const walkExternal = async (relative) => {
    const full = path.join(external, relative);
    const info = await lstat(full);
    if (info.isDirectory()) {
      for (const name of (await readdir(full)).sort()) await walkExternal(relative ? `${relative}/${name}` : name);
      return;
    }
    if (!info.isFile()) throw Error("source_special_file_forbidden");
    const archived = `${skillPrefix}/${relative.split(path.sep).join("/")}`;
    if (!isDistributableSourcePath(archived)) {
      // Generated archives and manifests are rebuilt by the packer.
      if (/^assets\/(office-source\.tar\.gz|source-manifest\.json)$/.test(relative.split(path.sep).join("/")))
        return;
      throw Error("unsupported_source_file");
    }
    files.set(archived, await readFile(full));
  };
  for (const relative of [
    "scripts",
    "references",
    "agents",
    "SKILL.md",
    "README.md",
    "assets/THIRD_PARTY_NOTICES.md",
  ])
    await walkExternal(relative);
  return files;
}
