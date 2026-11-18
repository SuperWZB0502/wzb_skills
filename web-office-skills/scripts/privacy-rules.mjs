import { createHash } from "node:crypto";

export const fingerprint = (value) =>
  createHash("sha256").update(value).digest("hex");
const escape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const fixtureFile = (file) =>
  /(?:^|\/)(?:__tests__|fixtures?|examples?|test-data)(?:\/|$)|\.(?:test|spec)\.[^/]+$/i.test(
    file,
  );
const genericValue = (value) =>
  /^(?:\$\{|<|\{|process\.|import\.|https?:\/\/|(?:your|example|sample|dummy|fake|test|mock|fixture|placeholder|redacted|changeme|replace|development|not-a-real|some-key|old-key|new-key|memory-only|secret-for-test|user-key|provider-key|temporary-key|configured|unconfigured)(?:[-_ :]|$))/i.test(
    value,
  ) ||
  /^(?:x{3,}|\*{3,}|\.{3,}|[01]{8,}|(?:abc|123|test|fake|dummy){2,})$/i.test(
    value,
  ) ||
  /(?:example|placeholder|not[-_]real|fake[-_]|[-_]fake|[-_]test|[-_]fixture)/i.test(
    value,
  ) ||
  /^(?:access_token|refresh_token|authorization|authentication|api_key|client_secret)$/i.test(
    value,
  );
const attribution = (file, line) =>
  /(?:^|\/)(?:LICENSE[^/]*|NOTICE[^/]*|THIRD_PARTY[^/]*)(?:$|\.)/i.test(file) ||
  /copyright|\bauthor\b|\bmaintainers?\b/i.test(line);

export function createRules(markers = []) {
  const normalized = [...new Set(markers)];
  const maskName = (file) => {
    let result = file;
    for (const marker of normalized)
      result = result.replace(
        new RegExp(escape(marker), "gi"),
        `[marker:${fingerprint(marker).slice(0, 12)}]`,
      );
    return result
      .replace(
        /(?:sk-(?:ant-)?|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{16,}/g,
        (value) => `[secret:${fingerprint(value).slice(0, 12)}]`,
      )
      .replace(
        /(?:\/(?:home|Users)\/|[A-Za-z]:\\Users\\)[^\s/\\]+/g,
        (value) => `[home:${fingerprint(value).slice(0, 12)}]`,
      )
      .replace(/[\u0000-\u001f\u007f]/g, "?");
  };
  function finding(
    file,
    line,
    rule,
    value,
    severity = "high",
    status = "blocked",
  ) {
    return {
      file: maskName(file),
      line,
      rule,
      severity,
      status,
      fingerprint: fingerprint(value),
      masked: "[redacted]",
    };
  }
  function scanPath(file) {
    const entries = [];
    const name = file
      .split("!")
      .at(-1)
      .replace(/^skill\//, "")
      .replace(/^(?:skills\/)?web-office-skills\//, "");
    const fixture =
      fixtureFile(name) || /(?:example|sample|template|fixture)/i.test(name);
    if (
      /(?:^|\/)(?:\.env(?:\.[^/]*)?|auth\.json|credentials?(?:\.json|\.ya?ml)?|cookies?\.(?:json|txt)|\.npmrc|\.netrc|\.git-credentials|id_(?:rsa|ed25519|dsa|ecdsa)(?:\.pub)?|[^/]+\.(?:pem|p12|pfx|key))$/i.test(
        name,
      )
    )
      entries.push(
        finding(
          file,
          0,
          "credential-file",
          name,
          fixture ? "low" : "high",
          fixture ? "needsReview" : "blocked",
        ),
      );
    if (
      /\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?$|(?:^|\/)(?:workspace|runtime)\.json$/i.test(
        name,
      )
    )
      entries.push(
        finding(
          file,
          0,
          "runtime-database-or-config",
          name,
          fixture ? "low" : "high",
          fixture ? "needsReview" : "blocked",
        ),
      );
    if (
      /^(?:app\/)?(?:state|projects|logs|artifacts|cache|tmp|node_modules|\.git|\.install-staging)(?:\/|$)|(?:^|\/)(?:\.ssh|\.codex|\.claude)(?:\/|$)/i.test(
        name,
      )
    )
      entries.push(finding(file, 0, "runtime-or-private-directory", name));
    for (const marker of normalized)
      if (name.toLowerCase().includes(marker.toLowerCase()))
        entries.push(
          finding(file, 0, "configured-personal-marker-path", marker),
        );
    return entries;
  }
  function scanText(file, data, { metadata = false } = {}) {
    const text = data.toString("utf8"),
      entries = [];
    const binary =
      data.includes(0) ||
      /\.(?:png|jpe?g|webp|gif|ico|woff2?|ttf|otf)$/i.test(file);
    const fixture = fixtureFile(file);
    const lines = text.split(/\r?\n/);
    const emit = (
      index,
      rule,
      value,
      review = false,
      severity = "high",
      strong = false,
    ) => {
      const simulated = (!strong && fixture) || binary || genericValue(value);
      entries.push(
        finding(
          file,
          index + 1,
          rule,
          value,
          review || simulated ? "low" : severity,
          review || simulated ? "needsReview" : "blocked",
        ),
      );
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      for (const marker of normalized)
        if (
          line.toLowerCase().includes(marker.toLowerCase()) &&
          (!binary || metadata)
        ) {
          const encoded =
            /\bintegrity:\s*sha\d+-|["'](?:integrity|sha256|sha512)["']\s*:/i.test(
              line,
            );
          // Short strings in cryptographic digests and compressed bitmap bytes are not identity evidence.
          if (encoded) continue;
          const review = attribution(file, line);
          entries.push(
            finding(
              file,
              metadata ? 0 : i + 1,
              "configured-personal-marker",
              marker,
              review ? "low" : "high",
              review ? "needsReview" : "blocked",
            ),
          );
        }
      for (const match of line.matchAll(
        /-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----/g,
      ))
        emit(i, "private-key-material", match[0], false, "high", true);
      for (const match of line.matchAll(
        /\b(?:sk-(?:ant-[A-Za-z0-9-]*-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16})\b/g,
      ))
        emit(
          i,
          "known-credential-format",
          match[0],
          match[0].startsWith("sk-") && match[0].length < 40,
          "high",
          true,
        );
      for (const match of line.matchAll(
        /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g,
      ))
        emit(i, "jwt-token", match[0], false, "high", true);
      for (const match of line.matchAll(/\bBearer\s+([A-Za-z0-9._~-]{12,})/gi))
        emit(i, "literal-bearer-token", match[1]);
      for (const match of line.matchAll(
        /\b(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|session[_-]?(?:secret|token)|password|passwd|secret|token)\b["']?\s*[:=]\s*["'`]([^"'`\r\n]{6,})["'`]/gi,
      )) {
        const value = match[1];
        // Expressions, regexes and names describe credential handling; only literals are candidates.
        if (/^[A-Za-z0-9_./+=:@!$%&*(){}<>? -]+$/.test(value))
          emit(i, "literal-secret-assignment", value);
      }
      for (const match of line.matchAll(
        /https?:\/\/[^\s/"'<>:@]+:([^\s/"'<>@]+)@/gi,
      ))
        emit(i, "url-embedded-password", match[1]);
      for (const match of line.matchAll(
        /(?:\/(?:home|Users)\/|[A-Za-z]:\\Users\\)([^\s/\\"'`;,()\]]+)/g,
      )) {
        const user = match[1];
        const generic =
          /^(?:user|username|you|your[-_]?user|your[-_]?name|someone|example|alice|bob|test|demo|me|\.{2,}|<.*|\$.*|\{.*)$/i.test(
            user,
          ) || /\be\.?g\.?\b|\bexample\b|예:|例如|例:/i.test(line);
        emit(i, "absolute-user-home-path", match[0], generic);
      }
      for (const match of line.matchAll(
        /(?:工号|employee[_ -]?id|staff[_ -]?id)\s*["']?\s*[:=：]\s*["']?([A-Za-z]?\d{5,12})\b/gi,
      ))
        emit(i, "employee-identifier", match[1]);
      for (const match of line.matchAll(
        /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g,
      ))
        emit(
          i,
          attribution(file, line)
            ? "third-party-attribution-email"
            : "email-address",
          match[0],
          true,
          "medium",
        );
    }
    return entries;
  }
  return { scanPath, scanText, finding, maskName };
}
