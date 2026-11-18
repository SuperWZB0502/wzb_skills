#!/usr/bin/env node
import path from "node:path";
import { pathToFileURL } from "node:url";
import { mkdir, readFile, rm, open } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { namedOptions, skillFiles, verifyDelivery } from "./verify.mjs";
import { writeZip, readZip } from "./zip.mjs";
import { scanPrivacy } from "./privacy-scan.mjs";
import { packOfficeSource } from "./pack-source.mjs";
import {
  canonicalRoot,
  managedPath,
  readManaged,
  atomicWrite,
  digest,
} from "./runtime-tools.mjs";

/** Build and inspect a self-contained distribution; never includes workspace state or projects. */
export async function packageDelivery({
  workspaceRoot,
  skillDirectory,
  output,
  personalMarkersFile,
}) {
  const root = await canonicalRoot(workspaceRoot),
    skill = await managedPath(root, skillDirectory ?? "skills/web-office-skills");
  const out = path.resolve(root, output ?? "artifacts/web-office-skills-v1.zip");
  if (
    path.dirname(out) !== path.join(root, "artifacts") ||
    !out.endsWith(".zip")
  )
    throw Error("package_output_requires_artifacts_zip");
  await managedPath(root, path.dirname(out), { createDirectory: true });
  const lockPath = await managedPath(root, "artifacts/office-package.lock", {
    allowMissing: true,
  });
  let lock;
  try {
    lock = await open(lockPath, "wx", 0o600);
  } catch {
    throw Error("package_already_in_progress");
  }
  const stage = path.join(root, "artifacts/package-staging", randomUUID()),
    stagedSkill = path.join(stage, "web-office-skills");
  const originals = new Map();
  try {
    await managedPath(root, stagedSkill, { createDirectory: true });
    for (const [name, data] of await skillFiles(skill))
      await atomicWrite(root, path.join(stagedSkill, name), data);
    const manifest = await packOfficeSource({
      workspaceRoot: root,
      skillDirectory: stagedSkill,
      output: path.join(stagedSkill, "assets"),
    });
    await verifyDelivery({
      skillDirectory: stagedSkill,
      workspaceRoot: root,
      autoBundle: false,
    });
    const entries = new Map(
      [...(await skillFiles(stagedSkill))].map(([name, data]) => [
        `web-office-skills/${name}`,
        data,
      ]),
    );
    const zip = writeZip(entries),
      decoded = readZip(zip);
    if (
      decoded.size !== entries.size ||
      [...entries].some(([name, data]) => !decoded.get(name)?.equals(data))
    )
      throw Error("zip_roundtrip_mismatch");
    const stagedZip = path.join(stage, "delivery.zip");
    await atomicWrite(root, stagedZip, zip);
    const privacy = await scanPrivacy({
      skill: stagedSkill,
      root,
      personalMarkersFile,
      zip: stagedZip,
      report: path.join(root, "artifacts/skill-privacy-latest.json"),
    });
    if (privacy.summary.blocked)
      throw Error("privacy_scan_blocked_review_redacted_report");
    const previousMetadata = await readManaged(
      root,
      "artifacts/skill-package.json",
    )
      .then((bytes) => JSON.parse(bytes))
      .catch(() => null);
    const archive = await readManaged(
      root,
      path.join(stagedSkill, "assets/office-source.tar.gz"),
    );
    const sourceMetadata = await readManaged(
      root,
      path.join(stagedSkill, "assets/source-manifest.json"),
    );
    const metadata = {
      bundle: path.basename(out),
      bytes: zip.length,
      sha256: digest(zip),
      sourceSha256: manifest.sha256,
      sourceFileCount: Object.keys(manifest.files).length,
      revision:
        "Portable scripted build, publication, delivery verification and redacted privacy audit",
      privacyStatus: privacy.status,
      privacyBlocked: privacy.summary.blocked,
      privacyNeedsReview: privacy.summary.needsReview,
      ...(previousMetadata?.initialAcceptanceSourceSha256
        ? {
            initialAcceptanceSourceSha256:
              previousMetadata.initialAcceptanceSourceSha256,
          }
        : {}),
    };
    const updates = new Map([
      [path.join(skill, "assets/office-source.tar.gz"), archive],
      [path.join(skill, "assets/source-manifest.json"), sourceMetadata],
      [out, zip],
      [
        path.join(root, "artifacts/skill-package.json"),
        Buffer.from(JSON.stringify(metadata, null, 2) + "\n"),
      ],
    ]);
    const oldSums = await readManaged(root, "artifacts/SHA256SUMS")
      .then((bytes) => bytes.toString())
      .catch(() => "");
    const sums = [
      `${metadata.sha256}  ${path.relative(path.join(root, "artifacts"), out)}`,
      `${manifest.sha256}  ${path.relative(path.join(root, "artifacts"), path.join(skill, "assets/office-source.tar.gz"))}`,
      ...oldSums.split("\n").filter((line) => line.includes("  history/")),
    ];
    updates.set(
      path.join(root, "artifacts/SHA256SUMS"),
      Buffer.from(sums.join("\n") + "\n"),
    );
    const configuration = await readManaged(root, "workspace.json")
      .then((bytes) => JSON.parse(bytes))
      .catch(() => null);
    if (configuration)
      updates.set(
        path.join(root, "workspace.json"),
        Buffer.from(
          JSON.stringify(
            { ...configuration, sourceSha256: manifest.sha256 },
            null,
            2,
          ) + "\n",
        ),
      );
    const backup = path.join(
      root,
      "artifacts/history",
      `package-${/^[a-f0-9]{64}$/.test(previousMetadata?.sha256 ?? "") ? previousMetadata.sha256.slice(0, 12) : "initial"}-${randomUUID()}`,
    );
    await managedPath(root, backup, { createDirectory: true });
    for (const file of updates.keys()) {
      const original = await readManaged(root, file).catch((error) => {
        if (error.code === "ENOENT" || error.message === "managed_path_missing")
          return null;
        throw error;
      });
      originals.set(file, original);
      if (original && file !== path.join(root, "workspace.json"))
        await atomicWrite(
          root,
          path.join(backup, path.basename(file)),
          original,
        );
    }
    const verificationFile = path.join(
      root,
      "artifacts/skill-delivery-verification.json",
    );
    originals.set(
      verificationFile,
      await readManaged(root, verificationFile).catch((error) => {
        if (error.code === "ENOENT" || error.message === "managed_path_missing")
          return null;
        throw error;
      }),
    );
    try {
      for (const [file, bytes] of updates) await atomicWrite(root, file, bytes);
      const verified = await verifyDelivery({
        skillDirectory: skill,
        workspaceRoot: root,
      });
      await atomicWrite(
        root,
        verificationFile,
        JSON.stringify(verified, null, 2) + "\n",
      );
    } catch (error) {
      const restoreEntries = [...originals];
      const restored = await Promise.allSettled(
        restoreEntries.map(async ([file, original]) => {
          if (original) await atomicWrite(root, file, original);
          else
            await rm(await managedPath(root, file, { allowMissing: true }), {
              force: true,
            });
        }),
      );
      const failed = restored.flatMap((result, index) =>
        result.status === "rejected"
          ? [path.relative(root, restoreEntries[index][0])]
          : [],
      );
      if (failed.length) {
        await atomicWrite(
          root,
          `artifacts/package-recovery-${randomUUID()}.json`,
          JSON.stringify(
            {
              error: "package_restore_incomplete",
              backup: path.relative(root, backup),
              failed,
            },
            null,
            2,
          ) + "\n",
        ).catch(() => {});
        throw Error("package_failed_restore_incomplete");
      }
      throw error;
    }

    return {
      ok: true,
      ...metadata,
      output: out,
      privacyReport: "artifacts/skill-privacy-latest.json",
      verificationReport: "artifacts/skill-delivery-verification.json",
    };
  } finally {
    await rm(stage, { recursive: true, force: true });
    await lock.close();
    await rm(lockPath, { force: true });
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  try {
    const options = namedOptions(process.argv.slice(2), [
      "target",
      "skill",
      "out",
      "personal-markers-file",
    ]);
    if (!options.target) throw Error("workspace_root_required");
    process.stdout.write(
      JSON.stringify(
        await packageDelivery({
          workspaceRoot: options.target,
          skillDirectory: options.skill,
          output: options.out,
          personalMarkersFile: options["personal-markers-file"],
        }),
        null,
        2,
      ) + "\n",
    );
  } catch (error) {
    process.stderr.write(
      (/^[a-z0-9_]+$/.test(error.message) ? error.message : "package_failed") +
        "\n",
    );
    process.exitCode = 1;
  }
}
