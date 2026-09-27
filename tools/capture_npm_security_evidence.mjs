import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const web = join(root, "web");
const label = process.argv[2];
if (!label || !/^[a-z0-9_-]+$/i.test(label)) {
  throw new Error("usage: node tools/capture_npm_security_evidence.mjs <label>");
}
const output = join(root, "evidence", "npm-security", label);
mkdirSync(output, { recursive: true });
const env = { ...process.env, NPM_CONFIG_CACHE: join(root, ".npm-cache") };

function capture(name, args) {
  let stdout = "";
  let exitCode = 0;
  try {
    stdout = execFileSync(process.env.ComSpec || "cmd.exe", [
      "/d", "/s", "/c", "npm.cmd", ...args,
    ], {
      cwd: web, env, encoding: "utf8", maxBuffer: 64 * 1024 * 1024,
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    exitCode = Number(error.status ?? 1);
    stdout = String(error.stdout ?? "");
    if (!stdout.trim()) throw error;
  }
  const path = join(output, `${name}.json`);
  writeFileSync(path, stdout, "utf8");
  return {
    name, exit_code: exitCode,
    sha256: createHash("sha256").update(stdout).digest("hex"),
  };
}

const commands = [
  capture("npm-audit", ["audit", "--json"]),
  capture("npm-ls-all", ["ls", "--all", "--json"]),
  capture("npm-outdated", ["outdated", "--json"]),
];
const manifest = {
  schema_version: "npm_security_evidence_v1",
  label,
  captured_at: new Date().toISOString(),
  commands,
};
writeFileSync(
  join(output, "manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
  "utf8",
);
console.log(JSON.stringify(manifest));
